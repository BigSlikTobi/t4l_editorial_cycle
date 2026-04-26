import { jsonResponse, preflight } from "../_shared/cors.ts";
import { clientFromRequest } from "../_shared/supabase.ts";

interface MentionedPlayer {
  player_id: string;
  display_name: string;
  headshot: string | null;
}

Deno.serve(async (req: Request) => {
  const pre = preflight(req);
  if (pre) return pre;

  if (req.method !== "POST") {
    return jsonResponse({ error: "method not allowed" }, 405);
  }

  let raw: unknown = {};
  try {
    raw = await req.json();
  } catch {
    return jsonResponse({ error: "invalid JSON body" }, 400);
  }

  const body = (raw ?? {}) as Record<string, unknown>;
  const idNum = typeof body.id === "number" ? body.id : Number(body.id);
  if (!Number.isFinite(idNum) || !Number.isInteger(idNum) || idNum <= 0) {
    return jsonResponse({ error: "id must be a positive integer" }, 400);
  }

  const supabase = clientFromRequest(req);

  const { data: article, error } = await supabase
    .from("team_article")
    .select("*")
    .eq("id", idNum)
    .maybeSingle();

  if (error) return jsonResponse({ error: error.message }, 500);
  if (!article) return jsonResponse({ error: "not found" }, 404);

  const playerIds: string[] = Array.isArray(article.mentioned_players)
    ? (article.mentioned_players as unknown[]).filter((p): p is string => typeof p === "string")
    : [];

  let enriched: MentionedPlayer[] = [];
  if (playerIds.length > 0) {
    const { data: players, error: pErr } = await supabase
      .from("players")
      .select("player_id,display_name,headshot")
      .in("player_id", playerIds);

    if (pErr) return jsonResponse({ error: pErr.message }, 500);

    const byId = new Map<string, { display_name: string | null; headshot: string | null }>();
    for (const row of players ?? []) {
      byId.set(String((row as { player_id: string }).player_id), {
        display_name: (row as { display_name: string | null }).display_name,
        headshot: (row as { headshot: string | null }).headshot,
      });
    }

    enriched = playerIds.map((pid) => {
      const hit = byId.get(pid);
      return {
        player_id: pid,
        display_name: hit?.display_name ?? pid,
        headshot: hit?.headshot ?? null,
      };
    });
  }

  return jsonResponse({ ...article, mentioned_players: enriched });
});
