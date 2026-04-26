import { corsHeaders, jsonResponse, preflight } from "../_shared/cors.ts";
import { clientFromRequest } from "../_shared/supabase.ts";

const ALLOWED_LANGUAGES = new Set(["en-US", "de-DE"]);
const LIST_COLUMNS =
  "id,headline,sub_headline,introduction,image,team,language,author,created_at,updated_at";

interface Cursor {
  created_at: string;
  id: number;
}

interface ListBody {
  language?: string;
  limit?: number;
  cursor?: Cursor | null;
}

function parseBody(raw: unknown): { ok: true; value: ListBody } | { ok: false; error: string } {
  const body = (raw ?? {}) as Record<string, unknown>;

  const language = body.language === undefined ? "en-US" : String(body.language);
  if (!ALLOWED_LANGUAGES.has(language)) {
    return { ok: false, error: `language must be one of ${[...ALLOWED_LANGUAGES].join(", ")}` };
  }

  let limit = 20;
  if (body.limit !== undefined) {
    const n = Number(body.limit);
    if (!Number.isFinite(n) || !Number.isInteger(n)) {
      return { ok: false, error: "limit must be an integer" };
    }
    limit = Math.max(1, Math.min(50, n));
  }

  let cursor: Cursor | null = null;
  if (body.cursor !== undefined && body.cursor !== null) {
    const c = body.cursor as Record<string, unknown>;
    const created_at = typeof c.created_at === "string" ? c.created_at : null;
    const id = typeof c.id === "number" ? c.id : Number(c.id);
    if (!created_at || !Number.isFinite(id)) {
      return { ok: false, error: "cursor must be { created_at: string, id: number }" };
    }
    cursor = { created_at, id };
  }

  return { ok: true, value: { language, limit, cursor } };
}

Deno.serve(async (req: Request) => {
  const pre = preflight(req);
  if (pre) return pre;

  if (req.method !== "POST") {
    return jsonResponse({ error: "method not allowed" }, 405);
  }

  let raw: unknown = {};
  if (req.headers.get("content-length") !== "0") {
    try {
      raw = await req.json();
    } catch {
      return jsonResponse({ error: "invalid JSON body" }, 400);
    }
  }

  const parsed = parseBody(raw);
  if (!parsed.ok) return jsonResponse({ error: parsed.error }, 400);
  const { language, limit, cursor } = parsed.value;

  const supabase = clientFromRequest(req);
  let query = supabase
    .from("team_article")
    .select(LIST_COLUMNS)
    .eq("language", language)
    .order("created_at", { ascending: false })
    .order("id", { ascending: false })
    .limit(limit!);

  if (cursor) {
    // (created_at, id) < (cursor.created_at, cursor.id)
    const ts = cursor.created_at;
    query = query.or(
      `created_at.lt.${ts},and(created_at.eq.${ts},id.lt.${cursor.id})`,
    );
  }

  const { data, error } = await query;
  if (error) {
    return jsonResponse({ error: error.message }, 500);
  }

  const items = data ?? [];
  let next_cursor: Cursor | null = null;
  if (items.length === limit) {
    const last = items[items.length - 1] as { created_at: string; id: number };
    next_cursor = { created_at: last.created_at, id: last.id };
  }

  return jsonResponse({ items, next_cursor });
});
