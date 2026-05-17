"""Setup mode UI: the screen where the player and DM converse to define the
world before play begins. See design/text_adventure_design.md.

Layout (960x600):
  +----------------------------------------------------+
  |  [portrait 152x152]    Game: ...                   |
  |                        Tone: ...                   |
  |                        Style: ...                  |
  |                        Player: ...                 |
  +----------------------------------------------------+
  |  DM: ...                                           |
  |  > player input                                    |
  |  DM: ...                                           |
  +----------------------------------------------------+
  |  > [input box]                                     |
  +----------------------------------------------------+

When the DM signals interview_complete, a banner appears prompting the
player to press Enter to begin. SetupMode exposes:
  - .done           — True once player has begun play
  - .quit_requested — True if player closed the window
"""

import sys
import textwrap

import pygame

from config import INTERNAL_WIDTH, INTERNAL_HEIGHT
from rendering.ui import TextInput, get_font


def _log(msg):
    print(f"[SETUP] {msg}", file=sys.stderr, flush=True)


# Colors
COLOR_BG = (18, 18, 24)
COLOR_PANEL = (28, 28, 36)
COLOR_DIVIDER = (60, 60, 72)
COLOR_PORTRAIT_BG = (40, 40, 50)
COLOR_PORTRAIT_BORDER = (90, 90, 110)
COLOR_STATUS_LABEL = (140, 140, 150)
COLOR_STATUS_VALUE = (220, 220, 220)
COLOR_DM = (220, 215, 200)
COLOR_USER = (220, 180, 80)
COLOR_SYSTEM = (130, 130, 145)
COLOR_BANNER = (140, 220, 140)

# Layout constants
TOP_PANEL_H = 184
PORTRAIT_BOX = pygame.Rect(20, 16, 152, 152)
STATUS_X = PORTRAIT_BOX.right + 20
STATUS_Y_START = PORTRAIT_BOX.y
INPUT_HEIGHT = 28
INPUT_MARGIN = 12
TEXT_PANEL_Y = TOP_PANEL_H + 10
TEXT_LINE_HEIGHT = 22
TEXT_PADDING_X = 20


class SetupMode:
    def __init__(self, surface, world_state, dm, game_slug):
        self.surface = surface
        self.world_state = world_state
        self.dm = dm
        self._slug = game_slug

        self.font = get_font()
        self.font_small = get_font(14)
        # Measure actual character width (consolas is monospace)
        self._char_w = self.font.size("M")[0]
        self._char_w_small = self.font_small.size("M")[0]

        # Console state — list of (source, text) lines
        self.console_lines = []

        # Input box
        input_y = INTERNAL_HEIGHT - INPUT_HEIGHT - INPUT_MARGIN
        self.text_input = TextInput(
            y=input_y,
            width=INTERNAL_WIDTH - INPUT_MARGIN * 2,
            x=INPUT_MARGIN,
        )

        # Text panel bounds
        self._text_panel_top = TEXT_PANEL_Y
        self._text_panel_bottom = input_y - 8
        self._max_visible_lines = (self._text_panel_bottom - self._text_panel_top) // TEXT_LINE_HEIGHT
        # Leave one char of slack so we don't render right up against the edge
        self._wrap_chars = (INTERNAL_WIDTH - TEXT_PADDING_X * 2) // self._char_w - 1

        # State flags
        self._waiting = False
        self._opening_sent = False
        self._scroll_offset = 0
        self.done = False
        self.quit_requested = False

        # Kick off whatever's next based on what's already been done
        if not self.dm.connected:
            self._add_lines("system",
                            "DM not connected. Set GEMINI_API_KEY in .env and restart.")
        elif self.dm.phase == self.dm.PHASE_INTERVIEW:
            self._waiting = True
            self._opening_sent = True
            self.console_lines.append(("system", "..."))
            self.dm.start_interview()
        elif self.dm.phase == self.dm.PHASE_CREATING:
            # Resuming a save where interview is done but creation never ran
            self._add_lines("system", "Preparing the world...")
            self._waiting = True
            self.dm.start_creation()
        else:
            # PHASE_PLAY — world already fully seeded
            self._add_lines("system",
                            "Setup is already complete. Press Enter to begin your adventure.")

    # ------------------------------------------------------------------ #
    # Main loop hooks
    # ------------------------------------------------------------------ #

    def update(self, dt, events):
        self.text_input.active = True
        self.text_input.busy = self._waiting
        self.text_input.update(dt)

        for event in events:
            if event.type == pygame.MOUSEWHEEL:
                self._scroll_offset = max(0, self._scroll_offset - event.y * 2)
                self._clamp_scroll()
                continue
            result = self.text_input.handle_event(event)
            if result is not None:
                self._handle_input(result)

        if self._waiting:
            response = self.dm.poll_result()
            if response:
                self._waiting = False
                self._handle_dm_response(response)

    def render(self):
        self.surface.fill(COLOR_BG)

        # Top status panel
        pygame.draw.rect(self.surface, COLOR_PANEL,
                         (0, 0, INTERNAL_WIDTH, TOP_PANEL_H))
        pygame.draw.line(self.surface, COLOR_DIVIDER,
                         (0, TOP_PANEL_H), (INTERNAL_WIDTH, TOP_PANEL_H), 1)

        self._render_portrait()
        self._render_status()

        # Text panel
        self._render_console()

        # Banner if interview complete
        if self.dm.phase == self.dm.PHASE_PLAY:
            self._render_banner()

        # Input
        self.text_input.render(self.surface)

    # ------------------------------------------------------------------ #
    # Rendering helpers
    # ------------------------------------------------------------------ #

    def _render_portrait(self):
        pygame.draw.rect(self.surface, COLOR_PORTRAIT_BG, PORTRAIT_BOX)
        pygame.draw.rect(self.surface, COLOR_PORTRAIT_BORDER, PORTRAIT_BOX, 1)

        portrait_path = self.world_state.get("player", {}).get("portrait_path")
        if portrait_path:
            try:
                img = pygame.image.load(portrait_path)
                img = pygame.transform.smoothscale(img, (PORTRAIT_BOX.width - 4, PORTRAIT_BOX.height - 4))
                self.surface.blit(img, (PORTRAIT_BOX.x + 2, PORTRAIT_BOX.y + 2))
                return
            except Exception:
                pass

        # Placeholder text
        label = self.font_small.render("(portrait)", True, (110, 110, 120))
        self.surface.blit(label,
                          (PORTRAIT_BOX.centerx - label.get_width() // 2,
                           PORTRAIT_BOX.centery - label.get_height() // 2))

    def _render_status(self):
        meta = self.world_state.get("meta", {})
        player = self.world_state.get("player", {})
        dm_inst = self.world_state.get("dm_instructions", {})

        rows = [
            ("Title", meta.get("title") or "—"),
            ("Tone", meta.get("tone") or "—"),
            ("Visual style", meta.get("visual_style") or "—"),
            ("Player", player.get("name") or "—"),
            ("Description", player.get("description") or "—"),
            ("Plot seeds", ", ".join(dm_inst.get("plot_seeds") or []) or "—"),
        ]

        y = STATUS_Y_START
        for label, value in rows:
            label_surf = self.font_small.render(f"{label}:", True, COLOR_STATUS_LABEL)
            self.surface.blit(label_surf, (STATUS_X, y))

            # Wrap value across one or two lines
            value_str = str(value)
            available_chars = max(20, (INTERNAL_WIDTH - STATUS_X - 130) // self._char_w_small - 1)
            wrapped = textwrap.wrap(value_str, width=available_chars) or [""]
            for i, line in enumerate(wrapped[:2]):
                # Truncate the second line with an ellipsis if there'd be more
                if i == 1 and len(wrapped) > 2:
                    line = line[:available_chars - 1] + "…"
                value_surf = self.font_small.render(line, True, COLOR_STATUS_VALUE)
                self.surface.blit(value_surf, (STATUS_X + 130, y + i * 16))
            y += 16 * min(len(wrapped), 2) + 6

    def _render_console(self):
        # Determine visible window
        total = len(self.console_lines)
        end = total - self._scroll_offset
        start = max(0, end - self._max_visible_lines)
        visible = self.console_lines[start:end]

        y = self._text_panel_top + 4
        for source, text in visible:
            color = self._color_for(source)
            surf = self.font.render(text, True, color)
            self.surface.blit(surf, (TEXT_PADDING_X, y))
            y += TEXT_LINE_HEIGHT

    def _render_banner(self):
        msg = "Setup complete — press Enter to begin your adventure"
        surf = self.font.render(msg, True, COLOR_BANNER)
        x = (INTERNAL_WIDTH - surf.get_width()) // 2
        y = INTERNAL_HEIGHT - INPUT_HEIGHT - INPUT_MARGIN - TEXT_LINE_HEIGHT - 4
        pygame.draw.rect(self.surface, (24, 40, 24),
                         (x - 12, y - 4, surf.get_width() + 24, surf.get_height() + 8))
        pygame.draw.rect(self.surface, COLOR_BANNER,
                         (x - 12, y - 4, surf.get_width() + 24, surf.get_height() + 8), 1)
        self.surface.blit(surf, (x, y))

    def _color_for(self, source):
        return {
            "dm": COLOR_DM,
            "user": COLOR_USER,
            "system": COLOR_SYSTEM,
        }.get(source, COLOR_DM)

    # ------------------------------------------------------------------ #
    # Input + DM dispatch
    # ------------------------------------------------------------------ #

    def _handle_input(self, text):
        text = text.strip()

        # If interview is complete, any Enter (empty or not) begins the game.
        if self.dm.phase == self.dm.PHASE_PLAY:
            self.done = True
            return

        if not text:
            return

        # Busy: queue a system note rather than firing a duplicate request.
        if self._waiting:
            self._add_lines("system", "(thinking — please wait)")
            return

        self._scroll_offset = 0
        self._add_lines("user", text)
        self._waiting = True
        self.console_lines.append(("system", "..."))
        self.dm.send_message(text)

    def _handle_dm_response(self, response):
        # Creation result has a different shape — route separately.
        if "creation_complete" in response:
            self._handle_creation_response(response)
            return

        # Remove the "..." placeholder if it's the last line
        if self.console_lines and self.console_lines[-1] == ("system", "..."):
            self.console_lines.pop()

        response_text = response.get("response_text") or "[No response text]"
        self._add_lines("dm", response_text)

        # Persist to disk after every response
        self._autosave()

        # Scroll to bottom on new response
        self._scroll_offset = 0

        # If the interview just completed, the DM has transitioned to
        # CREATING. Auto-kick the creation pass; nothing for the player to
        # do during this beat.
        if self.dm.phase == self.dm.PHASE_CREATING:
            self._add_lines("system", "Preparing the world...")
            self._waiting = True
            self.dm.start_creation()

    def _handle_creation_response(self, response):
        self._waiting = False
        if response.get("creation_complete"):
            loc_count = response.get("location_count", 0)
            npc_count = response.get("npc_count", 0)
            secret_count = response.get("secret_count", 0)
            thread_count = response.get("thread_count", 0)
            self._add_lines(
                "system",
                f"World ready — {loc_count} location, {npc_count} NPC(s), "
                f"{secret_count} secrets, {thread_count} plot threads."
            )
            self._autosave()
        else:
            err = response.get("error", "unknown error")
            self._add_lines("system", f"World creation failed: {err}")

    def _autosave(self):
        from world.bible import save_game
        try:
            save_game(self.world_state, self._slug)
        except Exception as e:
            _log(f"autosave failed: {e}")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _add_lines(self, source, text):
        for line in self._wrap_text(text):
            self.console_lines.append((source, line))
        self._clamp_scroll()

    def _wrap_text(self, text):
        # Hard-wrap on width, preserving paragraph breaks
        out = []
        for paragraph in text.split("\n"):
            if not paragraph.strip():
                out.append("")
                continue
            wrapped = textwrap.wrap(paragraph, width=self._wrap_chars,
                                    break_long_words=False, replace_whitespace=False)
            out.extend(wrapped or [""])
        return out

    def _clamp_scroll(self):
        max_scroll = max(0, len(self.console_lines) - self._max_visible_lines)
        self._scroll_offset = max(0, min(self._scroll_offset, max_scroll))
