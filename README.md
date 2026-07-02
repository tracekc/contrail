# Contrail

A 24/7, fully-automated YouTube channel that narrates live global air traffic.
Public channel name: **Skywatch**. Goal: a real, growing audience around an
always-on live stream — *not* monetization.

The product is a live world map (rendered by us from open ADS-B data, so there
is no third-party video to license) with an AI "flight desk" that talks over it:
ambient context during quiet hours, and live play-by-play when something
notable happens (emergency squawks, diversions, ultra-long-haul crossings).

## How it feels

- **Quiet hour (≈90% of airtime):** slow-updating map, a calm voice every couple
  of minutes — airborne counts, busiest fields, longest flights in progress.
  Ambient "leave it on" companion.
- **Event spike (several times a day):** a 7700 squawk, a diversion, a
  loss-of-signal — the desk locks on and narrates the arc to resolution. This is
  the share/subscribe moment.

## Architecture

```
            ┌─────────────┐
ADS-B feed→ │  Ingestor   │  poll adsb.fi / OpenSky every POLL_INTERVAL_SECONDS
            └──────┬──────┘  normalize aircraft states
                   ▼
            ┌─────────────┐
            │  Enricher   │  hex→type/operator (static DB), callsign→route (opt API)
            └──────┬──────┘
                   ▼
            ┌──────────────────┐
            │ Event detector   │  rules engine → scored "story candidates" (see below)
            └──────┬───────────┘  dedup + cooldown per aircraft
                   ▼
            ┌─────────────┐      ┌──────────────┐
            │  Director   │◄─────│ Dossier/prep │  precomputed color for known
            │ (rundown)   │      │  (web search)│  recurring/notable flights
            └──────┬──────┘      └──────────────┘
                   │ chooses next segment, writes script (LLM)
                   ▼
            ┌─────────────┐
            │  Narrator   │  script → TTS → buffered audio queue
            └──────┬──────┘
                   ▼
            ┌─────────────┐
            │  Renderer   │  HTML/canvas map scene driven by live state,
            └──────┬──────┘  composites overlays + synced captions (headless Chromium)
                   ▼
            ┌─────────────┐
            │  Streamer   │  ffmpeg mux video+audio → RTMP → YouTube
            └─────────────┘
```

An **orchestrator/clock** ties the tick together:
`fetch → enrich → detect → direct → narrate → render → stream`, keeping audio a
segment or two ahead so the stream is near-live and never starves.

## Event-detection rules (what's worth narrating)

Priority, highest first. Each emits a candidate; the director scores by priority
× freshness, applies a per-aircraft cooldown, and avoids repeating a story *type*
too often.

| Event | Trigger | Notes |
|---|---|---|
| General emergency | squawk 7700 | top priority, full play-by-play |
| Radio failure | squawk 7600 | |
| Hijack code | squawk 7500 | **sensitive** — handle conservatively or suppress |
| Rapid descent / anomaly | vertical rate beyond threshold | corroborate before narrating |
| Loss of signal | tracked flight drops | caveat: may be a coverage gap, not an event |
| Diversion | inferred destination change | |
| Go-around | altitude/vertical pattern near a field | |
| Ultra-long-haul | callsign/route on a curated list | ambient color |
| Rare type | A380, 747, An-124, etc. from type DB | ambient color |
| Milestones | busiest field this hour, airborne-count thresholds, transatlantic rush window | ambient filler |

**Editorial guardrail:** keep the spine on civil aviation. Treat military /
conflict-zone tracking lightly or skip it — that's the only place this niche gets
controversial or draws platform/government heat.

## Stack

- **Python** orchestrator + components.
- **Playwright (headless Chromium)** rendering an HTML/Leaflet/canvas map scene.
- **ffmpeg** for mux + RTMP push.
- **Anthropic SDK** for the director/dossier; **OpenAI or ElevenLabs** for TTS.

## Known constraints (designed-around, not bugs)

- **Coverage gaps:** community ADS-B receivers cluster over US/Europe; oceans go
  dark. Narrate gaps as suspense ("beyond receiver range, we'll re-acquire near
  Ireland"). Optionally set `COVERAGE_BBOX` to a strong-coverage region.
- **Free-tier rate limits:** cap refresh cadence via `POLL_INTERVAL_SECONDS`. The
  map doesn't need to be twitchy to feel alive.
- **Route data** is the weak free spot; without an enrichment API, origins/
  destinations are best-effort.
- **Enthusiast scrutiny:** aviation viewers catch wrong aircraft types instantly.
  Hard facts come from structured data; keep invented "color" clearly soft.

## Build phases

1. **Ingest + enrich + detect** ✅ — `run_phase1.py`, prints scored candidates.
2. **Director + narrator** ✅ — `contrail/director.py`, `contrail/narrate.py`.
3. **Renderer** ✅ — `contrail/renderer/` (scene.html + Playwright capture).
4. **Streamer** ✅ — `contrail/stream.py` (frames + audio → ffmpeg → RTMP).
5. **Harden** — gap handling, segment variety, route enrichment, viewer count,
   global "busiest airport", dossier/search color. (ongoing)

## Running

```
pip install -r requirements.txt
playwright install chromium                 # one-time, for the renderer

python run_phase1.py                        # detect-only console feed (no keys)
python run_live.py --silent                 # on-air lines as text (ANTHROPIC_API_KEY)
python run_live.py                           # + speak locally (ANTHROPIC + OPENAI keys)
python run_live.py --stream-test            # render+narrate to a local MP4 (needs ffmpeg)
python run_live.py --stream                  # go live to YouTube (needs YOUTUBE_STREAM_KEY)
```

The orchestrator writes `contrail/renderer/state.json`; the scene polls it. The
streamer drives its own headless page and pipes PNG frames + s16le narration
audio into one ffmpeg process.

**ffmpeg note:** streaming needs a working ffmpeg. If `ffmpeg -version` errors
with a missing dylib (e.g. libx265), repair with `brew reinstall ffmpeg`.

## Path to first viewers

- Be relentlessly consistent — a 24/7 stream's uptime *is* its discoverability.
- Title/thumbnail around the live hook ("LIVE: world air traffic + emergencies").
- Lean into recurring spikes; clip notable emergencies into shorts as feeders.
- Seed in aviation communities once it's stable and not embarrassing.

## Config

See `.env.sample`. Minimal run needs: `ANTHROPIC_API_KEY`, a TTS key
(`OPENAI_API_KEY` by default), and `YOUTUBE_STREAM_KEY`. ADS-B works keyless via
adsb.fi. Everything else is optional enrichment.
