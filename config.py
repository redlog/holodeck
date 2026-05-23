import os
from dotenv import load_dotenv

load_dotenv(override=True)

# Display
INTERNAL_WIDTH = 1280
INTERNAL_HEIGHT = 800
DISPLAY_SCALE = 1

# UI
FONT_SIZE = 16
FONT_PATH = None  # Set to a .ttf path for custom pixel font

# Gameplay
AUTOSAVE_ON_RESPONSE = True

# Gemini models — all must be defined in .env
_REQUIRED_MODELS = [
    "GEMINI_API_KEY",
    "GEMINI_DM_MODEL",
    "GEMINI_NPC_MODEL",
    "GEMINI_IMAGE_MODEL",
    "GEMINI_VISION_MODEL",
    "GEMINI_SCENERY_MODEL",
]
_missing = [k for k in _REQUIRED_MODELS if not os.getenv(k)]
if _missing:
    import sys
    for k in _missing:
        print(f"Missing required environment variable: {k}", file=sys.stderr)
    print("Define them in .env and try again.", file=sys.stderr)
    sys.exit(1)

GEMINI_DM_MODEL = os.environ["GEMINI_DM_MODEL"]
GEMINI_NPC_MODEL = os.environ["GEMINI_NPC_MODEL"]
GEMINI_IMAGE_MODEL = os.environ["GEMINI_IMAGE_MODEL"]
GEMINI_VISION_MODEL = os.environ["GEMINI_VISION_MODEL"]
SCENERY_MODEL = os.environ["GEMINI_SCENERY_MODEL"]
