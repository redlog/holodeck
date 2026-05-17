import sys
from pathlib import Path

import numpy as np
from PIL import Image

from config import (
    INTERNAL_WIDTH, INTERNAL_HEIGHT,
    PRIORITY_MAP_WIDTH, PRIORITY_MAP_HEIGHT,
    PRIORITY_BLOCK, PRIORITY_WALKABLE_MIN, PRIORITY_WALKABLE_MAX, PRIORITY_FOREGROUND,
)


def _log(msg):
    print(f"[PMAP] {msg}", file=sys.stderr, flush=True)


class PriorityMap:
    MAP_WIDTH = PRIORITY_MAP_WIDTH
    MAP_HEIGHT = PRIORITY_MAP_HEIGHT

    def __init__(self, path=None):
        self._data = None
        self._foreground_surface = None
        self._debug_surface = None
        if path:
            self.load(path)

    def load(self, path):
        img = Image.open(path).convert("L")
        if img.size != (self.MAP_WIDTH, self.MAP_HEIGHT):
            img = img.resize((self.MAP_WIDTH, self.MAP_HEIGHT), Image.NEAREST)
        self._data = np.array(img, dtype=np.uint8)
        self._foreground_surface = None
        self._debug_surface = None
        _log(f"Loaded priority map from {path} ({self._data.shape})")

    def save(self, path):
        if self._data is None:
            return
        img = Image.fromarray(self._data, mode="L")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        img.save(path, "PNG")

    @property
    def loaded(self):
        return self._data is not None

    def _to_map_coords(self, x, y):
        mx = int(x * self.MAP_WIDTH / INTERNAL_WIDTH)
        my = int(y * self.MAP_HEIGHT / INTERNAL_HEIGHT)
        mx = max(0, min(self.MAP_WIDTH - 1, mx))
        my = max(0, min(self.MAP_HEIGHT - 1, my))
        return mx, my

    def get_band(self, x, y):
        if self._data is None:
            return PRIORITY_BLOCK
        mx, my = self._to_map_coords(x, y)
        return int(self._data[my, mx]) // 16

    def can_walk(self, x, y):
        band = self.get_band(x, y)
        return PRIORITY_WALKABLE_MIN <= band <= PRIORITY_WALKABLE_MAX

    def get_draw_priority(self, foot_y):
        return self.get_band(INTERNAL_WIDTH // 2, foot_y)

    def get_foreground_mask(self):
        import pygame
        if self._foreground_surface is not None:
            return self._foreground_surface

        if self._data is None:
            return None

        fg_mask = self._data >= (PRIORITY_FOREGROUND * 16)
        if not fg_mask.any():
            self._foreground_surface = None
            return None

        alpha = np.zeros((self.MAP_HEIGHT, self.MAP_WIDTH, 4), dtype=np.uint8)
        alpha[fg_mask, 3] = 255

        mask_img = Image.fromarray(alpha, "RGBA")
        mask_img = mask_img.resize((INTERNAL_WIDTH, INTERNAL_HEIGHT), Image.NEAREST)

        surf = pygame.image.fromstring(
            mask_img.tobytes(), (INTERNAL_WIDTH, INTERNAL_HEIGHT), "RGBA"
        ).convert_alpha()
        self._foreground_surface = surf
        return self._foreground_surface

    def to_debug_surface(self):
        import pygame
        if self._debug_surface is not None:
            return self._debug_surface

        if self._data is None:
            return None

        bands = self._data // 16
        # Black = highest priority (band 15), white = lowest (band 0)
        gray = (255 - bands * 17).clip(0, 255).astype(np.uint8)
        rgb = np.stack([gray, gray, gray], axis=-1)

        debug_img = Image.fromarray(rgb, "RGB")
        debug_img = debug_img.resize((INTERNAL_WIDTH, INTERNAL_HEIGHT), Image.NEAREST)

        surf = pygame.image.fromstring(
            debug_img.tobytes(), (INTERNAL_WIDTH, INTERNAL_HEIGHT), "RGB"
        ).convert()
        self._debug_surface = surf
        return self._debug_surface
