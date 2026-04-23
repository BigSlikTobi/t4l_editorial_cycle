"""CLI entry point for the ingestion worker.

Registered in pyproject.toml as `ingestion-worker`. Intended to be run on
a cron (every 15-30 minutes) independently from the editorial cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from app.config import get_settings
from app.ingestion.worker import run_ingestion_cycle


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    summary = asyncio.run(run_ingestion_cycle(settings))
    print(json.dumps(summary.as_dict()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
