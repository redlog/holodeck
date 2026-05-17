"""DungeonMaster: the single agent that runs both the setup conversation
and (eventually) play-mode turns.

The same agent persona carries through the whole session. It currently
operates in two phases:

  PHASE_INTERVIEW — the setup conversation. Gathers genre, tone, visual
                    style, player character, premise, starting situation.
                    Does NOT create world content; that comes after the
                    interview completes.

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


class DungeonMaster(BaseAgent):
    """The DM. Owns the interview now; will grow to own play turns next."""

    PHASE_INTERVIEW = "interview"
    PHASE_PLAY = "play"

    def __init__(self, world_state):
        super().__init__(model=GEMINI_DM_MODEL, temperature=0.9)
        self.world_state = world_state
        self._history = []
        self.phase = self.PHASE_PLAY if self._setup_already_complete() else self.PHASE_INTERVIEW

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
                _log("Interview complete — transitioning to play phase")
                self.phase = self.PHASE_PLAY

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
    # Play phase (stub — implemented next)
    # ------------------------------------------------------------------ #

    def _process_play_turn(self, user_text):
        # TODO: see design/text_adventure_design.md sections "DM agent" and
        # "Per-turn flow". Intent parse -> resolve -> state diff.
        self._result_queue.put({
            "response_text": "[Play mode is under construction. Your message was: "
                             + user_text[:200] + "]"
        })
