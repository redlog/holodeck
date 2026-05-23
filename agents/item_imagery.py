"""Item sprite generation.

An item description -> a 64x64 sprite PNG cached on disk.
One sprite per inventory item, generated when the item is first acquired.
"""

import io
import sys
from pathlib import Path

from PIL import Image

from agents.base import BaseAgent
from agents.prompts import ITEM_SPRITE_TEMPLATE
from config import GEMINI_IMAGE_MODEL


def _log(msg):
    print(f"[ITEMIMG] {msg}", file=sys.stderr, flush=True)


SPRITE_SIZE = (64, 64)


class ItemImageryAgent(BaseAgent):
    def __init__(self, cache_dir):
        super().__init__(model=GEMINI_IMAGE_MODEL, temperature=0.8)
        self._cache_dir = Path(cache_dir)
        self._pending = {}

    @property
    def pending(self):
        return bool(self._pending)

    def is_pending(self, item_id):
        return item_id in self._pending

    def generate_sprite(self, item_id, item_entry, visual_style):
        if item_id in self._pending:
            return
        self._pending[item_id] = True
        _log(f"Starting sprite generation for '{item_id}'")
        self._run_threaded(self._pipeline, item_id, item_entry, visual_style)

    def _pipeline(self, item_id, item_entry, visual_style):
        try:
            description = (
                item_entry.get("visual_description")
                or item_entry.get("item", "a mysterious object")
            )
            _log(f"[{item_id}] painting sprite...")
            sprite_bytes = self._generate_sprite(description, visual_style, item_id)
            if not sprite_bytes:
                self._result_queue.put(("error", item_id, "Failed to generate sprite"))
                return

            sprite_path = self._save_sprite(item_id, sprite_bytes)
            self._result_queue.put(("item_complete", item_id, {
                "sprite_path": str(sprite_path),
            }))
            _log(f"[{item_id}] sprite saved -> {sprite_path}")

        except Exception as e:
            _log(f"[{item_id}] Pipeline error: {e}")
            self._result_queue.put(("error", item_id, str(e)))
        finally:
            self._pending.pop(item_id, None)

    def _generate_sprite(self, description, visual_style, item_id=""):
        prompt = ITEM_SPRITE_TEMPLATE.format(
            visual_style=visual_style or "painterly adventure-game art",
            description=description,
        )
        return self._call_image(prompt, aspect_ratio="1:1", context=f"item:{item_id}")

    def _save_sprite(self, item_id, image_bytes):
        path = self._cache_dir / "items" / f"{item_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)

        img = Image.open(io.BytesIO(image_bytes))
        img = img.resize(SPRITE_SIZE, Image.LANCZOS)
        img = img.convert("RGBA")
        img.save(path, "PNG")
        return path
