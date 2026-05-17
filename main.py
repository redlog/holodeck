import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pygame
from config import INTERNAL_WIDTH, INTERNAL_HEIGHT, DISPLAY_SCALE
from modes.play_mode import PlayMode
from modes.holodeck_mode import HolodeckMode
from world.bible import (
    save_game, load_game, get_save_slots, get_game_dir,
    list_games, create_game,
)
from rendering.save_load_ui import SaveLoadUI
from rendering.game_menu import GameMenu
from dm.dungeon_master import DungeonMaster
from dm.image_gen import ImageGenerator
from agents.author import AuthorAgent
from agents.scenery import SceneryAgent
from agents.character_imagery import CharacterImageryAgent


def _flip(screen, window):
    if DISPLAY_SCALE == 1:
        window.blit(screen, (0, 0))
    else:
        pygame.transform.scale(screen, window.get_size(), window)
    pygame.display.flip()


def _scale_mouse(pos):
    return (pos[0] // DISPLAY_SCALE, pos[1] // DISPLAY_SCALE)


def run_menu(screen, window, clock):
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
                event = pygame.event.Event(event.type, pos=_scale_mouse(event.pos), rel=event.rel, buttons=event.buttons)
            menu.handle_event(event)

        if menu.result:
            action, value = menu.result
            if action == "quit":
                return None, None
            elif action == "new":
                slug, world_state = create_game(value)
                return slug, world_state
            elif action == "load":
                world_state = load_game(value)
                return value, world_state

        menu.render()
        _flip(screen, window)


def run_game(screen, window, clock, game_slug, world_state):
    cache_dir = get_game_dir(game_slug)

    has_world = bool(world_state["player"]["current_room"])
    current_mode = "holodeck" if not has_world else "play"

    # New multi-agent system
    author = AuthorAgent(world_state)
    scenery = SceneryAgent(cache_dir)
    character = CharacterImageryAgent(cache_dir)

    # Legacy (kept for fallback/compatibility)
    dm = DungeonMaster(world_state)
    image_gen = ImageGenerator(cache_dir=cache_dir)

    play_mode = PlayMode(screen, world_state, image_gen, game_slug,
                         scenery_agent=scenery, character_agent=character)
    holodeck_mode = HolodeckMode(screen, world_state, author=author, scenery=scenery,
                                  character=character, image_gen=image_gen, game_slug=game_slug)
    save_load_ui = SaveLoadUI(screen)

    title = world_state.get("meta", {}).get("title", "The Holodeck")
    pygame.display.set_caption(f"The Holodeck — {title}")

    running = True
    while running:
        dt = clock.tick(60)

        raw_events = pygame.event.get()
        mode_switch = False
        filtered_events = []

        # Scale mouse coordinates to internal resolution
        events = []
        for event in raw_events:
            if event.type == pygame.MOUSEBUTTONDOWN:
                event = pygame.event.Event(event.type, button=event.button, pos=_scale_mouse(event.pos))
            elif event.type == pygame.MOUSEBUTTONUP:
                event = pygame.event.Event(event.type, button=event.button, pos=_scale_mouse(event.pos))
            elif event.type == pygame.MOUSEMOTION:
                event = pygame.event.Event(event.type, pos=_scale_mouse(event.pos), rel=event.rel, buttons=event.buttons)
            events.append(event)

        for event in events:
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                if save_load_ui.active:
                    save_load_ui.active = False
                else:
                    running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_BACKQUOTE:
                if not save_load_ui.active:
                    mode_switch = True
            else:
                filtered_events.append(event)
        events = filtered_events

        # Save/load UI captures all input when active
        if save_load_ui.active:
            for event in events:
                result = save_load_ui.handle_event(event)
                if result and result != "consumed" and result != "cancelled":
                    action, slot = result
                    if action == "save":
                        save_game(world_state, game_slug, slot)
                        holodeck_mode.console_lines.append(
                            ("system", f"Game saved to '{slot}'.")
                        )
                    elif action == "load":
                        loaded = load_game(game_slug, slot)
                        if loaded:
                            world_state = loaded
                            author = AuthorAgent(world_state)
                            scenery = SceneryAgent(cache_dir)
                            character = CharacterImageryAgent(cache_dir)
                            dm = DungeonMaster(world_state)
                            image_gen = ImageGenerator(cache_dir=cache_dir)
                            play_mode = PlayMode(screen, world_state, image_gen, game_slug,
                         scenery_agent=scenery, character_agent=character)
                            holodeck_mode = HolodeckMode(screen, world_state, author=author,
                                                         scenery=scenery, character=character,
                                                         image_gen=image_gen, game_slug=game_slug)
                            holodeck_mode.console_lines.append(
                                ("system", f"Game loaded from '{slot}'.")
                            )
                            has_world = bool(world_state["player"]["current_room"])
                            current_mode = "holodeck" if not has_world else "play"
            events = []

        # Handle F5/F7 from remaining events
        for event in events[:]:
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_F5:
                    save_load_ui.open_save(get_save_slots(game_slug))
                    events.remove(event)
                elif event.key == pygame.K_F7:
                    save_load_ui.open_load(get_save_slots(game_slug))
                    events.remove(event)

        if mode_switch:
            current_mode = "holodeck" if current_mode == "play" else "play"

        if holodeck_mode.wants_resume:
            holodeck_mode.wants_resume = False
            current_mode = "play"

        screen.fill((0, 0, 0))

        if current_mode == "play":
            play_mode.update(dt, events)
            play_mode.render()
        else:
            play_mode.update(dt, [])
            play_mode.render()
            holodeck_mode.update(dt, events)
            holodeck_mode.render()

        for note in play_mode.notifications:
            holodeck_mode.console_lines.append(("system", note))
        play_mode.notifications.clear()

        save_load_ui.render()

        _flip(screen, window)

    # Autosave on exit
    save_game(world_state, game_slug, "autosave")


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
        run_game(screen, window, clock, game_slug, world_state)
        pygame.display.set_caption("The Holodeck")

    pygame.quit()


if __name__ == "__main__":
    main()
