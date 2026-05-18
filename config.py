import os

# Display
INTERNAL_WIDTH = 1280
INTERNAL_HEIGHT = 800
DISPLAY_SCALE = 2

# UI
FONT_SIZE = 16
FONT_PATH = None  # Set to a .ttf path for custom pixel font

# Gameplay
AUTOSAVE_ON_RESPONSE = True

# Gemini models — overridable via environment
GEMINI_DM_MODEL = os.getenv("GEMINI_DM_MODEL", "gemini-2.5-pro")
GEMINI_AUTHOR_MODEL = os.getenv("GEMINI_AUTHOR_MODEL", "gemini-2.5-pro")
GEMINI_NPC_MODEL = os.getenv("GEMINI_NPC_MODEL", "gemini-2.5-flash")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
GEMINI_VISION_MODEL = os.getenv("GEMINI_VISION_MODEL", "gemini-2.5-flash")
# Room backgrounds go through Imagen — it natively supports aspect ratios.
# (Gemini's image model returns 1024x1024 squares no matter what.)
SCENERY_MODEL = os.getenv("SCENERY_MODEL", "imagen-4.0-generate-001")
