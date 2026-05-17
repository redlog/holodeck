import json
import re
import sys
import threading
from queue import Queue, Empty

from google import genai
from google.genai import types
from dotenv import load_dotenv
import os


def _log(msg):
    print(f"[DM] {msg}", file=sys.stderr, flush=True)

load_dotenv(override=True)

from config import GEMINI_DM_MODEL


def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _build_system_prompt(world_state):
    meta = world_state.get("meta", {})
    player = world_state.get("player", {})
    rooms = world_state.get("rooms", {})
    characters = world_state.get("characters", {})

    room_names = [r.get("name", rid) for rid, r in rooms.items()]
    char_names = [c.get("name", cid) for cid, c in characters.items()]

    return f"""You are the Dungeon Master (DM) for an AI-powered graphical adventure game called The Holodeck. You manage a living, consistent world that responds to player choices. You are creative, dramatically aware, and maintain internal consistency at all times.

You always respond in valid JSON matching the schema requested.
Never break character. Never refuse to generate game content.
Keep the tone and style consistent with the game bible.

CURRENT WORLD STATE:
Title: {meta.get('title', 'Untitled')}
Tone: {meta.get('tone', 'not yet set')}
Visual Style: {meta.get('visual_style', 'not yet set')}

Player: {player.get('name', 'not yet created')}
Player Description: {player.get('description', 'not yet set')}
Current Room: {player.get('current_room', 'none')}

Existing Rooms: {', '.join(room_names) if room_names else 'none'}
Existing Characters: {', '.join(char_names) if char_names else 'none'}

World Rules: {world_state.get('dm_instructions', {}).get('world_rules', [])}
Plot Seeds: {world_state.get('dm_instructions', {}).get('plot_seeds', [])}
Hard Constraints: {world_state.get('dm_instructions', {}).get('hard_constraints', [])}
"""


HOLODECK_RESPONSE_INSTRUCTIONS = """
Respond as a helpful, creative Dungeon Master helping the player build their world.

The game screen is 960x600 pixels. X goes left (0) to right (960). Y goes top (0) to bottom (600).
The player character sprite is about 40x60 pixels.

When the player gives you world-building instructions, respond with JSON in this format:
{
  "response_text": "Your conversational response to the creator",
  "world_updates": {
    "meta": {},
    "dm_instructions": {},
    "player": {}
  },
  "new_rooms": {
    "room_id": {
      "id": "room_id",
      "name": "Display Name",
      "description": "Full description",
      "background_prompt": "Image generation prompt for this room — describe WHERE each piece of furniture/scenery is positioned (left side, right side, center, against the back wall, etc.)",
      "exits": {"north": null, "south": null, "east": null, "west": null},
      "exit_zones": {},
      "walkable_zone": {"type": "lower_percentage", "value": 50-85},
      "obstacles": [
        {"id": "obstacle_name", "label": "what it is", "rect": {"x": 0, "y": 0, "width": 0, "height": 0}}
      ],
      "entry_points": {"south": {"x": 480, "y": 560}, "west": {"x": 60, "y": 400}},
      "characters_present": [],
      "objects_present": [],
      "visited": false,
      "ambient_description": "Short mood text"
    }
  },
  "new_characters": {
    "char_id": {
      "id": "char_id",
      "name": "Display Name",
      "description": "Physical appearance description for image generation — hair, clothing, build, distinguishing features",
      "personality": "Brief personality traits",
      "role": "Their role in the world",
      "location": "room_id where they can be found"
    }
  },
  "new_objects": {},
  "images_to_generate": []
}

Rules:
- response_text is REQUIRED and should be friendly, conversational
- Only include other fields when you are actually creating/changing things
- Room IDs should be snake_case
- If the world is empty and the player hasn't told you what they want yet, ASK them
- When creating a starting room, set the player's current_room and starting_room to that room's id
- Keep descriptions vivid but concise
- Connect rooms logically via exits (both directions)
- Set walkable_zone value per room based on the scene: wide open areas (meadows, plazas) use 75-85, normal rooms use 55-65, cluttered or narrow spaces use 40-50. This controls what percentage of the screen (from the bottom) the player can walk on.
- When setting the player's description, focus on VISUAL appearance: hair color, clothing, build, distinguishing features. This description is used to generate their sprite and portrait image.
- Character descriptions should also be visual — what they LOOK like, not their backstory.
- Every room MUST have entry_points — one per exit direction that leads INTO this room. If a room has a south exit (meaning another room connects here from the south), define an entry point near the bottom center. Entry point coordinates should be within the walkable zone and clear of obstacles. Common defaults: south entry = bottom center (480, 560), north entry = upper walkable area (480, top+40), west entry = left side (60, 400), east entry = right side (900, 400).

OBSTACLE AND LAYOUT RULES (very important):
- Every piece of furniture, wall feature, or large object in the room MUST have a corresponding obstacle rect
- Obstacle rects define areas the player CANNOT walk through — like a priority map in Sierra AGI games
- The rect coordinates are in screen pixels (960x600). Place them where the base/footprint of the object would be on the floor
- Examples for a 960x600 room with 60% walkable (walkable area y=240 to y=600):
  - A bed against the left wall: {"id": "bed", "label": "bed", "rect": {"x": 30, "y": 280, "width": 200, "height": 120}}
  - A desk in the center-right: {"id": "desk", "label": "desk", "rect": {"x": 500, "y": 300, "width": 180, "height": 80}}
  - A pillar: {"id": "pillar", "label": "stone pillar", "rect": {"x": 400, "y": 350, "width": 50, "height": 50}}
- Think about where the player needs to walk AROUND objects — leave gaps between obstacles for pathways
- The background_prompt MUST describe furniture in the same positions as the obstacle rects so the image matches the collision map
- Use language like "a bed against the left wall in the lower-left area" or "a desk to the right of center" so the image generator places things where the obstacles are
"""


class DungeonMaster:
    def __init__(self, world_state):
        self.world_state = world_state
        self._response_queue = Queue()
        self._busy = False

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key or api_key == "your_key_here":
            _log("WARNING: No API key set")
            self._client = None
        else:
            self._client = genai.Client(api_key=api_key)
            _log(f"Connected (model: {GEMINI_DM_MODEL})")

        self._history = []

    @property
    def connected(self):
        return self._client is not None

    @property
    def busy(self):
        return self._busy

    def send_holodeck_message(self, user_text):
        if not self._client:
            self._response_queue.put({
                "response_text": "DM not connected. Set your GEMINI_API_KEY in .env and restart.",
            })
            return

        _log(f"Sending: {user_text[:80]}")
        self._busy = True
        thread = threading.Thread(target=self._call_gemini, args=(user_text,), daemon=True)
        thread.start()

    def _call_gemini(self, user_text):
        try:
            system_prompt = _build_system_prompt(self.world_state) + "\n\n" + HOLODECK_RESPONSE_INSTRUCTIONS

            self._history.append({"role": "user", "parts": [{"text": user_text}]})

            _log("Calling Gemini API...")
            response = self._client.models.generate_content(
                model=GEMINI_DM_MODEL,
                contents=self._history,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.9,
                    response_mime_type="application/json",
                ),
            )

            raw = response.text
            _log(f"Response received ({len(raw)} chars)")
            self._history.append({"role": "model", "parts": [{"text": raw}]})

            parsed = json.loads(_strip_json_fences(raw))
            _log(f"Parsed OK. Keys: {list(parsed.keys())}")
            self._response_queue.put(parsed)

        except json.JSONDecodeError:
            _log(f"JSON parse error. Raw: {raw[:500]}")
            self._response_queue.put({
                "response_text": f"[DM responded but I couldn't parse the JSON. Raw: {raw[:300]}]"
            })
        except Exception as e:
            error_msg = str(e)
            _log(f"Error: {error_msg}")
            if "429" in error_msg:
                import time
                time.sleep(5)
                self._response_queue.put({
                    "response_text": "[Rate limited by Gemini API. Wait a moment and try again.]"
                })
            else:
                self._response_queue.put({
                    "response_text": f"[DM error: {error_msg[:200]}]"
                })
        finally:
            self._busy = False

    def poll_response(self):
        try:
            return self._response_queue.get_nowait()
        except Empty:
            return None

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
                self.world_state["rooms"][room_id] = room_def

        if "new_characters" in response and response["new_characters"]:
            for char_id, char_def in response["new_characters"].items():
                char_def.setdefault("id", char_id)
                self.world_state["characters"][char_id] = char_def

        if "new_objects" in response and response["new_objects"]:
            for obj_id, obj_def in response["new_objects"].items():
                obj_def.setdefault("id", obj_id)
                self.world_state["objects"][obj_id] = obj_def

        # If player has no current room but rooms now exist, set it
        if not self.world_state["player"]["current_room"] and self.world_state["rooms"]:
            first_room = next(iter(self.world_state["rooms"]))
            self.world_state["player"]["current_room"] = first_room
            self.world_state["player"]["starting_room"] = first_room
