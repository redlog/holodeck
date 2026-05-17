import pygame
from config import HOLODECK_GRID_COLOR, HOLODECK_GRID_SPACING, INTERNAL_WIDTH, INTERNAL_HEIGHT


def apply_holodeck_overlay(surface):
    gray = pygame.Surface(surface.get_size())
    gray.fill((20, 20, 30))
    gray.set_alpha(180)
    surface.blit(gray, (0, 0))

    w, h = surface.get_size()
    for x in range(0, w, HOLODECK_GRID_SPACING):
        pygame.draw.line(surface, HOLODECK_GRID_COLOR, (x, 0), (x, h), 1)
    for y in range(0, h, HOLODECK_GRID_SPACING):
        pygame.draw.line(surface, HOLODECK_GRID_COLOR, (0, y), (w, y), 1)
