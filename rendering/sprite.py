import pygame
from config import SPRITE_FRAME_WIDTH, SPRITE_FRAME_HEIGHT, FRAME_DELAY_MS, SPRITE_WALK_FRAMES


class AnimatedSprite:
    FACING_SOUTH = 0
    FACING_NORTH = 1
    FACING_WEST = 2
    FACING_EAST = 3

    def __init__(self, sprite_sheet_path=None, frame_width=SPRITE_FRAME_WIDTH, frame_height=SPRITE_FRAME_HEIGHT):
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.current_loop = self.FACING_SOUTH
        self.current_frame = 0
        self.frame_timer = 0
        self.frame_delay = FRAME_DELAY_MS
        self.x = 480
        self.y = 400
        self.moving = False
        self._target_x = None
        self._target_y = None
        self._walk_direction = None

        if sprite_sheet_path:
            self._load_sheet(sprite_sheet_path)
        else:
            self._make_placeholder()

    def _load_sheet(self, path):
        raw = pygame.image.load(path).convert_alpha()

        expected_w = self.frame_width * SPRITE_WALK_FRAMES
        expected_h = self.frame_height * 3

        if raw.get_width() != expected_w or raw.get_height() != expected_h:
            raw = pygame.transform.scale(raw, (expected_w, expected_h))

        self.frames = {
            self.FACING_SOUTH: [],
            self.FACING_NORTH: [],
            self.FACING_WEST: [],
            self.FACING_EAST: [],
        }

        for row, direction in enumerate([self.FACING_SOUTH, self.FACING_NORTH, self.FACING_WEST]):
            for col in range(SPRITE_WALK_FRAMES):
                frame = pygame.Surface((self.frame_width, self.frame_height), pygame.SRCALPHA)
                frame.blit(raw, (0, 0),
                           (col * self.frame_width, row * self.frame_height,
                            self.frame_width, self.frame_height))
                self.frames[direction].append(frame)

        self.frames[self.FACING_EAST] = [
            pygame.transform.flip(f, True, False) for f in self.frames[self.FACING_WEST]
        ]

    def _make_placeholder(self):
        self.frames = {}
        for direction in [self.FACING_SOUTH, self.FACING_NORTH, self.FACING_WEST, self.FACING_EAST]:
            frames = []
            for i in range(4):
                surf = pygame.Surface((self.frame_width, self.frame_height), pygame.SRCALPHA)
                # Body
                body_color = (80, 60, 140)
                pygame.draw.ellipse(surf, body_color, (8, 20, 24, 35))
                # Head
                pygame.draw.circle(surf, (200, 170, 140), (20, 16), 10)
                # Walking bob
                bob = (i % 2) * 2 if direction in (self.FACING_SOUTH, self.FACING_NORTH) else 0
                # Direction indicator
                if direction == self.FACING_SOUTH:
                    pygame.draw.circle(surf, (40, 40, 40), (16, 14 + bob), 2)
                    pygame.draw.circle(surf, (40, 40, 40), (24, 14 + bob), 2)
                elif direction == self.FACING_NORTH:
                    pass  # no eyes visible from behind
                elif direction == self.FACING_WEST:
                    pygame.draw.circle(surf, (40, 40, 40), (14, 14 + bob), 2)
                elif direction == self.FACING_EAST:
                    pygame.draw.circle(surf, (40, 40, 40), (26, 14 + bob), 2)
                # Legs - alternate for walking
                leg_offset = 4 if i % 2 == 0 else -4
                pygame.draw.line(surf, (50, 40, 30), (15, 52), (15 + leg_offset, 59), 2)
                pygame.draw.line(surf, (50, 40, 30), (25, 52), (25 - leg_offset, 59), 2)
                frames.append(surf)
            self.frames[direction] = frames

    def set_target(self, x, y):
        self._target_x = x
        self._target_y = y
        self._walk_direction = None

    def set_walk_direction(self, direction):
        if self._walk_direction == direction:
            self._walk_direction = None
            self._target_x = None
            self._target_y = None
            self.moving = False
        else:
            self._walk_direction = direction
            self._target_x = None
            self._target_y = None

    def stop(self):
        self._walk_direction = None
        self._target_x = None
        self._target_y = None
        self.moving = False

    def update(self, dt, can_walk_fn=None, speed=3):
        dx, dy = 0, 0

        if self._walk_direction:
            if self._walk_direction == "north":
                dx, dy = 0, -speed
            elif self._walk_direction == "south":
                dx, dy = 0, speed
            elif self._walk_direction == "west":
                dx, dy = -speed, 0
            elif self._walk_direction == "east":
                dx, dy = speed, 0

            self.current_loop = {
                "north": self.FACING_NORTH,
                "south": self.FACING_SOUTH,
                "west": self.FACING_WEST,
                "east": self.FACING_EAST,
            }[self._walk_direction]

        elif self._target_x is not None:
            tdx = self._target_x - self.x
            tdy = self._target_y - self.y
            dist = (tdx * tdx + tdy * tdy) ** 0.5

            if dist < speed:
                self.x = self._target_x
                self.y = self._target_y
                self._target_x = None
                self._target_y = None
                self.moving = False
                return
            else:
                dx = (tdx / dist) * speed
                dy = (tdy / dist) * speed

                if abs(tdx) > abs(tdy):
                    self.current_loop = self.FACING_EAST if tdx > 0 else self.FACING_WEST
                else:
                    self.current_loop = self.FACING_SOUTH if tdy > 0 else self.FACING_NORTH
        else:
            self.moving = False

        if dx != 0 or dy != 0:
            new_x = self.x + dx
            new_y = self.y + dy

            if can_walk_fn:
                moved = False
                if can_walk_fn(new_x, new_y):
                    self.x = new_x
                    self.y = new_y
                    moved = True
                elif can_walk_fn(new_x, self.y):
                    self.x = new_x
                    moved = True
                elif can_walk_fn(self.x, new_y):
                    self.y = new_y
                    moved = True

                if not moved and self._walk_direction:
                    self.moving = False
                    return
                self.moving = moved
            else:
                self.x = new_x
                self.y = new_y
                self.moving = True

        if self.moving:
            self.frame_timer += dt
            if self.frame_timer >= self.frame_delay:
                self.frame_timer = 0
                self.current_frame = (self.current_frame + 1) % len(
                    self.frames[self.current_loop]
                )
        else:
            self.current_frame = 0

    def get_current_frame(self):
        return self.frames[self.current_loop][self.current_frame]

    def render(self, surface):
        frame = self.get_current_frame()
        x = int(self.x - self.frame_width // 2)
        y = int(self.y - self.frame_height)
        surface.blit(frame, (x, y))
