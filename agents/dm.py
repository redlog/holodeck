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


PLAY_SYSTEM_PROMPT = """You are the DM of a graphical text adventure called The Holodeck. You are now in PLAY MODE — the player is exploring the world and you are narrating their experience.

You will receive:
- The current world state (location, NPCs present, inventory, plot threads)
- Your DM bible (secrets, planned beats, scratchpad) — hidden from the player
- Recent conversation history
- The player's latest input

YOUR JOB ON EACH TURN:

1. PARSE INTENT. Classify what the player is trying to do into one of these actions:
   - "look" — examine the surroundings or a specific target
   - "move" — travel to a different location
   - "talk" — speak to an NPC
   - "take" — pick up an item
   - "use" — use an item, possibly on a target
   - "wait" — let time pass
   - "freeform" — anything else (creative actions, combat, manipulation, etc.)

2. RESOLVE. Based on the intent:
   - NARRATE the outcome. You are the storyteller — write vivid, atmospheric prose in the game's established tone. Keep narration to 2-5 sentences typically; big dramatic moments can be longer.
   - If an NPC speaks, voice them yourself in character. Write their dialog in quotes. Include physical tells (gestures, expressions) woven into the narration.
   - If the action is impossible, refuse with in-fiction narration ("The piano is bolted to the stage."). Never break the fourth wall.
   - If the player seems stuck, weave a subtle hint into the environment or NPC dialog.

SCENE IMAGES ARE PART OF THE WORLD:
The player sees a painted scene image for each location. The "image_prompt" in the location data describes exactly what was painted — every prop, detail, and visual clue shown on screen. When the player asks about something they can see ("what's on the desk?", "who's that in the corner?", "what does the sign say?"), answer based on the image_prompt — it is the visual ground truth. If you mention something in narration that should be visible, make sure it's consistent with what was painted. The player WILL notice details in the scene and ask about them.

3. EMIT STATE CHANGES. Report any changes to the world as structured data.

RESPONSE FORMAT — respond with JSON:
{
  "intent": {
    "action": "look|move|talk|take|use|wait|freeform",
    "target": "what/who the action is directed at (optional)",
    "detail": "any qualifier — topic of conversation, destination, etc. (optional)"
  },
  "narration": "Your narrative text. This is what the player reads.",
  "speaker": "dm",
  "state_changes": {
    "current_location_id": null,
    "create_location": null,
    "image_dirty": [],
    "inventory_add": [],
    "inventory_remove": [],
    "discovered_features_add": [],
    "npc_updates": {},
    "reveal_secret": [],
    "update_threads": [],
    "bible_append": null,
    "events_log_append": null
  }
}

STATE CHANGES — field details:

- "current_location_id": set to a location id string when the player moves. null if staying put.

- "create_location": when the player moves to a place that doesn't exist yet, you MUST create it. Provide a full location object:
  {"id": "docks", "name": "The Docks", "summary": "...", "image_prompt": "...", "present_npc_ids": [], "discovered_features": [...]}
  The image_prompt is CRITICAL — it becomes the visual ground truth for this location. Write it as a rich, detailed painterly description that an image generator can paint from. Include:
    * The physical space, lighting, mood, and atmosphere
    * Specific props and objects the player might examine or interact with
    * Environmental storytelling — clues, evidence, or details that hint at the plot (a half-open drawer, a stain on the floor, a photograph turned face-down)
    * Any NPCs present and what they're doing
    * Details consistent with the game's tone and visual style
  Everything you put in the image_prompt will be painted and shown to the player. Everything you leave out will be invisible. Be generous with detail — the player will scrutinize every inch of the scene.

- "image_dirty": list of location ids whose appearance has changed enough to warrant a new image (e.g., a fire breaks out, lights turn on/off, major destruction). Usually empty.

- "inventory_add": list of objects when the player picks something up. Each entry:
  {"item": "brass key", "provenance": "Found in the top drawer of Marta's desk while she was in the kitchen."}
  The provenance is a short narrative paragraph of where/how the item was acquired.

- "inventory_remove": list of item name strings when an item is consumed, given away, or lost.

- "discovered_features_add": list of strings to add to the current location's discovered_features when the player notices new details.

- "npc_updates": dict of npc_id → partial NPC state to merge. For example:
  {"bartender": {"mood_toward_player": "hostile", "current_intent": "Call the bouncer"}}
  Use this to update mood, intent, dialog_summary, known_to_player, or current_location_id.

- "reveal_secret": list of secret id strings from the DM bible when a secret is revealed to the player through narration or discovery.

- "update_threads": list of objects to update plot threads:
  [{"id": "brother_murder", "status": "active", "known_to_player": true}]

- "bible_append": optional string to append to the DM bible scratchpad when you make a new private decision or note. Example: "Decided that the warehouse key is hidden in the piano bench."

- "events_log_append": optional string to append to the current location's events log. Brief summary of what just happened here.

RULES:
- OMIT state_changes fields that have no changes (null, empty list, empty dict). Only include fields with actual changes.
- The "narration" field is the ONLY thing the player sees. Never leak bible secrets, intent classification, or state machinery into narration.
- You KNOW your bible. Use it to maintain consistency. If a secret says "the bartender is the murderer," never let the bartender accidentally confess unless the player has earned that revelation.
- When the player LOOKs at the current room, describe what they see — use the location summary, discovered features, present NPCs, and current conditions. Add new details as discovered_features_add.
- When the player MOVEs, you may create a new location or move to an existing one. Always set current_location_id. If creating, also set create_location.
- INVENTORY is common-sense only. The player can carry small/medium items. Refuse absurd pickups narratively.
- NPCs are voiced by you. Stay in character for each one. Update their npc_updates when their mood or intent changes from the interaction.
- Be a GREAT storyteller. Create tension, atmosphere, surprises. Reveal secrets gradually. Reward clever play.
- Keep the game MOVING. If the player does something reasonable, make it work and advance the story. Don't block progress with arbitrary puzzle gates.
- The "speaker" field is normally "dm". When an NPC is the primary voice in the narration (direct dialog), set it to the npc_id so the UI can show their portrait.
"""


CREATION_SYSTEM_PROMPT = """You are the Author / DM. The interview is complete. You will now do your hidden PREP for the game — the same prep a tabletop GM does in private before the players arrive.

You are given the world state captured from the interview (title, tone, visual style, player, premise, starting location concept, interview summary, plot seeds). Use ALL of it.

Your job, in ONE response, is to:

1. CREATE THE STARTING LOCATION as a structured entry. Be concrete: name it, summarize it, and write a rich image_prompt. The image_prompt is CRITICAL — it becomes the visual ground truth for this location. The player will see a painted scene based on this description and will examine every detail closely. Write it as a vivid painterly description including:
   - The physical space, lighting, mood, atmosphere
   - Specific props and objects (documents on a desk, items on shelves, stains, wear patterns)
   - Environmental storytelling — visual clues that hint at your secrets and planned beats (a half-open drawer, a photograph, a specific book title, a mark on the wall)
   - Any NPCs present and what they're doing physically
   - Details that reward the observant player — not everything should be obvious
   List "discovered_features" the player would notice on entry. Set its present_npc_ids based on which NPCs (if any) are physically there.

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
    # Play phase
    # ------------------------------------------------------------------ #

    MAX_PLAY_HISTORY = 20  # keep last N exchanges in context

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
            # Trim to keep context manageable
            if len(self._play_history) > self.MAX_PLAY_HISTORY:
                self._play_history = self._play_history[-self.MAX_PLAY_HISTORY:]

            raw = self._call_text(PLAY_SYSTEM_PROMPT, self._play_history)
            self._play_history.append(
                {"role": "model", "parts": [{"text": raw}]}
            )

            parsed = json.loads(_strip_json_fences(raw))
            self._apply_play_changes(parsed)

            self._result_queue.put(parsed)

        except json.JSONDecodeError:
            _log("Play turn JSON parse error")
            self._result_queue.put({
                "narration": "[The DM stumbles over their words. Try again.]",
                "speaker": "dm",
            })
        except Exception as e:
            _log(f"Play turn error: {e}")
            self._result_queue.put({
                "narration": f"[DM error: {str(e)[:200]}]",
                "speaker": "dm",
            })

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
        sections.append(
            f"GAME: {meta.get('title', 'Untitled')}\n"
            f"Tone: {meta.get('tone', '')}\n"
            f"Visual style: {meta.get('visual_style', '')}"
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
                npc_lines.append(
                    f"  {npc.get('name', nid)} (id: {nid}): "
                    f"{npc.get('public_persona', '')} | "
                    f"Intent: {npc.get('current_intent', '')} | "
                    f"Mood toward player: {npc.get('mood_toward_player', '')} | "
                    f"Known: {npc.get('known_to_player', False)} | "
                    f"Dialog so far: {npc.get('dialog_summary_with_player', '') or '(none)'}"
                )
            sections.append("NPCs PRESENT:\n" + "\n".join(npc_lines))
        else:
            sections.append("NPCs PRESENT: (none)")

        # Player
        player = ws.get("player", {})
        inv = player.get("inventory", [])
        inv_str = ", ".join(inv) if inv else "(empty)"
        sections.append(
            f"PLAYER: {player.get('name', 'Unknown')}\n"
            f"Inventory: {inv_str}"
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
            sections.append(
                "PLAYER INPUT: [This is the very first turn. The player has just arrived. "
                "Narrate the opening scene — describe where they are, what they see, "
                "the atmosphere, any NPCs present. Set the mood and hook them into the story. "
                "Do NOT ask the player a question; just paint the scene.]"
            )
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

        # Image dirty flags
        for lid in changes.get("image_dirty") or []:
            loc = ws.get("locations", {}).get(lid)
            if loc:
                loc["image_dirty"] = True

        # Inventory
        player = ws.setdefault("player", {})
        inv = player.setdefault("inventory", [])
        for item_entry in changes.get("inventory_add") or []:
            if isinstance(item_entry, dict):
                inv.append(item_entry.get("item", str(item_entry)))
            else:
                inv.append(str(item_entry))
        for item_name in changes.get("inventory_remove") or []:
            try:
                inv.remove(item_name)
            except ValueError:
                pass

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
