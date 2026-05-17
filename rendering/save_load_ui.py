import pygame
from config import INTERNAL_WIDTH, INTERNAL_HEIGHT
from rendering.ui import get_font


class SaveLoadUI:
    def __init__(self, surface):
        self.surface = surface
        self.active = False
        self.mode = None  # "save" or "load"
        self.slots = []
        self.selected = 0
        self.typing_name = False
        self.new_name = ""
        self.font = get_font(16)
        self.title_font = get_font(20)

    def open_save(self, slots):
        self.active = True
        self.mode = "save"
        self.slots = slots[:]
        if "autosave" in self.slots:
            self.slots.remove("autosave")
        self.slots.insert(0, "[New Save]")
        self.selected = 0
        self.typing_name = False
        self.new_name = ""

    def open_load(self, slots):
        self.active = True
        self.mode = "load"
        self.slots = slots[:]
        self.selected = 0
        self.typing_name = False
        if not self.slots:
            self.slots = ["(no saves found)"]

    def handle_event(self, event):
        if not self.active:
            return None

        if event.type != pygame.KEYDOWN:
            return None

        if self.typing_name:
            if event.key == pygame.K_RETURN:
                name = self.new_name.strip() or "save1"
                self.active = False
                return ("save", name)
            elif event.key == pygame.K_ESCAPE:
                self.typing_name = False
                self.new_name = ""
            elif event.key == pygame.K_BACKSPACE:
                self.new_name = self.new_name[:-1]
            elif event.unicode and event.unicode.isprintable():
                self.new_name += event.unicode
            return "consumed"

        if event.key == pygame.K_ESCAPE:
            self.active = False
            return "cancelled"
        elif event.key == pygame.K_UP:
            self.selected = (self.selected - 1) % len(self.slots)
        elif event.key == pygame.K_DOWN:
            self.selected = (self.selected + 1) % len(self.slots)
        elif event.key == pygame.K_RETURN:
            if self.mode == "save":
                if self.selected == 0:
                    self.typing_name = True
                    self.new_name = ""
                else:
                    self.active = False
                    return ("save", self.slots[self.selected])
            elif self.mode == "load":
                if self.slots[0] == "(no saves found)":
                    self.active = False
                    return "cancelled"
                self.active = False
                return ("load", self.slots[self.selected])

        return "consumed"

    def render(self):
        if not self.active:
            return

        overlay = pygame.Surface((INTERNAL_WIDTH, INTERNAL_HEIGHT))
        overlay.fill((0, 0, 0))
        overlay.set_alpha(200)
        self.surface.blit(overlay, (0, 0))

        panel_w, panel_h = 400, 300
        panel_x = (INTERNAL_WIDTH - panel_w) // 2
        panel_y = (INTERNAL_HEIGHT - panel_h) // 2
        panel_rect = pygame.Rect(panel_x, panel_y, panel_w, panel_h)

        pygame.draw.rect(self.surface, (20, 20, 30), panel_rect)
        pygame.draw.rect(self.surface, (120, 110, 50), panel_rect, 2)

        title = "SAVE GAME" if self.mode == "save" else "LOAD GAME"
        title_surf = self.title_font.render(title, False, (200, 180, 50))
        self.surface.blit(title_surf, (panel_x + 20, panel_y + 15))

        y = panel_y + 55
        for i, slot in enumerate(self.slots):
            if i > 8:
                break
            color = (255, 255, 200) if i == self.selected else (150, 150, 150)
            prefix = "> " if i == self.selected else "  "
            text = self.font.render(f"{prefix}{slot}", False, color)
            self.surface.blit(text, (panel_x + 20, y))
            y += 26

        if self.typing_name:
            prompt_surf = self.font.render(f"Name: {self.new_name}_", False, (150, 220, 150))
            self.surface.blit(prompt_surf, (panel_x + 20, panel_y + panel_h - 40))

        hint = self.font.render("Enter=Select  Esc=Cancel", False, (100, 100, 100))
        self.surface.blit(hint, (panel_x + 20, panel_y + panel_h - 18))
