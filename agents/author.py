"""Setup-phase Author/DM agent.

Conducts the initial conversation that establishes the world: genre, tone,
player character, opening situation, and the DM's hidden prep notes. Does
NOT yet handle play-mode dispatch — that's the next thing to build per
design/text_adventure_design.md. The freeform/creation phases of the old
spatial-game pipeline have been removed; they'll be replaced by the new DM.

The interview system prompt below is the surviving heart of months of
iteration; preserve its constraint structure when expanding.
"""

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


INTERVIEW_SYSTEM_PROMPT = """You are the Author / DM for a graphical text adventure called The Holodeck. You are in INTERVIEW MODE.

YOUR ONLY JOB right now is to ask the player questions to flesh out their game concept. You are FORBIDDEN from creating locations, characters, or world content yet. That comes later, after the interview, in a separate phase.

You must ask questions ONE OR TWO AT A TIME (never more than two). Be conversational, warm, curious, and build on their answers.

Across the conversation you need to learn:
1. Genre and setting (fantasy, sci-fi, noir, horror, comedy, historical, etc.)
2. Tone and atmosphere (bright/dark, lighthearted/grim, cozy/tense, surreal/grounded)
3. Visual style of the game's art (painterly, gritty, dreamy, etc.) — used for both backgrounds and portraits
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

ABSOLUTE RULES (violations make the setup fail):
- DO NOT include any room, location, or character creation in your response. Not even placeholders.
- Only include world_updates fields when you have CONFIRMED info from the player.
- "interview_complete" stays false until you have gathered ALL of: title, tone, visual_style, player name, player visual description, premise, starting location concept, AND at least 2-3 substantial backstory or character details. When ALL of those are present, set "interview_complete": true.
- Player "description" field must be PURELY VISUAL — only what you'd see (hair, clothes, build, features). Personality and backstory go in dm_instructions.plot_seeds.
"""


class AuthorAgent(BaseAgent):
    """Setup-phase conversational agent. Interview only for now.

    When the interview completes, the agent emits `interview_complete: true`
    in its final response. The new DM (still to be built) will pick up the
    world state from there.
    """

    PHASE_INTERVIEW = "interview"
    PHASE_DONE = "done"

    def __init__(self, world_state):
        super().__init__(model=GEMINI_AUTHOR_MODEL, temperature=0.9)
        self.world_state = world_state
        self._history = []
        self.phase = self.PHASE_DONE if self._setup_already_complete() else self.PHASE_INTERVIEW

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

    def start_interview(self):
        """Proactively post an opening greeting + first interview question."""
        if self.phase != self.PHASE_INTERVIEW:
            return
        if not self.connected:
            self._result_queue.put({
                "response_text": "Author Agent not connected. Set GEMINI_API_KEY in .env and restart.",
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
        if not self.connected:
            self._result_queue.put({
                "response_text": "Author Agent not connected. Set GEMINI_API_KEY in .env and restart.",
            })
            return
        if self.phase != self.PHASE_INTERVIEW:
            self._result_queue.put({
                "response_text": "[Setup is complete; play mode is not yet implemented.]",
            })
            return
        _log(f"Sending: {user_text[:80]}")
        self._run_threaded(self._process_message, user_text)

    def _process_message(self, user_text):
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
                _log("Interview complete. Setup done; waiting on new DM.")
                self.phase = self.PHASE_DONE

            self._result_queue.put(parsed)

        except json.JSONDecodeError:
            _log("JSON parse error")
            self._result_queue.put({
                "response_text": "[Author could not parse response. Please try rephrasing.]"
            })
        except Exception as e:
            _log(f"Error: {e}")
            self._result_queue.put({
                "response_text": f"[Author error: {str(e)[:200]}]"
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
