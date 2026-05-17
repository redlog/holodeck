import pygame
from config import FONT_PATH, FONT_SIZE, INTERNAL_WIDTH


def get_font(size=None):
    size = size or FONT_SIZE
    if FONT_PATH:
        return pygame.font.Font(FONT_PATH, size)
    return pygame.font.SysFont("consolas", size)


class TextInput:
    def __init__(self, y, width=INTERNAL_WIDTH, prompt="> ", x=0):
        self.rect = pygame.Rect(x, y, width, 28)
        self.prompt = prompt
        self.text = ""
        self.font = get_font()
        self.cursor_visible = True
        self.cursor_timer = 0
        self.active = True
        # Visual-only busy state. Input still accepts text so slash commands work,
        # but the field renders dimmed and shows a "(busy)" indicator.
        self.busy = False

    def handle_event(self, event):
        if not self.active:
            return None
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_RETURN:
                result = self.text
                self.text = ""
                return result
            elif event.key == pygame.K_BACKSPACE:
                self.text = self.text[:-1]
            elif event.key == pygame.K_v and (event.mod & pygame.KMOD_CTRL or event.mod & pygame.KMOD_META):
                try:
                    clipboard = pygame.scrap.get(pygame.SCRAP_TEXT)
                    if clipboard:
                        pasted = clipboard.decode("utf-8", errors="ignore").rstrip("\x00")
                        self.text += pasted.replace("\r", "").replace("\n", " ")
                except Exception:
                    pass
            elif event.unicode and event.unicode.isprintable():
                self.text += event.unicode
        return None

    def update(self, dt):
        self.cursor_timer += dt
        if self.cursor_timer >= 500:
            self.cursor_timer = 0
            self.cursor_visible = not self.cursor_visible

    def render(self, surface):
        bg_color = (15, 15, 20) if self.busy else (0, 0, 0)
        border_color = (50, 50, 60) if self.busy else (80, 80, 80)
        text_color = (110, 110, 110) if self.busy else (200, 200, 200)

        pygame.draw.rect(surface, bg_color, self.rect)
        pygame.draw.rect(surface, border_color, self.rect, 1)

        prompt = "[busy] " + self.prompt if self.busy else self.prompt
        display = prompt + self.text
        if self.cursor_visible and self.active and not self.busy:
            display += "_"

        text_surf = self.font.render(display, False, text_color)
        max_width = self.rect.width - 12
        offset_x = max(0, text_surf.get_width() - max_width)
        surface.set_clip(self.rect.inflate(-4, -4))
        surface.blit(text_surf, (self.rect.x + 6 - offset_x, self.rect.y + 6))
        surface.set_clip(None)
