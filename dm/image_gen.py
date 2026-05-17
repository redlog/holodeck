import hashlib
import io
import sys
import threading
from pathlib import Path
from queue import Queue, Empty

from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv
import os


def _log(msg):
    print(f"[IMG] {msg}", file=sys.stderr, flush=True)

load_dotenv(override=True)

from config import GEMINI_IMAGE_MODEL, INTERNAL_WIDTH, INTERNAL_HEIGHT, SPRITE_FRAME_WIDTH, SPRITE_FRAME_HEIGHT, SPRITE_WALK_FRAMES


def get_cache_path(cache_dir, prompt, image_type):
    key = hashlib.md5(prompt.encode()).hexdigest()[:12]
    return cache_dir / image_type / f"{key}.png"


def process_background(raw_image_bytes):
    img = Image.open(io.BytesIO(raw_image_bytes))
    img = img.resize((INTERNAL_WIDTH, INTERNAL_HEIGHT), Image.LANCZOS)
    img = img.quantize(256)
    img = img.convert("RGB")
    return img


def build_layout_diagram(room_def):
    from PIL import ImageDraw, ImageFont
    img = Image.new("RGB", (INTERNAL_WIDTH, INTERNAL_HEIGHT), (20, 20, 30))
    draw = ImageDraw.Draw(img)

    walkable = room_def.get("walkable_zone", {})
    pct = walkable.get("value", 65) / 100
    walkable_top = int(INTERNAL_HEIGHT * (1 - pct))

    draw.rectangle([0, 0, INTERNAL_WIDTH, walkable_top], fill=(40, 30, 50))
    draw.rectangle([0, walkable_top, INTERNAL_WIDTH, INTERNAL_HEIGHT], fill=(60, 80, 60))
    draw.line([(0, walkable_top), (INTERNAL_WIDTH, walkable_top)], fill=(0, 255, 0), width=2)
    draw.text((4, walkable_top + 4), "WALKABLE FLOOR", fill=(0, 255, 0))
    draw.text((4, walkable_top - 16), "WALLS / BACKDROP", fill=(200, 150, 255))

    for obs in room_def.get("obstacles", []):
        r = obs.get("rect", {})
        x, y = r.get("x", 0), r.get("y", 0)
        w, h = r.get("width", 0), r.get("height", 0)
        draw.rectangle([x, y, x + w, y + h], fill=(180, 60, 60), outline=(255, 0, 0), width=2)
        label = obs.get("label", obs.get("id", ""))
        draw.text((x + 4, y + 4), label.upper(), fill=(255, 255, 255))

    exits = room_def.get("exits", {})
    exit_zones = room_def.get("exit_zones", {})
    for direction, target in exits.items():
        if not target:
            continue
        if direction in exit_zones:
            z = exit_zones[direction]
            zx, zy = z.get("x", 0), z.get("y", 0)
            zw, zh = z.get("width", 0), z.get("height", 0)
        elif direction == "north":
            zx, zy, zw, zh = INTERNAL_WIDTH // 4, 0, INTERNAL_WIDTH // 2, 30
        elif direction == "south":
            zx, zy, zw, zh = INTERNAL_WIDTH // 4, INTERNAL_HEIGHT - 30, INTERNAL_WIDTH // 2, 30
        elif direction == "west":
            zx, zy, zw, zh = 0, INTERNAL_HEIGHT // 4, 30, INTERNAL_HEIGHT // 2
        elif direction == "east":
            zx, zy, zw, zh = INTERNAL_WIDTH - 30, INTERNAL_HEIGHT // 4, 30, INTERNAL_HEIGHT // 2
        else:
            continue
        draw.rectangle([zx, zy, zx + zw, zy + zh], fill=(50, 50, 180), outline=(0, 150, 255), width=2)
        draw.text((zx + 4, zy + 4), f"EXIT {direction.upper()}", fill=(200, 220, 255))

    return img


class ImageGenerator:
    def __init__(self, cache_dir=None):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key or api_key == "your_key_here":
            self._client = None
        else:
            self._client = genai.Client(api_key=api_key)

        self._cache_dir = cache_dir or Path("assets/cache")
        self._result_queue = Queue()
        self._pending = {}

    @property
    def connected(self):
        return self._client is not None

    def request_portrait(self, char_id, char_def, visual_style=""):
        description = char_def.get("description", char_def.get("name", "a character"))
        cache_path = get_cache_path(self._cache_dir, f"portrait_{char_id}_{description}", "portraits")
        if cache_path.exists():
            _log(f"Cache hit for portrait '{char_id}': {cache_path}")
            self._result_queue.put(("portrait", char_id, str(cache_path)))
            return

        key = f"portrait_{char_id}"
        if key in self._pending:
            return

        self._pending[key] = True
        prompt = (
            f"{visual_style}. "
            f"Character portrait for a point-and-click adventure game. "
            f"Head and shoulders portrait, facing slightly to the side. "
            f"Expressive face with clear features. "
            f"The background must be a single flat solid color — choose a color that complements "
            f"the character's appearance and mood (e.g. deep blue for a brooding character, "
            f"warm amber for a friendly one, dark crimson for a villain). "
            f"NO gradients, NO patterns, NO scenery in the background. "
            f"NO text, NO nameplate, NO caption, NO labels, NO name, NO title bar. "
            f"Just the character's face and upper body against the solid color, nothing else. "
            f"Character: {description}"
        )
        _log(f"Generating portrait for '{char_id}'...")
        thread = threading.Thread(
            target=self._generate_portrait,
            args=(char_id, prompt, cache_path, key),
            daemon=True,
        )
        thread.start()

    def _generate_portrait(self, char_id, prompt, cache_path, pending_key):
        try:
            _log(f"Calling {GEMINI_IMAGE_MODEL} for portrait...")
            image_bytes = self._call_image_api(prompt, aspect_ratio="1:1")

            if image_bytes:
                img = Image.open(io.BytesIO(image_bytes))
                img = img.resize((128, 128), Image.LANCZOS)
                img = img.convert("RGB")
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                img.save(cache_path, "PNG")
                _log(f"Portrait saved to {cache_path}")
                self._result_queue.put(("portrait", char_id, str(cache_path)))
            else:
                _log(f"No image data in portrait response for '{char_id}'")
                self._result_queue.put(("error", char_id, "No image in portrait response"))
        except Exception as e:
            _log(f"Error generating portrait '{char_id}': {e}")
            self._result_queue.put(("error", char_id, str(e)))
        finally:
            self._pending.pop(pending_key, None)

    def request_sprite(self, char_id, char_def, visual_style=""):
        description = char_def.get("description", char_def.get("name", "a character"))
        cache_path = get_cache_path(self._cache_dir, f"sprite_v2_{char_id}_{description}", "sprites")
        if cache_path.exists():
            _log(f"Cache hit for sprite '{char_id}': {cache_path}")
            self._result_queue.put(("sprite", char_id, str(cache_path)))
            return

        key = f"sprite_{char_id}"
        if key in self._pending:
            return

        self._pending[key] = True
        _log(f"Generating sprite (3 views) for '{char_id}'...")
        thread = threading.Thread(
            target=self._generate_sprite_3views,
            args=(char_id, description, visual_style, cache_path, key),
            daemon=True,
        )
        thread.start()

    def _generate_sprite_3views(self, char_id, description, visual_style, cache_path, pending_key):
        try:
            views = ["front", "back", "side"]
            view_strips = {}

            for view in views:
                _log(f"  Generating {view} walk strip for '{char_id}'...")
                prompt = self._build_sprite_view_prompt(view, description, visual_style)
                image_bytes = self._call_image_api(prompt, aspect_ratio="16:9")
                if not image_bytes:
                    _log(f"  No image data for {view} strip of '{char_id}'")
                    self._result_queue.put(("error", char_id, f"No image data for {view}"))
                    return
                frames = self._process_sprite_strip(image_bytes, debug_name=f"{char_id}_{view}")
                if frames is None:
                    _log(f"  Failed to process {view} strip of '{char_id}'")
                    self._result_queue.put(("error", char_id, f"Process failed for {view}"))
                    return
                view_strips[view] = frames
                _log(f"  {view} strip processed OK ({len(frames)} frames)")

            sheet = self._composite_sprite_sheet(view_strips)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            sheet.save(cache_path, "PNG")
            _log(f"Sprite sheet saved to {cache_path}")
            self._result_queue.put(("sprite", char_id, str(cache_path)))
        except Exception as e:
            _log(f"Error generating sprite '{char_id}': {e}")
            self._result_queue.put(("error", char_id, str(e)))
        finally:
            self._pending.pop(pending_key, None)

    def _build_sprite_view_prompt(self, view, description, visual_style):
        view_text = {
            "front": "facing directly TOWARD the camera (front view, full face visible)",
            "back": "facing directly AWAY from the camera (back of head and back of body visible, NO face visible)",
            "side": "in strict side profile facing to the LEFT (only one side of the body visible)",
        }[view]
        return (
            f"{visual_style}. "
            f"A horizontal sprite strip showing {SPRITE_WALK_FRAMES} frames of a smooth walking animation, "
            f"arranged side-by-side in a single row with equal spacing. "
            f"In every frame the character is {view_text}. "
            f"CRITICAL: each frame must show the COMPLETE character from the top of the head "
            f"all the way down to the bottom of the FEET — do not crop the head, hands, or feet. "
            f"Leave a small margin of background around the character in each frame. "
            f"The {SPRITE_WALK_FRAMES} frames show a complete walk cycle in equal increments: "
            f"contact pose (left foot just landed forward), down pose (weight shifting), "
            f"passing pose (legs cross), high pose (right leg lifting), "
            f"contact pose (right foot just landed forward), down pose, "
            f"passing pose (legs cross other way), high pose (left leg lifting). "
            f"Each successive frame is a small progressive change from the previous one — "
            f"the motion should be SMOOTH when played in sequence. "
            f"The SAME character appears in every frame — same face, same clothing, same proportions, same height. "
            f"Only the legs and arms change between frames to show walking motion. "
            f"The ENTIRE background is solid pure magenta (#FF00FF, RGB 255,0,255) — "
            f"uniform color with NO gradients, NO shadows, NO texture, NO other colors anywhere, "
            f"including between the frames. "
            f"No other characters, no objects, no scenery, no text, no labels, no frame borders, no grid lines. "
            f"Character: {description}"
        )

    def _call_image_api(self, prompt, reference_image_bytes=None, aspect_ratio="1:1"):
        """Call the configured image model. Returns image bytes or None."""
        if GEMINI_IMAGE_MODEL.startswith("imagen"):
            # Imagen API — no reference image support, separate method
            response = self._client.models.generate_images(
                model=GEMINI_IMAGE_MODEL,
                prompt=prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio=aspect_ratio,
                ),
            )
            if response.generated_images:
                return response.generated_images[0].image.image_bytes
            return None
        else:
            # Gemini multimodal image generation — supports reference images
            contents = []
            if reference_image_bytes:
                contents.append(types.Part.from_bytes(data=reference_image_bytes, mime_type="image/png"))
            contents.append(prompt)
            response = self._client.models.generate_content(
                model=GEMINI_IMAGE_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["image", "text"],
                ),
            )
            for part in response.candidates[0].content.parts:
                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    return part.inline_data.data
            return None

    def _process_sprite_strip(self, image_bytes, debug_name=None):
        """Take a 4-frame walk strip, chroma key whole image, slice into 4 columns,
        tight-crop each frame, and return a list of 4 frame-sized PIL images."""
        import numpy as np
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

        debug_dir = self._cache_dir / "_debug"
        if debug_name:
            debug_dir.mkdir(exist_ok=True)
            img.save(debug_dir / f"raw_{debug_name}.png", "PNG")

        arr = np.array(img)
        h, w = arr.shape[:2]

        # Sample bg color from all 4 corners
        corner = max(4, min(20, min(h, w) // 20))
        corner_pixels = np.concatenate([
            arr[:corner, :corner, :3].reshape(-1, 3),
            arr[:corner, -corner:, :3].reshape(-1, 3),
            arr[-corner:, :corner, :3].reshape(-1, 3),
            arr[-corner:, -corner:, :3].reshape(-1, 3),
        ])
        bg = np.median(corner_pixels, axis=0).astype(int)
        _log(f"  bg color sampled: RGB({bg[0]}, {bg[1]}, {bg[2]})")

        r = arr[:, :, 0].astype(int)
        g = arr[:, :, 1].astype(int)
        b = arr[:, :, 2].astype(int)
        diff_sq = (r - bg[0])**2 + (g - bg[1])**2 + (b - bg[2])**2
        mask = diff_sq < (80 * 80)
        arr[mask, 3] = 0

        keyed = Image.fromarray(arr, "RGBA")
        if debug_name:
            keyed.save(debug_dir / f"keyed_{debug_name}.png", "PNG")

        # Slice into N equal columns
        slice_w = w // SPRITE_WALK_FRAMES
        target_w, target_h = SPRITE_FRAME_WIDTH, SPRITE_FRAME_HEIGHT
        frames = []
        for i in range(SPRITE_WALK_FRAMES):
            slice_img = keyed.crop((i * slice_w, 0, (i + 1) * slice_w, h))
            bbox = slice_img.getbbox()
            if not bbox:
                _log(f"  Warning: frame {i} has no content; using prev frame")
                if frames:
                    frames.append(frames[-1].copy())
                else:
                    frames.append(Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0)))
                continue
            cropped = slice_img.crop(bbox)

            scale = min(target_w / cropped.width, target_h / cropped.height)
            new_w = max(1, int(cropped.width * scale))
            new_h = max(1, int(cropped.height * scale))
            resized = cropped.resize((new_w, new_h), Image.LANCZOS)

            canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
            # Bottom-center anchor so feet line up
            canvas.paste(resized, ((target_w - new_w) // 2, target_h - new_h), resized)
            frames.append(canvas)

        if debug_name:
            strip = Image.new("RGBA", (target_w * SPRITE_WALK_FRAMES, target_h), (0, 0, 0, 0))
            for i, f in enumerate(frames):
                strip.paste(f, (i * target_w, 0), f)
            strip.save(debug_dir / f"final_{debug_name}.png", "PNG")

        return frames

    def _process_sprite_image(self, image_bytes, debug_name=None):
        """Auto-detect bg from corners, chroma key, tight crop, scale to frame size."""
        import numpy as np
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

        debug_dir = self._cache_dir / "_debug"
        if debug_name:
            debug_dir.mkdir(exist_ok=True)
            img.save(debug_dir / f"raw_{debug_name}.png", "PNG")

        arr = np.array(img)
        h, w = arr.shape[:2]

        # Sample background from corners
        corner = max(4, min(20, min(h, w) // 20))
        corner_pixels = np.concatenate([
            arr[:corner, :corner, :3].reshape(-1, 3),
            arr[:corner, -corner:, :3].reshape(-1, 3),
            arr[-corner:, :corner, :3].reshape(-1, 3),
            arr[-corner:, -corner:, :3].reshape(-1, 3),
        ])
        bg = np.median(corner_pixels, axis=0).astype(int)
        _log(f"  bg color sampled: RGB({bg[0]}, {bg[1]}, {bg[2]})")

        # Distance-based chroma key
        r = arr[:, :, 0].astype(int)
        g = arr[:, :, 1].astype(int)
        b = arr[:, :, 2].astype(int)
        diff_sq = (r - bg[0])**2 + (g - bg[1])**2 + (b - bg[2])**2

        threshold = 80  # RGB distance; ~80 catches anti-aliasing edges
        mask = diff_sq < (threshold * threshold)
        arr[mask, 3] = 0

        keyed = Image.fromarray(arr, "RGBA")
        if debug_name:
            keyed.save(debug_dir / f"keyed_{debug_name}.png", "PNG")

        bbox = keyed.getbbox()
        if not bbox:
            _log(f"  Warning: no non-transparent pixels after chroma key")
            return None
        cropped = keyed.crop(bbox)

        target_w, target_h = SPRITE_FRAME_WIDTH, SPRITE_FRAME_HEIGHT
        scale = min(target_w / cropped.width, target_h / cropped.height)
        new_w = max(1, int(cropped.width * scale))
        new_h = max(1, int(cropped.height * scale))
        resized = cropped.resize((new_w, new_h), Image.LANCZOS)

        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        canvas.paste(resized, ((target_w - new_w) // 2, target_h - new_h), resized)
        if debug_name:
            canvas.save(debug_dir / f"final_{debug_name}.png", "PNG")
        return canvas

    def _composite_sprite_sheet(self, view_strips):
        """Combine front/back/side walk strips into an N x 3 sprite sheet
        where N = SPRITE_WALK_FRAMES.
        Row 0: south (front), Row 1: north (back), Row 2: west (side facing left).
        """
        fw, fh = SPRITE_FRAME_WIDTH, SPRITE_FRAME_HEIGHT
        sheet = Image.new("RGBA", (fw * SPRITE_WALK_FRAMES, fh * 3), (0, 0, 0, 0))

        for row_idx, view in enumerate(["front", "back", "side"]):
            frames = view_strips[view]
            for col, frame in enumerate(frames[:SPRITE_WALK_FRAMES]):
                sheet.paste(frame, (col * fw, row_idx * fh), frame)

        return sheet

    def request_background(self, room_id, room_def, visual_style=""):
        prompt = room_def.get("background_prompt", room_def.get("description", ""))
        cache_path = get_cache_path(self._cache_dir, prompt, "rooms")
        if cache_path.exists():
            _log(f"Cache hit for room '{room_id}': {cache_path}")
            self._result_queue.put(("background", room_id, str(cache_path)))
            return

        if room_id in self._pending:
            return

        self._pending[room_id] = True
        full_prompt = self._build_background_prompt(room_def, visual_style)

        layout_img = build_layout_diagram(room_def)
        layout_bytes = io.BytesIO()
        layout_img.save(layout_bytes, "PNG")
        layout_bytes = layout_bytes.getvalue()

        _log(f"Generating background for room '{room_id}' (with layout diagram)...")
        _log(f"  Prompt: {full_prompt[:200]}")
        thread = threading.Thread(
            target=self._generate_background,
            args=(room_id, full_prompt, cache_path, layout_bytes),
            daemon=True,
        )
        thread.start()

    def _build_background_prompt(self, room_def, visual_style):
        scene_prompt = room_def.get("background_prompt", room_def.get("description", ""))

        walkable = room_def.get("walkable_zone", {})
        pct = walkable.get("value", 65)

        geometry_parts = [
            f"The lower {pct}% of the image is floor/ground that a character could walk on.",
            f"The upper {100 - pct}% contains walls, ceiling, and scenery backdrop.",
        ]

        exits = room_def.get("exits", {})
        for direction, target in exits.items():
            if target:
                if direction == "west":
                    geometry_parts.append("A visible doorway or opening on the left edge.")
                elif direction == "east":
                    geometry_parts.append("A visible doorway or opening on the right edge.")
                elif direction == "north":
                    geometry_parts.append("A visible doorway or passage at the back/top of the scene.")
                elif direction == "south":
                    geometry_parts.append("A visible opening at the bottom/foreground.")

        obstacles = room_def.get("obstacles", [])
        if obstacles:
            geometry_parts.append("The following objects sit ON the floor area and the player must walk around them:")
            for obs in obstacles:
                label = obs.get("label", obs.get("id", "object"))
                rect = obs.get("rect", {})
                x_pct = rect.get("x", 0) / 960 * 100
                y_pct = rect.get("y", 0) / 600 * 100
                if x_pct < 33:
                    h_pos = "on the left side"
                elif x_pct < 66:
                    h_pos = "in the center"
                else:
                    h_pos = "on the right side"
                if y_pct < 40:
                    v_pos = "toward the back"
                elif y_pct < 70:
                    v_pos = "in the middle area"
                else:
                    v_pos = "toward the front"
                geometry_parts.append(f"- {label} {h_pos}, {v_pos} of the room")

        composition = " ".join(geometry_parts)

        return (
            f"{visual_style}. "
            f"Game background scene for a point-and-click adventure game. "
            f"Empty background only — NO characters, NO people, NO figures, NO sprites. "
            f"Seen from a slight three-quarter overhead perspective. "
            f"CRITICAL SCALE RULE: A standing adult human in this scene would be approximately {SPRITE_FRAME_HEIGHT} pixels tall "
            f"(about {SPRITE_FRAME_HEIGHT * 100 // INTERNAL_HEIGHT}% of the image height). "
            f"All furniture, doorways, and objects MUST be sized consistently with this human scale. "
            f"A door should be roughly {int(SPRITE_FRAME_HEIGHT * 1.3)} pixels tall. "
            f"A table should be roughly {int(SPRITE_FRAME_HEIGHT * 0.5)} pixels tall. "
            f"A chair should be roughly {int(SPRITE_FRAME_HEIGHT * 0.55)} pixels tall. "
            f"{composition} "
            f"Scene: {scene_prompt}"
        )

    def _generate_background(self, room_id, prompt, cache_path, layout_bytes=None):
        try:
            _log(f"Calling {GEMINI_IMAGE_MODEL}...")

            # Imagen doesn't support reference images; only pass layout to Gemini multimodal models
            if GEMINI_IMAGE_MODEL.startswith("imagen"):
                ref = None
            else:
                ref = layout_bytes
                if ref:
                    prompt = (
                        "The reference image is a SPATIAL LAYOUT REFERENCE ONLY — it is NOT part of the final image. "
                        "DO NOT draw any rectangles, labels, text overlays, colored zones, or diagram elements in your output. "
                        "The output must be a natural-looking painted game scene with NO debug graphics. "
                        "Use the diagram ONLY to understand WHERE to place objects:\n"
                        "- Green area = where the floor/ground should be\n"
                        "- Purple area = where walls/ceiling/backdrop should be\n"
                        "- Red rectangles = where furniture/obstacles should be drawn as REAL objects\n"
                        "- Blue rectangles = where doorways/exits should appear\n\n"
                        + prompt
                    )

            image_bytes = self._call_image_api(prompt, reference_image_bytes=ref, aspect_ratio="16:9")

            if image_bytes:
                _log(f"Image received for '{room_id}' ({len(image_bytes)} bytes). Processing...")
                img = process_background(image_bytes)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                img.save(cache_path, "PNG")
                _log(f"Saved to {cache_path}")
                self._result_queue.put(("background", room_id, str(cache_path)))
            else:
                _log(f"No image data in response for '{room_id}'")
                self._result_queue.put(("error", room_id, "No image in response"))

        except Exception as e:
            _log(f"Error generating '{room_id}': {e}")
            self._result_queue.put(("error", room_id, str(e)))
        finally:
            self._pending.pop(room_id, None)

    def poll_result(self):
        try:
            return self._result_queue.get_nowait()
        except Empty:
            return None
