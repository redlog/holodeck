# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The Holodeck is a graphical AI-driven text adventure built on pygame. The player converses
in free text with an AI "Dungeon Master" (DM). Each location has a generated background
image, characters have generated portraits, and inventory items have generated sprites.
All narrative reasoning, character voice, and imagery come from Google Gemini models.

`design/text_adventure_design.md` is the canonical architecture spec. The other files in
`design/` (`original_spatial_design.md`, `priority_map_exploration.md`) describe a
**superseded** sprite/spatial engine that was ripped out — ignore them except as history.

## Running

```bash
# Windows (the dev's primary environment)
venv\Scripts\activate
python main.py
```

Requires a `.env` file (gitignored) at the repo root. `config.py` fails fast at import if
any of these are missing:

```
GEMINI_API_KEY=...
GEMINI_DM_MODEL=...        # DM conversation + interview + play turns
GEMINI_NPC_MODEL=...       # NPC dialogue
GEMINI_IMAGE_MODEL=...     # character portraits + item sprites
GEMINI_VISION_MODEL=...    # screenshot analysis
GEMINI_SCENERY_MODEL=...   # room backgrounds (an imagen-* model uses the Imagen API path)
DEBUG=true                 # optional — writes full prompts/responses to <game_dir>/ai_log/
```

Dependencies: `pip install -r requirements.txt` (pygame, google-genai, Pillow, python-dotenv;
numpy/trimesh/shapely are leftovers from the old spatial engine).

There is **no test suite, linter, or build step** — this is a run-and-observe pygame app.

## Architecture

The whole app is a single pygame window. `main.py` drives a state machine over three loops:
**menu → setup (interview) → play**, each implemented by polling its own object every frame.
The window renders to a fixed `1280x800` internal `Surface` (`config.INTERNAL_*`) that is
letterbox-scaled to the resizable OS window; `main.py`'s `_scale_mouse`/`_flip` helpers
translate between window and internal coordinates, so all UI code works in internal coords.

### The agents (`agents/`)

All agents extend `BaseAgent` (`agents/base.py`), which owns the Gemini client, safety
settings (all OFF), token logging to `<game_dir>/token_log.csv`, debug AI logging, and the
**threading model**: `_run_threaded` runs an API call on a daemon thread and pushes the
result onto `self._result_queue`. UI code never blocks — it calls `poll_result()` each frame
and checks the `busy`/`pending` properties. This non-blocking poll pattern is the backbone of
every mode's update loop.

- **`DungeonMaster` (`dm.py`)** — the central agent, one instance per game session, shared
  between setup and play. It is a phase machine: `PHASE_INTERVIEW` → `PHASE_CREATING` →
  `PHASE_PLAY` (phase is re-derived from world_state on load). Interview gathers genre/tone/
  character/premise and writes only to `meta`/`player`/`dm_instructions` (any world content it
  tries to emit during the interview is **scrubbed** — see `_scrub_interview_response`).
  Creating is a one-shot pass that seeds locations, NPCs, the DM bible, and plot threads. Play
  turns parse player intent, narrate, and emit `state_changes` diffs that `_apply_play_changes`
  applies to world_state. `PLAY_SYSTEM` is loaded into a Gemini **context cache** (4h TTL) to
  cut per-turn cost; play history is trimmed/compacted (`_trim_history`) to stay bounded.
- **`NPCAgent` (`npc.py`)** — voices a single NPC for one exchange. Stateless: all context is
  passed in per call. When the player's intent is `talk`, the DM resolves the target NPC,
  dispatches synchronously to the NPC agent (on the DM's worker thread), then **weaves** the
  NPC's speech/tells/state-change back through the DM (`_weave_npc_response`) so narration stays
  in one voice. The initial DM lead-in is intentionally discarded to avoid duplicate dialog.
- **Imagery agents** — `CharacterImageryAgent` (256x256 portraits), `SceneryAgent` (16:9 room
  backgrounds, supports delta/reference-image regeneration for small scene changes),
  `ItemImageryAgent` (512x512 item sprites). Each caches PNGs under the game dir and tracks a
  `_pending` set so the same asset isn't generated twice concurrently.

All prompt text lives in **`agents/prompts.py`** (the largest and most-iterated file). The
interview/creation/play system prompts encode hard-won behavioral constraints — preserve their
structure when editing.

### World state & persistence (`world/bible.py`)

`world_state` is a single nested dict (schema seeded by `new_game()`): `meta`, `dm_instructions`,
`player` (with rich `inventory` entries), `current_location_id`, `narrative_clock`, `locations`,
`npcs`, `plot_threads`, and `dm_bible` (secrets / planned_beats / scratchpad). It is the single
source of truth threaded through every agent and mode.

- Games live in `games/<slug>/` (gitignored). Slugs are timestamp-based (`game-YYYYMMDD-HHMMSS`);
  the human-readable name is `meta.title`, set by the DM during the interview.
- Per-game layout: `game.json` (the autosave), `saves/<slot>.json`, and `rooms/`, `portraits/`,
  `items/` for generated PNGs. `_migrate_if_needed` upgrades older directory layouts on load.
- **Secret obfuscation**: on save, spoiler fields (bible secrets, planned beats, scratchpad, NPC
  `hides`/`lies_about`, hidden plot threads) are ROT13'd and marked with `_secrets_obfuscated_v1`.
  ROT13 is self-inverse, so the same `_transform_secrets` deobfuscates on load. This only deters
  casual reading of the save file, not the in-memory state. Autosave also writes a plaintext
  `game_decrypted.json` alongside `game.json` for debugging.
- `save_game`/`load_game` carry optional `_session` data (play history + console lines).
  `load_game` runs schema migrations (`_migrate_inventory`, `_migrate_npcs`) to backfill fields.

### Modes (`modes/`) and rendering (`rendering/`)

`SetupMode` and `PlayMode` are the two big UI classes. Each exposes `update(dt, events)` and
`render()`, owns a `TextInput`, maintains a `console_lines` transcript, and drives its agents via
the poll pattern. `PlayMode` additionally manages the room image, speaker portrait, inventory
drawer, item detail modal, save/load UI, and **slash commands** (`/help`, `/save`, `/load`,
`/inventory`, `/quit`; F5/F9/F12 hotkeys). `rendering/` holds the smaller shared widgets:
`ui.py` (`get_font`, `TextInput`), `game_menu.py` (new/load/quit), `save_load_ui.py`.

## Conventions

- Each module logs to stderr with a bracketed tag via a local `_log` helper (`[DM]`, `[NPC]`,
  `[PLAY]`, `[SCENERY]`, etc.). Follow this when adding logging.
- Agents return JSON; `_strip_json_fences` tolerates ```` ```json ```` fences. DM/NPC code
  always defends against malformed JSON with fallbacks rather than crashing the play loop.
- `image_dirty` flags on locations drive lazy image regeneration; the LLM may return them as
  strings or dicts, so applier code normalizes both.
