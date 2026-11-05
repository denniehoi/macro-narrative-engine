// Start de dagelijkse run handmatig vanuit het dashboard.
// Alleen ingelogde goedkeurders; maximaal 1 keer per 15 minuten en 4 keer per 24 uur.
// De GitHub-sleutel (GH_DISPATCH_TOKEN) staat alleen hier, nooit in de webpagina.
import { createClient } from "jsr:@supabase/supabase-js@2";

const REPO = "denniehoi/macro-narrative-engine";
const WORKFLOW = "daily.yml";
const COOLDOWN_MIN = 15;
const MAX_PER_DAY = 4;
const ALLOWED_ORIGINS = ["https://denniehoi.github.io", "http://localhost:8765", "null"];

function corsHeaders(origin: string | null): Record<string, string> {
  const o = origin && ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    "Access-Control-Allow-Origin": o,
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Vary": "Origin",
  };
}

Deno.serve(async (req: Request) => {
  const headers = { ...corsHeaders(req.headers.get("origin")), "Content-Type": "application/json" };
  const reply = (status: number, body: Record<string, unknown>) =>
    new Response(JSON.stringify(body), { status, headers });

  if (req.method === "OPTIONS") return new Response("ok", { headers });
  if (req.method !== "POST") return reply(405, { error: "Alleen POST" });

  const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
    auth: { persistSession: false },
  });

  // 1. Wie vraagt het?
  const jwt = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
  const { data: auth, error: authError } = await admin.auth.getUser(jwt);
  const email = auth?.user?.email?.toLowerCase();
  if (authError || !email) return reply(401, { error: "Niet ingelogd" });
  const { data: me } = await admin.from("app_users").select("role").eq("email", email).maybeSingle();
  if (me?.role !== "approver") return reply(403, { error: "Alleen goedkeurders kunnen een run starten" });

  const log = (status: string, message: string) =>
    admin.from("run_requests").insert({ requested_by: email, status, message });

  // 2. Loopt er al een run, of is er net een gestart?
  const halfHourAgo = new Date(Date.now() - 30 * 60_000).toISOString();
  const { data: running } = await admin.from("runs").select("id").eq("status", "running")
    .gte("started_at", halfHourAgo).limit(1);
  if (running?.length) {
    await log("refused", "er liep al een run");
    return reply(409, { error: "Er loopt al een run. Wacht tot die klaar is." });
  }
  const dayAgo = new Date(Date.now() - 24 * 3600_000).toISOString();
  const { data: recent } = await admin.from("run_requests").select("requested_at").eq("status", "sent")
    .gte("requested_at", dayAgo).order("requested_at", { ascending: false });
  if (recent?.length) {
    const minutes = (Date.now() - new Date(recent[0].requested_at).getTime()) / 60_000;
    if (minutes < COOLDOWN_MIN) {
      await log("refused", "te snel na de vorige");
      return reply(429, { error: `Er is net een run gestart. Probeer het over ${Math.ceil(COOLDOWN_MIN - minutes)} minuten opnieuw.` });
    }
    if (recent.length >= MAX_PER_DAY) {
      await log("refused", "daglimiet");
      return reply(429, { error: `Maximaal ${MAX_PER_DAY} handmatige runs per 24 uur (nieuwstokens sparen).` });
    }
  }

  // 3. GitHub laten starten.
  const token = Deno.env.get("GH_DISPATCH_TOKEN");
  if (!token) return reply(500, { error: "De GitHub-sleutel ontbreekt in Supabase (GH_DISPATCH_TOKEN)." });
  const gh = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "macro-narrative-dashboard",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (gh.status !== 204) {
    const text = (await gh.text()).slice(0, 300);
    await log("failed", `GitHub ${gh.status}: ${text}`);
    return reply(502, { error: `GitHub weigerde de run (${gh.status}). Controleer de GitHub-sleutel.` });
  }
  await log("sent", "gestart");
  return reply(200, { ok: true });
});
