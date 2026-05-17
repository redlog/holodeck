import pygame
import textwrap
from config import INTERNAL_WIDTH, INTERNAL_HEIGHT
from rendering.holodeck_overlay import apply_holodeck_overlay
from rendering.ui import TextInput, get_font


class HolodeckMode:
    def __init__(self, surface, world_state, dm=None, image_gen=None):
        self.surface = surface
        self.world_state = world_state
        self.dm = dm
        self.image_gen = image_gen
        self.console_lines = [
            ("system", "=== HOLODECK CONSOLE ==="),
            ("system", "Type commands to build your world."),
            ("system", "Press ~ to return to play mode."),
        ]
        self._border = 32
        self.text_input = TextInput(y=INTERNAL_HEIGHT - self._border - 28, width=INTERNAL_WIDTH - self._border * 2, x=self._border)
        self.font = get_font()
        self._line_height = 24
        self.console_y = self._border + 8
        self._console_bottom = INTERNAL_HEIGHT - self._border - 36
        self.max_visible_lines = (self._console_bottom - self.console_y) // self._line_height
        self.wants_resume = False
        self._waiting = False
        self._scroll_offset = 0

    @property
    def busy(self):
        if self._waiting:
            return True
        if self.image_gen and self.image_gen._pending:
            return True
        return False

    def update(self, dt, events):
        self.text_input.active = not self.busy
        self.text_input.update(dt)

        for event in events:
            if event.type == pygame.MOUSEWHEEL:
                self._scroll_offset -= event.y * 2
                max_scroll = max(0, len(self.console_lines) - self.max_visible_lines)
                self._scroll_offset = max(0, min(self._scroll_offset, max_scroll))
            elif not self.busy:
                result = self.text_input.handle_event(event)
                if result is not None:
                    self._handle_input(result)

        if self.dm and self._waiting:
            response = self.dm.poll_response()
            if response:
                self._waiting = False
                self._handle_dm_response(response)

    def _append_lines(self, source, text):
        for line in self._wrap_text(text):
            self.console_lines.append((source, line))

    def _handle_input(self, text):
        if not text.strip():
            return
        if text.strip().lower() == "resume":
            self.wants_resume = True
            return
        self._scroll_offset = 0
        self._append_lines("user", text)

        if self.dm and self.dm.connected:
            self._waiting = True
            self.console_lines.append(("system", "..."))
            self.dm.send_holodeck_message(text)
        else:
            self.console_lines.append(("dm", "[DM not connected] Set GEMINI_API_KEY in .env"))

    def _handle_dm_response(self, response):
        self._scroll_offset = 0
        # Remove the "..." line
        if self.console_lines and self.console_lines[-1] == ("system", "..."):
            self.console_lines.pop()

        response_text = response.get("response_text", "[No response]")
        self._append_lines("dm", response_text)

        self.dm.apply_response(response)

        if response.get("new_rooms"):
            names = [r.get("name", rid) for rid, r in response["new_rooms"].items()]
            self._append_lines("system", f"Created rooms: {', '.join(names)}")
        if response.get("new_characters"):
            names = [c.get("name", cid) for cid, c in response["new_characters"].items()]
            self._append_lines("system", f"Created characters: {', '.join(names)}")

        self._generate_character_images(response)

    def _generate_character_images(self, response):
        if not self.image_gen or not self.image_gen.connected:
            return
        visual_style = self.world_state.get("meta", {}).get("visual_style", "")

        # Generate sprites and portraits for new characters
        if response.get("new_characters"):
            for char_id, char_def in response["new_characters"].items():
                self.image_gen.request_portrait(char_id, char_def, visual_style)
                self.image_gen.request_sprite(char_id, char_def, visual_style)
                self._append_lines("system", f"Generating images for {char_def.get('name', char_id)}...")

        # Generate player sprite if player description was just set
        updates = response.get("world_updates", {})
        if updates and updates.get("player"):
            player = self.world_state["player"]
            if player.get("description") and not player.get("sprite_sheet_path"):
                player_def = {"name": player.get("name", "Player"), "description": player["description"]}
                self.image_gen.request_sprite("player", player_def, visual_style)
                self.image_gen.request_portrait("player", player_def, visual_style)
                self._append_lines("system", "Generating player sprite and portrait...")

    def _wrap_text(self, text):
        max_chars = (INTERNAL_WIDTH - self._border * 2 - 24) // self.font.size("A")[0]
        lines = []
        for paragraph in text.split("\n"):
            wrapped = textwrap.wrap(paragraph, width=max_chars)
            lines.extend(wrapped if wrapped else [""])
        return lines

    def render(self):
        b = self._border
        w, h = INTERNAL_WIDTH, INTERNAL_HEIGHT

        # Black background for console area
        inner = pygame.Rect(b, b, w - b * 2, h - b * 2)
        self.surface.fill((10, 10, 15), inner)

        # Grid lines in the border strips only
        from config import HOLODECK_GRID_COLOR, HOLODECK_GRID_SPACING
        for x in range(0, w, HOLODECK_GRID_SPACING):
            pygame.draw.line(self.surface, HOLODECK_GRID_COLOR, (x, 0), (x, b), 1)
            pygame.draw.line(self.surface, HOLODECK_GRID_COLOR, (x, h - b), (x, h), 1)
        for y in range(0, h, HOLODECK_GRID_SPACING):
            pygame.draw.line(self.surface, HOLODECK_GRID_COLOR, (0, y), (b, y), 1)
            pygame.draw.line(self.surface, HOLODECK_GRID_COLOR, (w - b, y), (w, y), 1)

        # Border edge
        pygame.draw.rect(self.surface, HOLODECK_GRID_COLOR, inner, 1)

        self._render_console()

    def _render_console(self):
        b = self._border
        total = len(self.console_lines)
        if self._scroll_offset == 0:
            start = max(0, total - self.max_visible_lines)
        else:
            start = max(0, total - self.max_visible_lines - self._scroll_offset)
        visible = self.console_lines[start:start + self.max_visible_lines]
        y = self.console_y
        for source, line in visible:
            color = self._line_color(source)
            text_surf = self.font.render(line, False, color)
            self.surface.blit(text_surf, (b + 12, y))
            y += self._line_height
            if y > self._console_bottom:
                break

        self.text_input.render(self.surface)

    def _line_color(self, source):
        if source == "system":
            return (200, 180, 50)
        elif source == "user":
            return (150, 200, 150)
        elif source == "dm":
            return (150, 180, 220)
        return (180, 180, 180)
