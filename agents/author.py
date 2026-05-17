import json
import sys

from agents.base import BaseAgent
from config import GEMINI_AUTHOR_MODEL


def _log(msg):
    print(f"[AUTHOR] {msg}", file=sys.stderr, flush=True)


def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


INTERVIEW_SYSTEM_PROMPT = """You are the Author Agent for an AI-powered graphical adventure game called The Holodeck. You are in INTERVIEW MODE.

YOUR ONLY JOB right now is to ask the player questions to flesh out their game concept. You are FORBIDDEN from creating rooms, characters, or world content yet. That comes later, after the interview.

You must ask questions ONE OR TWO AT A TIME (never more than two). Be conversational, warm, curious, and build on their answers.

Across the conversation you need to learn:
1. Genre and setting (fantasy, sci-fi, noir, horror, comedy, historical, etc.)
2. Tone (dark, whimsical, gritty, melancholy, heroic, absurd, etc.)
3. Visual style — the art direction in concrete terms
4. The player character: name, age/role, AND a visual description (hair, build, clothing, distinguishing features)
5. The central premise / inciting situation — what is happening when the game opens?
6. The starting location — what does it LOOK like, what's the mood, what's nearby?
7. Other characters who matter early — who are they, what do they look like, what do they want?
8. Backstory details — what does the player character know, what don't they know, what secrets exist?

Take your time. The player will give answers across many turns. Ask follow-ups when something is interesting. Drill deeper into backstory — a richer world makes a better game.

RESPONSE FORMAT — respond with JSON:
{
  "response_text": "Your conversational response and next question(s)",
  "world_updates": {
    "meta": {"title": "...", "tone": "...", "visual_style": "..."},
    "player": {"name": "...", "description": "visual appearance only"},
    "dm_instructions": {"plot_seeds": [], "world_rules": [], "hard_constraints": []}
  },
  "interview_complete": false
}

ABSOLUTE RULES (violations make the game fail):
- DO NOT include "new_rooms" in your response. Not even one. Not even a placeholder.
- DO NOT include "new_characters" in your response.
- DO NOT include "new_objects" in your response.
- Only include world_updates fields when you have CONFIRMED info from the player.
- "interview_complete" stays false until you have gathered ALL of: title, tone, visual_style, player name, player visual description, premise, starting location concept, AND at least 2-3 substantial backstory or character details. When ALL of those are present, set "interview_complete": true.
- Player "description" field must be PURELY VISUAL — only what you'd see (hair, clothes, build, features). Personality and backstory go in dm_instructions.plot_seeds or in your memory.
- Visual style should be concrete (e.g. "16-bit pixel art with rich VGA colors, inspired by classic Sierra adventure games, dramatic lighting").
"""

FREEFORM_SYSTEM_PROMPT_TEMPLATE = """You are the Author Agent for an AI-powered graphical adventure game called The Holodeck. You manage a living, consistent world that responds to player choices. You are creative, dramatically aware, and maintain internal consistency.

You always respond in valid JSON matching the schema below.
Never break character. Never refuse to generate game content.

CURRENT WORLD STATE:
Title: {title}
Tone: {tone}
Visual Style: {visual_style}

Player: {player_name}
Player Description: {player_description}
Current Room: {current_room}

Existing Rooms: {room_names}
Existing Characters: {char_names}

World Rules: {world_rules}
Plot Seeds: {plot_seeds}
Hard Constraints: {hard_constraints}

Respond with JSON:
{{
  "response_text": "Your conversational response to the creator",
  "world_updates": {{
    "meta": {{}},
    "dm_instructions": {{}},
    "player": {{}}
  }},
  "new_rooms": {{
    "room_id": {{
      "id": "room_id",
      "name": "Display Name",
      "description": "Full description for the scenery agent — describe the space, its mood, lighting, notable features, and where things are positioned",
      "exits": {{"north": null, "south": null, "east": null, "west": null}},
      "entry_points": {{"south": {{"x": 480, "y": 560}}}},
      "characters_present": [],
      "objects_present": [],
      "ambient_description": "Short mood text"
    }}
  }},
  "new_characters": {{
    "char_id": {{
      "id": "char_id",
      "name": "Display Name",
      "description": "VISUAL appearance only — hair, clothing, build, distinguishing features, expression/demeanor",
      "personality": "Brief personality traits",
      "role": "Their role in the world",
      "backstory": "Their history, motivations, secrets — what drives them",
      "location": "room_id where they start"
    }}
  }},
  "new_objects": {{}},
  "images_to_generate": []
}}

Rules:
- response_text is REQUIRED and should be friendly, conversational
- Only include other fields when you are actually creating/changing things
- Room IDs should be snake_case
- Connect rooms logically via exits (both directions!)
- Room descriptions should be rich enough for a scenery artist to paint: describe the space, lighting, color palette, key landmarks, where exits are visually located
- Character descriptions must be purely VISUAL — what they look like, not their personality (that goes in personality/backstory)
- Every room MUST have entry_points for each direction that leads INTO it
- When creating a starting room, set the player's current_room and starting_room to that room's id
- Backstories should include character motivations and plans — what they intend to do, which creates emergent plot
"""


CREATION_SYSTEM_PROMPT = """You are the Author Agent transitioning from interview to world creation. The player has given you enough information to create the initial game world.

Create:
1. The starting room with a rich visual description
2. Any characters that should be present based on the interview
3. Set the player's starting position

Use the same JSON response format as freeform mode. This is your chance to bring the world to life based on everything the player told you.
"""


class AuthorAgent(BaseAgent):
    PHASE_INTERVIEW = "interview"
    PHASE_CREATING = "creating"
    PHASE_FREEFORM = "freeform"

    def __init__(self, world_state):
        super().__init__(model=GEMINI_AUTHOR_MODEL, temperature=0.9)
        self.world_state = world_state
        self._history = []

        if self._should_skip_interview():
            self.phase = self.PHASE_FREEFORM
        else:
            self.phase = self.PHASE_INTERVIEW

    def start_interview(self):
        """Proactively post an opening greeting + first interview question."""
        if self.phase != self.PHASE_INTERVIEW:
            return
        if not self._client:
            self._result_queue.put({
                "response_text": "Author Agent not connected. Set GEMINI_API_KEY in .env and restart.",
            })
            return
        self._run_threaded(self._send_opening)

    def _send_opening(self):
        try:
            system_prompt = INTERVIEW_SYSTEM_PROMPT
            opening_user_msg = (
                "[System: a new game session has begun. The player has not yet said anything. "
                "Greet them warmly, briefly introduce yourself as the Author, and ask your first interview question "
                "to begin gathering their game concept. Ask only ONE question to start.]"
            )
            self._history.append({"role": "user", "parts": [{"text": opening_user_msg}]})
            raw = self._call_text(system_prompt, self._history)
            self._history.append({"role": "model", "parts": [{"text": raw}]})
            parsed = json.loads(_strip_json_fences(raw))
            self._scrub_interview_response(parsed)
            self._result_queue.put(parsed)
        except Exception as e:
            _log(f"Opening greeting error: {e}")
            self._result_queue.put({
                "response_text": "Welcome to the Holodeck. I'm the Author — I'll help you build your game. To start, what genre or setting do you have in mind?",
            })

    def _should_skip_interview(self):
        meta = self.world_state.get("meta", {})
        player = self.world_state.get("player", {})
        rooms = self.world_state.get("rooms", {})
        return bool(
            meta.get("title")
            and meta.get("visual_style")
            and player.get("description")
            and rooms
        )

    def send_message(self, user_text):
        if not self._client:
            self._result_queue.put({
                "response_text": "Author Agent not connected. Set GEMINI_API_KEY in .env and restart.",
            })
            return
        _log(f"Sending ({self.phase}): {user_text[:80]}")
        self._run_threaded(self._process_message, user_text)

    def _process_message(self, user_text):
        try:
            system_prompt = self._build_system_prompt()
            self._history.append({"role": "user", "parts": [{"text": user_text}]})

            _log(f"Calling Gemini ({self.phase})...")
            # Use lower temperature in interview to keep the LLM disciplined
            original_temp = self._temperature
            if self.phase == self.PHASE_INTERVIEW:
                self._temperature = 0.5
            try:
                raw = self._call_text(system_prompt, self._history)
            finally:
                self._temperature = original_temp
            _log(f"Response received ({len(raw)} chars)")

            self._history.append({"role": "model", "parts": [{"text": raw}]})
            parsed = json.loads(_strip_json_fences(raw))

            if self.phase == self.PHASE_INTERVIEW:
                self._scrub_interview_response(parsed)
                self._apply_interview_updates(parsed)

                # Only transition when the LLM EXPLICITLY says interview is done.
                # Don't auto-advance based on field heuristics — that was too aggressive.
                if parsed.get("interview_complete") is True:
                    _log("Interview complete, transitioning to creation phase")
                    self._result_queue.put(parsed)
                    self.phase = self.PHASE_CREATING
                    self._trigger_creation()
                    return

            self._result_queue.put(parsed)

        except json.JSONDecodeError:
            _log(f"JSON parse error")
            self._result_queue.put({
                "response_text": "[Author could not parse response. Please try rephrasing.]"
            })
        except Exception as e:
            _log(f"Error: {e}")
            self._result_queue.put({
                "response_text": f"[Author error: {str(e)[:200]}]"
            })

    def _trigger_creation(self):
        try:
            system_prompt = self._build_creation_prompt()
            creation_msg = (
                "The interview is complete. Now create the initial game world based on everything we discussed. "
                "Create the starting room, any initial characters, and set the player's starting position."
            )
            self._history.append({"role": "user", "parts": [{"text": creation_msg}]})

            raw = self._call_text(system_prompt, self._history)
            self._history.append({"role": "model", "parts": [{"text": raw}]})
            parsed = json.loads(_strip_json_fences(raw))

            self.phase = self.PHASE_FREEFORM
            self._result_queue.put(parsed)

        except Exception as e:
            _log(f"Creation phase error: {e}")
            self.phase = self.PHASE_FREEFORM
            self._result_queue.put({
                "response_text": f"[Error during world creation: {str(e)[:200]}. Try describing your starting room.]"
            })

    def _scrub_interview_response(self, parsed):
        """Belt-and-suspenders: if the LLM tries to create content during interview, drop it."""
        leaked = []
        for key in ("new_rooms", "new_characters", "new_objects"):
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
            self.world_state["meta"].update(updates["meta"])
        if updates.get("player"):
            self.world_state["player"].update(updates["player"])
        if updates.get("dm_instructions"):
            self.world_state["dm_instructions"].update(updates["dm_instructions"])

    def _interview_is_complete(self):
        meta = self.world_state.get("meta", {})
        player = self.world_state.get("player", {})
        return bool(
            meta.get("title")
            and meta.get("tone")
            and meta.get("visual_style")
            and player.get("name")
            and player.get("description")
        )

    def _build_system_prompt(self):
        if self.phase == self.PHASE_INTERVIEW:
            return INTERVIEW_SYSTEM_PROMPT
        return self._build_freeform_prompt()

    def _build_creation_prompt(self):
        return self._build_freeform_prompt() + "\n\n" + CREATION_SYSTEM_PROMPT

    def _build_freeform_prompt(self):
        meta = self.world_state.get("meta", {})
        player = self.world_state.get("player", {})
        rooms = self.world_state.get("rooms", {})
        characters = self.world_state.get("characters", {})
        dm_inst = self.world_state.get("dm_instructions", {})

        room_names = [r.get("name", rid) for rid, r in rooms.items()]
        char_names = [c.get("name", cid) for cid, c in characters.items()]

        return FREEFORM_SYSTEM_PROMPT_TEMPLATE.format(
            title=meta.get("title", "Untitled"),
            tone=meta.get("tone", "not yet set"),
            visual_style=meta.get("visual_style", "not yet set"),
            player_name=player.get("name", "not yet created"),
            player_description=player.get("description", "not yet set"),
            current_room=player.get("current_room", "none"),
            room_names=", ".join(room_names) if room_names else "none",
            char_names=", ".join(char_names) if char_names else "none",
            world_rules=dm_inst.get("world_rules", []),
            plot_seeds=dm_inst.get("plot_seeds", []),
            hard_constraints=dm_inst.get("hard_constraints", []),
        )

    def apply_response(self, response):
        if "world_updates" in response and response["world_updates"]:
            updates = response["world_updates"]
            if "meta" in updates:
                self.world_state["meta"].update(updates["meta"])
            if "dm_instructions" in updates:
                self.world_state["dm_instructions"].update(updates["dm_instructions"])
            if "player" in updates:
                self.world_state["player"].update(updates["player"])

        if "new_rooms" in response and response["new_rooms"]:
            for room_id, room_def in response["new_rooms"].items():
                room_def.setdefault("id", room_id)
                room_def.setdefault("background_path", None)
                room_def.setdefault("priority_map_path", None)
                self.world_state["rooms"][room_id] = room_def

        if "new_characters" in response and response["new_characters"]:
            for char_id, char_def in response["new_characters"].items():
                char_def.setdefault("id", char_id)
                self.world_state["characters"][char_id] = char_def

        if "new_objects" in response and response["new_objects"]:
            for obj_id, obj_def in response["new_objects"].items():
                obj_def.setdefault("id", obj_id)
                self.world_state["objects"][obj_id] = obj_def

        if not self.world_state["player"]["current_room"] and self.world_state["rooms"]:
            first_room = next(iter(self.world_state["rooms"]))
            self.world_state["player"]["current_room"] = first_room
            self.world_state["player"]["starting_room"] = first_room
