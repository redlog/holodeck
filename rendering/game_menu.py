import pygame
from config import INTERNAL_WIDTH, INTERNAL_HEIGHT
from rendering.ui import get_font


class GameMenu:
    def __init__(self, surface):
        self.surface = surface
        self.games = []
        self.selected = 0
        self.result = None
        self.title_font = get_font(28)
        self.font = get_font(16)
        self.small_font = get_font(12)

    def set_games(self, games):
        self.games = games
        self.selected = 0

    def handle_event(self, event):
        if event.type != pygame.KEYDOWN:
            return

        total = len(self.games) + 1  # +1 for "New Game"

        if event.key == pygame.K_UP:
            self.selected = (self.selected - 1) % total
        elif event.key == pygame.K_DOWN:
            self.selected = (self.selected + 1) % total
        elif event.key == pygame.K_RETURN:
            if self.selected == 0:
                self.result = ("new", None)
            else:
                game = self.games[self.selected - 1]
                self.result = ("load", game["slug"])
        elif event.key == pygame.K_ESCAPE:
            self.result = ("quit", None)

    def render(self):
        self.surface.fill((15, 12, 25))

        # Title
        title = self.title_font.render("T H E   H O L O D E C K", False, (200, 180, 50))
        self.surface.blit(title, (INTERNAL_WIDTH // 2 - title.get_width() // 2, 60))

        subtitle = self.small_font.render("AI-Driven Adventure Game Engine", False, (120, 110, 80))
        self.surface.blit(subtitle, (INTERNAL_WIDTH // 2 - subtitle.get_width() // 2, 100))

        # Menu items
        y = 180
        items = ["[ New Game ]"] + [g["title"] for g in self.games]

        for i, label in enumerate(items):
            if i == self.selected:
                color = (255, 255, 200)
                prefix = "> "
            else:
                color = (140, 140, 140)
                prefix = "  "

            text = self.font.render(f"{prefix}{label}", False, color)
            self.surface.blit(text, (INTERNAL_WIDTH // 2 - 150, y))

            if i > 0:
                game = self.games[i - 1]
                if game.get("last_played"):
                    date_str = game["last_played"][:16].replace("T", " ")
                    date_surf = self.small_font.render(date_str, False, (80, 80, 80))
                    self.surface.blit(date_surf, (INTERNAL_WIDTH // 2 + 160, y + 3))

            y += 32

        # Hints
        hint = self.small_font.render("Enter=Select  Esc=Quit", False, (70, 70, 70))
        self.surface.blit(hint, (INTERNAL_WIDTH // 2 - hint.get_width() // 2, INTERNAL_HEIGHT - 40))
