import pygame
import textwrap
from config import INTERNAL_WIDTH, INTERNAL_HEIGHT
from rendering.holodeck_overlay import apply_holodeck_overlay
from rendering.ui import TextInput, get_font


class HolodeckMode:
    def __init__(self, surface, world_state, author=None, scenery=None, character=None,
                 dm=None, image_gen=None):
        self.surface = surface
        self.world_state = world_state
        self.author = author
        self.scenery = scenery
        self.character = character
        # Legacy fallbacks
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
        self._opening_sent = False

        # If the author agent is in interview phase, kick off a proactive greeting
        if self.author and self.author.connected and getattr(self.author, "phase", None) == "interview":
            self.console_lines.append(("system", "..."))
            self._waiting = True
            self._opening_sent = True
            self.author.start_interview()

    @property
    def busy(self):
        if self._waiting:
            return True
        if self.author and self.author.busy:
            return True
        if self.scenery and self.scenery._pending:
            return True
        if self.character and self.character._pending:
            return True
        # Legacy
        if self.image_gen and self.image_gen._pending:
            return True
        return False

    def update(self, dt, events):
        # Always allow typing — slash commands need to work even while the LLM is thinking
        self.text_input.active = True
        self.text_input.update(dt)

        for event in events:
            if event.type == pygame.MOUSEWHEEL:
                self._scroll_offset -= event.y * 2
                max_scroll = max(0, len(self.console_lines) - self.max_visible_lines)
                self._scroll_offset = max(0, min(self._scroll_offset, max_scroll))
                continue
            result = self.text_input.handle_event(event)
            if result is not None:
                self._handle_input(result)

        if self._waiting:
            response = None
            if self.author:
                response = self.author.poll_result()
            elif self.dm:
                response = self.dm.poll_response()
            if response:
                self._waiting = False
                self._handle_dm_response(response)

        self._poll_agent_results()

    def _append_lines(self, source, text):
        for line in self._wrap_text(text):
            self.console_lines.append((source, line))

    def _handle_input(self, text):
        if not text.strip():
            return
        stripped = text.strip()
        lowered = stripped.lower()

        # Slash commands always run instantly, even if the LLM is busy
        if stripped.startswith("/"):
            self._scroll_offset = 0
            self._append_lines("user", stripped)
            self._handle_command(stripped[1:].strip())
            return

        if lowered == "resume":
            self.wants_resume = True
            return

        # Non-command input goes to the LLM; block if already waiting
        if self.busy:
            self._append_lines("system", "(busy — wait for the current response, or type /help)")
            return

        self._scroll_offset = 0
        self._append_lines("user", text)

        if self.author and self.author.connected:
            self._waiting = True
            self.console_lines.append(("system", "..."))
            self.author.send_message(text)
        elif self.dm and self.dm.connected:
            self._waiting = True
            self.console_lines.append(("system", "..."))
            self.dm.send_holodeck_message(text)
        else:
            self.console_lines.append(("dm", "[Not connected] Set GEMINI_API_KEY in .env"))

    def _handle_dm_response(self, response):
        self._scroll_offset = 0
        if self.console_lines and self.console_lines[-1] == ("system", "..."):
            self.console_lines.pop()

        response_text = response.get("response_text", "[No response]")
        self._append_lines("dm", response_text)

        if self.author:
            self.author.apply_response(response)
        elif self.dm:
            self.dm.apply_response(response)

        if response.get("new_rooms"):
            names = [r.get("name", rid) for rid, r in response["new_rooms"].items()]
            self._append_lines("system", f"Created rooms: {', '.join(names)}")
            self._trigger_scenery_generation(response["new_rooms"])

        if response.get("new_characters"):
            names = [c.get("name", cid) for cid, c in response["new_characters"].items()]
            self._append_lines("system", f"Created characters: {', '.join(names)}")
            self._trigger_character_generation(response["new_characters"])

        # Check if player description was just set (triggers player sprite)
        updates = response.get("world_updates", {})
        if updates and updates.get("player"):
            player = self.world_state["player"]
            if player.get("description") and not player.get("sprite_sheet_path"):
                self._trigger_character_generation({
                    "player": {"id": "player", "name": player.get("name", "Player"), "description": player["description"]}
                })

    def _handle_command(self, cmd):
        parts = cmd.split()
        if not parts:
            return
        action = parts[0].lower()
        visual_style = self.world_state.get("meta", {}).get("visual_style", "")

        if action == "help":
            self._append_lines("system", "Commands:")
            self._append_lines("system", "  /regen room <id|all>     - regenerate room scenery + priority map")
            self._append_lines("system", "  /regen char <id|all>     - regenerate character imagery")
            self._append_lines("system", "  /regen player            - regenerate player imagery")
            self._append_lines("system", "  /list rooms              - list room ids")
            self._append_lines("system", "  /list chars              - list character ids")
            self._append_lines("system", "  /phase                   - show author agent phase")
            return

        if action == "phase":
            if self.author:
                self._append_lines("system", f"Author phase: {self.author.phase}")
            else:
                self._append_lines("system", "No author agent.")
            return

        if action == "list" and len(parts) >= 2:
            sub = parts[1].lower()
            if sub.startswith("room"):
                ids = list(self.world_state.get("rooms", {}).keys())
                self._append_lines("system", f"Rooms: {', '.join(ids) if ids else '(none)'}")
            elif sub.startswith("char"):
                ids = list(self.world_state.get("characters", {}).keys())
                self._append_lines("system", f"Characters: {', '.join(ids) if ids else '(none)'}")
            return

        if action == "regen" and len(parts) >= 2:
            target = parts[1].lower()
            arg = parts[2] if len(parts) >= 3 else None

            if target == "room":
                if not self.scenery or not self.scenery.connected:
                    self._append_lines("system", "Scenery agent not connected.")
                    return
                rooms = self.world_state.get("rooms", {})
                if arg == "all":
                    targets = list(rooms.items())
                elif arg and arg in rooms:
                    targets = [(arg, rooms[arg])]
                else:
                    self._append_lines("system", f"Unknown room id. Try /list rooms")
                    return
                for rid, rdef in targets:
                    self.scenery.generate_room(rid, rdef, visual_style)
                    self._append_lines("system", f"Regenerating scenery for {rid}...")
                return

            if target == "char":
                if not self.character or not self.character.connected:
                    self._append_lines("system", "Character agent not connected.")
                    return
                chars = self.world_state.get("characters", {})
                if arg == "all":
                    targets = list(chars.items())
                elif arg and arg in chars:
                    targets = [(arg, chars[arg])]
                else:
                    self._append_lines("system", f"Unknown character id. Try /list chars")
                    return
                for cid, cdef in targets:
                    self.character.generate_character(cid, cdef, visual_style)
                    self._append_lines("system", f"Regenerating imagery for {cid}...")
                return

            if target == "player":
                if not self.character or not self.character.connected:
                    self._append_lines("system", "Character agent not connected.")
                    return
                player = self.world_state["player"]
                desc = player.get("description")
                if not desc:
                    self._append_lines("system", "Player has no description set.")
                    return
                pdef = {"name": player.get("name", "Player"), "description": desc}
                self.character.generate_character("player", pdef, visual_style)
                self._append_lines("system", "Regenerating player imagery...")
                return

        self._append_lines("system", f"Unknown command. Type /help for the list.")

    def _trigger_scenery_generation(self, new_rooms):
        if not self.scenery or not self.scenery.connected:
            return
        visual_style = self.world_state.get("meta", {}).get("visual_style", "")
        for room_id, room_def in new_rooms.items():
            self.scenery.generate_room(room_id, room_def, visual_style)
            self._append_lines("system", f"Generating scenery for {room_def.get('name', room_id)}...")

    def _trigger_character_generation(self, new_characters):
        if self.character and self.character.connected:
            visual_style = self.world_state.get("meta", {}).get("visual_style", "")
            for char_id, char_def in new_characters.items():
                self.character.generate_character(char_id, char_def, visual_style)
                self._append_lines("system", f"Generating imagery for {char_def.get('name', char_id)}...")
        elif self.image_gen and self.image_gen.connected:
            # Legacy fallback
            visual_style = self.world_state.get("meta", {}).get("visual_style", "")
            for char_id, char_def in new_characters.items():
                self.image_gen.request_portrait(char_id, char_def, visual_style)
                self.image_gen.request_sprite(char_id, char_def, visual_style)
                self._append_lines("system", f"Generating images for {char_def.get('name', char_id)}...")

    def _poll_agent_results(self):
        if self.scenery:
            result = self.scenery.poll_result()
            while result:
                kind, entity_id, data = result
                if kind == "room_complete":
                    room_def = self.world_state.get("rooms", {}).get(entity_id)
                    if room_def:
                        room_def["background_path"] = data["background_path"]
                        room_def["priority_map_path"] = data["priority_map_path"]
                    self._append_lines("system", f"Scenery complete for {entity_id}.")
                elif kind == "error":
                    self._append_lines("system", f"Scenery error for {entity_id}: {data}")
                result = self.scenery.poll_result()

        if self.character:
            result = self.character.poll_result()
            while result:
                kind, entity_id, data = result
                if kind == "character_complete":
                    if entity_id == "player":
                        self.world_state["player"]["sprite_sheet_path"] = data["sprite_path"]
                        self.world_state["player"]["portrait_path"] = data["portrait_path"]
                    else:
                        char_def = self.world_state.get("characters", {}).get(entity_id)
                        if char_def:
                            char_def["sprite_path"] = data["sprite_path"]
                            char_def["portrait_path"] = data["portrait_path"]
                    self._append_lines("system", f"Character imagery complete for {entity_id}.")
                elif kind == "error":
                    self._append_lines("system", f"Character imagery error for {entity_id}: {data}")
                result = self.character.poll_result()

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
