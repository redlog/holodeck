import io
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from agents.base import BaseAgent
from config import (
    GEMINI_CHARACTER_MODEL, GEMINI_VISION_MODEL,
    SPRITE_FRAME_WIDTH, SPRITE_FRAME_HEIGHT, SPRITE_WALK_FRAMES,
    SPRITE_KEY_POSE_SIZE, CHARACTER_CONSISTENCY_CHECK,
)


def _log(msg):
    print(f"[CHARIMG] {msg}", file=sys.stderr, flush=True)


VIEW_DESCRIPTIONS = {
    "front": "facing directly TOWARD the camera (front view, full face visible)",
    "back": "facing directly AWAY from the camera (back of head and body visible, NO face)",
    "side": "in strict side profile facing to the LEFT (only one side of body visible)",
}


class CharacterImageryAgent(BaseAgent):
    def __init__(self, cache_dir):
        super().__init__(model=GEMINI_CHARACTER_MODEL, temperature=0.8)
        self._cache_dir = Path(cache_dir)
        self._pending = {}

    def generate_character(self, char_id, char_def, visual_style):
        if char_id in self._pending:
            return
        self._pending[char_id] = True
        _log(f"Starting character imagery pipeline for '{char_id}'")
        self._run_threaded(self._pipeline, char_id, char_def, visual_style)

    def _pipeline(self, char_id, char_def, visual_style):
        try:
            description = char_def.get("description", char_def.get("name", "a character"))

            # Phase 1: Generate key poses
            _log(f"[{char_id}] Phase 1: Generating key poses...")
            poses = {}
            for view in ["front", "back", "side"]:
                _log(f"[{char_id}]   Generating {view} pose...")
                pose_bytes = self._generate_key_pose(view, description, visual_style)
                if not pose_bytes:
                    self._result_queue.put(("error", char_id, f"Failed to generate {view} pose"))
                    return
                poses[view] = pose_bytes
                _log(f"[{char_id}]   {view} pose OK")

            # Phase 2: Generate walk strips using poses as references
            _log(f"[{char_id}] Phase 2: Generating walk strips...")
            strips = {}
            for view in ["front", "back", "side"]:
                _log(f"[{char_id}]   Generating {view} walk strip...")
                strip_bytes = self._generate_walk_strip(view, description, visual_style, poses[view])
                if not strip_bytes:
                    self._result_queue.put(("error", char_id, f"Failed to generate {view} walk strip"))
                    return
                frames = self._process_strip(strip_bytes, f"{char_id}_{view}")
                if not frames:
                    self._result_queue.put(("error", char_id, f"Failed to process {view} strip"))
                    return
                strips[view] = frames
                _log(f"[{char_id}]   {view} strip processed ({len(frames)} frames)")

            # Phase 3: Generate portrait
            _log(f"[{char_id}] Phase 3: Generating portrait...")
            portrait_bytes = self._generate_portrait(description, visual_style, poses["front"])
            if not portrait_bytes:
                self._result_queue.put(("error", char_id, "Failed to generate portrait"))
                return

            # Phase 4: Consistency check
            if CHARACTER_CONSISTENCY_CHECK:
                _log(f"[{char_id}] Phase 4: Consistency check...")
                if not self._check_consistency(poses["front"], portrait_bytes):
                    _log(f"[{char_id}]   Inconsistent, regenerating portrait with stronger guidance...")
                    portrait_bytes = self._generate_portrait(
                        description, visual_style, poses["front"],
                        extra_guidance="Match the face, hair, and features from the reference image EXACTLY. Same eye color, same hairstyle, same facial structure."
                    )
                    if not portrait_bytes:
                        self._result_queue.put(("error", char_id, "Failed portrait regeneration"))
                        return

            # Save outputs
            sheet = self._composite_sheet(strips)
            sprite_path = self._save_sprite(char_id, sheet)
            portrait_path = self._save_portrait(char_id, portrait_bytes)

            self._result_queue.put(("character_complete", char_id, {
                "sprite_path": str(sprite_path),
                "portrait_path": str(portrait_path),
            }))
            _log(f"[{char_id}] Pipeline complete")

        except Exception as e:
            _log(f"[{char_id}] Pipeline error: {e}")
            self._result_queue.put(("error", char_id, str(e)))
        finally:
            self._pending.pop(char_id, None)

    def _generate_key_pose(self, view, description, visual_style):
        prompt = (
            f"{visual_style}. "
            f"A single video game character standing in a neutral idle pose, {VIEW_DESCRIPTIONS[view]}. "
            f"FULL BODY must be visible — from the top of the head to the bottom of the feet. "
            f"Do NOT crop any part of the character. Leave a margin of background around them. "
            f"The character should be centered in the image. "
            f"The ENTIRE background must be solid pure magenta (#FF00FF, RGB 255,0,255). "
            f"No gradients, no shadows on the background, no floor shadow, no other colors in the background. "
            f"No other characters, no objects, no scenery, no text, no labels. "
            f"Character: {description}"
        )
        return self._call_image(prompt, aspect_ratio="9:16")

    def _generate_walk_strip(self, view, description, visual_style, reference_pose):
        prompt = (
            f"{visual_style}. "
            f"Using the reference image as the EXACT character design (same face, same clothing, "
            f"same proportions, same colors), create a horizontal strip showing "
            f"{SPRITE_WALK_FRAMES} frames of a smooth walking animation arranged side-by-side. "
            f"The character in EVERY frame must be IDENTICAL to the reference — "
            f"only the legs and arms change to show walking motion. "
            f"The character is {VIEW_DESCRIPTIONS[view]} in every frame. "
            f"Walk cycle: contact, down, passing, up, contact, down, passing, up. "
            f"Each frame shows the COMPLETE character from head to feet. "
            f"The ENTIRE background is solid pure magenta (#FF00FF). "
            f"No text, no borders, no labels, no frame numbers, no grid lines."
        )
        return self._call_image(prompt, reference_images=[reference_pose], aspect_ratio="16:9")

    def _generate_portrait(self, description, visual_style, reference_pose, extra_guidance=""):
        guidance = extra_guidance + " " if extra_guidance else ""
        prompt = (
            f"{visual_style}. "
            f"{guidance}"
            f"Character portrait for a point-and-click adventure game. "
            f"Head and shoulders portrait based on the reference image character. "
            f"Same face, same hair, same clothing visible at the shoulders. "
            f"Facing slightly to the side, expressive face with clear features. "
            f"The background must be a single flat solid color that complements "
            f"the character's appearance (NOT magenta). "
            f"NO gradients, NO patterns, NO scenery. "
            f"NO text, NO nameplate, NO caption, NO labels. "
            f"Character: {description}"
        )
        return self._call_image(prompt, reference_images=[reference_pose], aspect_ratio="1:1")

    def _check_consistency(self, pose_bytes, portrait_bytes):
        from google import genai
        from google.genai import types

        api_key = os.getenv("GEMINI_API_KEY")
        client = genai.Client(api_key=api_key)

        contents = [
            types.Part.from_bytes(data=pose_bytes, mime_type="image/png"),
            types.Part.from_bytes(data=portrait_bytes, mime_type="image/png"),
            (
                "Compare these two images. Image 1 is a full-body character sprite. "
                "Image 2 is a portrait. Are they clearly the same character? "
                "Check: same hair color/style, same facial features, same clothing colors. "
                "Respond with JSON only: {\"consistent\": true, \"reason\": \"...\"} "
                "or {\"consistent\": false, \"reason\": \"...\"}"
            ),
        ]

        try:
            response = client.models.generate_content(
                model=GEMINI_VISION_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                ),
            )
            result = json.loads(response.text)
            consistent = result.get("consistent", True)
            _log(f"  Consistency: {consistent} — {result.get('reason', '')}")
            return consistent
        except Exception as e:
            _log(f"  Consistency check failed: {e}, assuming consistent")
            return True

    def _process_strip(self, image_bytes, debug_name=None):
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        arr = np.array(img)
        h, w = arr.shape[:2]

        debug_dir = self._cache_dir / "_debug"
        if debug_name:
            debug_dir.mkdir(parents=True, exist_ok=True)
            img.save(debug_dir / f"raw_{debug_name}.png", "PNG")

        # Sample background color from corners
        corner = max(4, min(20, min(h, w) // 20))
        corner_pixels = np.concatenate([
            arr[:corner, :corner, :3].reshape(-1, 3),
            arr[:corner, -corner:, :3].reshape(-1, 3),
            arr[-corner:, :corner, :3].reshape(-1, 3),
            arr[-corner:, -corner:, :3].reshape(-1, 3),
        ])
        bg = np.median(corner_pixels, axis=0).astype(int)

        # Chroma key
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
        target_w, target_h = SPRITE_FRAME_WIDTH, SPRITE_FRAME_HEIGHT
        slice_w = w // SPRITE_WALK_FRAMES
        frames = []
        for i in range(SPRITE_WALK_FRAMES):
            slice_img = keyed.crop((i * slice_w, 0, (i + 1) * slice_w, h))
            bbox = slice_img.getbbox()
            if not bbox:
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
            canvas.paste(resized, ((target_w - new_w) // 2, target_h - new_h), resized)
            frames.append(canvas)

        if debug_name:
            strip = Image.new("RGBA", (target_w * SPRITE_WALK_FRAMES, target_h), (0, 0, 0, 0))
            for i, f in enumerate(frames):
                strip.paste(f, (i * target_w, 0), f)
            strip.save(debug_dir / f"final_{debug_name}.png", "PNG")

        return frames

    def _composite_sheet(self, view_strips):
        fw, fh = SPRITE_FRAME_WIDTH, SPRITE_FRAME_HEIGHT
        sheet = Image.new("RGBA", (fw * SPRITE_WALK_FRAMES, fh * 3), (0, 0, 0, 0))

        for row_idx, view in enumerate(["front", "back", "side"]):
            frames = view_strips[view]
            for col, frame in enumerate(frames[:SPRITE_WALK_FRAMES]):
                sheet.paste(frame, (col * fw, row_idx * fh), frame)

        return sheet

    def _save_sprite(self, char_id, sheet):
        path = self._cache_dir / "sprites" / f"{char_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(path, "PNG")
        return path

    def _save_portrait(self, char_id, image_bytes):
        path = self._cache_dir / "portraits" / f"{char_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)

        img = Image.open(io.BytesIO(image_bytes))
        img = img.resize((128, 128), Image.LANCZOS)
        img = img.convert("RGB")
        img.save(path, "PNG")
        return path
