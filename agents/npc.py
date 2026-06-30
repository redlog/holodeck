"""NPC agent: gives each NPC their own voice, knowledge, and behavior.

The DM dispatches to an NPC agent when the player talks to a character.
The NPC responds with speech, physical tells, and internal state changes.
The DM then weaves the response into narration.

NPC agents are lightweight and stateless between calls — all context is
passed in on each dispatch. They use gemini-2.5-flash for speed and cost.
"""

import json
import sys

from agents.base import BaseAgent
from agents.prompts import NPC_SYSTEM
from config import GEMINI_NPC_MODEL


def _log(msg):
    print(f"[NPC] {msg}", file=sys.stderr, flush=True)


def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


class NPCAgent(BaseAgent):
    """Voices a single NPC for one exchange. Stateless — instantiate per call
    or reuse; all context comes from the dispatch arguments."""

    def __init__(self, game_dir=None):
        super().__init__(model=GEMINI_NPC_MODEL, temperature=0.85, game_dir=game_dir)

    def speak(self, npc_data, player_input, scene_context, world_state,
              recent_scene=""):
        """Synchronous call — returns the NPC response dict directly.

        Called from a DM worker thread, so blocking is fine.

        Args:
            npc_data: the full NPC dict from world_state["npcs"][npc_id]
            player_input: what the player said/did
            scene_context: short description of what's happening in the scene
            world_state: full world state (for location name, etc.)
            recent_scene: plain-text transcript of the last few moments of
                narration/dialog the player has seen, so the (otherwise
                stateless) NPC knows what was just said in its presence —
                including lines the DM narrated on its behalf.

        Returns:
            dict with keys: speech, tells, internal_state_change
            On failure, returns a fallback with empty tells.
        """
        npc_name = npc_data.get("name", "Unknown")
        _log(f"Speaking as {npc_name}")

        system_prompt = self._build_system_prompt(npc_data, scene_context,
                                                  world_state, recent_scene)

        user_msg = f"The player says: {player_input}"
        contents = [{"role": "user", "parts": [{"text": user_msg}]}]

        try:
            raw = self._call_text(system_prompt, contents, context=npc_name)
            # strict=False tolerates raw newlines/tabs the model leaves
            # unescaped inside string values (otherwise 'Invalid control
            # character' kills an otherwise-valid response).
            parsed = json.loads(_strip_json_fences(raw), strict=False)
            parsed.setdefault("speech", "...")
            parsed.setdefault("tells", [])
            parsed.setdefault("internal_state_change", {})
            parsed.setdefault("new_npcs", {})
            _log(f"{npc_name} says: {parsed['speech'][:80]}")
            return parsed
        except Exception as e:
            _log(f"Error voicing {npc_name}: {e}")
            return {
                "speech": "...",
                "tells": [f"{npc_name} seems lost in thought for a moment"],
                "internal_state_change": {},
            }

    def _build_system_prompt(self, npc_data, scene_context, world_state,
                             recent_scene=""):
        loc_id = npc_data.get("current_location_id", "")
        loc = world_state.get("locations", {}).get(loc_id, {})
        loc_name = loc.get("name", loc_id)

        knows = npc_data.get("knows", [])
        hides = npc_data.get("hides", [])
        lies_about = npc_data.get("lies_about", [])

        knows_str = "\n".join(f"- {k}" for k in knows) if knows else "(nothing special)"
        hides_str = "\n".join(f"- {h}" for h in hides) if hides else "(nothing)"
        lies_str = "\n".join(f"- {l}" for l in lies_about) if lies_about else "(nothing)"

        recent_dialog = npc_data.get("dialog_summary_with_player", "")
        if not recent_dialog:
            recent_dialog = "(first conversation)"

        if not recent_scene:
            recent_scene = "(you have just been approached; nothing notable has been said yet)"

        return NPC_SYSTEM.format(
            name=npc_data.get("name", "Unknown"),
            persona=npc_data.get("public_persona", "An unremarkable person."),
            voice=npc_data.get("voice", "Speaks normally, nothing distinctive."),
            knows=knows_str,
            hides=hides_str,
            lies_about=lies_str,
            location=loc_name,
            intent=npc_data.get("current_intent", "Going about their business."),
            mood=npc_data.get("mood_toward_player", "neutral"),
            scene_context=scene_context,
            recent_scene=recent_scene,
            recent_dialog=recent_dialog,
        )
