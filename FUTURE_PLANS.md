# Contrail / Skywatch — Future Plans

Parked ideas that aren't built yet, with enough detail to pick up cold later.
Written in plain language, first-principles. Not committed work — a roadmap.

Status legend: 🔴 not started · 🟡 partial/prototype exists · 🟢 done
Ratings are rough L/M/H judgments. Cost blends ongoing $ and build effort.

## Quick glance

| Idea | Summary | Feasibility | Impact | Cost | Status |
|---|---|---|---|---|---|
| Data enrichment (§3) | Routes, aircraft history, photos — mostly from free community APIs | High | High | Low (free) | 🔴 |
| Narrative memory (§1) | Remember featured planes/incidents; callbacks, arcs, records, recaps | Med | High | Low | 🟡 |
| Real ATC audio | Real controller/pilot voices for the focus region — authenticity/drama | Med | High | Low–Med | 🔴 |
| Anchor personality | Deepen Miles: running bits, opinions, live-chat interaction | High | Med–High | Low | 🟡 |
| Real map basemap (§2) | MapLibre + self-hosted PMTiles; detailed tiles so motion reads | Med | Med | Med–High | 🔴 |
| Hosting & cost for top-notch visuals (§4) | Basic hosting migrated to Hetzner CX33 (~$9.59/mo); GPU path still needed for real map tier | Med | Med | Med–High | 🟢 |
| Real stats | Make viewer count / "busiest airport" chips real, or hide them | High | Low–Med | Low | 🔴 |
| Number-reading fix | Stop odd-altitude mangling ("thirteen-eight-five-zero") | High | Low | Low | 🔴 |
| Temp-file cleanup | Delete narration .wav clips after ffmpeg reads them | High | Low (reliability) | Low | 🔴 |
| Edge TTS | Replaced Piper ONNX; zero local CPU, eliminates stop-motion stalls | High | High | Free | 🟢 |

Rough priority (see full reasoning at bottom): data enrichment → memory → real ATC → personality → real map. The last four rows are small hygiene/quality fixes to fold in opportunistically.

---

## 1. Narrative memory for the commentary 🟡

**Problem it solves.** Right now Miles (the anchor) has amnesia. Every line is
generated fresh from the current snapshot. The only existing memory is shallow:
a cooldown list ("don't repeat this exact thing too soon") and the
`IncidentTracker` (follows *one* emergency across cycles). Because there's no
narrative memory, the show is a string of disconnected blurbs. Memory is what
turns disconnected blurbs into a *show with continuity* — the single biggest
retention lever after the personality we already added.

### Memory layers (by timescale)
- **Working (this second):** current aircraft snapshot. 🟢 have it.
- **Short-term (minutes):** what we just said, so we don't repeat. 🟡 partial (cooldowns).
- **Episodic (this session / hours):** planes we've featured, incidents covered,
  storylines in progress, records set tonight. 🔴 the big gap.
- **Long-term (across days):** recurring aircraft that become "regulars,"
  all-time records. 🔴 missing.

### What to remember (entities)
- **Featured aircraft**, keyed by hex (stable per aircraft): callsign, type,
  when we talked about it, *what angle we used*, last known state. Enables
  "coming back" to a plane later.
- **Threads / arcs:** a plane or situation we're loosely following over time
  (a long-haul crossing the region, a rare type inbound, an airport rush). Each
  has state: open → updating → dormant → closed. The `IncidentTracker` is
  already a working prototype of a single arc — generalize it.
- **Alerts:** open situations (emergencies, radio failures) with status/resolution.
- **Session facts & records:** highest seen tonight, rarest type, how many A380s,
  busiest moment, incidents handled. Gives ambient filler a running scoreboard.

### How the mechanism works
- A **structured memory store** (structured facts, never prose — so we never
  "misremember").
- **Persistence to disk (critical):** the stream recycles every 6h and a restart
  currently wipes all state. Memory must be saved to a file so the show doesn't
  forget the night every 6 hours.
- **Salience scoring on the way in:** decide what's worth keeping (an emergency
  or a rare Antonov is memorable; the 400th cruising easyJet is not).
- **Retrieval on the way out:** when composing a line, pull only the *few*
  relevant remembered items into the prompt (not the whole history — token cost).
- **Decay / pruning:** forget stale low-value items; keep the good.

### Features this unlocks (the interesting part)
- **Callbacks / continuity:** "Remember that Emirates A380 climbing out of Dubai
  earlier? She's at cruise now." Feels like one continuous broadcast.
- **Following a flight as a mini-story (arc):** climb-out → cruise → descent →
  landing. A beginning-middle-end; a resolved arc is satisfying and clip-worthy.
- **"Coming back" after a detour:** "Right, back to that Qantas we were watching."
- **Anticipation → payoff:** "A rare Antonov's inbound, should reach us within the
  hour — we'll watch for it," then actually deliver. One of the strongest hooks.
- **Records & superlatives tonight:** "highest we've seen all night," "third A380
  of the evening," "busiest London's been since we came on."
- **Recurring characters (long-term):** frequent aircraft become regulars the
  audience recognizes.
- **Incident recaps & resolution:** "That emergency from forty minutes ago landed
  safely at Gatwick." Closure is compelling.
- **Smarter anti-repetition:** remembering the *angle* used last time → say
  something genuinely new.
- **Session bookends for new viewers:** "If you're just joining us, in the last
  hour we've had a medical diversion, a rare Antonov, and a transatlantic rush."

### Caveats / what makes it hard
- **Coverage drop-offs:** planes leave receiver range, so "coming back to it" can
  fail — needs graceful handling ("lost beyond our coverage"), like the incident
  tracker's lost-signal logic.
- **Route data is the weak link:** arcs are much richer with a known destination.
  That's the deferred route enrichment (see below) — this feature makes it more
  worth revisiting.
- **Hallucination risk:** callbacks must come from stored structured facts, never
  the LLM's paraphrase of a paraphrase, or it'll confidently misremember.
- **Rhythm:** too many callbacks exhaust, too few feel disconnected — needs a
  cadence (e.g. a callback every N lines, arc check-ins on a timer).
- **Token cost:** feed a selected handful of memory items, not the whole log.

### How it maps to current code
- `Director._aired` (cooldowns) and `Director._incident` are the seeds.
- Generalize `IncidentTracker` into a `SessionMemory` component that both the
  detector and director read/write, persisted to JSON on disk.

---

## 2. Real map basemap — MapLibre + PMTiles 🔴

**Problem it solves.** The current basemap is a coarse country-outline vector
(Natural Earth 110m) on flat fill. It has zero ground detail below country scale,
so at a tight/close zoom there's nothing to see — a plane sits on blank fill. This
is why we *can't* get the adsb.lol "planes visibly move at 5nm" look: real trackers
draw detailed map tiles that stream past the aircraft. We proved this live: the
chase-cam works mechanically, but with no ground detail there's nothing to slide.

**The fix:** replace the d3/canvas basemap with **MapLibre GL JS** (open-source,
BSD-2, WebGL vector map) drawing a detailed **self-hosted PMTiles** basemap, with
MapLibre's `flyTo`/`easeTo` for the smooth chase/zoom camera. Keep our aircraft
markers + overlays on top; the orchestrator's `state.json` contract (aircraft +
focus + camera box) can stay the same — we feed `flyTo` instead of our custom tween.

### Why PMTiles specifically
- A single-file tile archive (Protomaps) served locally → **no per-tile network,
  no provider bills, no rate limits.** For 24/7 rendering this matters: public OSM
  tile servers ban automated heavy use, and paid providers cost per tile.
- Alternative is OpenFreeMap (hosted) or MapTiler/Stadia (keyed, paid tiers) —
  PMTiles self-host is the clean, license-safe, unlimited option.

### Performance / resource impact (measured reasoning)
The current renderer is cheap *because* it's flat and static. A real map costs on
three axes:
1. **Render CPU/GPU:** MapLibre is WebGL. In headless Chromium with no real GPU it
   falls back to SwiftShader (software GL) → 1–2+ CPU cores continuously, vs a tiny
   fraction now. A real GPU makes this nearly free — matters a lot on a Mac mini.
2. **Memory:** ~150–300 MB (current page) → ~500 MB–1 GB+.
3. **Video encoding (the sneaky one):** a detailed map has lots of fine detail →
   needs more bitrate (~4.5 → 6–8 Mbps). The chase-cam makes it worse: when the
   camera pans, *every pixel moves every frame*, so inter-frame compression (which
   currently skips the static flat background) becomes nearly useless. Net: more
   encoder CPU **and** ~50% more upload (~1.4 TB/mo → ~2–2.5 TB/mo).
4. **Disk:** one PMTiles file — regional subset ~hundreds of MB; whole planet tens
   of GB.

**The real risk isn't crashing — it's the screenshot→ffmpeg pipeline dropping
frames** if software-WebGL readback can't sustain 8fps.

### How to keep it affordable
- **Self-host PMTiles** → kills tile network cost.
- **Use a dark, minimal map style** (few labels, muted detail) → looks good *and*
  encodes cheaper. Single best lever — cuts both render and bitrate cost.
- Keep **8fps / 720p**; don't chase 60fps.
- Run on hardware with a **real GPU** if possible (turns the WebGL cost from
  "1–2 cores" into "nearly free").

### Decision guidance
Only worth it if positioning the channel as "relaxing ambient flight map" where
visual beauty drives retention. It fixes *ambiance*, not *interest* — so do it
**after** validating the format (and after memory + real-ATC, which fix interest).
It moves the machine requirement from "runs on anything" to "wants a capable box."

---

## 3. Data sources & enrichment 🔴

**Problem it solves.** Today we have callsign + position + a basic bundled
aircraft DB (reg/type). Missing: routes (origin/destination), aircraft history
(age/operator/serial), and photos. Adding these makes the commentary far richer
("a 12-year-old 777 out of San Francisco, here's what she looks like") and is a
prerequisite for the best version of narrative memory (§1) — arcs need destinations.

**Key insight: most of what we need is FREE from community APIs.** Paid providers
are only necessary if we want guaranteed schedule coverage or ever monetize.

### Recommended free stack (do in this order)
1. **adsbdb.com** — `callsign → route` (origin/dest airports w/ coords) + `hex →
   aircraft` (reg, type, operator) + a photo URL. Free, open-source, fair-use.
   Purpose-built for this. **Cache per aircraft** (route doesn't change mid-flight;
   caching also feeds the memory layer).
2. **Planespotters.net API** — `reg`/`hex → photo(s)` with photographer credit.
   Free for non-commercial *with attribution*. Show the actual tail's photo in the
   tracking panel. High visual payoff.
3. **OpenSky metadata CSV / Mictronics basic-ac-db** — bundle locally for
   built-year/operator; enables "this 14-year-old 747" color with zero API calls.
4. **hexdb.io** — free `hex→aircraft` / `callsign→route` fallback to cross-check adsbdb.
5. **aviationweather.gov** (METAR) + **OurAirports CSV** — free airport/weather color.

### Paid options (only if needed later)
| Source | Cost | Why |
|---|---|---|
| AeroDataBox (RapidAPI) | Freemium (cheap) | More reliable schedules/routes by flight number |
| AviationStack | Freemium (~100–500/mo free) | Already stubbed in `.env`; routes/status, limited free tier |
| FlightAware AeroAPI | Paid per-query | Gold standard: gates, actual times; redistribution restricted |
| FlightRadar24 API | Paid (business) | Rich + historical; expensive; display licensing restricted |
| ADS-B Exchange (RapidAPI) | Paid | Live + historical, unfiltered |

### Position feed upgrades (optional)
- **Own receiver** (RTL-SDR ~$30 + dump1090/readsb): free, local ~1 Hz, no rate
  limits — the real smoothness fix, but only ~150–250 nm around the antenna.
- **OpenSky live** (OAuth2): free non-commercial global states; no type in live data.

### Caveats / terms
- **Rebroadcast/commercial clauses matter** — a public channel is arguably
  commercial. Free community sources (adsbdb, hexdb, Planespotters w/ attribution,
  own receiver, OpenSky metadata) are the clean path. Read paid-API display terms
  before putting their data on-air.
- **Free route data isn't 100% coverage** — crowd-sourced route DBs miss obscure/
  charter flights. Fine for a channel, not for navigation.
- **Be polite / cache** — one lookup per aircraft per flight, stored in memory.

### How it maps to current code
- `enrich.py` is where reg/type/airline backfill already lives — add route + photo
  + built-year lookups there, cached by hex. `AVIATIONSTACK_API_KEY` is already
  stubbed in `.env` as the paid fallback slot.

---

## 4. Hosting & cost for top-notch visuals 🔴

**Current state (2026-07-02):** Basic streaming migrated to Hetzner CX33 (4 vCPU, 8 GB, Nuremberg, ~$9.59/mo). TTS switched from local Piper ONNX to Edge TTS (Microsoft Neural, zero local CPU). Stream runs at speed=1.04x, Excellent health on YouTube. The GPU path below is only relevant when pursuing the real map basemap (§2).

Relevant only if pursuing the real map (§2) at 1080p/high-fps. Summary: the GPU
is the *cheap* part — **bandwidth is the real cost**, and it decides the provider.

### Try the Mac mini first (likely $0)
The mini already has a capable GPU (Apple Silicon). MapLibre is WebGL; a
GPU-enabled/non-headless Chromium can use it, and ffmpeg can hardware-encode via
`h264_videotoolbox`. This may deliver 80–90% of "top notch" for **no extra cost**,
tied to home internet/uptime. Test this before paying for cloud.

### The two cost drivers if going cloud
1. **GPU instance** (24/7 ≈ 730 hrs/mo). A modest **NVENC card (T4 / RTX-4000
   class)** handles 1080p60 render + hardware encode. Do NOT buy A100/H100 — those
   are ML training cards, wasteful here.
2. **Egress bandwidth** — a detailed, panning 1080p map ≈ 8 Mbps ≈ **~2.6 TB/mo**
   to YouTube. Hyperscalers charge ~$0.09/GB ≈ **~$235/mo just for bandwidth**;
   value hosts include it (~$0). This single factor decides the provider.

### Ballpark monthly (24/7; ranges, not quotes; ~2025 prices)
| Option | GPU | Bandwidth | ~Total/mo |
|---|---|---|---|
| Hetzner dedicated GPU (RTX 4000 SFF Ada, NVENC) | ~$200 flat | included | ~$200 |
| RunPod / value GPU cloud (RTX A4000-class) | ~$150–260 | often included (verify) | ~$180–300 |
| AWS g4dn.xlarge (T4) on-demand | ~$385 | ~$235 egress | ~$620 |
| AWS same, 1-yr reserved | ~$180 | ~$235 egress | ~$420 |
| GCP T4 | ~$260–380 | ~$235 egress | ~$500–615 |

Reserved discounts cut hyperscaler *compute* 40–60% but **not egress** — so AWS/GCP
stay pricey for streaming regardless.

Add LLM (Haiku) ~$50–150/mo; TTS $0 (Piper runs on the box). All-in:
- **Value provider (flat-rate, bandwidth included):** ~$230–400/mo
- **Hyperscaler:** ~$500–800/mo

### Recommendation
1. Test the **Mac mini** (GPU render + VideoToolbox) — likely free and good enough.
2. If dedicated cloud is wanted, use a **flat-rate GPU box with included bandwidth
   (~$200/mo)** — not AWS/GCP, where egress alone roughly doubles the bill.
3. Don't over-buy the GPU; a single NVENC card is plenty.

---

## Other parked items (brief)

- **Real ATC audio 🔴** — the single biggest *authenticity/drama* lever for the
  aviation niche (real voices). Pull from public/own receiver to stay clean; check
  LiveATC terms. Pairs naturally with the focused airport/region.
- **Anchor personality depth 🟡** — Miles exists (persona prompt). Could add
  running bits, opinions, live-chat interaction (Neuro-sama-style) for engagement.
- **Real stats 🔴** — viewer count / "busiest airport" chips are placeholders;
  make them real or hide them.
- **Number-reading fix 🔴** — occasional odd-altitude mangling
  ("thirteen-eight-five-zero"); tighten the numbers-for-the-ear rule.
- **Temp-file cleanup 🔴** — narration `.wav` temp files aren't deleted (~0.6 MB
  each, ~230/hr). Fine short-term; delete each clip after ffmpeg consumes it before
  any multi-day unattended run.

---

## Suggested order (from the ranked assessment)
1. Validate appeal cheaply (already live for a soak test).
2. **Data enrichment (§3)** — cheap, high impact, and unlocks the rest.
3. Fix the format's core weakness: **memory (§1)** + **real ATC audio**.
4. Only if positioning as ambient beauty: **real map (§2)**.
5. Harden for 24/7 (supervision/failover exist; add temp cleanup, etc.).
6. Cost discipline — done (Edge TTS via Microsoft Neural cloud, Hetzner CX33 ~$9.59/mo).
