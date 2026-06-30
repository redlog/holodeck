"""GameSession: the UI-agnostic controller for one open game.

This is the logic that used to live inside SetupMode and PlayMode, lifted
out of the renderer. It owns the world_state, the DM, and the three imagery
agents, and it drives the interview → creation → play lifecycle.

The DM is called synchronously (FastAPI runs request handlers in a
threadpool, so blocking on a Gemini call is fine). Image generation stays
asynchronous — the imagery agents run on background threads and their
results are drained by `poll_assets()`, which the frontend calls on a timer
while `pending` is True.

A process-level registry of these (see server/app.py) keeps sessions alive
across requests; they are NOT recreated per request, because the DM holds
in-memory conversation history and a context-cache handle.
"""

import os
import sys
import threading

from world.bible import save_game, get_game_dir
from agents.dm import DungeonMaster
from agents.character_imagery import CharacterImageryAgent
from agents.scenery import SceneryAgent
from agents.item_imagery import ItemImageryAgent


def _log(msg):
    print(f"[SESSION] {msg}", file=sys.stderr, flush=True)


def _file_exists(path):
    return bool(path and os.path.isfile(path))


class GameSession:
    def __init__(self, slug, world_state, play_history=None,
                 console_lines=None, started=False):
        self.slug = slug
        self.world_state = world_state
        game_dir = get_game_dir(slug)

        self.dm = DungeonMaster(world_state, game_dir=game_dir)
        if play_history:
            self.dm.set_play_history(play_history)

        self.portraits = CharacterImageryAgent(game_dir)
        self.scenery = SceneryAgent(game_dir)
        self.items = ItemImageryAgent(game_dir)

        # Transcript is a list of [source, text] pairs (source in
        # {"dm", "user", "system"}). Stored unwrapped — the browser wraps.
        # Same shape as the legacy console_lines so saves stay compatible.
        self.transcript = [list(e) for e in (console_lines or [])
                           if isinstance(e, (list, tuple)) and len(e) == 2]

        self._speaker_id = "player"   # whose portrait is currently shown
        self._started = started       # whether the first DM beat has run
        self._lock = threading.Lock()

        # Per-game style-anchor image, shared as a reference by every portrait
        # and room paint so the whole game holds one art style. Lazily produced
        # on first imagery trigger (blocking once), then cached; loaded from
        # disk on resume. None means "couldn't make it" → un-anchored fallback.
        self._style_ref = None
        self._style_ref_loaded = False

    @property
    def speaker_id(self):
        return self._speaker_id

    @property
    def pending(self):
        """True if any image is still being generated."""
        return self.portraits.pending or self.scenery.pending or self.items.pending

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self):
        """Produce the first DM beat for a freshly opened/created game.

        Idempotent — does nothing if already started. For a new game this is
        the interview greeting; for a resumed PLAY game it queues missing
        assets and narrates the resumed scene.
        """
        with self._lock:
            if self._started:
                return
            self._started = True

            if not self.dm.connected:
                self._say("system", "DM not connected. Set GEMINI_API_KEY in .env and restart.")
                return

            phase = self.dm.phase
            if phase == self.dm.PHASE_INTERVIEW:
                self._apply_dm_text(self.dm.start_interview())
            elif phase == self.dm.PHASE_CREATING:
                self._run_creation_and_kick()
            else:  # PHASE_PLAY — world already seeded
                self._enter_play()
            self._autosave()

    def submit(self, text):
        """Handle one line of player input. Blocking."""
        with self._lock:
            text = (text or "").strip()
            if not text:
                return

            phase = self.dm.phase
            if phase == self.dm.PHASE_CREATING:
                # Creation runs synchronously under this lock, so if we're still
                # in CREATING when the player sends input, the earlier attempt
                # failed. Let them retry rather than dead-ending.
                self._say("user", text)
                self._say("system", "Retrying world creation…")
                self._run_creation_and_kick()
                self._autosave()
                return

            self._say("user", text)

            if phase == self.dm.PHASE_INTERVIEW:
                self._apply_dm_text(self.dm.send_message(text))
                # Interview may have just completed → run creation, then play.
                if self.dm.phase == self.dm.PHASE_CREATING:
                    self._run_creation_and_kick()
            else:  # PHASE_PLAY
                self._apply_play_response(self.dm.send_message(text))

            self._autosave()

    # ------------------------------------------------------------------ #
    # Phase transitions
    # ------------------------------------------------------------------ #

    def _run_creation_and_kick(self):
        self._say("system", "Preparing the world...")
        result = self.dm.start_creation() or {}
        if result.get("creation_complete"):
            # Don't announce location/NPC/secret/thread counts — that leaks the
            # shape of the mystery before the player has discovered anything.
            self._enter_play()
        else:
            self._say("system", f"World creation failed: {result.get('error', 'unknown error')}")

    def _enter_play(self):
        """Entering PLAY: ensure imagery exists, then narrate the scene."""
        self._queue_missing_assets()
        self._apply_play_response(self.dm.narrate_opening())

    # ------------------------------------------------------------------ #
    # Applying DM results
    # ------------------------------------------------------------------ #

    def _apply_dm_text(self, resp):
        """Interview-phase response: just narration text."""
        if not resp:
            return
        self._say("dm", resp.get("response_text") or "[no response]")

    def _apply_play_response(self, resp):
        """Play-phase response: narration + speaker + state-change-driven imagery."""
        if not resp:
            return
        text = resp.get("narration") or resp.get("response_text") or "[no response]"

        speaker = resp.get("speaker", "dm")
        if speaker and speaker != "dm":
            npc = self.world_state.get("npcs", {}).get(speaker)
            if npc:
                self._speaker_id = speaker
                # Paint the NPC's portrait on first conversation if absent.
                if not _file_exists(npc.get("portrait_path")):
                    style = self.world_state.get("meta", {}).get("visual_style", "")
                    self.portraits.generate_portrait(speaker, npc, style,
                                                     style_ref=self._style_anchor())
        else:
            self._speaker_id = "player"

        # Narration is always styled as DM prose; the portrait swap conveys
        # who is "speaking".
        self._say("dm", text)
        self._check_imagery_triggers(resp)

    # ------------------------------------------------------------------ #
    # Imagery triggers (from play-turn state changes)
    # ------------------------------------------------------------------ #

    def _check_imagery_triggers(self, resp):
        changes = resp.get("state_changes")
        if not changes:
            return

        new_loc = changes.get("create_location")
        if isinstance(new_loc, dict) and new_loc.get("id"):
            self._trigger_room_render(new_loc["id"])

        for entry in changes.get("image_dirty") or []:
            if isinstance(entry, dict):
                self._trigger_room_render(entry.get("id"), change=entry.get("change"))
            elif isinstance(entry, str):
                self._trigger_room_render(entry)

        new_loc_id = changes.get("current_location_id")
        if new_loc_id:
            loc = self.world_state.get("locations", {}).get(new_loc_id, {})
            if loc.get("image_dirty") and not loc.get("image_path"):
                self._trigger_room_render(new_loc_id)

        # Newly acquired items — look up the rich entry the DM already added.
        for raw_entry in changes.get("inventory_add") or []:
            item_name = (raw_entry.get("item", "") if isinstance(raw_entry, dict)
                         else str(raw_entry)).lower()
            inv = self.world_state.get("player", {}).get("inventory", [])
            for entry in reversed(inv):
                if (isinstance(entry, dict)
                        and entry.get("item", "").lower() == item_name
                        and entry.get("item_id")):
                    self._trigger_item_sprite(entry)
                    break

    def _queue_missing_assets(self):
        """Re-queue any room/portrait/item image whose file is missing on disk."""
        ws = self.world_state
        style = ws.get("meta", {}).get("visual_style", "")

        for loc_id, loc in ws.get("locations", {}).items():
            if not _file_exists(loc.get("image_path")):
                loc["image_dirty"] = True
                loc["image_path"] = None
                self._trigger_room_render(loc_id)

        player = ws.get("player", {})
        if player.get("description") and not _file_exists(player.get("portrait_path")):
            self.portraits.generate_portrait("player", player, style,
                                             style_ref=self._style_anchor())
        for npc_id, npc in ws.get("npcs", {}).items():
            if npc.get("description") and not _file_exists(npc.get("portrait_path")):
                self.portraits.generate_portrait(npc_id, npc, style,
                                                 style_ref=self._style_anchor())

        for entry in player.get("inventory", []):
            if isinstance(entry, dict):
                item_id = entry.get("item_id")
                if item_id and not _file_exists(entry.get("sprite_path")):
                    self.items.generate_sprite(item_id, entry, style)

    def _style_anchor(self):
        """Lazily produce + cache the per-game style-reference image bytes.

        Blocking on first call (one image generation); cheap on resume (read
        from disk). Returns None if it can't be produced, in which case imagery
        falls back to the un-anchored paths.
        """
        if self._style_ref_loaded:
            return self._style_ref
        self._style_ref_loaded = True
        style = self.world_state.get("meta", {}).get("visual_style", "")
        self._style_ref = self.scenery.ensure_style_anchor(style)
        if self._style_ref:
            self.world_state.setdefault("meta", {})["style_ref_path"] = str(
                self.scenery.style_anchor_path())
        return self._style_ref

    def _trigger_room_render(self, loc_id, change=None):
        if not loc_id:
            return
        loc = self.world_state.get("locations", {}).get(loc_id)
        if not loc or not loc.get("image_prompt"):
            return
        style = self.world_state.get("meta", {}).get("visual_style", "")
        ctx = self._build_scenery_context(loc)
        self.scenery.generate_room(loc_id, loc, style, game_context=ctx,
                                   change=change, style_ref=self._style_anchor())
        _log(f"Triggered room render for: {loc_id}")

    def _trigger_item_sprite(self, item_entry):
        item_id = item_entry.get("item_id")
        if not item_id:
            return
        style = self.world_state.get("meta", {}).get("visual_style", "")
        self.items.generate_sprite(item_id, item_entry, style)
        _log(f"Triggered item sprite for: {item_id}")

    def _build_scenery_context(self, loc):
        ws = self.world_state
        npcs = ws.get("npcs", {})
        present = [npcs[nid] for nid in loc.get("present_npc_ids", []) if nid in npcs]

        visual_clues = []
        loc_name = loc.get("name", "").lower()
        bible = ws.get("dm_bible", {})
        for secret in bible.get("secrets", []):
            if not secret.get("revealed", False):
                if loc_name and loc_name in secret.get("fact", "").lower():
                    visual_clues.append(secret["fact"])
        for beat in bible.get("planned_beats", []):
            if loc_name and loc_name in beat.lower():
                visual_clues.append(beat)

        return {
            "tone": ws.get("meta", {}).get("tone", ""),
            "present_npcs": present,
            "visual_clues": visual_clues,
            "discovered_features": loc.get("discovered_features", []),
            "events_log": loc.get("events_log_summary", ""),
        }

    # ------------------------------------------------------------------ #
    # Draining background image jobs
    # ------------------------------------------------------------------ #

    def poll_assets(self):
        """Drain completed image jobs into world_state. Returns True if pending."""
        with self._lock:
            changed = False
            changed = self._drain(self.portraits, self._apply_portrait_result) or changed
            changed = self._drain(self.scenery, self._apply_room_result) or changed
            changed = self._drain(self.items, self._apply_item_result) or changed
            if changed:
                self._autosave()
            return self.pending

    @staticmethod
    def _drain(agent, apply_fn):
        changed = False
        r = agent.poll_result()
        while r is not None:
            changed = apply_fn(r) or changed
            r = agent.poll_result()
        return changed

    def _apply_portrait_result(self, result):
        if not (isinstance(result, tuple) and len(result) == 3):
            return False
        tag, target_id, payload = result
        if tag != "portrait_complete":
            if tag == "error":
                _log(f"portrait error {target_id}: {payload}")
            return False
        path = payload.get("portrait_path")
        if target_id == "player":
            self.world_state.setdefault("player", {})["portrait_path"] = path
        else:
            npc = self.world_state.get("npcs", {}).get(target_id)
            if npc:
                npc["portrait_path"] = path
        return True

    def _apply_room_result(self, result):
        if not (isinstance(result, tuple) and len(result) == 3):
            return False
        tag, loc_id, payload = result
        if tag != "room_complete":
            if tag == "error":
                _log(f"room error {loc_id}: {payload}")
            return False
        loc = self.world_state.get("locations", {}).get(loc_id)
        if loc:
            loc["image_path"] = payload.get("image_path")
            version = payload.get("image_version")
            if version is not None:
                loc["image_version"] = version
            loc["image_dirty"] = False
        return True

    def _apply_item_result(self, result):
        if not (isinstance(result, tuple) and len(result) == 3):
            return False
        tag, item_id, payload = result
        if tag != "item_complete":
            if tag == "error":
                _log(f"item error {item_id}: {payload}")
            return False
        sprite_path = payload.get("sprite_path")
        if not sprite_path:
            return False
        for entry in self.world_state.get("player", {}).get("inventory", []):
            if isinstance(entry, dict) and entry.get("item_id") == item_id:
                entry["sprite_path"] = sprite_path
                return True
        return False

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def save_to_slot(self, slot):
        with self._lock:
            save_game(self.world_state, self.slug, slot,
                      play_history=self.dm.get_play_history(),
                      console_lines=self.transcript)

    def _autosave(self):
        """Caller already holds self._lock."""
        try:
            save_game(self.world_state, self.slug, "autosave",
                      play_history=self.dm.get_play_history(),
                      console_lines=self.transcript)
        except Exception as e:
            _log(f"autosave failed: {e}")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _say(self, source, text):
        self.transcript.append([source, text])
