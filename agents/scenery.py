import io
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


PRIORITY_MAP_PROMPT_TEMPLATE = """Generate a {width}x{height} GRAYSCALE image representing a Sierra-style priority map for an adventure game room.

This is a TECHNICAL depth/walkability map, NOT artwork. Use ONLY shades of gray:

- Pure black (0): Impassable areas — walls, sky, ceiling, solid furniture tops
- Dark gray (bands 1-3, pixel values 16-63): Background scenery behind walkable areas — wall decorations, distant objects
- Medium gray graduated by depth (bands 4-14, pixel values 64-239): Walkable ground. DARKER gray = further from viewer (top of walkable area). LIGHTER gray = closer to viewer (bottom of walkable area). The gradient should follow the floor's depth perspective.
- Pure white (band 15, pixel value 240-255): Foreground elements that should render IN FRONT of characters — overhanging branches, awnings, railings in the foreground

CRITICAL RULES:
- The walkable area must be a CONTINUOUS region (no isolated walkable pixels)
- Walkable areas should include paths, corridors, and open floor spaces
- Objects ON the floor (tables, chairs, barrels) should be black (impassable) at their base footprint
- The depth gradient goes from dark (top/far) to light (bottom/near) within the walkable zone
- Exit locations should have walkable corridors leading to the edges of the image
- Make paths interesting — winding, varied width, going around obstacles — NOT just a flat rectangle

Room description: {description}

Exits: {exits}

Key features to account for: {features}
"""

BACKGROUND_PROMPT_TEMPLATE = """{visual_style}.
Game background scene for a point-and-click adventure game.
The attached grayscale reference image is a SPATIAL LAYOUT MAP — use it to understand WHERE things go:
- Black areas in the map = walls, ceiling, sky, or solid objects (paint scenery here)
- Gray gradient areas = walkable floor/ground (paint traversable ground here, with depth perspective matching the gradient)
- White areas = foreground elements that should overlap characters (paint overhanging objects here)

Paint a natural, detailed scene following this spatial structure. Do NOT include any characters or people.
Seen from a slight three-quarter overhead perspective.

CRITICAL SCALE: A standing adult would be approximately 96 pixels tall ({height_pct}% of image height). Size all objects accordingly.

Scene description: {description}

NO text, NO labels, NO UI elements, NO debug graphics. Just a beautiful painted game scene.
"""

CORRECTION_PROMPT = """I'm providing two images:
1. A painted game background scene
2. The original priority map that guided the painting

The priority map may not perfectly match what was actually painted. I need you to generate a CORRECTED priority map (640x400 grayscale) that matches the ACTUAL painted scene:

- Areas that are clearly walls/ceiling/solid objects in the painting → black (0)
- Areas that are clearly walkable floor/ground → medium gray gradient (darker at top/far, lighter at bottom/near, pixel values 64-239)
- Background decorations behind walkable areas → dark gray (16-63)
- Foreground objects that would overlap a character → white (240-255)

Look at the actual painting carefully. If a path curves differently than the original map, follow the painting. If furniture is positioned slightly differently, match the painting.

Output ONLY a 640x400 grayscale image. No text.
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
            # Step 1: Generate priority map
            _log(f"[{room_id}] Step 1: Generating priority map...")
            priority_map_bytes = self._generate_priority_map(room_def)
            if not priority_map_bytes:
                self._result_queue.put(("error", room_id, "Failed to generate priority map"))
                return
            priority_map_path = self._save_priority_map(room_id, priority_map_bytes)
            _log(f"[{room_id}] Priority map saved to {priority_map_path}")

            # Step 2: Generate painted background
            _log(f"[{room_id}] Step 2: Generating background artwork...")
            background_bytes = self._generate_background(room_def, visual_style, priority_map_bytes)
            if not background_bytes:
                self._result_queue.put(("error", room_id, "Failed to generate background"))
                return
            background_path = self._save_background(room_id, background_bytes)
            _log(f"[{room_id}] Background saved to {background_path}")

            # Step 3: Correction pass
            _log(f"[{room_id}] Step 3: Running correction pass...")
            corrected_bytes = self._correct_priority_map(priority_map_bytes, background_bytes, room_def)
            if corrected_bytes:
                self._save_priority_map(room_id, corrected_bytes)
                _log(f"[{room_id}] Corrected priority map saved")
            else:
                _log(f"[{room_id}] Correction pass failed, keeping original priority map")

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

    def _generate_priority_map(self, room_def):
        description = room_def.get("description", "")
        exits = room_def.get("exits", {})
        exit_descriptions = []
        for direction, target in exits.items():
            if target:
                exit_descriptions.append(f"{direction} exit (leads to {target})")

        features = []
        for obj in room_def.get("objects_present", []):
            if isinstance(obj, str):
                features.append(obj)
            elif isinstance(obj, dict):
                features.append(obj.get("name", obj.get("id", "")))

        prompt = PRIORITY_MAP_PROMPT_TEMPLATE.format(
            width=PRIORITY_MAP_WIDTH,
            height=PRIORITY_MAP_HEIGHT,
            description=description,
            exits=", ".join(exit_descriptions) if exit_descriptions else "none specified",
            features=", ".join(features) if features else "standard room furnishings as described",
        )

        image_bytes = self._call_image(prompt, aspect_ratio="16:10")
        if not image_bytes:
            return None

        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        img = img.resize((PRIORITY_MAP_WIDTH, PRIORITY_MAP_HEIGHT), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def _generate_background(self, room_def, visual_style, priority_map_bytes):
        description = room_def.get("description", "")
        height_pct = int(96 * 100 / INTERNAL_HEIGHT)

        prompt = BACKGROUND_PROMPT_TEMPLATE.format(
            visual_style=visual_style or "Painterly adventure game art",
            description=description,
            height_pct=height_pct,
        )

        image_bytes = self._call_image(
            prompt,
            reference_images=[priority_map_bytes],
            aspect_ratio="16:10",
        )
        if not image_bytes:
            return None

        img = Image.open(io.BytesIO(image_bytes))
        img = img.resize((INTERNAL_WIDTH, INTERNAL_HEIGHT), Image.LANCZOS)
        img = img.quantize(256).convert("RGB")

        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    def _correct_priority_map(self, priority_map_bytes, background_bytes, room_def):
        from google import genai
        from google.genai import types
        import os

        api_key = os.getenv("GEMINI_API_KEY")
        vision_client = genai.Client(api_key=api_key)

        contents = [
            types.Part.from_bytes(data=background_bytes, mime_type="image/png"),
            types.Part.from_bytes(data=priority_map_bytes, mime_type="image/png"),
            CORRECTION_PROMPT,
        ]

        try:
            response = vision_client.models.generate_content(
                model=GEMINI_VISION_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["image", "text"],
                ),
            )

            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    img = Image.open(io.BytesIO(part.inline_data.data)).convert("L")
                    img = img.resize((PRIORITY_MAP_WIDTH, PRIORITY_MAP_HEIGHT), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                    return buf.getvalue()
        except Exception as e:
            _log(f"Correction pass error: {e}")

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
