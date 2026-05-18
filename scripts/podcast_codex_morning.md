# T4L German Podcast - Codex Morning Runtime

You run unattended in `/Users/tobiaslatta/Projects/github/bigsliktobi/T4L/t4l_editorial_cycle`.
Create and publish one German T4L morning podcast before 07:00 Europe/Berlin.

## Hard Rules

- German-only v1: produce only `script.de-DE.json`.
- Factual accuracy is non-negotiable.
- Every factual/statistical/injury/transaction/quote/disputed claim in the script
  must carry a claim marker like `[C7]`.
- Every supported claim in `claim_ledger.json` must cite at least one source in
  `source_ledger.json`.
- Source attribution is for the hidden ledgers, not for the hosts' spoken voice.
  Marcus and Robin are experts; they should not say "laut Quelle", "wie aus den
  Quellen zu erkennen", "according to", or similar source-meta phrasing on air.
- Host memory is for chemistry only. Never use it as evidence for real NFL facts.
- Recent episode continuity is also for chemistry and callbacks only. Never use
  it as evidence for real NFL facts.
- Audio generation is forbidden until `podcast validate-artifacts` passes.

## Step 1 - Initialize

Run:

```bash
./venv/bin/editorial-cycle podcast init-codex-run
```

Use the printed episode directory for all remaining paths. Read:

- `selected_clusters.json`
- `analytics_pack.json`
- `continuity_context.json`
- `pronunciation_guide.json`
- `conversation_memory_snapshot.md`
- `editorial_memory/wiki/what_makes_a_story_readworthy.md`
- `editorial_memory/wiki/thin_story_rejection_rules.md`

## Step 2 - Research Selected Clusters

Use the current source articles as the primary source universe. For the selected
top clusters only, use websearch to add context, background, and claim checks.
Use `continuity_context.json` only for short callbacks, open loops, and
avoid-repeating guidance. It is not evidence for NFL facts.

This podcast is not a news read. Treat each news item as the doorway into a
football question. `analytics_pack.json` includes `angle_candidates` per cluster:
each candidate has a stat-led question, observations, follow-up searches, a
host split, and a caution. Use those candidates to develop sharper original
angles, then verify the angle with a second source pass before it reaches the
script.

### Pass 2A - Independent News Grounding

Spawn two research agents in parallel. They must investigate independently and
must not share notes, sources, conclusions, or intermediate claims before both
have returned. Give both agents the same `selected_clusters.json`,
`analytics_pack.json`, `conversation_memory_snapshot.md`, and source policy, but
assign different investigative roles:

- Agent A investigates like Marcus would prepare for the show: human stakes,
  locker-room implications, timeline, pressure points, what a fan or former
  player would notice, and where the obvious story may be too convenient.
- Agent B investigates like Robin would prepare for the show: tape/metrics
  framing, scheme context, comparable situations, source reliability, what the
  numbers do and do not prove, and where a confident take would overreach. Agent
  B should treat `analytics_pack.json` as the deterministic nflverse/nflreadpy
  stat source for player, team, roster, schedule context, derived metrics, and
  `angle_candidates`.

Both agents must also flag any player, coach, place, or brand names likely to be
mispronounced in German TTS. Use official team media guides, league/team pages,
player interviews, broadcast clips, or reputable pronunciation guides where
available. Do not guess hard names; mark uncertain names as candidates.

Write:

- `research_a.md`: Agent A's independent findings, source list, skeptical stress
  test, thin-source concerns, competing reads, and questions Marcus should bring
  into the conversation.
- `research_b.md`: strongest grounded synthesis, primary/authoritative sources,
  context, what is genuinely known, and questions Robin should bring into the
  conversation.
- pronunciation notes: confirmed pronunciations, source URL, and names still
  needing verification. Example: Tyler Shough / Shough is pronounced like
  "SHUCK"; T4L is pronounced "Tackle for Loss", never "Tee Vier Ell".

Do not merge the two research reports while writing them. The merge happens only
in `notes.md` and then in the podcast conversation.

### Pass 2B - Analytics Angle Mining

After both independent reports exist, read every cluster's `angle_candidates` in
`analytics_pack.json`. For each selected cluster, decide whether one stat-led
angle is strong enough to pursue. A strong angle must:

- Start from the news hook, but ask a deeper football question.
- Use a concrete stat observation from `analytics_pack.json`.
- Explain why that stat changes, complicates, or sharpens the story.
- Trigger at least one follow-up web investigation using the candidate's
  `followup_searches` or a better query you formulate.
- Survive a source-quality check: coach quote, team context, credible beat
  reporting, film/scheme analysis, injury context, or transaction history.

Write a new section in both research files called `Analytics-led follow-ups`.
For every pursued angle, include:

- cluster id and headline.
- stat observation(s) from `analytics_pack.json`.
- the deeper question.
- follow-up sources searched and what they confirmed or weakened.
- whether the angle is `use`, `maybe`, or `cut`.
- exact wording guardrails: what Robin can say, what Marcus can feel/ask, and
  what would be overclaiming.

If a candidate is weak, cut it. Do not force stats into the show. The goal is not
"more numbers"; the goal is a better football conversation.

### Pass 2C - Four-Section Specialist Research

After `research_a.md`, `research_b.md`, and the analytics follow-ups exist,
build a mandatory four-section episode. Use roughly 150 spoken German words per
minute:

- `target`: 3,750 spoken words.
- `normal range`: 3,000-3,750 spoken words.
- `minimum viable`: 2,250 spoken words, only when the source universe is truly
  thin after specialist research.

Create `section_plan.json` with exactly four sections in this order:

1. `news`
2. `player_of_day`
3. `team_of_day`
4. `deep_dive`

Planning rules:

- News uses the strongest current clusters.
- Player of the Day should be connected to today's news, but may use
  historical/stat context to make the spotlight meaningful.
- Team of the Day should be connected to today's news, but does not need to be
  the player's team.
- Deep Dive is mandatory. It is not fallback filler. It should turn the day into
  a bigger football question.
- Write a clear red line explaining why News → Player → Team → Deep Dive belongs
  in one episode. If a connection is weak, say so and soften it on air.
- Include rejected player, team, and deep-dive candidates and why they were
  weaker.

Run specialist research for maximum depth and write:

- `research_news.md`: final news board, current hooks, strongest clusters,
  supported claims, and what must stay short.
- `research_player_of_day.md`: current hook, recent/season/career stats, usage,
  role, injury/status, team context, comparable players, source triangulation,
  pronunciation risks, and wording guardrails.
- `research_team_of_day.md`: current hook, roster/team stats, schedule/context,
  coaching/organizational angle, tactical identity, source triangulation,
  pronunciation risks, and wording guardrails.
- `research_deep_dive.md`: central question, historical trend data, examples,
  counterexamples, what the evidence proves, what it only suggests, what it does
  not support, and wording guardrails for a grounded 5-8 minute discussion.

Then write `section_synthesis.md` with:

- final red line.
- handover logic before the Player, Team, and Deep Dive jingles.
- what each section contributes that the others do not.
- safe claims, risky claims, and claims to cut.
- any forced connection that should be softened on air.

## Step 3 - Ledgers

Create `source_ledger.json`:

```json
[
  {
    "id": "S1",
    "title": "Source title",
    "url": "https://example.com",
    "publisher": "Publisher",
    "published_at": "2026-05-13",
    "accessed_at": "2026-05-13",
    "source_type": "web",
    "reliability_note": "Primary/team/source/journalism/context note."
  }
]
```

Create `claim_ledger.json`:

```json
[
  {
    "id": "1",
    "text": "Specific factual claim.",
    "source_ids": ["S1"],
    "claim_type": "fact",
    "confidence": "high",
    "exact_quote": null,
    "number_checked": true,
    "status": "supported"
  }
]
```

Unsupported claims must be cut or clearly marked as speculation and worded as
speculation in the script.

Update `pronunciation_guide.json` before writing the script:

```json
{
  "generated_at": "2026-05-13T00:00:00Z",
  "entries": [
    {
      "term": "T4L",
      "spoken_as": "Tackle for Loss",
      "note": "Brand pronunciation; do not spell the letters in German.",
      "source_url": null,
      "confidence": "high"
    },
    {
      "term": "Tyler Shough",
      "spoken_as": "TY-ler SHUCK",
      "note": "Last name rhymes with 'shuck' / 'aw shucks'.",
      "source_url": "https://www.si.com/nfl/nfl/tyler-shough-talks-about-his-name-saints-coach-kellen-moore",
      "confidence": "high"
    }
  ],
  "candidates_to_check": []
}
```

Rules:

- Keep the default T4L entry.
- Add only pronunciations that are source-backed or obvious brand/team usage.
- Leave uncertain names in `candidates_to_check`; do not send guessed
  pronunciations to TTS.
- Every name that appears repeatedly in `script.de-DE.json` and is not obvious
  German/English should either have an entry or remain listed as an unresolved
  candidate.

When using `analytics_pack.json`, cite it in `source_ledger.json` as:

```json
{
  "id": "NFLVERSE",
  "title": "nflverse data via nflreadpy",
  "url": "https://nflreadpy.nflverse.com/",
  "publisher": "nflverse",
  "source_type": "data",
  "reliability_note": "Deterministic nflverse data loaded through nflreadpy; not a live breaking-news source."
}
```

Any stat taken from `analytics_pack.json` must use `"source_ids": ["NFLVERSE"]`
or include `NFLVERSE` alongside a confirming news/source URL.

## Step 4 - Notes

Write `notes.md` with:

- top story order and why.
- section plan summary from `section_plan.json`.
- red line from `section_synthesis.md`.
- per-story segment board:
  - news hook.
  - analytics-led question.
  - follow-up evidence from web/source investigation.
  - final angle decision: `use`, `maybe`, or `cut`.
  - what Marcus owns emotionally.
  - what Robin owns analytically.
  - what would be overclaiming.
- where Agent A and Agent B independently agree.
- where their investigations genuinely diverge.
- which host owns which discovered angle in the conversation.
- which stats and `angle_candidates` from `analytics_pack.json` are strong
  enough to move the story, and which are too stale/thin to use.
- points of agreement.
- real tensions only where evidence supports them.
- source-quality concerns.
- listener primer.
- where Marcus and Robin should use shared shorthand, callbacks, or teasing.
- which names need pronunciation help for TTS, which were verified, and which
  should be avoided or rewritten if still uncertain.
- where disagreement should be avoided because it would be fake.
- Player of the Day, Team of the Day, and Deep Dive handover wording before
  their jingles.

## Step 5 - German Script

Write `script.de-DE.json` as a `PodcastScript` object.

Rules:

- `language` must be `"de-DE"`.
- Use `color` for Marcus and `analyst` for Robin.
- Use `sections`, not a flat `body`, for the main episode. The sections must be
  exactly `news`, `player_of_day`, `team_of_day`, `deep_dive` in that order.
- Deep Dive is mandatory. Do not make it conditional on thin news.
- Player of the Day, Team of the Day, and Deep Dive must use the specialist
  research files and `section_synthesis.md`.
- Before Player of the Day, Team of the Day, and Deep Dive, write a short spoken
  handover that naturally tees up the configured section jingle.
- Write German sentences with NFL terms in English by default. Use natural
  Denglish: Coverage, Route, Edge, Pocket, Run Game, Pass Rush, Play-Caller,
  Locker Room, Game Script, Target Share, Pressure, Checkdown, Red Zone, Drive,
  Snap, First Down, Touchdown, Play-Action, Pre-Snap, and Post-Snap should stay
  English when German football fans would say them that way.
- Write the final German script TTS-safe: if compact English notation could be
  spelled out badly by the voice model, rewrite it the way it should be heard.
  Use "ein Uhr nachmittags" instead of "1pm" / "1 PM", "halb neun morgens" or
  "acht Uhr dreißig morgens" instead of "8:30 AM", "Dienstag" instead of
  "Tue.", "gegen" instead of "vs.", "ungefähr" instead of "approx.", and
  "Nummer" instead of "No.".
- Be cautious with abbreviations. NFL-native abbreviations that German Football
  listeners know are okay when natural: QB, WR, TE, RB, EPA, DVOA, IR, TD.
  Everything else should be written out or rephrased before TTS.
- Write quarters in spoken German: "erstes Quarter", "zweites Quarter",
  "drittes Quarter", "viertes Quarter" instead of "Q1", "Q2", "Q3", "Q4".
- Core tone: two former football professionals talking to each other, not two
  Harvard professors elaborating on a topic. Keep it easy, lifted, street-level,
  and immediately understandable.
- Prefer "language from the street" over "language from the books": "Guck mal",
  "der Punkt ist", "das kaufe ich nicht", "auf dem Feld zählt's", "zeig es mir
  Sonntag", "das ist echt", "das ist Quatsch" when it fits.
- Avoid professor/book words and polished essay structure: no "Diskurs",
  "Narrativ" as a crutch, "kontextualisieren", "Implikation", "Spannungsfeld",
  "insofern", "folglich", "gleichwohl", "dementsprechend", "substantiell",
  "manifestiert sich", or long setup sentences that sound written, not spoken.
- Avoid artificial German foreign-word filler most of the time:
  "operationalisieren", "Signifikanz", "Konstellation", "Dynamik" as filler,
  "validieren", "evaluieren", "indizieren", "Kausalität", "Volatilität".
  Use street-level verbs instead: "das wackelt", "das hält", "das killt dich",
  "das sieht man", "das ist ein echter Test". English football/analytics terms
  are the exception and should stay English.
- If a line sounds smart but no former player would say it naturally in a studio
  conversation, rewrite it shorter and plainer.
- Marcus and Robin mostly talk to each other, but when they include the listener
  they address them directly with "du": "du kennst das", "wenn du Bears-Fan
  bist", "merk dir den Punkt". Do not say distant lines like "der Hörer merkt".
  Each major section should include at least one short direct listener beat.
- Marcus and Robin are old friends, not debate-club opponents.
- The episode should feel like both hosts did their own homework before the
  microphones turned on. Marcus brings findings from `research_a.md`.
  Robin brings findings from `research_b.md`; they compare investigations on air.
- Keep source work invisible in the spoken conversation. The hosts can say
  "Mahomes hat 2025..." or "Der Punkt bei Dallas ist...", but not "laut Yahoo",
  "die Quellen zeigen", "wie berichtet wurde", "laut NFL.com", or "according to
  the report". Put all attribution in `[C#]`, `claim_ledger.json`, and
  `source_ledger.json`; `render-artifact` removes the claim markers before TTS.
- Each main section should have a football question, not just a news update.
  Preferred shape:
  1. Marcus gives the news hook and why fans care.
  2. Robin introduces the analytics-led question.
  3. Marcus pushes the human/locker-room implication.
  4. Robin brings the follow-up source context and says what the numbers do NOT
     prove.
  5. They land on one listener takeaway.
- Default pattern: agree, build, tease, clarify, reframe. But the show needs
  more real opinion and more friction than a neutral news read.
- Disagreement is allowed when the evidence, role, film read, locker-room read,
  or interpretation genuinely supports it. Marcus and Robin may hold different
  takes over the same facts and stay with those takes.
- When a genuine divergence exists, make it sound like two prepared friends
  comparing notes: "Ich bin bei meiner Recherche an X hängen geblieben..." /
  "Spannend, ich bin über Y gekommen..." Do not make it sound like generic
  argument.
- Occasionally let a disagreement remain unresolved: "Ich bleib dabei", "Da
  komm ich nicht mit", "Das ist mir zu billig", "Nein, für mich ist genau das
  der Punkt." Friendly, fact-grounded, but not always softened into consensus.
- Include at least three old-friends beats from the memory snapshot: callback,
  gentle teasing, shared shorthand, or finishing each other's thought.
- Use at most two recurring catchphrases.
- Use "Tape on, let's go." if the old show-opening shorthand is needed. Do not
  use "Kaffee auf, Tape an."
- Use at most one Whiteboard callback.
- Use either one "Filmraum ohne Fenster" tease or one "Trailer-Stimme" tease;
  do not use both as repeated bits. "Trailer-Stimme" may appear at most once.
- Avoid repeating comparison formulas like "Das ist kein X, das ist Y" or "nicht
  X, sondern Y". One contrast can land, but repeated contrast lines become a
  pattern. Make the point more directly instead of replacing it with a new
  catchphrase.
- Jokes must never invent real NFL facts.
- Keep most turns short. One idea per sentence. Use fragments, quick reactions,
  and direct questions when they sound natural: "Eben.", "Das ist der Punkt.",
  "Und jetzt?", "Was machst du damit als Coach?"
- Write spoken brand mentions as "Tackle for Loss" when the line should say the
  brand aloud. Do not write "T4L" inside spoken dialogue unless the intended
  sound is still "Tackle for Loss" per `pronunciation_guide.json`.
- Make the conversation less perfect on purpose: use occasional short fragments,
  self-corrections, half-sentences, quick reactions, and asymmetrical turn
  lengths. Keep it intelligible and factual; do not add filler monologues.
- Put claim markers after factual claims, e.g. `[C7]`.
- Robin may use nflverse stats from `analytics_pack.json`, but only when the
  corresponding claim is in `claim_ledger.json` and cites `NFLVERSE`.
- Robin may introduce an analytics-led angle only if `notes.md` marks it `use`
  or `maybe` and the follow-up research found supporting context. If follow-up
  research weakens the angle, mention the limitation or cut the angle.
- Aim for the normal 20-25 minute range when the research supports it. Never
  stretch with unsourced filler, repeated claims, or generic debate.

## Step 5.5 - Host Authority Pass

Before validation or rendering, reread `script.de-DE.json` as Marcus and Robin's
producer. Rewrite weak or generic host statements so both hosts sound like they
fully understand the football context behind their takes.

Create `host_authority_notes.md` with:

- weak/generic lines found and how they were fixed.
- places where Marcus' human/locker-room/game-flow read was made more specific.
- places where Robin's tape/metrics/scheme read was made more specific.
- places where uncertainty stayed in the script because the evidence did not
  support a stronger claim.
- confirmation that no new unsupported facts, stats, quotes, injuries,
  transactions, source mentions, or claim markers were added.

Rules for the pass:

- Preserve the `PodcastScript` schema, section order, speakers, and claim
  markers.
- Do not add facts. Use only selected clusters, research files,
  `section_synthesis.md`, `analytics_pack.json`, `source_ledger.json`, and
  `claim_ledger.json`.
- Make confidence mean "owned and specific", not "certain about everything".
  If evidence is weak, write confident uncertainty.
- Keep Denglish, direct `du` listener address, street-level German, and real but
  friendly disagreement.

## Step 6 - Validate, Render, Publish

Run:

```bash
./venv/bin/editorial-cycle podcast validate-artifacts var/podcast/episodes/YYYY-MM-DD
./venv/bin/editorial-cycle podcast render-artifact var/podcast/episodes/YYYY-MM-DD
./venv/bin/editorial-cycle podcast publish-artifact var/podcast/episodes/YYYY-MM-DD
```

Replace `var/podcast/episodes/YYYY-MM-DD` with the exact directory printed by
`init-codex-run`.

`render-artifact` prefers Gemini Batch TTS with `gemini-3.1-flash-tts-preview`.
It falls back to synchronous Gemini TTS only if the batch fails or remains
unfinished after 12 ten-minute status checks, and records the reason in
`tts_status.json`.

`publish-artifact` is still personal-only, but it must rehearse the friends-safe
publication format before upload: cleaned listener metadata, German show notes
with public source links, a concise AI disclosure, a safety report, and a
copyright-friendly generated thumbnail. Thumbnail prompts must forbid logos,
team marks, official uniforms, player likenesses, recognizable real people,
source-image copying, generated text, and watermarks.

## Final Check

Verify the episode directory contains:

- `manifest.json`
- `selected_clusters.json`
- `analytics_pack.json`
- `section_plan.json`
- `pronunciation_guide.json`
- `research_a.md`
- `research_b.md`
- `research_news.md`
- `research_player_of_day.md`
- `research_team_of_day.md`
- `research_deep_dive.md`
- `section_synthesis.md`
- `host_authority_notes.md`
- `notes.md`
- `source_ledger.json`
- `claim_ledger.json`
- `conversation_memory_snapshot.md`
- `script.de-DE.json`
- `tts_status.json`
- `audio_probe.json`
- `public_metadata.json`
- `show_notes.md`
- `publication_safety_report.json`
- `thumbnail_prompt.json`
- `thumbnail.png` or `thumbnail.jpg`
- `upload_metadata.json`
