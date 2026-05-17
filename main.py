"""Holodeck entry point.

Currently shows the game menu (new / load / quit) and, after a selection,
displays a placeholder while the new text-adventure play loop is being
built. See design/text_adventure_design.md for the target architecture.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pygame

from config import INTERNAL_WIDTH, INTERNAL_HEIGHT, DISPLAY_SCALE
from world.bible import (
    save_game, load_game, get_game_dir,
    list_games, create_game,
)
from rendering.game_menu import GameMenu
from rendering.ui import get_font


def _flip(screen, window):
    if DISPLAY_SCALE == 1:
        window.blit(screen, (0, 0))
    else:
        pygame.transform.scale(screen, window.get_size(), window)
    pygame.display.flip()


def _scale_mouse(pos):
    return (pos[0] // DISPLAY_SCALE, pos[1] // DISPLAY_SCALE)


def run_menu(screen, window, clock):
    """Return (game_slug, world_state) the user wants to play, or (None, None) to quit."""
    menu = GameMenu(screen)
    menu.set_games(list_games())

    while True:
        clock.tick(60)
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return None, None
            if event.type == pygame.MOUSEBUTTONDOWN:
                event = pygame.event.Event(event.type, button=event.button, pos=_scale_mouse(event.pos))
            elif event.type == pygame.MOUSEMOTION:
                event = pygame.event.Event(event.type, pos=_scale_mouse(event.pos),
                                           rel=event.rel, buttons=event.buttons)
            menu.handle_event(event)

        if menu.result:
            action, value = menu.result
            if action == "quit":
                return None, None
            if action == "new":
                slug, world_state = create_game(value)
                return slug, world_state
            if action == "load":
                world_state = load_game(value)
                return value, world_state

        menu.render()
        _flip(screen, window)


def run_placeholder(screen, window, clock, game_slug, world_state):
    """Temporary screen shown after the menu while play mode is being rebuilt."""
    font = get_font()
    title = world_state.get("meta", {}).get("title") or game_slug

    lines = [
        f"Loaded: {title}",
        "",
        "Play mode is under construction.",
        "See design/text_adventure_design.md.",
        "",
        "Press any key to return to the menu.",
    ]

    while True:
        clock.tick(60)
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return False
            if event.type == pygame.KEYDOWN:
                return True

        screen.fill((20, 20, 30))
        y = INTERNAL_HEIGHT // 2 - (len(lines) * 22) // 2
        for line in lines:
            surf = font.render(line, True, (220, 220, 220))
            screen.blit(surf, ((INTERNAL_WIDTH - surf.get_width()) // 2, y))
            y += 22
        _flip(screen, window)


def main():
    pygame.init()
    display_w = INTERNAL_WIDTH * DISPLAY_SCALE
    display_h = INTERNAL_HEIGHT * DISPLAY_SCALE
    window = pygame.display.set_mode((display_w, display_h))
    pygame.scrap.init()
    screen = pygame.Surface((INTERNAL_WIDTH, INTERNAL_HEIGHT))
    pygame.display.set_caption("The Holodeck")
    clock = pygame.time.Clock()

    while True:
        game_slug, world_state = run_menu(screen, window, clock)
        if not game_slug:
            break

        title = world_state.get("meta", {}).get("title", "The Holodeck")
        pygame.display.set_caption(f"The Holodeck — {title}")

        # Ensure the game dir exists so saves work later.
        get_game_dir(game_slug)

        keep_running = run_placeholder(screen, window, clock, game_slug, world_state)
        save_game(world_state, game_slug, "autosave")
        pygame.display.set_caption("The Holodeck")
        if not keep_running:
            break

    pygame.quit()


if __name__ == "__main__":
    main()
