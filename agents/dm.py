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
    "dm_instructions": {"plot_seeds": [], "world_rules": [], "hard_constraints": []}
  },
  "interview_complete": false
}

RULES:
- DO NOT include any room, location, NPC, or object creation in your response. Not even placeholders. That happens after the interview.
- Only include world_updates fields when you have CONFIRMED info from the player (treat your own proposals as confirmed once the player doesn't push back).
- "interview_complete": stays false until you have ALL of: title, tone, visual_style, player name, player visual description, premise, AND starting location concept. As soon as all seven are captured, set it to true and emit a brief warm send-off.
- Player "description" field must be PURELY VISUAL — only what you'd see (hair, clothes, build, features). Anything else (personality, backstory, secrets) goes into dm_instructions.plot_seeds only if the player VOLUNTEERS it.
- plot_seeds is your scratchpad for player-volunteered specifics. Things like "player's brother was killed three years ago" go there. Things YOU invent in private don't — you'll record those in the DM bible after the interview.
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
