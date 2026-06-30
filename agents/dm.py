"""DungeonMaster: the single agent that runs the whole session.

The same agent persona carries through three phases:

  PHASE_INTERVIEW — the setup conversation. Gathers genre, tone, visual
                    style, player character, premise, starting situation.
                    Does NOT create world content.

  PHASE_CREATING  — runs once immediately after the interview. The DM
                    privately seeds the starting location, any NPCs
                    visibly present at the opening, the DM bible
                    (secrets and planned beats), and initial plot threads.
                    No player interaction during this phase.

  PHASE_PLAY      — turn-based narration. Receives free-text player input,
                    parses intent, narrates, and emits state diffs that
                    _apply_play_changes applies to world_state.

The public methods (start_interview, send_message, start_creation,
narrate_opening) are SYNCHRONOUS — they return the parsed result dict
directly. They are meant to be called from a worker thread or a threadpool
(FastAPI runs request handlers in one); GameSession serializes turns per
game. Image generation is the only thing that stays asynchronous, and that
lives in the imagery agents, not here.

The interview prompt and scrubbing logic are the surviving heart of months
of iteration; preserve their constraint structure when expanding.
"""

import json
import re
import sys
import traceback

from google.genai import types

from agents.base import BaseAgent
from agents.npc import NPCAgent
from agents.prompts import (
    INTERVIEW_SYSTEM, INTERVIEW_OPENING_DIRECTIVE,
    CREATION_SYSTEM, CLOSED_WORLD_CREATION_ADDENDUM,
    PLAY_SYSTEM, CLOSED_WORLD_PLAY_ADDENDUM,
    OPENING_SCENE_DIRECTIVE, RESUMED_SCENE_DIRECTIVE,
    DM_NPC_DISPATCH,
)
from config import GEMINI_DM_MODEL


def _log(msg):
    print(f"[DM] {msg}", file=sys.stderr, flush=True)


def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _thread_id_looks_like_prose(tid):
    """A real thread id is a short lowercase_snake_case slug. When the model
    instead drops the summary sentence (or a spaced title) into the id field,
    it has whitespace or runs long — the signature of the duplicate-thread bug."""
    return (" " in tid) or (len(tid) > 40)


def _slugify_thread_id(tid):
    slug = re.sub(r"[^a-z0-9]+", "_", tid.lower()).strip("_")
    return slug[:60]


class DungeonMaster(BaseAgent):
    """The DM. Owns the interview, the post-interview creation pass, and
    (next) play turns."""

    PHASE_INTERVIEW = "interview"
    PHASE_CREATING = "creating"
    PHASE_PLAY = "play"

    def __init__(self, world_state, game_dir=None):
        super().__init__(model=GEMINI_DM_MODEL, temperature=0.9, game_dir=game_dir)
        self.world_state = world_state
        self._history = []
        self._npc_agent = NPCAgent(game_dir=game_dir)
        self._play_cache = None
        if self._world_already_seeded():
            self.phase = self.PHASE_PLAY
            # Repair authored maps on load so older/buggy closed games self-heal.
            if self._is_closed_world():
                self._validate_closed_world_map()
        elif self._setup_already_complete():
            self.phase = self.PHASE_CREATING
        else:
            self.phase = self.PHASE_INTERVIEW

    def _is_closed_world(self):
        return self.world_state.get("meta", {}).get("world_mode") == "closed"

    def _creation_system(self):
        if self._is_closed_world():
            return CREATION_SYSTEM + CLOSED_WORLD_CREATION_ADDENDUM
        return CREATION_SYSTEM

    def _play_system(self):
        if self._is_closed_world():
            return PLAY_SYSTEM + CLOSED_WORLD_PLAY_ADDENDUM
        return PLAY_SYSTEM

    def get_play_history(self):
        """Return the play-phase conversation history (for save)."""
        return list(getattr(self, "_play_history", []))

    def set_play_history(self, history):
        """Restore play-phase conversation history (from load)."""
        self._play_history = list(history) if history else []

    def _world_already_seeded(self):
        return bool(self.world_state.get("current_location_id")
                    and self.world_state.get("locations"))

    def _setup_already_complete(self):
        meta = self.world_state.get("meta", {})
        player = self.world_state.get("player", {})
        return bool(
            meta.get("title")
            and meta.get("visual_style")
            and meta.get("tone")
            and player.get("name")
            and player.get("description")
        )

    # ------------------------------------------------------------------ #
    # Interview phase
    # ------------------------------------------------------------------ #

    def start_interview(self):
        """Synchronous. Return the opening greeting + first interview question."""
        if self.phase != self.PHASE_INTERVIEW:
            return None
        if not self.connected:
            return {"response_text": "DM not connected. Set GEMINI_API_KEY in .env and restart."}
        return self._send_opening()

    def _send_opening(self):
        try:
            opening_user_msg = INTERVIEW_OPENING_DIRECTIVE
            self._history.append({"role": "user", "parts": [{"text": opening_user_msg}]})
            raw = self._call_text(INTERVIEW_SYSTEM, self._history, context="interview")
            self._history.append({"role": "model", "parts": [{"text": raw}]})
            parsed = json.loads(_strip_json_fences(raw))
            self._scrub_interview_response(parsed)
            return parsed
        except Exception as e:
            _log(f"Opening greeting error: {e}")
            return {
                "response_text": "Welcome to the Holodeck. I'll help you build your game. To start, what genre or setting do you have in mind?",
            }

    def send_message(self, user_text):
        """Synchronous. Player text input, dispatched by phase. Returns the result dict."""
        if not self.connected:
            return {"response_text": "DM not connected. Set GEMINI_API_KEY in .env and restart."}
        if self.phase == self.PHASE_INTERVIEW:
            _log(f"interview send: {user_text[:80]}")
            return self._process_interview(user_text)
        elif self.phase == self.PHASE_CREATING:
            _log("Input received during CREATING phase, ignored")
            return {"response_text": "[The DM is preparing the world; please wait.]"}
        elif self.phase == self.PHASE_PLAY:
            _log(f"play send: {user_text[:80]}")
            return self._process_play_turn(user_text)
        else:
            return {"response_text": f"[Unknown phase: {self.phase}]"}

    def _process_interview(self, user_text):
        try:
            self._history.append({"role": "user", "parts": [{"text": user_text}]})

            original_temp = self._temperature
            self._temperature = 0.5  # interview wants discipline, not creativity
            try:
                raw = self._call_text(INTERVIEW_SYSTEM, self._history, context="interview")
            finally:
                self._temperature = original_temp

            self._history.append({"role": "model", "parts": [{"text": raw}]})
            parsed = json.loads(_strip_json_fences(raw))
            self._scrub_interview_response(parsed)
            self._apply_interview_updates(parsed)

            if parsed.get("interview_complete") is True:
                _log("Interview complete — transitioning to creating phase")
                self.phase = self.PHASE_CREATING

            return parsed

        except json.JSONDecodeError:
            _log("JSON parse error")
            return {"response_text": "[DM could not parse response. Please try rephrasing.]"}
        except Exception as e:
            _log(f"Error: {e}")
            return {"response_text": f"[DM error: {str(e)[:200]}]"}

    def _scrub_interview_response(self, parsed):
        """If the LLM tries to create world content during the interview, drop it."""
        leaked = []
        for key in ("new_rooms", "new_characters", "new_objects", "new_locations"):
            if parsed.get(key):
                leaked.append(key)
                parsed.pop(key, None)
        if leaked:
            _log(f"Scrubbed {leaked} from interview response")

    def _apply_interview_updates(self, parsed):
        updates = parsed.get("world_updates")
        if not updates:
            return
        if updates.get("meta"):
            self.world_state.setdefault("meta", {}).update(updates["meta"])
        if updates.get("player"):
            self.world_state.setdefault("player", {}).update(updates["player"])
        if updates.get("dm_instructions"):
            self.world_state.setdefault("dm_instructions", {}).update(updates["dm_instructions"])

    # ------------------------------------------------------------------ #
    # Creating phase — runs once after the interview
    # ------------------------------------------------------------------ #

    def start_creation(self):
        """Synchronous one-shot world creation pass.

        Must be called while phase == PHASE_CREATING. Returns
        {'creation_complete': True, ...} on success or
        {'creation_complete': False, 'error': '...'} on failure.
        """
        if self.phase != self.PHASE_CREATING:
            _log(f"start_creation called in phase {self.phase}, ignoring")
            return {"creation_complete": False, "error": f"wrong phase: {self.phase}"}
        if not self.connected:
            return {"creation_complete": False,
                    "error": "DM not connected (GEMINI_API_KEY missing)."}
        return self._run_creation()

    def _run_creation(self):
        try:
            # Build a fresh context for the creation call. We don't want the
            # interview history's chatter — just the structured world state.
            ws = self.world_state
            meta = ws.get("meta", {})
            player = ws.get("player", {})
            dm_inst = ws.get("dm_instructions", {})

            user_msg = (
                "WORLD STATE FROM INTERVIEW:\n\n"
                f"Title: {meta.get('title', '')}\n"
                f"Tone: {meta.get('tone', '')}\n"
                f"Visual style: {meta.get('visual_style', '')}\n\n"
                f"Player name: {player.get('name', '')}\n"
                f"Player description: {player.get('description', '')}\n\n"
                f"Premise:\n{dm_inst.get('premise', '')}\n\n"
                f"Starting location concept:\n{dm_inst.get('starting_location_concept', '')}\n\n"
                f"Plot seeds (player-volunteered):\n- " + "\n- ".join(dm_inst.get("plot_seeds", []) or ["(none)"]) + "\n\n"
                f"Interview summary:\n{dm_inst.get('interview_summary', '')}\n\n"
                "Now do your hidden prep and emit the JSON described in the system prompt."
            )

            raw = self._call_text(self._creation_system(), [
                {"role": "user", "parts": [{"text": user_msg}]}
            ], context="author")
            parsed = json.loads(_strip_json_fences(raw))
            self._apply_creation(parsed)
            if self._is_closed_world():
                self._validate_closed_world_map()
            self.phase = self.PHASE_PLAY
            _log("Creation complete — transitioning to play phase")
            return {
                "creation_complete": True,
                "starting_location_id": parsed.get("starting_location_id"),
                "location_count": len(parsed.get("new_locations") or {}),
                "npc_count": len(parsed.get("new_npcs") or {}),
                "secret_count": len(parsed.get("dm_bible", {}).get("secrets") or []),
                "thread_count": len(parsed.get("plot_threads") or []),
            }
        except json.JSONDecodeError as e:
            _log(f"Creation JSON parse error: {e}")
            return {"creation_complete": False, "error": f"JSON parse: {e}"}
        except Exception as e:
            _log(f"Creation error: {e}")
            return {"creation_complete": False, "error": str(e)}

    def _apply_creation(self, parsed):
        ws = self.world_state

        # Locations — merge into existing dict
        new_locations = parsed.get("new_locations") or {}
        ws.setdefault("locations", {})
        for loc_id, loc_def in new_locations.items():
            loc_def.setdefault("image_path", None)
            loc_def.setdefault("image_dirty", True)  # needs first render
            loc_def.setdefault("present_npc_ids", [])
            loc_def.setdefault("discovered_features", [])
            loc_def.setdefault("events_log_summary", "")
            loc_def.setdefault("exits", [])  # closed-world adjacency; empty in open mode
            loc_def.setdefault("visible_exits", [])  # spoiler-free spatial labels for the UI
            loc_def.setdefault("visited", False)  # gates revealing a destination's name
            loc_def["id"] = loc_id
            ws["locations"][loc_id] = loc_def

        starting_id = parsed.get("starting_location_id")
        if starting_id and starting_id in ws["locations"]:
            ws["current_location_id"] = starting_id
            ws["locations"][starting_id]["visited"] = True

        # Narrative clock
        clock = parsed.get("narrative_clock")
        if clock:
            ws["narrative_clock"] = clock

        # NPCs
        new_npcs = parsed.get("new_npcs") or {}
        ws.setdefault("npcs", {})
        for npc_id, npc_def in new_npcs.items():
            npc_def.setdefault("portrait_path", None)
            npc_def.setdefault("known_to_player", False)
            npc_def.setdefault("dialog_summary_with_player", "")
            npc_def.setdefault("voice", "")
            npc_def.setdefault("knows", [])
            npc_def.setdefault("hides", [])
            npc_def.setdefault("lies_about", [])
            npc_def["id"] = npc_id
            ws["npcs"][npc_id] = npc_def

        # DM bible
        bible = parsed.get("dm_bible") or {}
        ws_bible = ws.setdefault("dm_bible", {"secrets": [], "planned_beats": [], "scratchpad": ""})
        if "secrets" in bible:
            ws_bible["secrets"] = bible["secrets"]
        if "planned_beats" in bible:
            ws_bible["planned_beats"] = bible["planned_beats"]
        if "scratchpad" in bible:
            ws_bible["scratchpad"] = bible["scratchpad"]

        # Win condition (closed-world): may arrive top-level or inside the bible.
        win = parsed.get("win_condition") or bible.get("win_condition")
        if win:
            ws_bible["win_condition"] = win

        # Plot threads
        threads = parsed.get("plot_threads") or []
        if threads:
            ws["plot_threads"] = threads

        # Starting inventory
        player = ws.setdefault("player", {})
        inv = player.setdefault("inventory", [])
        for item_entry in parsed.get("starting_inventory") or []:
            if not isinstance(item_entry, dict):
                item_entry = {"item": str(item_entry)}
            item_name = item_entry.get("item", "???")
            item_id = re.sub(r"[^a-z0-9]+", "_", item_name.lower()).strip("_")
            existing_ids = {e.get("item_id") for e in inv if isinstance(e, dict)}
            base_id, counter = item_id, 2
            while item_id in existing_ids:
                item_id = f"{base_id}_{counter}"
                counter += 1
            inv.append({
                "item": item_name,
                "item_id": item_id,
                "provenance": item_entry.get("provenance", ""),
                "found_location_id": "",
                "found_location_name": "",
                "turn_acquired": 0,
                "sprite_path": None,
                "visual_description": item_entry.get("visual_description", ""),
            })
            _log(f"Starting inventory: {item_name} (id={item_id})")

    def _validate_closed_world_map(self):
        """Repair the authored map so the closed world stays coherent.

        Closed mode forbids creating locations during play, so an exit that
        points at a nonexistent location id would strand the player. Prune
        dangling exits, force exits to be bidirectional, and warn about any
        location unreachable from the start.
        """
        ws = self.world_state
        locs = ws.get("locations", {})
        valid_ids = set(locs)

        # Drop exits pointing at locations that don't exist.
        for lid, loc in locs.items():
            exits = loc.get("exits") or []
            kept = [e for e in exits if e in valid_ids]
            dropped = [e for e in exits if e not in valid_ids]
            if dropped:
                _log(f"[closed-world] {lid}: pruned dangling exits {dropped} "
                     f"(no such location)")
            loc["exits"] = kept

        # Force bidirectionality — if A→B exists, ensure B→A exists too.
        for lid, loc in locs.items():
            for dest in loc.get("exits", []):
                dest_loc = locs.get(dest, {})
                dest_exits = dest_loc.setdefault("exits", [])
                if lid not in dest_exits:
                    dest_exits.append(lid)
                    _log(f"[closed-world] added reverse exit {dest} -> {lid}")

        # Warn about locations unreachable from the start (coherence smell).
        start = ws.get("current_location_id")
        if start and start in locs:
            seen, frontier = {start}, [start]
            while frontier:
                cur = frontier.pop()
                for dest in locs.get(cur, {}).get("exits", []):
                    if dest not in seen:
                        seen.add(dest)
                        frontier.append(dest)
            unreachable = valid_ids - seen
            if unreachable:
                _log(f"[closed-world] WARNING: locations unreachable from "
                     f"'{start}': {sorted(unreachable)}")

    # ------------------------------------------------------------------ #
    # Play phase
    # ------------------------------------------------------------------ #

    RECENT_HISTORY = 6     # last N exchanges get full context
    MAX_PLAY_HISTORY = 20  # total exchanges kept (older ones are trimmed)

    def _ensure_play_cache(self):
        """Lazily create (or return) a cached version of PLAY_SYSTEM."""
        if self._play_cache is not None:
            return self._play_cache
        try:
            cache = self._client.caches.create(
                model=self._model,
                config=types.CreateCachedContentConfig(
                    system_instruction=self._play_system(),
                    ttl="14400s",  # 4 hours
                ),
            )
            self._play_cache = cache
            _log(f"PLAY_SYSTEM cached: {cache.name}")
        except Exception as e:
            _log(f"Cache creation failed, running uncached: {e}")
            self._play_cache = None
        return self._play_cache

    def narrate_opening(self):
        """Synchronous. Narrate the opening (or resumed) scene with no player input."""
        if self.phase != self.PHASE_PLAY:
            return None
        if not self.connected:
            return {"response_text": "DM not connected. Set GEMINI_API_KEY in .env and restart."}
        return self._process_play_turn(None)

    def _process_play_turn(self, user_text):
        try:
            context_msg = self._build_play_context(user_text)

            if not hasattr(self, "_play_history"):
                self._play_history = []

            self._play_history.append(
                {"role": "user", "parts": [{"text": context_msg}]}
            )
            self._trim_history()

            loc_id = self.world_state.get("current_location_id", "")
            loc_name = self.world_state.get("locations", {}).get(loc_id, {}).get("name", loc_id) or "unknown"
            cache = self._ensure_play_cache()
            raw = self._call_text(self._play_system(), self._play_history, context=loc_name,
                                  cached_content=cache.name if cache else None)
            self._play_history.append(
                {"role": "model", "parts": [{"text": raw}]}
            )

            parsed = json.loads(_strip_json_fences(raw))
            self._apply_play_changes(parsed)

            intent = parsed.get("intent", {})
            action = intent.get("action")
            target_npc_id = self._resolve_talk_target(intent) if action == "talk" else None

            if target_npc_id:
                npc_response = self._dispatch_to_npc(target_npc_id, user_text)
                if npc_response:
                    woven = self._weave_npc_response(
                        target_npc_id, npc_response, user_text, parsed
                    )
                    if woven:
                        self._apply_play_changes(woven)
                        parsed = self._merge_talk_results(parsed, woven, target_npc_id)

            return parsed

        except json.JSONDecodeError:
            _log("Play turn JSON parse error")
            return {
                "narration": "[The DM stumbles over their words. Try again.]",
                "speaker": "dm",
            }
        except Exception as e:
            _log(f"Play turn error: {e}\n{traceback.format_exc()}")
            return {
                "narration": f"[DM error: {str(e)[:200]}]",
                "speaker": "dm",
            }

    def _resolve_talk_target(self, intent):
        """Find the npc_id for a talk intent's target."""
        target = (intent.get("target") or "").lower()
        if not target:
            return None

        npcs = self.world_state.get("npcs", {})
        loc_id = self.world_state.get("current_location_id")
        loc = self.world_state.get("locations", {}).get(loc_id, {}) if loc_id else {}
        present_ids = loc.get("present_npc_ids", [])

        import re

        def name_matches(target, name, npc_id):
            name_lower = name.lower()
            # Exact substring
            if target in name_lower:
                return True
            # Word-based: strip punctuation and check all target words appear in name words
            name_words = set(re.sub(r"[^a-z0-9\s]", "", name_lower).split())
            target_words = re.sub(r"[^a-z0-9\s]", "", target).split()
            if target_words and all(w in name_words for w in target_words):
                return True
            # ID contains target
            if target in npc_id:
                return True
            return False

        # Direct id match
        if target in npcs and target in present_ids:
            return target

        for nid in present_ids:
            npc = npcs.get(nid, {})
            if name_matches(target, npc.get("name", ""), nid):
                return nid

        _log(f"Could not resolve talk target '{target}' to a present NPC")
        return None

    def _recent_scene_transcript(self, max_entries=6):
        """Plain-text transcript of the last few moments of the scene.

        The NPC agent is stateless: on its own it has no idea what the DM just
        narrated — including dialog the DM put in the NPC's own mouth during the
        opening or a prior beat. Without this, an NPC will flatly deny having
        said something the player plainly saw it say. We reconstruct the recent
        player-facing flow from _play_history (player inputs + DM narration) and
        hand it to the NPC so it can stay coherent with what just happened.
        """
        history = getattr(self, "_play_history", [])
        lines = []
        for entry in history[-max_entries:]:
            role = entry.get("role")
            text = entry.get("parts", [{}])[0].get("text", "")
            if role == "user":
                marker = "PLAYER INPUT: "
                idx = text.rfind(marker)
                if idx < 0:
                    continue  # NPC-dispatch bookkeeping message — skip
                said = text[idx + len(marker):].strip()
                # Skip the opening/resumed-scene directives (bracketed system text).
                if said and not said.startswith("["):
                    lines.append(f"Player: {said}")
            elif role == "model":
                try:
                    narration = (json.loads(_strip_json_fences(text)).get("narration") or "").strip()
                except (json.JSONDecodeError, ValueError):
                    narration = ""
                if narration:
                    lines.append(f"Narration: {narration}")
        return "\n".join(lines)

    def _dispatch_to_npc(self, npc_id, player_input):
        """Call the NPC agent and get their response. Synchronous (on worker thread)."""
        npc_data = self.world_state.get("npcs", {}).get(npc_id)
        if not npc_data:
            return None
        if not self._npc_agent.connected:
            _log("NPC agent not connected, DM will voice this NPC directly")
            return None

        # Talking to someone establishes who they are — reveal their name in
        # the on-screen "who's here" panel from now on.
        npc_data["known_to_player"] = True

        loc_id = self.world_state.get("current_location_id")
        loc = self.world_state.get("locations", {}).get(loc_id, {}) if loc_id else {}
        npcs = self.world_state.get("npcs", {})
        other_npcs = [
            npcs[nid].get("name", nid)
            for nid in loc.get("present_npc_ids", [])
            if nid != npc_id and nid in npcs
        ]
        scene_context = (
            f"Location: {loc.get('name', loc_id)}. "
            f"{loc.get('summary', '')} "
        )
        if other_npcs:
            scene_context += f"Also present: {', '.join(other_npcs)}."

        _log(f"Dispatching to NPC agent: {npc_data.get('name', npc_id)}")
        recent_scene = self._recent_scene_transcript()
        response = self._npc_agent.speak(npc_data, player_input, scene_context,
                                         self.world_state, recent_scene=recent_scene)
        # Apply any NPCs the NPC introduced (e.g., "you should talk to Officer Peterson")
        npc_introduced = response.pop("new_npcs", None) or {}
        if npc_introduced:
            self._apply_new_npcs(npc_introduced)
        return response

    def _weave_npc_response(self, npc_id, npc_response, player_input, initial_parsed):
        """Send the NPC response back to the DM to weave into narration."""
        npc_data = self.world_state.get("npcs", {}).get(npc_id, {})
        npc_name = npc_data.get("name", npc_id)

        dispatch_msg = DM_NPC_DISPATCH.format(
            npc_name=npc_name,
            speech=npc_response.get("speech", "..."),
            tells=json.dumps(npc_response.get("tells", [])),
            state_change=json.dumps(npc_response.get("internal_state_change", {})),
            player_input=player_input or "(opening scene)",
        )

        self._play_history.append(
            {"role": "user", "parts": [{"text": dispatch_msg}]}
        )

        try:
            cache = self._ensure_play_cache()
            raw = self._call_text(self._play_system(), self._play_history, context=npc_name,
                                  cached_content=cache.name if cache else None)
            self._play_history.append(
                {"role": "model", "parts": [{"text": raw}]}
            )
            return json.loads(_strip_json_fences(raw))
        except Exception as e:
            _log(f"Weave error: {e}")
            # Fall back: construct narration directly from NPC response
            tells_prose = " ".join(npc_response.get("tells", []))
            speech = npc_response.get("speech", "...")
            fallback_narration = f'{tells_prose} "{speech}"' if tells_prose else f'"{speech}"'
            return {
                "narration": fallback_narration,
                "speaker": npc_id,
                "state_changes": {
                    "npc_updates": {
                        npc_id: npc_response.get("internal_state_change", {})
                    }
                },
            }

    def _merge_talk_results(self, initial, woven, npc_id):
        """Merge the initial DM lead-in with the woven NPC response.

        The woven narration already contains the full NPC interaction, so the
        initial lead-in is dropped — it was generated before the NPC agent ran
        and often has the NPC speaking prematurely, causing duplicate/contradictory dialog.
        """
        result = dict(woven)
        result["speaker"] = npc_id
        result["intent"] = initial.get("intent", {})
        return result

    def _trim_history(self):
        """Keep history bounded and strip world-state bloat from older entries.

        The most recent RECENT_HISTORY entries keep their full context (world
        state + player input). Older entries are compacted to just the player
        input and DM narration — the current turn's context already has the
        up-to-date world state, so repeating it in old entries is pure waste.

        Beyond MAX_PLAY_HISTORY total entries, the oldest are dropped entirely.
        """
        h = self._play_history

        # Hard cap: drop oldest entries beyond MAX_PLAY_HISTORY
        if len(h) > self.MAX_PLAY_HISTORY:
            h[:] = h[-self.MAX_PLAY_HISTORY:]

        # Compact everything older than RECENT_HISTORY entries from the end.
        # We count entries (not pairs) since NPC dispatch adds extra entries.
        cutoff = len(h) - self.RECENT_HISTORY
        for i in range(max(0, cutoff)):
            entry = h[i]
            text = entry.get("parts", [{}])[0].get("text", "")
            if entry["role"] == "user":
                h[i] = {"role": "user", "parts": [{"text": self._compact_user_entry(text)}]}
            elif entry["role"] == "model":
                h[i] = {"role": "model", "parts": [{"text": self._compact_model_entry(text)}]}

    @staticmethod
    def _compact_user_entry(text):
        """Strip world-state preamble from an old user message, keep player input."""
        # The player input is always at the end after "PLAYER INPUT: "
        marker = "PLAYER INPUT: "
        idx = text.rfind(marker)
        if idx >= 0:
            return marker + text[idx + len(marker):]
        # NPC dispatch messages — keep them short but intact
        if "NPC RESPONSE:" in text:
            # Extract just the speech and player input lines
            lines = text.split("\n")
            kept = [l for l in lines if l.strip().startswith(("speech:", "PLAYER INPUT:"))]
            return "\n".join(kept) if kept else text[:200]
        return text[:200]

    @staticmethod
    def _compact_model_entry(text):
        """Strip state_changes JSON bloat from old DM responses, keep narration."""
        try:
            parsed = json.loads(_strip_json_fences(text))
            narration = parsed.get("narration", "")
            speaker = parsed.get("speaker", "dm")
            # Reconstruct a minimal version
            compact = {"narration": narration, "speaker": speaker}
            return json.dumps(compact)
        except (json.JSONDecodeError, ValueError):
            return text[:300]

    def _build_play_context(self, user_text):
        ws = self.world_state
        loc_id = ws.get("current_location_id")
        loc = ws.get("locations", {}).get(loc_id, {}) if loc_id else {}
        npcs = ws.get("npcs", {})
        present_npcs = {
            nid: npcs[nid] for nid in loc.get("present_npc_ids", [])
            if nid in npcs
        }

        sections = []

        # Game identity
        meta = ws.get("meta", {})
        clock = ws.get("narrative_clock", "")
        sections.append(
            f"GAME: {meta.get('title', 'Untitled')}\n"
            f"Tone: {meta.get('tone', '')}\n"
            f"Visual style: {meta.get('visual_style', '')}\n"
            f"Current time: {clock or '(not set)'}"
        )

        # Current location
        image_prompt = loc.get("image_prompt", "")
        loc_section = (
            f"CURRENT LOCATION: {loc.get('name', loc_id or '(unknown)')}\n"
            f"Summary: {loc.get('summary', '')}\n"
            f"Scene image (what the player sees painted on screen): {image_prompt}\n"
            f"Discovered features: {', '.join(loc.get('discovered_features', []) or ['(none)'])}\n"
            f"Events here: {loc.get('events_log_summary', '') or '(none yet)'}"
        )
        # Closed world: the player may only move along these authored exits.
        if self._is_closed_world():
            exit_pairs = []
            for ex in loc.get("exits", []) or []:
                ex_name = ws.get("locations", {}).get(ex, {}).get("name", ex)
                exit_pairs.append(f"{ex_name} (id: {ex})")
            loc_section += (
                "\nExits (the ONLY places reachable from here): "
                + (", ".join(exit_pairs) if exit_pairs else "(none — this is a dead end)")
            )
        sections.append(loc_section)

        # Present NPCs
        if present_npcs:
            npc_lines = []
            for nid, npc in present_npcs.items():
                line = (
                    f"  {npc.get('name', nid)} (id: {nid}): "
                    f"{npc.get('public_persona', '')} | "
                    f"Voice: {npc.get('voice', 'normal')} | "
                    f"Intent: {npc.get('current_intent', '')} | "
                    f"Mood toward player: {npc.get('mood_toward_player', '')} | "
                    f"Known: {npc.get('known_to_player', False)} | "
                    f"Dialog so far: {npc.get('dialog_summary_with_player', '') or '(none)'}"
                )
                knows = npc.get("knows", [])
                hides = npc.get("hides", [])
                lies_about = npc.get("lies_about", [])
                if knows:
                    line += f"\n    Knows: {'; '.join(knows)}"
                if hides:
                    line += f"\n    Hides: {'; '.join(hides)}"
                if lies_about:
                    line += f"\n    Lies about: {'; '.join(lies_about)}"
                npc_lines.append(line)
            sections.append("NPCs PRESENT:\n" + "\n".join(npc_lines))
        else:
            sections.append("NPCs PRESENT: (none)")

        # Off-screen NPCs (for NPC tick when time passes)
        offscreen_npcs = {
            nid: npc for nid, npc in npcs.items()
            if nid not in present_npcs
        }
        if offscreen_npcs:
            off_lines = []
            for nid, npc in offscreen_npcs.items():
                off_loc_id = npc.get("current_location_id", "?")
                off_loc = ws.get("locations", {}).get(off_loc_id, {})
                off_loc_name = off_loc.get("name", off_loc_id)
                off_lines.append(
                    f"  {npc.get('name', nid)} (id: {nid}): "
                    f"at {off_loc_name} | "
                    f"Intent: {npc.get('current_intent', '?')}"
                )
            sections.append("OFF-SCREEN NPCs:\n" + "\n".join(off_lines))

        # Player
        player = ws.get("player", {})
        inv = player.get("inventory", [])
        if inv:
            inv_lines = []
            for entry in inv:
                if isinstance(entry, dict):
                    name = entry.get("item", "???")
                    prov = entry.get("provenance", "")
                    loc = entry.get("found_location_name", "")
                    parts = [name]
                    if prov:
                        parts.append(f"({prov})")
                    if loc:
                        parts.append(f"[found: {loc}]")
                    inv_lines.append("  - " + " ".join(parts))
                else:
                    inv_lines.append(f"  - {entry}")
            inv_str = "\n".join(inv_lines)
        else:
            inv_str = "(empty)"
        sections.append(
            f"PLAYER: {player.get('name', 'Unknown')}\n"
            f"Inventory:\n{inv_str}"
        )

        # Known locations (for move validation)
        known_locs = [
            f"  {lid}: {ldata.get('name', lid)}"
            for lid, ldata in ws.get("locations", {}).items()
        ]
        if known_locs:
            sections.append("KNOWN LOCATIONS:\n" + "\n".join(known_locs))

        # Plot threads the player knows about
        known_threads = [
            t for t in ws.get("plot_threads", [])
            if t.get("known_to_player")
        ]
        if known_threads:
            thread_lines = [
                f"  (id: {t.get('id', '?')}) [{t.get('status', '?')}] {t.get('summary', '')}"
                for t in known_threads
            ]
            sections.append("ACTIVE PLOT THREADS (player knows):\n" + "\n".join(thread_lines))

        # DM bible (hidden from player, visible to DM)
        bible = ws.get("dm_bible", {})
        win_condition = bible.get("win_condition", "")
        if win_condition:
            sections.append(f"WIN CONDITION (how the game is won — hidden from player):\n  {win_condition}")
        secrets = bible.get("secrets", [])
        if secrets:
            secret_lines = [
                f"  {'[REVEALED] ' if s.get('revealed') else ''}"
                f"(id: {s.get('id', '?')}) {s.get('fact', '')}"
                for s in secrets
            ]
            sections.append("DM BIBLE — SECRETS:\n" + "\n".join(secret_lines))
        beats = bible.get("planned_beats", [])
        if beats:
            sections.append("DM BIBLE — PLANNED BEATS:\n  " + "\n  ".join(beats))
        scratchpad = bible.get("scratchpad", "")
        if scratchpad:
            sections.append(f"DM BIBLE — SCRATCHPAD:\n{scratchpad}")

        # Hidden plot threads
        hidden_threads = [
            t for t in ws.get("plot_threads", [])
            if not t.get("known_to_player")
        ]
        if hidden_threads:
            thread_lines = [
                f"  (id: {t.get('id', '?')}) [{t.get('status', '?')}] {t.get('summary', '')}"
                for t in hidden_threads
            ]
            sections.append("HIDDEN PLOT THREADS (player doesn't know yet):\n" + "\n".join(thread_lines))

        # DM instructions (premise, interview summary)
        dm_inst = ws.get("dm_instructions", {})
        if dm_inst.get("interview_summary"):
            sections.append(f"INTERVIEW SUMMARY:\n{dm_inst['interview_summary']}")

        # The player's input (or opening-scene directive)
        if user_text is None:
            has_history = bool(getattr(self, "_play_history", []))
            sections.append(RESUMED_SCENE_DIRECTIVE if has_history else OPENING_SCENE_DIRECTIVE)
        else:
            sections.append(f"PLAYER INPUT: {user_text}")

        return "\n\n".join(sections)

    def _apply_new_npcs(self, new_npcs):
        """Add dynamically created NPCs to world state and their location's present list."""
        ws = self.world_state
        for npc_id, npc_def in new_npcs.items():
            if not isinstance(npc_def, dict):
                continue
            if npc_id in ws.get("npcs", {}):
                _log(f"Dynamic NPC {npc_id} already exists, skipping")
                continue
            npc_def.setdefault("portrait_path", None)
            npc_def.setdefault("known_to_player", False)
            npc_def.setdefault("dialog_summary_with_player", "")
            npc_def.setdefault("voice", "")
            npc_def.setdefault("knows", [])
            npc_def.setdefault("hides", [])
            npc_def.setdefault("lies_about", [])
            npc_def["id"] = npc_id
            ws.setdefault("npcs", {})[npc_id] = npc_def
            npc_loc_id = npc_def.get("current_location_id")
            if npc_loc_id:
                loc_data = ws.get("locations", {}).get(npc_loc_id, {})
                if npc_id not in loc_data.get("present_npc_ids", []):
                    loc_data.setdefault("present_npc_ids", []).append(npc_id)
            _log(f"Dynamic NPC created: {npc_def.get('name', npc_id)} (id={npc_id})")

    def _apply_play_changes(self, parsed):
        ws = self.world_state
        changes = parsed.get("state_changes")
        if not changes:
            return

        # New location creation (must happen before current_location_id change)
        new_loc = changes.get("create_location")
        if isinstance(new_loc, dict) and new_loc.get("id"):
            loc_id = new_loc["id"]
            new_loc.setdefault("image_path", None)
            new_loc.setdefault("image_dirty", True)
            new_loc.setdefault("present_npc_ids", [])
            new_loc.setdefault("discovered_features", [])
            new_loc.setdefault("events_log_summary", "")
            new_loc.setdefault("exits", [])
            new_loc.setdefault("visible_exits", [])
            new_loc.setdefault("visited", False)
            ws.setdefault("locations", {})[loc_id] = new_loc
            _log(f"Created new location: {loc_id}")

        # Dynamically created NPCs (must happen before location change so present_npc_ids is correct)
        new_npcs = changes.get("new_npcs") or {}
        if new_npcs:
            self._apply_new_npcs(new_npcs)

        # Location change
        new_loc_id = changes.get("current_location_id")
        if new_loc_id and new_loc_id in ws.get("locations", {}):
            ws["current_location_id"] = new_loc_id
            ws["locations"][new_loc_id]["visited"] = True
            _log(f"Moved to: {new_loc_id}")

        # Narrative clock
        new_clock = changes.get("advance_clock")
        if new_clock:
            ws["narrative_clock"] = new_clock
            _log(f"Clock advanced: {new_clock}")

        # NPC tick — off-screen NPC updates when time passes
        for npc_id, npc_patch in (changes.get("npc_tick") or {}).items():
            npc = ws.get("npcs", {}).get(npc_id)
            if npc and isinstance(npc_patch, dict):
                old_loc = npc.get("current_location_id")
                npc.update(npc_patch)
                new_loc = npc.get("current_location_id")
                # Update present_npc_ids if the NPC moved
                if old_loc != new_loc:
                    old_loc_data = ws.get("locations", {}).get(old_loc, {})
                    new_loc_data = ws.get("locations", {}).get(new_loc, {})
                    if npc_id in old_loc_data.get("present_npc_ids", []):
                        old_loc_data["present_npc_ids"].remove(npc_id)
                    if new_loc and npc_id not in new_loc_data.get("present_npc_ids", []):
                        new_loc_data.setdefault("present_npc_ids", []).append(npc_id)
                    _log(f"NPC tick: {npc_id} moved {old_loc} → {new_loc}")
                else:
                    _log(f"NPC tick: {npc_id} updated (stayed in {old_loc})")

        # Image dirty flags
        for lid in changes.get("image_dirty") or []:
            if isinstance(lid, dict):
                lid = lid.get("id") or lid.get("location_id") or lid.get("location")
            if not isinstance(lid, str):
                continue
            loc = ws.get("locations", {}).get(lid)
            if loc:
                loc["image_dirty"] = True

        # Inventory — rich entries with provenance
        player = ws.setdefault("player", {})
        inv = player.setdefault("inventory", [])
        turn_num = len(getattr(self, "_play_history", []))
        cur_loc = ws.get("locations", {}).get(ws.get("current_location_id", ""), {})
        for item_entry in changes.get("inventory_add") or []:
            if isinstance(item_entry, dict):
                item_name = item_entry.get("item", str(item_entry))
            else:
                item_name = str(item_entry)
                item_entry = {"item": item_name}

            item_id = re.sub(r"[^a-z0-9]+", "_", item_name.lower()).strip("_")
            # Deduplicate id if needed
            existing_ids = {e.get("item_id") for e in inv if isinstance(e, dict)}
            base_id = item_id
            counter = 2
            while item_id in existing_ids:
                item_id = f"{base_id}_{counter}"
                counter += 1

            rich_entry = {
                "item": item_name,
                "item_id": item_id,
                "provenance": item_entry.get("provenance", ""),
                "found_location_id": ws.get("current_location_id", ""),
                "found_location_name": cur_loc.get("name", ""),
                "turn_acquired": turn_num,
                "sprite_path": None,
                "visual_description": item_entry.get("visual_description", ""),
            }
            inv.append(rich_entry)
            _log(f"Inventory add: {item_name} (id={item_id})")

        for item_name in changes.get("inventory_remove") or []:
            # Match by item name (case-insensitive)
            lower = item_name.lower()
            for i, entry in enumerate(inv):
                entry_name = entry.get("item", entry) if isinstance(entry, dict) else str(entry)
                if entry_name.lower() == lower:
                    inv.pop(i)
                    _log(f"Inventory remove: {item_name}")
                    break

        # Discovered features for current location
        cur_loc_id = ws.get("current_location_id")
        cur_loc = ws.get("locations", {}).get(cur_loc_id, {}) if cur_loc_id else {}
        for feat in changes.get("discovered_features_add") or []:
            existing = cur_loc.setdefault("discovered_features", [])
            if feat not in existing:
                existing.append(feat)

        # NPC updates
        for npc_id, npc_patch in (changes.get("npc_updates") or {}).items():
            npc = ws.get("npcs", {}).get(npc_id)
            if npc and isinstance(npc_patch, dict):
                npc.update(npc_patch)

        # Reveal secrets
        bible = ws.get("dm_bible", {})
        for secret_id in changes.get("reveal_secret") or []:
            for s in bible.get("secrets", []):
                if s.get("id") == secret_id:
                    s["revealed"] = True
                    _log(f"Secret revealed: {secret_id}")

        # Update plot threads. The applier matches on id, but the model
        # occasionally supplies a bad id — the summary sentence, or a freshly
        # invented slug for a thread that already exists — which historically
        # piled up duplicate threads. Reconcile to an existing thread before
        # appending, and never persist a prose id.
        threads = ws.setdefault("plot_threads", [])
        for thread_update in changes.get("update_threads") or []:
            tid = (thread_update.get("id") or "").strip()
            if not tid:
                continue
            target = next((t for t in threads if t.get("id") == tid), None)
            if target is None and _thread_id_looks_like_prose(tid):
                # Salvage a prose id: match it to an existing thread by its
                # slugified form or by summary text, and normalize what we store.
                slug = _slugify_thread_id(tid)
                key = tid.lower()
                target = next(
                    (t for t in threads
                     if t.get("id") == slug
                     or (t.get("summary", "") or "").strip().lower() == key),
                    None,
                )
                thread_update = {**thread_update, "id": slug}
                _log(f"normalized prose thread id {tid!r} -> {slug!r}"
                     + (" (matched existing)" if target else " (new)"))
            if target is not None:
                # Keep the canonical id; only apply the other fields.
                target.update({k: v for k, v in thread_update.items() if k != "id"})
            else:
                threads.append(thread_update)

        # Bible scratchpad append
        append_text = changes.get("bible_append")
        if append_text:
            existing = bible.get("scratchpad", "")
            bible["scratchpad"] = (existing + "\n" + append_text).strip()

        # Events log append
        log_text = changes.get("events_log_append")
        if log_text and cur_loc:
            existing = cur_loc.get("events_log_summary", "")
            cur_loc["events_log_summary"] = (
                (existing + " " + log_text).strip()
            )
