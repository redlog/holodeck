"""Play mode UI: the in-game screen where the DM narrates and the player
acts. See design/text_adventure_design.md for the layout spec.

Layout (1280x800):
  +--------------------------------------------+--------+
  |                                            | Inven- |
  |        [Room background image]             | tory   |
  |                                            | drawer |
  +---------+----------------------------------+ (when  |
  | Por-    | DM narrates the scene...         | open)  |
  | trait   | > player input                   |        |
  | 140x140 | ┃ NPC: dialog                    |        |
  +---------+----------------------------------+--------+
  | > [input box]                                       |
  +-----------------------------------------------------+
"""

import sys
import textwrap

import pygame

from config import INTERNAL_WIDTH, INTERNAL_HEIGHT
from rendering.ui import TextInput, get_font
from rendering.save_load_ui import SaveLoadUI
from world.bible import save_game, load_game, get_save_slots


def _log(msg):
    print(f"[PLAY] {msg}", file=sys.stderr, flush=True)


# Layout constants
INVENTORY_DRAWER_W = 240
MAIN_AREA_W = INTERNAL_WIDTH  # full width when drawer closed

ROOM_IMAGE_RECT = pygame.Rect(0, 0, MAIN_AREA_W, 480)
PORTRAIT_RECT = pygame.Rect(15, 492, 140, 140)
TEXT_PANEL_RECT = pygame.Rect(170, 492, MAIN_AREA_W - 170 - 15, 255)
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

# Inventory drawer colors
COLOR_DRAWER_BG = (18, 18, 24)
COLOR_DRAWER_BORDER = (60, 60, 72)
COLOR_ITEM_NAME = (210, 205, 190)
COLOR_ITEM_HOVER = (35, 35, 45)
COLOR_ITEM_SELECTED = (40, 40, 55)
COLOR_PROVENANCE = (150, 145, 135)
COLOR_DRAWER_TITLE = (180, 175, 160)
COLOR_DRAWER_TAB = (45, 45, 58)
COLOR_DRAWER_TAB_HOVER = (60, 60, 75)
COLOR_DRAWER_TAB_TEXT = (160, 155, 140)

# Item sprite
ITEM_SPRITE_SIZE = 48
ITEM_ROW_HEIGHT = 58
ITEM_PADDING = 8


class PlayMode:
    def __init__(self, surface, world_state, dm, game_slug,
                 portrait_agent=None, scenery_agent=None, item_agent=None):
        self.surface = surface
        self.world_state = world_state
        self.dm = dm
        self._slug = game_slug
        self._portrait_agent = portrait_agent
        self._scenery_agent = scenery_agent
        self._item_agent = item_agent

        self.font = get_font()
        self.font_small = get_font(14)
        self.font_tiny = get_font(12)
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

        # Inventory drawer state
        self._drawer_open = False
        self._drawer_scroll = 0
        self._selected_item_idx = None  # index into inventory for detail view
        self._item_sprites = {}  # item_id -> pygame.Surface
        self._item_sprites_loaded = set()  # paths already loaded

        # Input
        input_y = INTERNAL_HEIGHT - INPUT_HEIGHT - INPUT_MARGIN
        self.text_input = TextInput(
            y=input_y,
            width=INTERNAL_WIDTH - INPUT_MARGIN * 2,
            x=INPUT_MARGIN,
        )

        self._recalc_layout()

        # Save/Load UI
        self._save_load_ui = SaveLoadUI(surface)
        self._load_pending = None  # set to slot name when a load is confirmed

        # Exit signaling
        self.quit_requested = False
        self.menu_requested = False

        # Load any existing item sprites from saved state
        self._load_existing_item_sprites()

        self._render_opening_beat()

    def _recalc_layout(self):
        """Recalculate text panel geometry based on drawer state."""
        main_w = INTERNAL_WIDTH - INVENTORY_DRAWER_W if self._drawer_open else INTERNAL_WIDTH
        self._main_w = main_w

        self._room_rect = pygame.Rect(0, 0, main_w, 480)
        self._portrait_rect = pygame.Rect(15, 492, 140, 140)
        self._text_panel_rect = pygame.Rect(170, 492, main_w - 170 - 15, 255)
        self._text_panel_top = self._text_panel_rect.y + 4
        self._text_panel_bottom = self._text_panel_rect.bottom - 4
        self._max_visible_lines = (self._text_panel_bottom - self._text_panel_top) // TEXT_LINE_HEIGHT
        self._wrap_chars = (self._text_panel_rect.width - 16) // self._char_w - 1

        input_y = INTERNAL_HEIGHT - INPUT_HEIGHT - INPUT_MARGIN
        self.text_input.rect = pygame.Rect(
            INPUT_MARGIN, input_y, main_w - INPUT_MARGIN * 2, INPUT_HEIGHT
        )

        # Drawer tab (always visible on right edge)
        tab_w, tab_h = 24, 80
        self._drawer_tab_rect = pygame.Rect(
            main_w - tab_w if not self._drawer_open else main_w,
            300 - tab_h // 2, tab_w, tab_h
        )
        # Drawer panel rect
        self._drawer_rect = pygame.Rect(main_w, 0, INVENTORY_DRAWER_W, INTERNAL_HEIGHT)

    def _render_opening_beat(self):
        self._waiting = True
        self.dm.narrate_opening()

    def _load_existing_item_sprites(self):
        """Load sprites for items already in inventory (e.g. from a save)."""
        inv = self.world_state.get("player", {}).get("inventory", [])
        for entry in inv:
            if isinstance(entry, dict):
                sprite_path = entry.get("sprite_path")
                item_id = entry.get("item_id")
                if sprite_path and item_id and item_id not in self._item_sprites:
                    try:
                        img = pygame.image.load(sprite_path).convert_alpha()
                        self._item_sprites[item_id] = pygame.transform.smoothscale(
                            img, (ITEM_SPRITE_SIZE, ITEM_SPRITE_SIZE)
                        )
                        self._item_sprites_loaded.add(sprite_path)
                    except Exception as e:
                        _log(f"Failed to load item sprite {sprite_path}: {e}")

    # ------------------------------------------------------------------ #
    # Main loop hooks
    # ------------------------------------------------------------------ #

    def update(self, dt, events):
        self.text_input.active = True
        self.text_input.busy = self._waiting
        self.text_input.update(dt)

        for event in events:
            # Save/Load UI intercepts all input while active
            if self._save_load_ui.active:
                result = self._save_load_ui.handle_event(event)
                if result and result != "consumed":
                    self._handle_save_load_result(result)
                continue

            # F5 = save, F9 = load
            if event.type == pygame.KEYDOWN and event.key == pygame.K_F5:
                slots = get_save_slots(self._slug)
                self._save_load_ui.open_save(slots)
                continue
            if event.type == pygame.KEYDOWN and event.key == pygame.K_F9:
                slots = get_save_slots(self._slug)
                self._save_load_ui.open_load(slots)
                continue

            if event.type == pygame.MOUSEWHEEL:
                mx, my = pygame.mouse.get_pos()
                from config import DISPLAY_SCALE
                mx, my = mx // DISPLAY_SCALE, my // DISPLAY_SCALE
                if self._drawer_open and mx >= self._drawer_rect.x:
                    self._drawer_scroll = max(0, self._drawer_scroll - event.y * 2)
                    self._clamp_drawer_scroll()
                else:
                    self._scroll_offset = max(0, self._scroll_offset - event.y * 2)
                    self._clamp_scroll()
                continue
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if self._handle_drawer_click(event.pos):
                    continue
            result = self.text_input.handle_event(event)
            if result is not None:
                self._handle_input(result)

        if self._waiting:
            response = self.dm.poll_result()
            if response:
                self._waiting = False
                self._handle_dm_response(response)

        self._poll_imagery()

    def render(self):
        self.surface.fill(COLOR_BG)
        self._render_room()
        self._render_bottom_panel()
        self._render_portrait()
        self._render_console()
        self.text_input.render(self.surface)
        self._render_drawer_tab()
        if self._drawer_open:
            self._render_drawer()
        self._save_load_ui.render()

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #

    def _render_room(self):
        room_rect = self._room_rect
        loc_id = self.world_state.get("current_location_id")
        loc = self.world_state.get("locations", {}).get(loc_id, {}) if loc_id else {}
        path = loc.get("image_path")

        # Reload if path changed or room rect width changed (drawer toggle)
        need_reload = (path and (path != self._room_loaded_path
                                  or (self._room_surface and
                                      self._room_surface.get_width() != room_rect.width)))
        if need_reload:
            try:
                img = pygame.image.load(path).convert()
                sw, sh = img.get_size()
                target_w = room_rect.width
                scale = target_w / sw
                scaled_h = int(sh * scale)
                scaled = pygame.transform.smoothscale(img, (target_w, scaled_h))
                if scaled_h >= room_rect.height:
                    y_off = (scaled_h - room_rect.height) // 2
                    self._room_surface = scaled.subsurface(
                        pygame.Rect(0, y_off, target_w, room_rect.height)
                    ).copy()
                else:
                    canvas = pygame.Surface((target_w, room_rect.height))
                    canvas.fill(COLOR_BG)
                    canvas.blit(scaled, (0, (room_rect.height - scaled_h) // 2))
                    self._room_surface = canvas
                self._room_loaded_path = path
            except Exception as e:
                _log(f"failed to load room image {path}: {e}")
                self._room_surface = None
                self._room_loaded_path = None

        if self._room_surface:
            self.surface.blit(self._room_surface, room_rect)
        else:
            pygame.draw.rect(self.surface, COLOR_PANEL, room_rect)
            label = "Painting the scene..." if self._scenery_agent and self._scenery_agent.pending else "(no scene image)"
            surf = self.font.render(label, True, COLOR_PLACEHOLDER)
            self.surface.blit(surf, (room_rect.centerx - surf.get_width() // 2,
                                     room_rect.centery - surf.get_height() // 2))

        pygame.draw.line(self.surface, COLOR_DIVIDER,
                         (0, room_rect.bottom), (self._main_w, room_rect.bottom), 1)

    def _render_bottom_panel(self):
        bottom = pygame.Rect(0, self._room_rect.bottom + 1,
                             self._main_w, INTERNAL_HEIGHT - self._room_rect.bottom - 1)
        pygame.draw.rect(self.surface, COLOR_PANEL, bottom)

    def _render_portrait(self):
        pr = self._portrait_rect
        pygame.draw.rect(self.surface, COLOR_PORTRAIT_BG, pr)
        pygame.draw.rect(self.surface, COLOR_PORTRAIT_BORDER, pr, 1)

        path = self._portrait_path_for(self._speaker_id)
        if path and path != self._portrait_loaded_path:
            try:
                img = pygame.image.load(path)
                img = pygame.transform.smoothscale(img, (pr.width - 4, pr.height - 4))
                self._portrait_surface = img.convert()
                self._portrait_loaded_path = path
            except Exception as e:
                _log(f"failed to load portrait {path}: {e}")
                self._portrait_surface = None
                self._portrait_loaded_path = None

        if self._portrait_surface:
            self.surface.blit(self._portrait_surface, (pr.x + 2, pr.y + 2))
        else:
            placeholder = ("Painting..." if self._portrait_agent and self._portrait_agent.pending
                           else "(portrait)")
            label = self.font_small.render(placeholder, True, COLOR_PLACEHOLDER)
            self.surface.blit(label,
                              (pr.centerx - label.get_width() // 2,
                               pr.centery - label.get_height() // 2))

        name = self._speaker_name(self._speaker_id)
        if name:
            label = self.font_small.render(name, True, COLOR_DM)
            self.surface.blit(label,
                              (pr.centerx - label.get_width() // 2,
                               pr.bottom + 4))

    def _render_console(self):
        total = len(self.console_lines)
        end = total - self._scroll_offset
        start = max(0, end - self._max_visible_lines)
        visible = self.console_lines[start:end]

        y = self._text_panel_top
        x = self._text_panel_rect.x + 8
        self.surface.set_clip(self._text_panel_rect)
        for source, text in visible:
            color = self._color_for(source)
            surf = self.font.render(text, True, color)
            self.surface.blit(surf, (x, y))
            y += TEXT_LINE_HEIGHT
        self.surface.set_clip(None)

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
    # Inventory drawer
    # ------------------------------------------------------------------ #

    def _toggle_drawer(self):
        self._drawer_open = not self._drawer_open
        self._selected_item_idx = None
        self._room_loaded_path = None  # force room image re-render at new width
        self._recalc_layout()
        self._rewrap_console()

    def _rewrap_console(self):
        """Re-wrap all console text after layout change (drawer toggle)."""
        old_lines = self.console_lines
        self.console_lines = []
        # Rebuild from source blocks — we stored (source, text) per wrapped line,
        # so we need to recombine and re-wrap. Approximate by re-wrapping each line.
        for source, text in old_lines:
            self.console_lines.append((source, text))
        self._clamp_scroll()

    def _render_drawer_tab(self):
        """Render the tab handle on the right edge that toggles the drawer."""
        inv = self.world_state.get("player", {}).get("inventory", [])
        count = len(inv)

        tab_w, tab_h = 24, 80
        if self._drawer_open:
            tab_x = self._main_w
        else:
            tab_x = INTERNAL_WIDTH - tab_w
        tab_rect = pygame.Rect(tab_x, 300 - tab_h // 2, tab_w, tab_h)
        self._drawer_tab_rect = tab_rect

        mx, my = pygame.mouse.get_pos()
        from config import DISPLAY_SCALE
        mx, my = mx // DISPLAY_SCALE, my // DISPLAY_SCALE
        hovered = tab_rect.collidepoint(mx, my)

        color = COLOR_DRAWER_TAB_HOVER if hovered else COLOR_DRAWER_TAB
        pygame.draw.rect(self.surface, color, tab_rect)
        pygame.draw.rect(self.surface, COLOR_DRAWER_BORDER, tab_rect, 1)

        # Draw bag icon (simple text) and count
        icon = self.font_small.render("Bag" if not self._drawer_open else "X", True, COLOR_DRAWER_TAB_TEXT)
        self.surface.blit(icon, (tab_rect.centerx - icon.get_width() // 2,
                                  tab_rect.centery - 14))
        if count > 0:
            count_surf = self.font_tiny.render(str(count), True, COLOR_USER)
            self.surface.blit(count_surf, (tab_rect.centerx - count_surf.get_width() // 2,
                                            tab_rect.centery + 6))

    def _render_drawer(self):
        """Render the inventory drawer panel."""
        dr = self._drawer_rect
        pygame.draw.rect(self.surface, COLOR_DRAWER_BG, dr)
        pygame.draw.line(self.surface, COLOR_DRAWER_BORDER,
                         (dr.x, dr.y), (dr.x, dr.bottom), 1)

        # Title
        title_surf = self.font.render("Inventory", True, COLOR_DRAWER_TITLE)
        self.surface.blit(title_surf, (dr.x + 12, 12))

        inv = self.world_state.get("player", {}).get("inventory", [])
        if not inv:
            empty = self.font_small.render("(empty)", True, COLOR_PLACEHOLDER)
            self.surface.blit(empty, (dr.x + 12, 44))
            return

        # Item list area
        list_top = 40
        list_bottom = dr.bottom - 12
        list_height = list_bottom - list_top
        max_visible = list_height // ITEM_ROW_HEIGHT

        # If an item is selected, split: top half = list, bottom half = detail
        if self._selected_item_idx is not None and 0 <= self._selected_item_idx < len(inv):
            detail_h = 200
            list_bottom = dr.bottom - detail_h
            list_height = list_bottom - list_top
            max_visible = max(1, list_height // ITEM_ROW_HEIGHT)

        self.surface.set_clip(pygame.Rect(dr.x, list_top, dr.width, list_height))

        start = self._drawer_scroll
        end = min(len(inv), start + max_visible)
        y = list_top
        mx, my = pygame.mouse.get_pos()
        from config import DISPLAY_SCALE
        mx, my = mx // DISPLAY_SCALE, my // DISPLAY_SCALE

        for i in range(start, end):
            entry = inv[i]
            item_name = entry.get("item", str(entry)) if isinstance(entry, dict) else str(entry)
            item_id = entry.get("item_id") if isinstance(entry, dict) else None

            row_rect = pygame.Rect(dr.x + 4, y, dr.width - 8, ITEM_ROW_HEIGHT)
            hovered = row_rect.collidepoint(mx, my)
            selected = (i == self._selected_item_idx)

            if selected:
                pygame.draw.rect(self.surface, COLOR_ITEM_SELECTED, row_rect, border_radius=4)
            elif hovered:
                pygame.draw.rect(self.surface, COLOR_ITEM_HOVER, row_rect, border_radius=4)

            # Sprite
            sprite_x = row_rect.x + ITEM_PADDING
            sprite_y = row_rect.y + (ITEM_ROW_HEIGHT - ITEM_SPRITE_SIZE) // 2
            sprite = self._item_sprites.get(item_id) if item_id else None
            if sprite:
                self.surface.blit(sprite, (sprite_x, sprite_y))
            else:
                sprite_rect = pygame.Rect(sprite_x, sprite_y, ITEM_SPRITE_SIZE, ITEM_SPRITE_SIZE)
                pygame.draw.rect(self.surface, COLOR_PORTRAIT_BG, sprite_rect, border_radius=4)
                if self._item_agent and item_id and self._item_agent.is_pending(item_id):
                    lbl = self.font_tiny.render("...", True, COLOR_PLACEHOLDER)
                else:
                    lbl = self.font_tiny.render("?", True, COLOR_PLACEHOLDER)
                self.surface.blit(lbl, (sprite_rect.centerx - lbl.get_width() // 2,
                                         sprite_rect.centery - lbl.get_height() // 2))

            # Item name (truncated to fit)
            name_x = sprite_x + ITEM_SPRITE_SIZE + 8
            max_name_w = row_rect.right - name_x - 4
            name_surf = self.font_small.render(item_name, True, COLOR_ITEM_NAME)
            if name_surf.get_width() > max_name_w:
                # Truncate with ellipsis
                while name_surf.get_width() > max_name_w and len(item_name) > 3:
                    item_name = item_name[:-1]
                    name_surf = self.font_small.render(item_name + "…", True, COLOR_ITEM_NAME)
            self.surface.blit(name_surf, (name_x, row_rect.y + ITEM_ROW_HEIGHT // 2 - name_surf.get_height() // 2))

            y += ITEM_ROW_HEIGHT

        self.surface.set_clip(None)

        # Detail panel for selected item
        if self._selected_item_idx is not None and 0 <= self._selected_item_idx < len(inv):
            self._render_item_detail(inv[self._selected_item_idx], list_bottom)

    def _render_item_detail(self, entry, top_y):
        """Render detail panel for a selected inventory item."""
        dr = self._drawer_rect
        detail_rect = pygame.Rect(dr.x + 1, top_y, dr.width - 1, dr.bottom - top_y)
        pygame.draw.rect(self.surface, (26, 26, 34), detail_rect)
        pygame.draw.line(self.surface, COLOR_DIVIDER,
                         (dr.x + 8, top_y), (dr.right - 8, top_y), 1)

        if not isinstance(entry, dict):
            return

        self.surface.set_clip(detail_rect.inflate(-4, -4))

        y = top_y + 8
        pad_x = dr.x + 12
        avail_w = dr.width - 24

        item_name = entry.get("item", "???")
        name_surf = self.font.render(item_name, True, COLOR_ITEM_NAME)
        self.surface.blit(name_surf, (pad_x, y))
        y += name_surf.get_height() + 6

        prov = entry.get("provenance", "")
        if prov:
            wrap_w = avail_w // self.font_tiny.size("M")[0] - 1
            for line in textwrap.wrap(prov, width=max(20, wrap_w)):
                line_surf = self.font_tiny.render(line, True, COLOR_PROVENANCE)
                self.surface.blit(line_surf, (pad_x, y))
                y += 16

        loc_name = entry.get("found_location_name", "")
        if loc_name:
            y += 4
            loc_surf = self.font_tiny.render(f"Found: {loc_name}", True, COLOR_SYSTEM)
            self.surface.blit(loc_surf, (pad_x, y))
            y += 16

        turn = entry.get("turn_acquired")
        if turn is not None:
            turn_surf = self.font_tiny.render(f"Turn {turn}", True, COLOR_SYSTEM)
            self.surface.blit(turn_surf, (pad_x, y))

        self.surface.set_clip(None)

    def _handle_drawer_click(self, pos):
        """Handle clicks on drawer tab and items. Returns True if consumed."""
        mx, my = pos
        if self._drawer_tab_rect.collidepoint(mx, my):
            self._toggle_drawer()
            return True

        if not self._drawer_open:
            return False

        dr = self._drawer_rect
        if not dr.collidepoint(mx, my):
            return False

        # Figure out which item row was clicked
        inv = self.world_state.get("player", {}).get("inventory", [])
        if not inv:
            return True

        list_top = 40
        rel_y = my - list_top
        if rel_y < 0:
            return True
        idx = self._drawer_scroll + rel_y // ITEM_ROW_HEIGHT
        if 0 <= idx < len(inv):
            if self._selected_item_idx == idx:
                self._selected_item_idx = None  # toggle off
            else:
                self._selected_item_idx = idx
        return True

    def _clamp_drawer_scroll(self):
        inv = self.world_state.get("player", {}).get("inventory", [])
        max_scroll = max(0, len(inv) - 5)
        self._drawer_scroll = max(0, min(self._drawer_scroll, max_scroll))

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
        # Narration text — new format uses "narration", old uses "response_text"
        text = response.get("narration") or response.get("response_text") or "[no response]"

        # Speaker swap for portrait
        speaker = response.get("speaker", "dm")
        if speaker and speaker != "dm":
            npc = self.world_state.get("npcs", {}).get(speaker)
            if npc:
                self._speaker_id = speaker
                source_tag = "dm"  # still styled as DM narration
            else:
                source_tag = "dm"
        else:
            self._speaker_id = "player"
            source_tag = "dm"

        self._add_lines(source_tag, text)

        # Trigger imagery for any dirty locations or new locations
        self._check_imagery_triggers(response)

        self._autosave()
        self._scroll_offset = 0

    def _autosave(self):
        try:
            save_game(self.world_state, self._slug)
        except Exception as e:
            _log(f"autosave failed: {e}")

    def _handle_save_load_result(self, result):
        if result == "cancelled":
            return
        action, slot = result
        if action == "save":
            try:
                save_game(
                    self.world_state, self._slug, slot,
                    play_history=self.dm.get_play_history(),
                    console_lines=self.console_lines,
                )
                self._add_lines("system", f"Game saved to slot: {slot}")
            except Exception as e:
                _log(f"save failed: {e}")
                self._add_lines("system", f"Save failed: {e}")
        elif action == "load":
            try:
                loaded = load_game(self._slug, slot)
                if loaded is None:
                    self._add_lines("system", "Save not found.")
                    return
                world_state, session = loaded
                if world_state is None:
                    self._add_lines("system", "Save not found.")
                    return
                self._apply_loaded_state(world_state, session)
                self._add_lines("system", f"Loaded save: {slot}")
            except Exception as e:
                _log(f"load failed: {e}")
                self._add_lines("system", f"Load failed: {e}")

    def _apply_loaded_state(self, world_state, session):
        """Replace all game state with data from a loaded save."""
        self.world_state.clear()
        self.world_state.update(world_state)

        # Restore DM conversation history
        if session and session.get("play_history"):
            self.dm.set_play_history(session["play_history"])
        else:
            self.dm.set_play_history([])

        # Restore console scrollback
        if session and session.get("console_lines"):
            self.console_lines = [
                (entry[0], entry[1])
                for entry in session["console_lines"]
                if isinstance(entry, (list, tuple)) and len(entry) == 2
            ]
        else:
            self.console_lines = []

        # Reset visual caches so images reload from new state
        self._room_surface = None
        self._room_loaded_path = None
        self._portrait_surface = None
        self._portrait_loaded_path = None
        self._item_sprites.clear()
        self._item_sprites_loaded.clear()

        # Reset scroll and drawer
        self._scroll_offset = 0
        self._drawer_scroll = 0
        self._selected_item_idx = None
        self._drawer_open = False
        self._recalc_layout()
        self._waiting = False

        # Reload item sprites from restored inventory
        self._load_existing_item_sprites()

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
        if self._item_agent:
            r = self._item_agent.poll_result()
            if r is not None:
                changed = self._apply_item_sprite_result(r) or changed
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
    # Imagery triggers from play-turn state changes
    # ------------------------------------------------------------------ #

    def _apply_item_sprite_result(self, result):
        if not isinstance(result, tuple) or len(result) != 3:
            return False
        tag, item_id, payload = result
        if tag == "error":
            _log(f"item sprite error: {item_id}: {payload}")
            return False
        if tag == "item_complete":
            sprite_path = payload.get("sprite_path")
            if not sprite_path:
                return False
            # Load sprite into cache
            try:
                img = pygame.image.load(sprite_path).convert_alpha()
                self._item_sprites[item_id] = pygame.transform.smoothscale(
                    img, (ITEM_SPRITE_SIZE, ITEM_SPRITE_SIZE)
                )
            except Exception as e:
                _log(f"failed to load item sprite {sprite_path}: {e}")
                return False
            # Update world_state entry with the path
            inv = self.world_state.get("player", {}).get("inventory", [])
            for entry in inv:
                if isinstance(entry, dict) and entry.get("item_id") == item_id:
                    entry["sprite_path"] = sprite_path
                    break
            return True
        return False

    def _check_imagery_triggers(self, response):
        changes = response.get("state_changes")
        if not changes:
            return

        # New location with image_dirty — render its background
        new_loc = changes.get("create_location")
        if isinstance(new_loc, dict) and new_loc.get("id"):
            self._trigger_room_render(new_loc["id"])

        # Existing locations marked dirty
        for lid in changes.get("image_dirty") or []:
            self._trigger_room_render(lid)

        # Location change — if moved to a location whose image hasn't loaded
        new_loc_id = changes.get("current_location_id")
        if new_loc_id:
            loc = self.world_state.get("locations", {}).get(new_loc_id, {})
            if loc.get("image_dirty") and not loc.get("image_path"):
                self._trigger_room_render(new_loc_id)

        # Item sprites for newly acquired items — look up from actual inventory
        # since _apply_play_changes already ran and assigned item_id there.
        for raw_entry in changes.get("inventory_add") or []:
            item_name = (raw_entry.get("item", "") if isinstance(raw_entry, dict)
                         else str(raw_entry)).lower()
            inv = self.world_state.get("player", {}).get("inventory", [])
            for entry in reversed(inv):
                if (isinstance(entry, dict)
                        and entry.get("item", "").lower() == item_name
                        and entry.get("item_id")):
                    self._trigger_item_sprite(entry)
                    break

    def _trigger_room_render(self, loc_id):
        if not self._scenery_agent:
            return
        loc = self.world_state.get("locations", {}).get(loc_id)
        if not loc or not loc.get("image_prompt"):
            return
        meta = self.world_state.get("meta", {})
        style = meta.get("visual_style", "")
        ctx = self._build_scenery_context(loc_id, loc)
        self._scenery_agent.generate_room(loc_id, loc, style, game_context=ctx)
        _log(f"Triggered room render for: {loc_id}")

    def _trigger_item_sprite(self, item_entry):
        if not self._item_agent:
            return
        item_id = item_entry.get("item_id")
        if not item_id or item_id in self._item_sprites:
            return
        visual_style = self.world_state.get("meta", {}).get("visual_style", "")
        self._item_agent.generate_sprite(item_id, item_entry, visual_style)
        _log(f"Triggered item sprite for: {item_id}")

    def _build_scenery_context(self, loc_id, loc):
        ws = self.world_state
        npcs = ws.get("npcs", {})
        present = [
            npcs[nid] for nid in loc.get("present_npc_ids", [])
            if nid in npcs
        ]

        # Gather visual clues from secrets relevant to this location
        visual_clues = []
        bible = ws.get("dm_bible", {})
        for secret in bible.get("secrets", []):
            if not secret.get("revealed", False):
                fact = secret.get("fact", "").lower()
                loc_name = loc.get("name", "").lower()
                if loc_name and loc_name in fact:
                    visual_clues.append(secret["fact"])
        # Planned beats that reference this location
        for beat in bible.get("planned_beats", []):
            loc_name = loc.get("name", "").lower()
            if loc_name and loc_name in beat.lower():
                visual_clues.append(beat)

        return {
            "tone": ws.get("meta", {}).get("tone", ""),
            "present_npcs": present,
            "visual_clues": visual_clues,
            "discovered_features": loc.get("discovered_features", []),
            "events_log": loc.get("events_log_summary", ""),
        }

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
