"""Character portrait generation.

A character description -> a 256x256 portrait PNG cached on disk.
No more sprite sheets, no walk frames, no consistency check against a sprite —
just a single portrait per character (player and NPCs alike).
"""

import io
import sys
from pathlib import Path

from PIL import Image

from agents.base import BaseAgent
from agents.prompts import PORTRAIT_TEMPLATE
from config import GEMINI_IMAGE_MODEL


def _log(msg):
    print(f"[CHARIMG] {msg}", file=sys.stderr, flush=True)


PORTRAIT_SIZE = (256, 256)


class CharacterImageryAgent(BaseAgent):
    def __init__(self, cache_dir):
        super().__init__(model=GEMINI_IMAGE_MODEL, temperature=0.8)
        self._cache_dir = Path(cache_dir)
        self._pending = {}

    @property
    def pending(self):
        return bool(self._pending)

    def generate_portrait(self, char_id, char_def, visual_style):
        if char_id in self._pending:
            return
        self._pending[char_id] = True
        _log(f"Starting portrait generation for '{char_id}'")
        self._run_threaded(self._pipeline, char_id, char_def, visual_style)

    def _pipeline(self, char_id, char_def, visual_style):
        try:
            description = char_def.get("description", char_def.get("name", "a character"))
            name = char_def.get("name", char_id)

            _log(f"[{char_id}] painting portrait of {name}...")
            portrait_bytes = self._generate_portrait(description, visual_style, char_id)
            if not portrait_bytes:
                self._result_queue.put(("error", char_id, "Failed to generate portrait"))
                return

            portrait_path = self._save_portrait(char_id, portrait_bytes)
            self._result_queue.put(("portrait_complete", char_id, {
                "portrait_path": str(portrait_path),
            }))
            _log(f"[{char_id}] portrait saved -> {portrait_path}")

        except Exception as e:
            _log(f"[{char_id}] Pipeline error: {e}")
            self._result_queue.put(("error", char_id, str(e)))
        finally:
            self._pending.pop(char_id, None)

    def _generate_portrait(self, description, visual_style, char_id=""):
        prompt = PORTRAIT_TEMPLATE.format(
            visual_style=visual_style or "painterly adventure-game art",
            description=description,
        )
        return self._call_image(prompt, aspect_ratio="1:1", context=f"portrait:{char_id}")

    def _save_portrait(self, char_id, image_bytes):
        path = self._cache_dir / "portraits" / f"{char_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)

        img = Image.open(io.BytesIO(image_bytes))
        img = img.resize(PORTRAIT_SIZE, Image.LANCZOS)
        img = img.convert("RGB")
        img.save(path, "PNG")
        return path
