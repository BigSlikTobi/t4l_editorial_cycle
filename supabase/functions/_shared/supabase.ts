import { createClient, SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";

export function clientFromRequest(req: Request): SupabaseClient {
  const url = Deno.env.get("SUPABASE_URL");
  const anon = Deno.env.get("SUPABASE_ANON_KEY");
  if (!url || !anon) {
    throw new Error("SUPABASE_URL or SUPABASE_ANON_KEY missing in function env");
  }
  const auth = req.headers.get("Authorization") ?? "";
  return createClient(url, anon, {
    global: { headers: auth ? { Authorization: auth } : {} },
    auth: { persistSession: false },
  });
}
