import pygame
from config import INTERNAL_WIDTH, INTERNAL_HEIGHT


class Renderer:
    def __init__(self, surface):
        self.surface = surface

    def clear(self):
        self.surface.fill((0, 0, 0))

    def render_room_placeholder(self, room_name):
        from rendering.ui import get_font
        font = get_font(24)
        text = font.render(room_name or "No Room", False, (180, 180, 180))
        rect = text.get_rect(center=(INTERNAL_WIDTH // 2, INTERNAL_HEIGHT // 2))
        self.surface.blit(text, rect)
