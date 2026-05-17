"""Play mode UI: the in-game screen where the DM narrates and the player
acts. See design/text_adventure_design.md for the layout spec.

Layout (960x600):
  +----------------------------------------------------+
  |                                                    |
  |        [Room background image — fills top]         |
  |                                                    |
  +---------+------------------------------------------+
  | Por-    | DM narrates the scene...                 |
  | trait   | > player input                           |
  | 140x140 | ┃ NPC: dialog                            |
  +---------+------------------------------------------+
  | > [input box]                                      |
  +----------------------------------------------------+

For v0 we render the background image, the player portrait (active
speaker slot), narration text, and the input box. The DM play-turn
logic is still a stub; this scaffold just proves the wiring works.
"""

import sys
import textwrap

import pygame

from config import INTERNAL_WIDTH, INTERNAL_HEIGHT
from rendering.ui import TextInput, get_font


def _log(msg):
    print(f"[PLAY] {msg}", file=sys.stderr, flush=True)


# Layout constants
ROOM_IMAGE_RECT = pygame.Rect(0, 0, INTERNAL_WIDTH, 360)
PORTRAIT_RECT = pygame.Rect(15, 372, 140, 140)
TEXT_PANEL_RECT = pygame.Rect(170, 372, INTERNAL_WIDTH - 170 - 15, 175)
INPUT_HEIGHT = 28
INPUT_MARGIN = 12
TEXT_LINE_HEIGHT = 22

# Colors
COLOR_BG = (12, 12, 16)
COLOR_PANEL = (22, 22, 28)
COLOR_PORTRAIT_BG = (40, 40, 50)
COLOR_PORTRAIT_BORDER = (90, 90, 110)
COLOR_DIVIDER = (60, 60, 72)
COLOR_DM = (220, 215, 200)
COLOR_USER = (220, 180, 80)
COLOR_SYSTEM = (130, 130, 145)
COLOR_PLACEHOLDER = (110, 110, 120)


class PlayMode:
    def __init__(self, surface, world_state, dm, game_slug,
                 portrait_agent=None, scenery_agent=None):
        self.surface = surface
        self.world_state = world_state
        self.dm = dm
        self._slug = game_slug
        self._portrait_agent = portrait_agent
        self._scenery_agent = scenery_agent

        self.font = get_font()
        self.font_small = get_font(14)
        self._char_w = self.font.size("M")[0]

        # Loaded pygame surfaces (cached so we don't reload PNGs every frame).
        self._room_surface = None
        self._room_loaded_path = None
        self._portrait_surface = None
        self._portrait_loaded_path = None
        self._speaker_id = "player"  # whose portrait is shown

        # Console state
        self.console_lines = []
        self._scroll_offset = 0
        self._waiting = False

        # Input
        input_y = INTERNAL_HEIGHT - INPUT_HEIGHT - INPUT_MARGIN
        self.text_input = TextInput(
            y=input_y,
            width=INTERNAL_WIDTH - INPUT_MARGIN * 2,
            x=INPUT_MARGIN,
        )

        self._text_panel_top = TEXT_PANEL_RECT.y + 4
        self._text_panel_bottom = TEXT_PANEL_RECT.bottom - 4
        self._max_visible_lines = (self._text_panel_bottom - self._text_panel_top) // TEXT_LINE_HEIGHT
        self._wrap_chars = (TEXT_PANEL_RECT.width - 16) // self._char_w - 1

        # Exit signaling
        self.quit_requested = False
        self.menu_requested = False

        # Opening narration: just the location summary for now. When the DM
        # play turn is implemented, it'll produce a proper opening beat.
        self._render_opening_beat()

    def _render_opening_beat(self):
        loc_id = self.world_state.get("current_location_id")
        loc = self.world_state.get("locations", {}).get(loc_id, {}) if loc_id else {}
        if loc.get("summary"):
            self._add_lines("dm", loc["summary"])
        else:
            self._add_lines("system", "(no starting location set)")

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

        # Pick up any pending imagery from the setup phase
        self._poll_imagery()

    def render(self):
        self.surface.fill(COLOR_BG)
        self._render_room()
        self._render_bottom_panel()
        self._render_portrait()
        self._render_console()
        self.text_input.render(self.surface)

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    def _render_room(self):
        loc_id = self.world_state.get("current_location_id")
        loc = self.world_state.get("locations", {}).get(loc_id, {}) if loc_id else {}
        path = loc.get("image_path")

        if path and path != self._room_loaded_path:
            try:
                img = pygame.image.load(path).convert()
                # Fit to the room rect by width, preserving aspect, then crop
                # the vertical center so we don't stretch the painting.
                sw, sh = img.get_size()
                target_w = ROOM_IMAGE_RECT.width
                scale = target_w / sw
                scaled_h = int(sh * scale)
                scaled = pygame.transform.smoothscale(img, (target_w, scaled_h))
                if scaled_h >= ROOM_IMAGE_RECT.height:
                    # Crop center band
                    y_off = (scaled_h - ROOM_IMAGE_RECT.height) // 2
                    self._room_surface = scaled.subsurface(
                        pygame.Rect(0, y_off, target_w, ROOM_IMAGE_RECT.height)
                    ).copy()
                else:
                    # Image is shorter than the slot (rare); letterbox vertically
                    canvas = pygame.Surface((target_w, ROOM_IMAGE_RECT.height))
                    canvas.fill(COLOR_BG)
                    canvas.blit(scaled, (0, (ROOM_IMAGE_RECT.height - scaled_h) // 2))
                    self._room_surface = canvas
                self._room_loaded_path = path
            except Exception as e:
                _log(f"failed to load room image {path}: {e}")
                self._room_surface = None
                self._room_loaded_path = None

        if self._room_surface:
            self.surface.blit(self._room_surface, ROOM_IMAGE_RECT)
        else:
            pygame.draw.rect(self.surface, COLOR_PANEL, ROOM_IMAGE_RECT)
            label = "Painting the scene..." if self._scenery_agent and self._scenery_agent.pending else "(no scene image)"
            surf = self.font.render(label, True, COLOR_PLACEHOLDER)
            self.surface.blit(surf, (ROOM_IMAGE_RECT.centerx - surf.get_width() // 2,
                                     ROOM_IMAGE_RECT.centery - surf.get_height() // 2))

        # Divider between image and bottom panel
        pygame.draw.line(self.surface, COLOR_DIVIDER,
                         (0, ROOM_IMAGE_RECT.bottom), (INTERNAL_WIDTH, ROOM_IMAGE_RECT.bottom), 1)

    def _render_bottom_panel(self):
        bottom = pygame.Rect(0, ROOM_IMAGE_RECT.bottom + 1,
                             INTERNAL_WIDTH, INTERNAL_HEIGHT - ROOM_IMAGE_RECT.bottom - 1)
        pygame.draw.rect(self.surface, COLOR_PANEL, bottom)

    def _render_portrait(self):
        pygame.draw.rect(self.surface, COLOR_PORTRAIT_BG, PORTRAIT_RECT)
        pygame.draw.rect(self.surface, COLOR_PORTRAIT_BORDER, PORTRAIT_RECT, 1)

        path = self._portrait_path_for(self._speaker_id)
        if path and path != self._portrait_loaded_path:
            try:
                img = pygame.image.load(path)
                img = pygame.transform.smoothscale(img, (PORTRAIT_RECT.width - 4, PORTRAIT_RECT.height - 4))
                self._portrait_surface = img.convert()
                self._portrait_loaded_path = path
            except Exception as e:
                _log(f"failed to load portrait {path}: {e}")
                self._portrait_surface = None
                self._portrait_loaded_path = None

        if self._portrait_surface:
            self.surface.blit(self._portrait_surface, (PORTRAIT_RECT.x + 2, PORTRAIT_RECT.y + 2))
        else:
            placeholder = ("Painting..." if self._portrait_agent and self._portrait_agent.pending
                           else "(portrait)")
            label = self.font_small.render(placeholder, True, COLOR_PLACEHOLDER)
            self.surface.blit(label,
                              (PORTRAIT_RECT.centerx - label.get_width() // 2,
                               PORTRAIT_RECT.centery - label.get_height() // 2))

        # Speaker name beneath the portrait
        name = self._speaker_name(self._speaker_id)
        if name:
            label = self.font_small.render(name, True, COLOR_DM)
            self.surface.blit(label,
                              (PORTRAIT_RECT.centerx - label.get_width() // 2,
                               PORTRAIT_RECT.bottom + 4))

    def _render_console(self):
        total = len(self.console_lines)
        end = total - self._scroll_offset
        start = max(0, end - self._max_visible_lines)
        visible = self.console_lines[start:end]

        y = self._text_panel_top
        x = TEXT_PANEL_RECT.x + 8
        for source, text in visible:
            color = self._color_for(source)
            surf = self.font.render(text, True, color)
            self.surface.blit(surf, (x, y))
            y += TEXT_LINE_HEIGHT

    def _color_for(self, source):
        return {
            "dm": COLOR_DM,
            "user": COLOR_USER,
            "system": COLOR_SYSTEM,
        }.get(source, COLOR_DM)

    def _portrait_path_for(self, speaker_id):
        if speaker_id == "player":
            return self.world_state.get("player", {}).get("portrait_path")
        npc = self.world_state.get("npcs", {}).get(speaker_id, {})
        return npc.get("portrait_path")

    def _speaker_name(self, speaker_id):
        if speaker_id == "player":
            return self.world_state.get("player", {}).get("name") or "You"
        npc = self.world_state.get("npcs", {}).get(speaker_id, {})
        return npc.get("name") or speaker_id

    # ------------------------------------------------------------------ #
    # Input + DM dispatch
    # ------------------------------------------------------------------ #

    def _handle_input(self, text):
        text = text.strip()
        if not text:
            return
        if self._waiting:
            self._add_lines("system", "(the DM is thinking — please wait)")
            return

        self._scroll_offset = 0
        self._add_lines("user", text)
        self._waiting = True
        self.dm.send_message(text)

    def _handle_dm_response(self, response):
        text = response.get("response_text") or "[no response]"
        self._add_lines("dm", text)
        self._autosave()
        self._scroll_offset = 0

    def _autosave(self):
        from world.bible import save_game
        try:
            save_game(self.world_state, self._slug)
        except Exception as e:
            _log(f"autosave failed: {e}")

    # ------------------------------------------------------------------ #
    # Imagery polling
    # ------------------------------------------------------------------ #

    def _poll_imagery(self):
        changed = False
        if self._portrait_agent:
            r = self._portrait_agent.poll_result()
            if r is not None:
                changed = self._apply_imagery_result(r, kind="portrait") or changed
        if self._scenery_agent:
            r = self._scenery_agent.poll_result()
            if r is not None:
                changed = self._apply_imagery_result(r, kind="room") or changed
        if changed:
            self._autosave()

    def _apply_imagery_result(self, result, kind):
        if not isinstance(result, tuple) or len(result) != 3:
            return False
        tag, target_id, payload = result
        if tag == "error":
            _log(f"image error: {kind} {target_id}: {payload}")
            return False

        if kind == "portrait" and tag == "portrait_complete":
            path = payload.get("portrait_path")
            if target_id == "player":
                self.world_state.setdefault("player", {})["portrait_path"] = path
            else:
                npc = self.world_state.get("npcs", {}).get(target_id)
                if npc:
                    npc["portrait_path"] = path
            return True

        if kind == "room" and tag == "room_complete":
            path = payload.get("image_path")
            loc = self.world_state.get("locations", {}).get(target_id)
            if loc:
                loc["image_path"] = path
                loc["image_dirty"] = False
            return True

        return False

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _add_lines(self, source, text):
        for line in self._wrap_text(text):
            self.console_lines.append((source, line))
        self._clamp_scroll()

    def _wrap_text(self, text):
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
