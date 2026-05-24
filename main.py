"""Holodeck entry point.

Currently shows the game menu (new / load / quit) and, after a selection,
displays a placeholder while the new text-adventure play loop is being
built. See design/text_adventure_design.md for the target architecture.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pygame

from config import INTERNAL_WIDTH, INTERNAL_HEIGHT
from world.bible import (
    save_game, load_game, get_game_dir,
    list_games, create_game,
)
from rendering.game_menu import GameMenu
from rendering.ui import get_font
from agents.dm import DungeonMaster
from agents.character_imagery import CharacterImageryAgent
from agents.scenery import SceneryAgent
from agents.item_imagery import ItemImageryAgent
from modes.setup_mode import SetupMode
from modes.play_mode import PlayMode


def _window_transform(window):
    win_w, win_h = window.get_size()
    scale = min(win_w / INTERNAL_WIDTH, win_h / INTERNAL_HEIGHT)
    offset_x = (win_w - int(INTERNAL_WIDTH * scale)) // 2
    offset_y = (win_h - int(INTERNAL_HEIGHT * scale)) // 2
    return scale, offset_x, offset_y


def _flip(screen, window):
    scale, ox, oy = _window_transform(window)
    scaled_w = int(INTERNAL_WIDTH * scale)
    scaled_h = int(INTERNAL_HEIGHT * scale)
    scaled = pygame.transform.scale(screen, (scaled_w, scaled_h))
    window.fill((0, 0, 0))
    window.blit(scaled, (ox, oy))
    pygame.display.flip()


def _scale_mouse(pos, window):
    scale, ox, oy = _window_transform(window)
    return (int((pos[0] - ox) / scale), int((pos[1] - oy) / scale))


def run_menu(screen, window, clock):
    """Return (game_slug, world_state) the user wants to play, or (None, None) to quit."""
    menu = GameMenu(screen)
    menu.set_games(list_games())

    while True:
        clock.tick(60)
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return None, None
            if event.type == pygame.VIDEORESIZE:
                continue
            if event.type == pygame.MOUSEBUTTONDOWN:
                event = pygame.event.Event(event.type, button=event.button, pos=_scale_mouse(event.pos, window))
            elif event.type == pygame.MOUSEMOTION:
                event = pygame.event.Event(event.type, pos=_scale_mouse(event.pos, window),
                                           rel=event.rel, buttons=event.buttons)
            menu.handle_event(event)

        if menu.result:
            action, value = menu.result
            if action == "quit":
                return None, None
            if action == "new":
                slug, world_state = create_game()
                return slug, world_state
            if action == "load":
                result = load_game(value)
                if result is None:
                    continue
                world_state, _session = result
                return value, world_state

        menu.render()
        _flip(screen, window)


def _collect_events(raw_events, window):
    """Translate raw pygame events to internal-coord events. Returns (events, quit)."""
    events = []
    for event in raw_events:
        if event.type == pygame.QUIT:
            return events, True
        if event.type == pygame.VIDEORESIZE:
            continue
        if event.type == pygame.MOUSEBUTTONDOWN:
            event = pygame.event.Event(event.type, button=event.button, pos=_scale_mouse(event.pos, window))
        elif event.type == pygame.MOUSEBUTTONUP:
            event = pygame.event.Event(event.type, button=event.button, pos=_scale_mouse(event.pos, window))
        elif event.type == pygame.MOUSEMOTION:
            event = pygame.event.Event(event.type, pos=_scale_mouse(event.pos, window),
                                       rel=event.rel, buttons=event.buttons)
        events.append(event)
    return events, False


def run_setup(screen, window, clock, game_slug, world_state, dm,
              portrait_agent, scenery_agent):
    """Run the setup-mode conversation. Returns (keep_running, completed):
       keep_running=False on window close, completed=True if the player
       finished setup and wants to begin play."""
    setup = SetupMode(screen, world_state, dm, game_slug,
                      portrait_agent=portrait_agent, scenery_agent=scenery_agent)

    while not setup.done:
        dt = clock.tick(60)
        events, quit_requested = _collect_events(pygame.event.get(), window)
        if quit_requested:
            return False, False

        # Allow Esc to bail back to menu without completing setup
        filtered = []
        bail = False
        for event in events:
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                bail = True
                break
            filtered.append(event)
        if bail:
            return True, False

        setup.update(dt, filtered)
        setup.render()
        _flip(screen, window)

    return True, True


def run_play(screen, window, clock, game_slug, world_state, dm,
             portrait_agent, scenery_agent, item_agent=None):
    """Run the play-mode loop. Returns keep_running."""
    play = PlayMode(screen, world_state, dm, game_slug,
                    portrait_agent=portrait_agent, scenery_agent=scenery_agent,
                    item_agent=item_agent)

    while True:
        dt = clock.tick(60)
        events, quit_requested = _collect_events(pygame.event.get(), window)
        if quit_requested:
            return False

        filtered = []
        bail = False
        for event in events:
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                bail = True
                break
            filtered.append(event)
        if bail:
            return True

        play.update(dt, filtered)
        if play.menu_requested:
            return True
        play.render()
        _flip(screen, window)


def main():
    pygame.init()
    window = pygame.display.set_mode((INTERNAL_WIDTH, INTERNAL_HEIGHT), pygame.RESIZABLE)
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
        cache_dir = get_game_dir(game_slug)

        # One set of agents per game session, shared between setup and play.
        dm = DungeonMaster(world_state, game_dir=cache_dir)
        portrait_agent = CharacterImageryAgent(cache_dir)
        scenery_agent = SceneryAgent(cache_dir)
        item_agent = ItemImageryAgent(cache_dir)

        if dm.phase != dm.PHASE_PLAY:
            keep_running, completed = run_setup(screen, window, clock, game_slug,
                                                world_state, dm, portrait_agent, scenery_agent)
            save_game(world_state, game_slug, "autosave")
            if not keep_running:
                break
            if not completed:
                pygame.display.set_caption("The Holodeck")
                continue

        keep_running = run_play(screen, window, clock, game_slug, world_state,
                                dm, portrait_agent, scenery_agent, item_agent)
        save_game(world_state, game_slug, "autosave")
        if not keep_running:
            break

        pygame.display.set_caption("The Holodeck")

    pygame.quit()


if __name__ == "__main__":
    main()
