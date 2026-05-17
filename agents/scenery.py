import io
import os
import sys
from pathlib import Path

from PIL import Image

from agents.base import BaseAgent
from config import (
    GEMINI_SCENERY_MODEL, GEMINI_VISION_MODEL,
    INTERNAL_WIDTH, INTERNAL_HEIGHT,
    PRIORITY_MAP_WIDTH, PRIORITY_MAP_HEIGHT,
)


def _log(msg):
    print(f"[SCENERY] {msg}", file=sys.stderr, flush=True)


BACKGROUND_PROMPT_TEMPLATE = """{visual_style}.
Game background scene for a point-and-click adventure game.
Paint a natural, detailed scene. Do NOT include any characters or people.
Seen from a slight three-quarter overhead perspective.

CRITICAL SCALE: A standing adult would be approximately 96 pixels tall ({height_pct}% of image height). Size all objects accordingly.

Scene description: {description}

NO text, NO labels, NO UI elements, NO debug graphics. Just a beautiful painted game scene.
"""

PRIORITY_MAP_PROMPT = """Look at this painted game background and generate a PRIORITY MAP — a simple flat-zoned
grayscale image showing where a character can and cannot walk.

This is a TECHNICAL map with FLAT filled regions, NOT artwork. Do NOT trace edges or add detail.
Think of it like a coloring book filled in with only 3-4 shades:

- Pure black (pixel value 0): Areas the character CANNOT walk — walls, buildings, sky, ceiling,
  furniture, tree trunks, solid objects. This should be the largest zone in most outdoor scenes
  (sky + buildings + objects).
- Medium gray (pixel value 128): Walkable ground — floors, paths, grass the character walks on,
  open areas. Fill these as FLAT solid regions, no gradient needed.
- Pure white (pixel value 255): Foreground elements that should render IN FRONT of characters —
  overhanging tree canopies, awnings, archways, railings in the foreground. Only use this for
  things a character would walk BEHIND/UNDER.

CRITICAL RULES:
- Use FLAT SOLID fills — no gradients, no texture, no dithering, no edge detail
- The walkable region must be CONTINUOUS (no isolated walkable islands)
- Think about where a person's FEET would be — that's what determines walkability
- Tree canopies that a character could walk under should be white (foreground overlay)
- Tree trunks should be black (impassable)
- The boundary between walkable and impassable should follow the scene geometry exactly
- Keep it simple: big flat zones of black, gray, and white

Output ONLY the grayscale image. No text, no labels.
"""


class SceneryAgent(BaseAgent):
    def __init__(self, cache_dir):
        super().__init__(model=GEMINI_SCENERY_MODEL, temperature=0.7)
        self._cache_dir = Path(cache_dir)
        self._pending = {}

    def generate_room(self, room_id, room_def, visual_style):
        if room_id in self._pending:
            return
        self._pending[room_id] = True
        _log(f"Starting scenery pipeline for '{room_id}'")
        self._run_threaded(self._pipeline, room_id, room_def, visual_style)

    def _pipeline(self, room_id, room_def, visual_style):
        try:
            # Step 1: Generate painted background
            _log(f"[{room_id}] Step 1: Generating background artwork...")
            background_bytes = self._generate_background(room_def, visual_style)
            if not background_bytes:
                self._result_queue.put(("error", room_id, "Failed to generate background"))
                return
            background_path = self._save_background(room_id, background_bytes)
            _log(f"[{room_id}] Background saved to {background_path}")

            # Step 2: Derive priority map from the actual painted background
            _log(f"[{room_id}] Step 2: Deriving priority map from background...")
            priority_map_bytes = self._derive_priority_map(background_bytes)
            if not priority_map_bytes:
                self._result_queue.put(("error", room_id, "Failed to derive priority map"))
                return
            priority_map_path = self._save_priority_map(room_id, priority_map_bytes)
            _log(f"[{room_id}] Priority map saved to {priority_map_path}")

            self._result_queue.put(("room_complete", room_id, {
                "background_path": str(background_path),
                "priority_map_path": str(priority_map_path),
            }))
            _log(f"[{room_id}] Pipeline complete")

        except Exception as e:
            _log(f"[{room_id}] Pipeline error: {e}")
            self._result_queue.put(("error", room_id, str(e)))
        finally:
            self._pending.pop(room_id, None)

    def _generate_background(self, room_def, visual_style):
        description = room_def.get("description", "")
        height_pct = int(96 * 100 / INTERNAL_HEIGHT)

        prompt = BACKGROUND_PROMPT_TEMPLATE.format(
            visual_style=visual_style or "Painterly adventure game art",
            description=description,
            height_pct=height_pct,
        )

        image_bytes = self._call_image(prompt, aspect_ratio="16:10")
        if not image_bytes:
            return None

        img = Image.open(io.BytesIO(image_bytes))
        img = img.resize((INTERNAL_WIDTH, INTERNAL_HEIGHT), Image.LANCZOS)
        img = img.quantize(256).convert("RGB")

        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def _derive_priority_map(self, background_bytes):
        from google import genai
        from google.genai import types

        api_key = os.getenv("GEMINI_API_KEY")
        client = genai.Client(api_key=api_key)

        contents = [
            types.Part.from_bytes(data=background_bytes, mime_type="image/png"),
            PRIORITY_MAP_PROMPT,
        ]

        try:
            response = client.models.generate_content(
                model=GEMINI_VISION_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["image", "text"],
                ),
            )

            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    img = Image.open(io.BytesIO(part.inline_data.data)).convert("L")
                    img = img.resize((PRIORITY_MAP_WIDTH, PRIORITY_MAP_HEIGHT), Image.NEAREST)
                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                    return buf.getvalue()
        except Exception as e:
            _log(f"Priority map derivation error: {e}")

        return None

    def _save_priority_map(self, room_id, image_bytes):
        path = self._cache_dir / "priority_maps" / f"{room_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(image_bytes)
        return path

    def _save_background(self, room_id, image_bytes):
        path = self._cache_dir / "rooms" / f"{room_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(image_bytes)
        return path
