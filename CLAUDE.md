# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The Holodeck is a graphical AI-driven text adventure. The player converses in free text with
an AI "Dungeon Master" (DM). Each location has a generated background image, characters have
generated portraits, and inventory items have generated sprites. All narrative reasoning,
character voice, and imagery come from Google Gemini models.

The UI is a **local web app**: a FastAPI backend serves a vanilla HTML/CSS/JS frontend that
talks to it over JSON. (It was originally a pygame desktop app; that layer was removed once the
game became a text-and-images chat client, which the browser handles far more cheaply. The
agent and world layers were untouched by that migration.)

`design/text_adventure_design.md` is the canonical *game* design spec. The other files in
`design/` (`original_spatial_design.md`, `priority_map_exploration.md`) describe a **superseded**
sprite/spatial engine that was ripped out long ago — ignore them except as history.

## Running

```bash
# Windows (the dev's primary environment)
venv\Scripts\activate
python run.py          # serves http://127.0.0.1:8000 and opens a browser
```

Requires a `.env` file (gitignored) at the repo root. `config.py` fails fast at import if any
of these are missing:

```
GEMINI_API_KEY=...
GEMINI_DM_MODEL=...        # DM conversation + interview + play turns
GEMINI_NPC_MODEL=...       # NPC dialogue
GEMINI_IMAGE_MODEL=...     # character portraits + item sprites
GEMINI_VISION_MODEL=...    # (declared/required; reserved for screenshot analysis)
GEMINI_SCENERY_MODEL=...   # room backgrounds (an imagen-* model uses the Imagen API path)
DEBUG=true                 # optional — writes full prompts/responses to <game_dir>/ai_log/
```

Dependencies: `pip install -r requirements.txt` (fastapi, uvicorn, google-genai, Pillow,
python-dotenv).

There is **no test suite, linter, or build step**. Verify changes by running the app, or with
FastAPI's `TestClient` against `server.app:app` for backend checks. Note: saving can fail
intermittently with `WinError 5` when Dropbox locks `game.json` mid-`os.replace` — a benign
environment race in `save_game`, not a code bug.

## Architecture

Three layers, cleanly separated. **The agent and world layers know nothing about the
transport** — keep it that way; UI concerns belong in `server/` and `static/`.

```
static/ + server/app.py   →   server/session.py   →   agents/ + world/
   (transport/UI)              (controller)            (game brain)
```

### Backend transport (`server/`)

- **`app.py`** — the FastAPI app. Endpoints are plain `def` (not `async def`) so they run in
  FastAPI's threadpool, which lets the DM's blocking Gemini calls happen without an event-loop
  dance. A process-level `_sessions` registry keeps `GameSession` objects alive across requests
  (the DM holds in-memory conversation history + a context-cache handle, so sessions must NOT be
  recreated per request). Mounts `games/` at `/media` (generated images) and `static/` at
  `/static`. Key routes: `POST /api/games` (new), `…/{slug}/open`, `…/input`, `…/poll` (drain
  finished images), `…/save`, `…/load`.
- **`session.py` — `GameSession`** — the UI-agnostic controller, the heart of the migration.
  It owns `world_state`, the DM, and the three imagery agents, and drives the
  interview→creation→play lifecycle (`start`, `submit`, `_run_creation_and_kick`, `_enter_play`).
  All the orchestration that used to live in the pygame modes is here: applying DM results,
  triggering room/portrait/item generation from `state_changes` (`_check_imagery_triggers`),
  re-queuing missing images on load (`_queue_missing_assets`), and draining finished image jobs
  (`poll_assets`). A per-session `threading.Lock` serializes turns; `_autosave` runs after each.
- **`view.py` — `to_player_view`** — **the security boundary.** `world_state` contains spoilers
  (the DM bible, NPC `hides`/`lies_about`/intents, hidden plot threads). The browser only ever
  receives this allow-listed projection. This is a *stricter, different* filter than the ROT13
  in `world/bible.py` (which only protects the save file). When adding fields to the view, never
  pass raw NPC/bible objects through. `media_url` maps image paths to `/media/...` URLs with an
  mtime cache-buster (room images are overwritten in place).

### Frontend (`static/`)

`index.html` + `style.css` + `app.js` (vanilla, no build). `app.js` holds a single `view`
object, re-renders the whole transcript on each turn, and polls `…/poll` every ~1.5s while
`view.pending` is true so generated images fill in. The color palette in `style.css` mirrors the
old pygame speaker colors (dm/user/system). Save/load are buttons (the old `/slash` commands and
F-key hotkeys are gone).

### The agents (`agents/`)

All extend `BaseAgent` (`agents/base.py`): Gemini client, safety settings (all OFF), token
logging to `<game_dir>/token_log.csv`, debug AI logging, and a threaded `_run_threaded →
_result_queue → poll_result()` helper.

- **`DungeonMaster` (`dm.py`)** — the central agent, one per session. Phase machine:
  `PHASE_INTERVIEW` → `PHASE_CREATING` → `PHASE_PLAY` (phase is re-derived from `world_state` on
  load). Its public methods (`start_interview`, `send_message`, `start_creation`,
  `narrate_opening`) are **synchronous — they return the parsed result dict directly** (the
  threaded queue path was removed; FastAPI's threadpool provides the concurrency). Interview
  writes only to `meta`/`player`/`dm_instructions`; any world content it leaks during the
  interview is **scrubbed** (`_scrub_interview_response`). Creating seeds locations/NPCs/bible/
  threads in one shot. Play turns emit `state_changes` diffs applied by `_apply_play_changes`.
  `PLAY_SYSTEM` is loaded into a Gemini **context cache** (4h TTL); play history is
  trimmed/compacted (`_trim_history`).
- **`NPCAgent` (`npc.py`)** — voices one NPC per exchange, stateless. On a `talk` intent the DM
  resolves the target, dispatches synchronously, then **weaves** the NPC's speech/tells/state-
  change back through the DM (`_weave_npc_response`) so narration stays in one voice; the initial
  DM lead-in is discarded to avoid duplicate dialog.
- **Imagery agents** — `CharacterImageryAgent` (256² portraits), `SceneryAgent` (16:9 rooms,
  supports delta regen against a reference image), `ItemImageryAgent` (512² sprites). These
  **keep the threaded model** — they are genuine fire-and-forget background work that the frontend
  fills in via polling. Each tracks a `_pending` set so the same asset isn't generated twice.

All prompt text lives in **`agents/prompts.py`** (largest, most-iterated file). The
interview/creation/play system prompts encode hard-won behavioral constraints — preserve their
structure when editing.

### World state & persistence (`world/bible.py`)

`world_state` is a single nested dict (schema seeded by `new_game()`): `meta`, `dm_instructions`,
`player` (with rich `inventory` entries), `current_location_id`, `narrative_clock`, `locations`,
`npcs`, `plot_threads`, `dm_bible` (secrets / planned_beats / scratchpad). Single source of
truth threaded through every agent and the session.

- Games live in `games/<slug>/` (gitignored). Slugs are timestamp-based (`game-YYYYMMDD-HHMMSS`);
  the human-readable name is `meta.title`, set by the DM during the interview.
- Per-game layout: `game.json` (autosave), `saves/<slot>.json`, and `rooms/`, `portraits/`,
  `items/` for generated PNGs. `_migrate_if_needed` upgrades older directory layouts on load.
- **Save-file obfuscation**: on save, spoiler fields are ROT13'd and marked
  `_secrets_obfuscated_v1`; ROT13 is self-inverse so the same `_transform_secrets` decodes on
  load. This only deters casual reading of the file — it is NOT the wire boundary (see
  `view.to_player_view`). Autosave also writes a plaintext `game_decrypted.json` for debugging.
- `save_game`/`load_game` carry optional `_session` data (play history + transcript lines).
  `load_game` runs schema migrations (`_migrate_inventory`, `_migrate_npcs`).

## Conventions

- Each module logs to stderr with a bracketed tag via a local `_log` helper (`[DM]`, `[NPC]`,
  `[SESSION]`, `[SCENERY]`, …). Follow this when adding logging.
- Agents return JSON; `_strip_json_fences` tolerates ```` ```json ```` fences. DM/NPC code always
  defends against malformed JSON with fallbacks rather than crashing a turn.
- `image_dirty` flags on locations drive lazy image regeneration; the LLM may return them as
  strings or dicts, so applier code normalizes both.
- The transcript is a list of `[source, text]` pairs (`source` ∈ {dm, user, system}), stored
  unwrapped (the browser wraps) and persisted as `console_lines` for save compatibility.
