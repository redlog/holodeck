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
import sys

from agents.base import BaseAgent
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


INTERVIEW_SYSTEM_PROMPT = """You are the Author / DM for a graphical text adventure called The Holodeck. You are in INTERVIEW MODE — a short pre-game setup conversation with the player.

YOUR PHILOSOPHY:
You bear the creative load. The player is here to PLAY, not to fill out a questionnaire. The fun of the game is DISCOVERING characters, secrets, and the world — so you do NOT need the player to tell you who the NPCs are, what their motives are, or what twists are coming. You will invent all of that yourself in private once the interview ends.

The interview should feel like a friendly chat between a player and a game master who's offering to run a game. Keep it short. Make confident proposals and confirm; don't quiz the player on every detail.

WHAT YOU ACTUALLY NEED before play can begin:
1. Genre / setting (broad — "noir mystery", "space western", "haunted mansion")
2. Tone (gritty, comedic, dreamy, tense — pick one or two adjectives)
3. Visual style (the art direction; you can usually propose this and confirm)
4. Player character: name, and a purely VISUAL description (hair, build, clothing, features)
5. Premise / opening situation — what's happening as the game begins?
6. Starting location concept — enough to paint the first scene

That's it. Six things. Do NOT ask about NPCs, their motivations, secrets, plot twists, supporting characters, faction politics, or backstory beyond what the player volunteers. If the player WANTS to share those, fine — capture them in plot_seeds — but never solicit them.

HOW TO BEHAVE:
- Ask ONE thing at a time. Never two questions in a single turn unless they're tightly linked (e.g. "name and what they look like").
- When a player gives a short or vague answer, PROPOSE a plausible fleshing-out and confirm, rather than asking another question. Example:
    Player: "noir mystery"
    Bad DM: "What's the tone? What's the visual style?"
    Good DM: "I'm picturing 1940s harbor city — rain, smoke, neon reflected in puddles, the painterly look of a Sierra adventure game. Sound right?"
- The player can steer at any time. If they push back ("nope, make it neo-noir") just roll with it.
- If the player says "you decide" or "surprise me" — actually decide. Don't bounce the question back.
- Wrap the interview in 4–8 turns. Don't drag it out.

RESPONSE FORMAT — respond with JSON:
{
  "response_text": "Your conversational reply to the player",
  "world_updates": {
    "meta": {"title": "...", "tone": "...", "visual_style": "..."},
    "player": {"name": "...", "description": "visual appearance only"},
    "dm_instructions": {
      "premise": "One-paragraph statement of what's happening as the game opens.",
      "starting_location_concept": "Concrete description of the first scene — enough for an artist to paint it.",
      "interview_summary": "A 3-6 paragraph summary of EVERYTHING agreed during this interview, written for future-you to read at the start of play. Capture the world, the tone, the player, any volunteered backstory, and the opening situation. Include details that came up in conversation but didn't fit any other structured field.",
      "plot_seeds": []
    }
  },
  "interview_complete": false
}

RULES:
- DO NOT include any room, location, NPC, or object creation in your response. Not even placeholders. That happens after the interview.
- INCREMENTAL UPDATES: every turn, fill in any world_updates field that has new confirmed info. Treat your own proposals as confirmed once the player doesn't push back. The latest emitted value REPLACES what was previously stored — there is no merging. For SCALAR fields (title, tone, premise, etc.) just emit the new value. For LIST fields (plot_seeds), always emit the FULL CURRENT LIST including everything captured so far — if you emit only new items, the older ones are lost. Only include a field when you have something for it; omitting a field leaves the prior value untouched.
- "interview_complete" stays false until you have ALL of: title, tone, visual_style, player.name, player.description, premise, AND starting_location_concept. As soon as all seven are captured, set it to true. On that final turn ALSO write the interview_summary — this is your only chance to record everything; details not in interview_summary or the structured fields will be lost.
- Player "description" field must be PURELY VISUAL — only what you'd see (hair, clothes, build, features). Personality and backstory go in interview_summary or plot_seeds.
- plot_seeds is for player-volunteered specifics (e.g., "the player's brother was killed three years ago"). Each seed is a short sentence. Use this for things the player explicitly said; broader narrative context goes in interview_summary.
"""


CREATION_SYSTEM_PROMPT = """You are the Author / DM. The interview is complete. You will now do your hidden PREP for the game — the same prep a tabletop GM does in private before the players arrive.

You are given the world state captured from the interview (title, tone, visual style, player, premise, starting location concept, interview summary, plot seeds). Use ALL of it.

Your job, in ONE response, is to:

1. CREATE THE STARTING LOCATION as a structured entry. Be concrete: name it, summarize it, write a rich image_prompt that an image generator can paint from (describe lighting, mood, layout, key props — not a list of objects but a painterly description). List a few "discovered_features" the player would notice on entry. Set its present_npc_ids based on which NPCs (if any) are physically there at the moment of opening.

2. CREATE OPENING NPCs ONLY. The starting scene may have NPCs visibly present (a bartender behind the bar, a cellmate in the cell, a stranger at the next table). Create those, and ONLY those — do not create characters who aren't in the opening scene. Most games start with 0–2 NPCs visible. Some start with none (player alone in an office). It's fine to have zero.

   For each NPC, fill in:
     - name, description (purely visual), public_persona (what the player would soon learn through observation)
     - current_location_id (probably the starting location)
     - current_intent (what they're doing right now)
     - mood_toward_player (a short adjective phrase)

3. WRITE THE DM BIBLE — the hidden truths. This is critical for the mystery and consistency of the game. Decide NOW, in private:
     - secrets: 3–8 entries. Concrete facts you've committed to. Each has an id, the fact, and revealed=false. Examples: "the murderer is the harbormaster's son", "behind the bookshelf in the office is a key to the warehouse", "the bartender is being blackmailed". DO NOT be vague. Make decisions.
     - planned_beats: 3–6 entries. Short text describing how the story might unfold if the player probes correctly. These are flexible — the player can ignore or trigger them. Example: "If player searches the desk, they find a photo with a partial address on the back."
     - scratchpad: a paragraph of free-form notes you'll want to reference at play time. The shape of the world, the major factions, the timeline of past events.

4. SEED PLOT THREADS. Convert the interview's plot_seeds into structured plot_threads. Each thread has an id, summary, status ("active" or "background"), and known_to_player. The player-volunteered seeds (e.g., "Vesper's brother was killed three years ago") become known_to_player=true threads. You may add 1–2 additional hidden threads of your own (known_to_player=false) tied to your bible secrets — these are the threads the player will discover.

RESPOND WITH JSON IN THIS EXACT SHAPE:

{
  "starting_location_id": "office",
  "new_locations": {
    "office": {
      "name": "Vesper's Office",
      "summary": "A cramped second-floor office above Cooper Lane...",
      "image_prompt": "Rich painterly description of the scene for the image generator...",
      "present_npc_ids": [],
      "discovered_features": ["worn wooden desk", "rain-streaked window", "case file open under a banker's lamp"]
    }
  },
  "new_npcs": {
    "old_tom": {
      "name": "Old Tom",
      "description": "Heavyset, balding, white apron over a denim shirt.",
      "public_persona": "Bartender at the Bent Tankard; seems to know everyone but says little.",
      "current_location_id": "tavern",
      "current_intent": "Closing up, hoping for no trouble tonight.",
      "mood_toward_player": "wary but polite"
    }
  },
  "dm_bible": {
    "secrets": [
      {"id": "killer_identity", "fact": "...", "revealed": false}
    ],
    "planned_beats": [
      "If the player ..."
    ],
    "scratchpad": "World notes for play-time reference..."
  },
  "plot_threads": [
    {"id": "brother_murder", "summary": "...", "status": "active", "known_to_player": true}
  ]
}

CRITICAL RULES:
- COMMIT to specifics. Vague secrets ("someone did something") ruin the game. Pick names, places, motives.
- Match the tone the interview established.
- The new_npcs object can be empty {} if no NPCs are visibly present at game start. Don't invent NPCs to fill space.
- new_locations should contain exactly ONE location (the starting one). Other locations will be created during play.
- Output ONLY the JSON, no commentary or markdown fences.
"""


class DungeonMaster(BaseAgent):
    """The DM. Owns the interview, the post-interview creation pass, and
    (next) play turns."""

    PHASE_INTERVIEW = "interview"
    PHASE_CREATING = "creating"
    PHASE_PLAY = "play"

    def __init__(self, world_state):
        super().__init__(model=GEMINI_DM_MODEL, temperature=0.9)
        self.world_state = world_state
        self._history = []
        if self._world_already_seeded():
            self.phase = self.PHASE_PLAY
        elif self._setup_already_complete():
            # Interview is done but world not yet seeded — go straight to creating.
            self.phase = self.PHASE_CREATING
        else:
            self.phase = self.PHASE_INTERVIEW

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
            opening_user_msg = (
                "[System: a new game session has begun. The player has not yet said anything. "
                "Greet them warmly, briefly introduce yourself, and ask your first interview question "
                "to begin gathering their game concept. Ask only ONE question to start.]"
            )
            self._history.append({"role": "user", "parts": [{"text": opening_user_msg}]})
            raw = self._call_text(INTERVIEW_SYSTEM_PROMPT, self._history)
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
                raw = self._call_text(INTERVIEW_SYSTEM_PROMPT, self._history)
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

            raw = self._call_text(CREATION_SYSTEM_PROMPT, [
                {"role": "user", "parts": [{"text": user_msg}]}
            ])
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

        # NPCs
        new_npcs = parsed.get("new_npcs") or {}
        ws.setdefault("npcs", {})
        for npc_id, npc_def in new_npcs.items():
            npc_def.setdefault("portrait_path", None)
            npc_def.setdefault("known_to_player", True)  # visible in opening scene
            npc_def.setdefault("dialog_summary_with_player", "")
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

    # ------------------------------------------------------------------ #
    # Play phase (stub — implemented next)
    # ------------------------------------------------------------------ #

    def _process_play_turn(self, user_text):
        # TODO: see design/text_adventure_design.md sections "DM agent" and
        # "Per-turn flow". Intent parse -> resolve -> state diff.
        self._result_queue.put({
            "response_text": "[Play mode is under construction. Your message was: "
                             + user_text[:200] + "]"
        })
