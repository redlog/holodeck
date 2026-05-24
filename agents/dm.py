"""DungeonMaster: the single agent that runs both the setup conversation
and (eventually) play-mode turns.

The same agent persona carries through the whole session. It currently
operates in two phases:

  PHASE_INTERVIEW — the setup conversation. Gathers genre, tone, visual
                    style, player character, premise, starting situation.
                    Does NOT create world content.

  PHASE_CREATING  — runs once immediately after the interview. The DM
                    privately seeds the starting location, any NPCs
                    visibly present at the opening, the DM bible
                    (secrets and planned beats), and initial plot threads.
                    No player interaction during this phase.

  PHASE_PLAY      — turn-based narration. Receives free-text player input,
                    parses intent, narrates, and emits state diffs.
                    *** Not yet implemented; see design/text_adventure_design.md ***

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
    CREATION_SYSTEM, PLAY_SYSTEM, OPENING_SCENE_DIRECTIVE,
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
        elif self._setup_already_complete():
            self.phase = self.PHASE_CREATING
        else:
            self.phase = self.PHASE_INTERVIEW

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
        """Proactively post an opening greeting + first interview question."""
        if self.phase != self.PHASE_INTERVIEW:
            return
        if not self.connected:
            self._result_queue.put({
                "response_text": "DM not connected. Set GEMINI_API_KEY in .env and restart.",
            })
            return
        self._run_threaded(self._send_opening)

    def _send_opening(self):
        try:
            opening_user_msg = INTERVIEW_OPENING_DIRECTIVE
            self._history.append({"role": "user", "parts": [{"text": opening_user_msg}]})
            raw = self._call_text(INTERVIEW_SYSTEM, self._history, context="interview")
            self._history.append({"role": "model", "parts": [{"text": raw}]})
            parsed = json.loads(_strip_json_fences(raw))
            self._scrub_interview_response(parsed)
            self._result_queue.put(parsed)
        except Exception as e:
            _log(f"Opening greeting error: {e}")
            self._result_queue.put({
                "response_text": "Welcome to the Holodeck. I'll help you build your game. To start, what genre or setting do you have in mind?",
            })

    def send_message(self, user_text):
        """Player text input. Dispatched to whichever phase we're in."""
        if not self.connected:
            self._result_queue.put({
                "response_text": "DM not connected. Set GEMINI_API_KEY in .env and restart.",
            })
            return
        if self.phase == self.PHASE_INTERVIEW:
            _log(f"interview send: {user_text[:80]}")
            self._run_threaded(self._process_interview, user_text)
        elif self.phase == self.PHASE_CREATING:
            _log("Input received during CREATING phase, ignored")
            self._result_queue.put({
                "response_text": "[The DM is preparing the world; please wait.]"
            })
        elif self.phase == self.PHASE_PLAY:
            _log(f"play send: {user_text[:80]}")
            self._run_threaded(self._process_play_turn, user_text)
        else:
            self._result_queue.put({"response_text": f"[Unknown phase: {self.phase}]"})

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

            self._result_queue.put(parsed)

        except json.JSONDecodeError:
            _log("JSON parse error")
            self._result_queue.put({
                "response_text": "[DM could not parse response. Please try rephrasing.]"
            })
        except Exception as e:
            _log(f"Error: {e}")
            self._result_queue.put({
                "response_text": f"[DM error: {str(e)[:200]}]"
            })

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
        """Kick off the one-shot world creation pass.

        Must be called while phase == PHASE_CREATING. Result lands on the
        result_queue as {'creation_complete': True, ...} on success or
        {'creation_complete': False, 'error': '...'} on failure.
        """
        if self.phase != self.PHASE_CREATING:
            _log(f"start_creation called in phase {self.phase}, ignoring")
            return
        if not self.connected:
            self._result_queue.put({
                "creation_complete": False,
                "error": "DM not connected (GEMINI_API_KEY missing).",
            })
            return
        self._run_threaded(self._run_creation)

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

            raw = self._call_text(CREATION_SYSTEM, [
                {"role": "user", "parts": [{"text": user_msg}]}
            ], context="author")
            parsed = json.loads(_strip_json_fences(raw))
            self._apply_creation(parsed)
            self.phase = self.PHASE_PLAY
            _log("Creation complete — transitioning to play phase")
            self._result_queue.put({
                "creation_complete": True,
                "starting_location_id": parsed.get("starting_location_id"),
                "location_count": len(parsed.get("new_locations") or {}),
                "npc_count": len(parsed.get("new_npcs") or {}),
                "secret_count": len(parsed.get("dm_bible", {}).get("secrets") or []),
                "thread_count": len(parsed.get("plot_threads") or []),
            })
        except json.JSONDecodeError as e:
            _log(f"Creation JSON parse error: {e}")
            self._result_queue.put({"creation_complete": False, "error": f"JSON parse: {e}"})
        except Exception as e:
            _log(f"Creation error: {e}")
            self._result_queue.put({"creation_complete": False, "error": str(e)})

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
            loc_def["id"] = loc_id
            ws["locations"][loc_id] = loc_def

        starting_id = parsed.get("starting_location_id")
        if starting_id and starting_id in ws["locations"]:
            ws["current_location_id"] = starting_id

        # Narrative clock
        clock = parsed.get("narrative_clock")
        if clock:
            ws["narrative_clock"] = clock

        # NPCs
        new_npcs = parsed.get("new_npcs") or {}
        ws.setdefault("npcs", {})
        for npc_id, npc_def in new_npcs.items():
            npc_def.setdefault("portrait_path", None)
            npc_def.setdefault("known_to_player", True)
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
                    system_instruction=PLAY_SYSTEM,
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
        """Fire a DM turn that narrates the opening scene (no player input)."""
        if self.phase != self.PHASE_PLAY:
            return
        if not self.connected:
            self._result_queue.put({
                "response_text": "DM not connected. Set GEMINI_API_KEY in .env and restart.",
            })
            return
        self._run_threaded(self._process_play_turn, None)

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
            raw = self._call_text(PLAY_SYSTEM, self._play_history, context=loc_name,
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

            self._result_queue.put(parsed)

        except json.JSONDecodeError:
            _log("Play turn JSON parse error")
            self._result_queue.put({
                "narration": "[The DM stumbles over their words. Try again.]",
                "speaker": "dm",
            })
        except Exception as e:
            _log(f"Play turn error: {e}\n{traceback.format_exc()}")
            self._result_queue.put({
                "narration": f"[DM error: {str(e)[:200]}]",
                "speaker": "dm",
            })

    def _resolve_talk_target(self, intent):
        """Find the npc_id for a talk intent's target."""
        target = (intent.get("target") or "").lower()
        if not target:
            return None

        npcs = self.world_state.get("npcs", {})
        loc_id = self.world_state.get("current_location_id")
        loc = self.world_state.get("locations", {}).get(loc_id, {}) if loc_id else {}
        present_ids = loc.get("present_npc_ids", [])

        # Direct id match
        if target in npcs and target in present_ids:
            return target

        # Name match against present NPCs
        for nid in present_ids:
            npc = npcs.get(nid, {})
            if target in npc.get("name", "").lower():
                return nid

        # Fuzzy: any present NPC whose name or id contains the target
        for nid in present_ids:
            npc = npcs.get(nid, {})
            name_lower = npc.get("name", "").lower()
            if target in name_lower or target in nid:
                return nid

        _log(f"Could not resolve talk target '{target}' to a present NPC")
        return None

    def _dispatch_to_npc(self, npc_id, player_input):
        """Call the NPC agent and get their response. Synchronous (on worker thread)."""
        npc_data = self.world_state.get("npcs", {}).get(npc_id)
        if not npc_data:
            return None
        if not self._npc_agent.connected:
            _log("NPC agent not connected, DM will voice this NPC directly")
            return None

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
        return self._npc_agent.speak(npc_data, player_input, scene_context, self.world_state)

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
            raw = self._call_text(PLAY_SYSTEM, self._play_history, context=npc_name,
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
        """Merge the initial DM lead-in with the woven NPC response."""
        lead_in = initial.get("narration", "")
        npc_narration = woven.get("narration", "")
        merged_narration = f"{lead_in}\n\n{npc_narration}".strip()

        result = dict(woven)
        result["narration"] = merged_narration
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
        sections.append(
            f"CURRENT LOCATION: {loc.get('name', loc_id or '(unknown)')}\n"
            f"Summary: {loc.get('summary', '')}\n"
            f"Scene image (what the player sees painted on screen): {image_prompt}\n"
            f"Discovered features: {', '.join(loc.get('discovered_features', []) or ['(none)'])}\n"
            f"Events here: {loc.get('events_log_summary', '') or '(none yet)'}"
        )

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
                f"  [{t.get('status', '?')}] {t.get('summary', '')}"
                for t in known_threads
            ]
            sections.append("ACTIVE PLOT THREADS (player knows):\n" + "\n".join(thread_lines))

        # DM bible (hidden from player, visible to DM)
        bible = ws.get("dm_bible", {})
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
                f"  [{t.get('status', '?')}] {t.get('summary', '')}"
                for t in hidden_threads
            ]
            sections.append("HIDDEN PLOT THREADS (player doesn't know yet):\n" + "\n".join(thread_lines))

        # DM instructions (premise, interview summary)
        dm_inst = ws.get("dm_instructions", {})
        if dm_inst.get("interview_summary"):
            sections.append(f"INTERVIEW SUMMARY:\n{dm_inst['interview_summary']}")

        # The player's input (or opening-scene directive)
        if user_text is None:
            sections.append(OPENING_SCENE_DIRECTIVE)
        else:
            sections.append(f"PLAYER INPUT: {user_text}")

        return "\n\n".join(sections)

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
            ws.setdefault("locations", {})[loc_id] = new_loc
            _log(f"Created new location: {loc_id}")

        # Location change
        new_loc_id = changes.get("current_location_id")
        if new_loc_id and new_loc_id in ws.get("locations", {}):
            ws["current_location_id"] = new_loc_id
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

        # Update plot threads
        for thread_update in changes.get("update_threads") or []:
            tid = thread_update.get("id")
            if not tid:
                continue
            found = False
            for t in ws.get("plot_threads", []):
                if t.get("id") == tid:
                    t.update(thread_update)
                    found = True
                    break
            if not found:
                ws.setdefault("plot_threads", []).append(thread_update)

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
