"""Shared submit→poll machinery for the three extraction cloud functions.

All three services (news_extraction_service, url_content_extraction_service,
article_knowledge_extraction) share the same job lifecycle:

  POST {submit_url}  body: {...payload, supabase: {url, jobs_table}}
    → 202 {status: "queued", job_id, expires_at}

  POST {poll_url}    body: {job_id, supabase: {...}}
    → 200 {status: "queued"|"running", job_id}
    → 200 {status: "succeeded", job_id, result: {...}}  (atomic consume)
    → 200 {status: "failed",    job_id, error:  {...}}

Cloud functions read their Supabase service credential from their own
runtime environment — we never ship long-lived secrets in request bodies,
which are more likely to be logged or retained than env vars.

`AsyncJobClient.run(payload)` submits, polls at the configured interval,
raises `JobFailedError` / `JobTimeoutError` on terminal failure, and
returns the `result` dict on success.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class JobFailedError(RuntimeError):
    """The remote cloud function reported a terminal failure."""

    def __init__(self, message: str, *, error: dict | None = None) -> None:
        super().__init__(message)
        self.error = error or {}


class JobTimeoutError(RuntimeError):
    """Polling exceeded the configured timeout without reaching terminal state."""


@dataclass(frozen=True)
class SupabaseJobsConfig:
    """Non-secret Supabase coordinates for the cloud-function job table.

    Every submit and poll body carries this block so the function can talk
    to our `public.extraction_jobs` table. The service-role key is NOT
    included here — cloud functions read it from their own runtime env
    (e.g. `SUPABASE_SERVICE_ROLE_KEY`) to keep long-lived secrets out of
    request bodies.
    """

    url: str
    jobs_table: str = "extraction_jobs"

    def as_dict(self) -> dict[str, str]:
        return {"url": self.url, "jobs_table": self.jobs_table}


class AsyncJobClient:
    """Generic submit/poll client for a cloud-function service pair."""

    def __init__(
        self,
        *,
        submit_url: str,
        poll_url: str,
        supabase: SupabaseJobsConfig,
        auth_token: str | None = None,
        poll_interval_seconds: float = 2.0,
        timeout_seconds: float = 300.0,
        http_timeout_seconds: float = 30.0,
    ) -> None:
        self._submit_url = submit_url
        self._poll_url = poll_url
        self._supabase = supabase
        self._auth_token = auth_token
        self._poll_interval = poll_interval_seconds
        self._timeout = timeout_seconds
        self._client = httpx.AsyncClient(timeout=http_timeout_seconds)

    def _auth_headers(self) -> dict[str, str]:
        if not self._auth_token:
            return {}
        return {"Authorization": f"Bearer {self._auth_token}"}

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncJobClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def submit(self, payload: dict[str, Any]) -> str:
        body = {**payload, "supabase": self._supabase.as_dict()}
        response = await self._client.post(
            self._submit_url, json=body, headers=self._auth_headers()
        )
        if response.status_code >= 400:
            raise JobFailedError(
                f"Submit failed ({response.status_code}): {response.text[:300]}"
            )
        data = response.json()
        job_id = data.get("job_id")
        if not job_id:
            raise JobFailedError(f"Submit response missing job_id: {data}")
        logger.debug("Submitted job %s to %s", job_id, self._submit_url)
        return job_id

    async def poll_once(self, job_id: str) -> dict[str, Any]:
        body = {"job_id": job_id, "supabase": self._supabase.as_dict()}
        response = await self._client.post(
            self._poll_url, json=body, headers=self._auth_headers()
        )
        if response.status_code == 404:
            raise JobFailedError(f"Job {job_id} not found or already consumed")
        if response.status_code >= 400:
            raise JobFailedError(
                f"Poll failed ({response.status_code}): {response.text[:300]}"
            )
        return response.json()

    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Submit, poll until terminal, and return the `result` dict."""
        job_id = await self.submit(payload)
        deadline = asyncio.get_event_loop().time() + self._timeout
        while True:
            data = await self.poll_once(job_id)
            status = data.get("status")
            if status == "succeeded":
                result = data.get("result")
                if result is None:
                    raise JobFailedError(f"Job {job_id} succeeded with no result")
                return result
            if status == "failed":
                raise JobFailedError(
                    f"Job {job_id} failed: {data.get('error')}",
                    error=data.get("error"),
                )
            if status == "expired":
                raise JobFailedError(f"Job {job_id} expired before completion")
            if status not in ("queued", "running"):
                raise JobFailedError(f"Job {job_id} returned unknown status {status!r}")
            if asyncio.get_event_loop().time() >= deadline:
                raise JobTimeoutError(
                    f"Job {job_id} did not finish within {self._timeout}s"
                )
            await asyncio.sleep(self._poll_interval)
