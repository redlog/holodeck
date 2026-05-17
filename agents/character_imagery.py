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
    "front": "facing directly TOWARD the camera (front view, full face visible, looking at the viewer)",
    "back": "facing directly AWAY from the camera (back of head and back of body visible, NO face shown)",
    "side": "in strict side profile facing to the LEFT (only the left side of the body visible)",
}

# 4-frame walk cycle pose descriptions, per view
WALK_POSES = {
    "front": [
        "stepping with the RIGHT foot forward, LEFT arm swinging forward to balance, RIGHT arm back. Mid-stride pose.",
        "legs passing through the center together, body weight balanced, arms close to sides. Neutral mid-step.",
        "stepping with the LEFT foot forward, RIGHT arm swinging forward to balance, LEFT arm back. Mid-stride pose.",
        "legs passing through the center together, body weight balanced, arms close to sides. Neutral mid-step.",
    ],
    "back": [
        "stepping with the RIGHT foot forward (visible at right side of body), LEFT arm swinging forward, RIGHT arm back. Seen from behind.",
        "legs passing through center together, arms close to sides. Seen from behind.",
        "stepping with the LEFT foot forward (visible at left side of body), RIGHT arm swinging forward, LEFT arm back. Seen from behind.",
        "legs passing through center together, arms close to sides. Seen from behind.",
    ],
    "side": [
        "FAR leg (rear) striding forward, NEAR leg planted back, FAR arm forward, NEAR arm back. Side profile facing left.",
        "legs together passing through center, arms close to sides. Side profile facing left.",
        "NEAR leg (front) striding forward, FAR leg planted back, NEAR arm forward, FAR arm back. Side profile facing left.",
        "legs together passing through center, arms close to sides. Side profile facing left.",
    ],
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

            # Phase 1: Generate key poses (front first, then back/side cascade off front
            # so clothing/hair/colors stay consistent across views)
            _log(f"[{char_id}] Phase 1: Generating key poses...")
            poses = {}

            _log(f"[{char_id}]   Generating front pose (no reference)...")
            front_bytes = self._generate_key_pose("front", description, visual_style, reference_pose=None)
            if not front_bytes:
                self._result_queue.put(("error", char_id, "Failed to generate front pose"))
                return
            poses["front"] = front_bytes
            _log(f"[{char_id}]   front pose OK")

            for view in ["back", "side"]:
                _log(f"[{char_id}]   Generating {view} pose (referencing front)...")
                pose_bytes = self._generate_key_pose(view, description, visual_style, reference_pose=front_bytes)
                if not pose_bytes:
                    self._result_queue.put(("error", char_id, f"Failed to generate {view} pose"))
                    return
                poses[view] = pose_bytes
                _log(f"[{char_id}]   {view} pose OK")

            # Phase 2: Generate walk frames as a single strip per view (4 frames each)
            _log(f"[{char_id}] Phase 2: Generating walk strips...")
            strips = {}
            for view in ["front", "back", "side"]:
                _log(f"[{char_id}]   Generating {view} walk strip (all {SPRITE_WALK_FRAMES} frames)...")
                strip_bytes = self._generate_walk_strip(
                    view, description, visual_style, poses[view]
                )
                if not strip_bytes:
                    self._result_queue.put(("error", char_id, f"Failed to generate {view} walk strip"))
                    return
                view_frames = self._slice_strip(strip_bytes, f"{char_id}_{view}")
                strips[view] = view_frames
                _log(f"[{char_id}]   {view} strip sliced into {len(view_frames)} frames")

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

    def _generate_key_pose(self, view, description, visual_style, reference_pose=None):
        if reference_pose is not None:
            # Cascade off the front pose so the same person (clothes, hair, colors,
            # build, accessories) appears in every view
            prompt = (
                f"{visual_style}. "
                f"The reference image shows the EXACT character — same clothing, same colors, "
                f"same hair, same build, same accessories. Generate the SAME character "
                f"standing in a neutral idle pose, but now {VIEW_DESCRIPTIONS[view]}. "
                f"\n\nThis is the same person from a different camera angle — every detail "
                f"of their wardrobe and appearance must match the reference exactly: "
                f"same shirt color and style, same pants/skirt color and style, same shoes, "
                f"same hair color and length, same skin tone, same body proportions, same height. "
                f"Only the camera angle changes.\n\n"
                f"FULL BODY must be visible — top of head to bottom of feet. Do not crop. "
                f"Leave a margin around the character. Center the character in the image. "
                f"The ENTIRE background must be solid pure magenta (#FF00FF). "
                f"No shadows, no floor, no gradients, no other colors, no other characters, "
                f"no objects, no text, no labels."
            )
            return self._call_image(prompt, reference_images=[reference_pose], aspect_ratio="9:16")

        # No reference yet — establish the canonical look from text alone
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
        pose_lines = "\n".join(
            f"  Frame {i + 1} (column {i + 1}): {WALK_POSES[view][i]}"
            for i in range(SPRITE_WALK_FRAMES)
        )
        prompt = (
            f"{visual_style}. "
            f"Using the reference image as the EXACT character design, generate a HORIZONTAL STRIP "
            f"containing exactly {SPRITE_WALK_FRAMES} frames of a walking animation, laid out "
            f"LEFT to RIGHT in a single row. Each frame shows the SAME character in a different "
            f"walk-cycle pose.\n\n"
            f"The character must be IDENTICAL to the reference image in every frame — "
            f"same face, same hair, same clothing, same colors, same proportions, same height.\n\n"
            f"View orientation for ALL frames: the character is {VIEW_DESCRIPTIONS[view]}. "
            f"Every frame uses this SAME camera angle — do NOT vary it.\n\n"
            f"The {SPRITE_WALK_FRAMES} poses from left to right:\n{pose_lines}\n\n"
            f"LAYOUT RULES:\n"
            f"- Exactly {SPRITE_WALK_FRAMES} figures side by side in one row, evenly spaced\n"
            f"- Each figure shows FULL BODY — top of head to bottom of feet, not cropped\n"
            f"- All figures stand on the same invisible ground line (feet aligned)\n"
            f"- Figures do NOT overlap — clear separation between each\n"
            f"- The ENTIRE background is solid pure magenta (#FF00FF, RGB 255,0,255)\n"
            f"- No shadows, no floor, no gradients, no other colors in the background\n"
            f"- No text, no labels, no borders, no frame dividers, no numbers"
        )
        return self._call_image(prompt, reference_images=[reference_pose], aspect_ratio="16:9")

    def _slice_strip(self, strip_bytes, debug_name=None):
        img = Image.open(io.BytesIO(strip_bytes)).convert("RGBA")
        arr = np.array(img)
        h, w = arr.shape[:2]

        debug_dir = self._cache_dir / "_debug"
        if debug_name:
            debug_dir.mkdir(parents=True, exist_ok=True)
            img.save(debug_dir / f"raw_strip_{debug_name}.png", "PNG")

        # Sample background color from the FULL strip corners (reliable, figures won't be here)
        corner = max(4, min(20, min(h, w) // 20))
        corner_pixels = np.concatenate([
            arr[:corner, :corner, :3].reshape(-1, 3),
            arr[:corner, -corner:, :3].reshape(-1, 3),
            arr[-corner:, :corner, :3].reshape(-1, 3),
            arr[-corner:, -corner:, :3].reshape(-1, 3),
        ])
        bg_color = np.median(corner_pixels, axis=0).astype(int)

        col_w = w // SPRITE_WALK_FRAMES
        frames = []
        for i in range(SPRITE_WALK_FRAMES):
            x0 = i * col_w
            x1 = x0 + col_w if i < SPRITE_WALK_FRAMES - 1 else w
            col_img = img.crop((x0, 0, x1, img.height))

            col_bytes = io.BytesIO()
            col_img.save(col_bytes, "PNG")
            processed = self._process_frame(col_bytes.getvalue(), f"{debug_name}_{i}" if debug_name else None, bg_override=bg_color)
            if not processed:
                processed = Image.new("RGBA", (SPRITE_FRAME_WIDTH, SPRITE_FRAME_HEIGHT), (0, 0, 0, 0))
            frames.append(processed)
        return frames

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

    def _process_frame(self, image_bytes, debug_name=None, bg_override=None):
        """Chroma key a single full-figure image, tight-crop, scale to frame size."""
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        arr = np.array(img)
        h, w = arr.shape[:2]

        debug_dir = self._cache_dir / "_debug"
        if debug_name:
            debug_dir.mkdir(parents=True, exist_ok=True)
            img.save(debug_dir / f"raw_{debug_name}.png", "PNG")

        if bg_override is not None:
            bg = np.asarray(bg_override, dtype=int)
        else:
            corner = max(4, min(20, min(h, w) // 20))
            corner_pixels = np.concatenate([
                arr[:corner, :corner, :3].reshape(-1, 3),
                arr[:corner, -corner:, :3].reshape(-1, 3),
                arr[-corner:, :corner, :3].reshape(-1, 3),
                arr[-corner:, -corner:, :3].reshape(-1, 3),
            ])
            bg = np.median(corner_pixels, axis=0).astype(int)

        # Chroma key — distance from sampled bg
        r = arr[:, :, 0].astype(int)
        g = arr[:, :, 1].astype(int)
        b = arr[:, :, 2].astype(int)
        diff_sq = (r - bg[0])**2 + (g - bg[1])**2 + (b - bg[2])**2
        mask = diff_sq < (80 * 80)
        arr[mask, 3] = 0

        keyed = Image.fromarray(arr, "RGBA")
        if debug_name:
            keyed.save(debug_dir / f"keyed_{debug_name}.png", "PNG")

        bbox = keyed.getbbox()
        if not bbox:
            _log(f"  No content after chroma key for {debug_name}")
            return None

        cropped = keyed.crop(bbox)

        target_w, target_h = SPRITE_FRAME_WIDTH, SPRITE_FRAME_HEIGHT
        scale = min(target_w / cropped.width, target_h / cropped.height)
        new_w = max(1, int(cropped.width * scale))
        new_h = max(1, int(cropped.height * scale))
        resized = cropped.resize((new_w, new_h), Image.LANCZOS)

        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
        # Bottom-center anchor so feet line up across all frames
        canvas.paste(resized, ((target_w - new_w) // 2, target_h - new_h), resized)

        if debug_name:
            canvas.save(debug_dir / f"final_{debug_name}.png", "PNG")

        return canvas

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
