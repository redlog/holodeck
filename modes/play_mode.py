import sys
import pygame
from pathlib import Path
from config import INTERNAL_WIDTH, INTERNAL_HEIGHT
from rendering.renderer import Renderer
from rendering.sprite import AnimatedSprite
from rendering.ui import get_font
from world.room import Room


def _log(msg):
    print(f"[PLAY] {msg}", file=sys.stderr, flush=True)


class PlayMode:
    def __init__(self, surface, world_state, image_gen=None, game_slug=None):
        self.surface = surface
        self.world_state = world_state
        self.image_gen = image_gen
        self._game_slug = game_slug
        self.renderer = Renderer(surface)
        self.background_color = (30, 20, 40)
        self._background_cache = {}
        self._loading_rooms = set()
        self._loading_anim = 0
        self._room_objects = {}
        self._debug_draw = False
        self.notifications = []

        sprite_path = world_state.get("player", {}).get("sprite_sheet_path")
        self.player_sprite = AnimatedSprite(sprite_sheet_path=sprite_path)

        pos = world_state.get("player", {}).get("position", {})
        self.player_sprite.x = pos.get("x", INTERNAL_WIDTH // 2)
        self.player_sprite.y = pos.get("y", INTERNAL_HEIGHT - 80)

        self._ensure_current_room()
        self._generate_missing_images()

    def _generate_missing_images(self):
        if not self.image_gen or not self.image_gen.connected:
            return
        visual_style = self.world_state.get("meta", {}).get("visual_style", "")

        player = self.world_state.get("player", {})
        # If player has a character_id, pull description from that character
        player_desc = player.get("description")
        if not player_desc and player.get("character_id"):
            char_def = self.world_state.get("characters", {}).get(player["character_id"], {})
            player_desc = char_def.get("description")

        if player_desc and not player.get("sprite_sheet_path"):
            player_name = player.get("name") or player.get("character_id", "Player")
            player_def = {"name": player_name, "description": player_desc}
            self.image_gen.request_sprite("player", player_def, visual_style)
            self.image_gen.request_portrait("player", player_def, visual_style)

        for char_id, char_def in self.world_state.get("characters", {}).items():
            if not char_def.get("portrait_path"):
                self.image_gen.request_portrait(char_id, char_def, visual_style)
            if not char_def.get("sprite_path"):
                self.image_gen.request_sprite(char_id, char_def, visual_style)

    def _ensure_current_room(self):
        room_id = self.world_state.get("player", {}).get("current_room")
        if room_id and room_id not in self._room_objects:
            room_def = self.world_state.get("rooms", {}).get(room_id)
            if room_def:
                self._room_objects[room_id] = Room(room_def)

    def _get_current_room(self):
        room_id = self.world_state.get("player", {}).get("current_room")
        if room_id and room_id in self._room_objects:
            return self._room_objects[room_id]
        return None

    def update(self, dt, events):
        self._loading_anim += dt
        self._ensure_current_room()

        room = self._get_current_room()

        for event in events:
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                mx, my = event.pos
                if room and room.can_walk(mx, my):
                    self.player_sprite.set_target(mx, my)

            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_F3:
                    self._debug_draw = not self._debug_draw
                elif event.key in (pygame.K_UP, pygame.K_w):
                    self.player_sprite.set_walk_direction("north")
                elif event.key in (pygame.K_DOWN, pygame.K_s):
                    self.player_sprite.set_walk_direction("south")
                elif event.key in (pygame.K_LEFT, pygame.K_a):
                    self.player_sprite.set_walk_direction("west")
                elif event.key in (pygame.K_RIGHT, pygame.K_d):
                    self.player_sprite.set_walk_direction("east")

        can_walk = room.can_walk if room else None
        self.player_sprite.update(dt, can_walk_fn=can_walk)

        # Check exit zones
        if room:
            direction, target_room_id = room.check_exit(self.player_sprite.x, self.player_sprite.y)
            if target_room_id:
                self._transition_room(target_room_id, direction)

        # Update world state position
        self.world_state["player"]["position"]["x"] = self.player_sprite.x
        self.world_state["player"]["position"]["y"] = self.player_sprite.y

        # Poll for image gen results
        if self.image_gen:
            result = self.image_gen.poll_result()
            while result:
                kind, entity_id, data = result
                if kind == "background":
                    self._load_background_surface(entity_id, data)
                    room_def = self.world_state.get("rooms", {}).get(entity_id)
                    if room_def:
                        room_def["background_path"] = data
                    self._loading_rooms.discard(entity_id)
                elif kind == "sprite":
                    _log(f"Sprite ready for '{entity_id}': {data}")
                    if entity_id == "player":
                        self.world_state["player"]["sprite_sheet_path"] = data
                        self.player_sprite = AnimatedSprite(sprite_sheet_path=data)
                        self.player_sprite.x = self.world_state["player"]["position"]["x"]
                        self.player_sprite.y = self.world_state["player"]["position"]["y"]
                    else:
                        char_def = self.world_state.get("characters", {}).get(entity_id)
                        if char_def:
                            char_def["sprite_path"] = data
                    name = entity_id if entity_id == "player" else self.world_state.get("characters", {}).get(entity_id, {}).get("name", entity_id)
                    self.notifications.append(f"Sprite ready for {name}.")
                elif kind == "portrait":
                    _log(f"Portrait ready for '{entity_id}': {data}")
                    if entity_id == "player":
                        self.world_state["player"]["portrait_path"] = data
                    else:
                        char_def = self.world_state.get("characters", {}).get(entity_id)
                        if char_def:
                            char_def["portrait_path"] = data
                    name = entity_id if entity_id == "player" else self.world_state.get("characters", {}).get(entity_id, {}).get("name", entity_id)
                    self.notifications.append(f"Portrait ready for {name}.")
                elif kind == "error":
                    self._loading_rooms.discard(entity_id)
                    self.notifications.append(f"Image generation failed for {entity_id}.")
                result = self.image_gen.poll_result()

            self._request_current_room_image()

    def _transition_room(self, target_room_id, from_direction):
        _log(f"Transitioning to room '{target_room_id}' from {from_direction}")

        target_def = self.world_state.get("rooms", {}).get(target_room_id)
        if not target_def:
            _log(f"Room '{target_room_id}' not found in world state")
            return

        self.world_state["player"]["current_room"] = target_room_id

        if target_room_id not in self._room_objects:
            self._room_objects[target_room_id] = Room(target_def)

        opposite = {"north": "south", "south": "north", "east": "west", "west": "east"}
        entry_dir = opposite.get(from_direction, "south")
        entry_x, entry_y = self._room_objects[target_room_id].get_entry_position(entry_dir)

        self.player_sprite.x = entry_x
        self.player_sprite.y = entry_y
        self.player_sprite.stop()

        self.world_state["player"]["position"]["x"] = entry_x
        self.world_state["player"]["position"]["y"] = entry_y

        if self._game_slug:
            from world.bible import save_game
            save_game(self.world_state, self._game_slug, "autosave")

    def _request_current_room_image(self):
        current_room_id = self.world_state.get("player", {}).get("current_room")
        if not current_room_id:
            return
        if current_room_id in self._background_cache:
            return
        if current_room_id in self._loading_rooms:
            return

        room_def = self.world_state.get("rooms", {}).get(current_room_id)
        if not room_def:
            return

        if room_def.get("background_path"):
            path = Path(room_def["background_path"])
            if path.exists():
                self._load_background_surface(current_room_id, str(path))
                return

        prompt = room_def.get("background_prompt")
        if prompt and self.image_gen and self.image_gen.connected:
            visual_style = self.world_state.get("meta", {}).get("visual_style", "")
            self._loading_rooms.add(current_room_id)
            self.image_gen.request_background(current_room_id, room_def, visual_style)

    def _load_background_surface(self, room_id, path):
        try:
            surf = pygame.image.load(path).convert()
            self._background_cache[room_id] = surf
        except Exception:
            pass

    def render(self):
        self.surface.fill(self.background_color)

        current_room_id = self.world_state.get("player", {}).get("current_room")
        if current_room_id and current_room_id in self._background_cache:
            self.surface.blit(self._background_cache[current_room_id], (0, 0))
        elif current_room_id and current_room_id in self._loading_rooms:
            self._render_loading(current_room_id)
        elif current_room_id and current_room_id in self.world_state.get("rooms", {}):
            room_def = self.world_state["rooms"][current_room_id]
            self.renderer.render_room_placeholder(room_def["name"])
        else:
            self.renderer.render_room_placeholder("The Void")

        # Draw player sprite
        if current_room_id:
            self.player_sprite.render(self.surface)

        if self._debug_draw:
            self._render_debug()

        # HUD
        self._render_hud(current_room_id)

    def _render_hud(self, current_room_id):
        font = get_font(12)
        if current_room_id:
            room_def = self.world_state.get("rooms", {}).get(current_room_id, {})
            name = room_def.get("name", "")

            # Room name label
            name_surf = font.render(name, False, (220, 220, 200))
            bg = pygame.Surface((name_surf.get_width() + 10, name_surf.get_height() + 6), pygame.SRCALPHA)
            bg.fill((0, 0, 0, 150))
            self.surface.blit(bg, (5, 5))
            self.surface.blit(name_surf, (10, 8))

            # Exit hints
            room = self._get_current_room()
            if room:
                hints = []
                for d, target in room.exits.items():
                    if target:
                        hints.append(d[0].upper())
                if hints:
                    exit_text = font.render(f"Exits: {' '.join(hints)}", False, (160, 160, 140))
                    ebg = pygame.Surface((exit_text.get_width() + 10, exit_text.get_height() + 6), pygame.SRCALPHA)
                    ebg.fill((0, 0, 0, 150))
                    self.surface.blit(ebg, (5, 28))
                    self.surface.blit(exit_text, (10, 31))

        hint = font.render("Press ~ for Holodeck Mode", False, (100, 100, 100))
        self.surface.blit(hint, (INTERNAL_WIDTH // 2 - hint.get_width() // 2, INTERNAL_HEIGHT - 20))

    def _render_debug(self):
        room = self._get_current_room()
        if not room:
            return

        font = get_font(10)

        # Walkable zone boundary
        pct = room.walkable_zone.get("value", 65) / 100
        top = int(INTERNAL_HEIGHT * (1 - pct))
        pygame.draw.line(self.surface, (0, 255, 0), (0, top), (INTERNAL_WIDTH, top), 1)
        label = font.render(f"walkable top (y={top})", False, (0, 255, 0))
        self.surface.blit(label, (4, top + 2))

        # Obstacles
        for obs in room.obstacles:
            r = obs.get("rect", {})
            rect = pygame.Rect(r.get("x", 0), r.get("y", 0), r.get("width", 0), r.get("height", 0))
            pygame.draw.rect(self.surface, (255, 0, 0), rect, 2)
            name = obs.get("label", obs.get("id", ""))
            name_surf = font.render(name, False, (255, 100, 100))
            self.surface.blit(name_surf, (rect.x + 2, rect.y + 2))

        # Exit zones
        for direction, zone in room.exit_zones.items():
            target = room.exits.get(direction)
            if not target:
                continue
            try:
                rect = pygame.Rect(int(zone.get("x", 0)), int(zone.get("y", 0)), int(zone.get("width", 0)), int(zone.get("height", 0)))
            except (TypeError, ValueError):
                continue
            pygame.draw.rect(self.surface, (0, 150, 255), rect, 2)
            exit_surf = font.render(f"exit {direction} -> {target}", False, (0, 150, 255))
            self.surface.blit(exit_surf, (rect.x + 2, rect.y + 2))

        # Player position
        pos_surf = font.render(f"({int(self.player_sprite.x)}, {int(self.player_sprite.y)})", False, (255, 255, 0))
        self.surface.blit(pos_surf, (int(self.player_sprite.x) + 20, int(self.player_sprite.y) - 10))

    def _render_loading(self, room_id):
        room_def = self.world_state.get("rooms", {}).get(room_id, {})
        font = get_font(18)
        name_surf = font.render(room_def.get("name", ""), False, (180, 180, 180))
        self.surface.blit(name_surf, (INTERNAL_WIDTH // 2 - name_surf.get_width() // 2, INTERNAL_HEIGHT // 2 - 30))

        dots = "." * (1 + (int(self._loading_anim / 500) % 3))
        loading_font = get_font(14)
        loading_surf = loading_font.render(f"Generating image{dots}", False, (120, 120, 140))
        self.surface.blit(loading_surf, (INTERNAL_WIDTH // 2 - loading_surf.get_width() // 2, INTERNAL_HEIGHT // 2 + 10))
