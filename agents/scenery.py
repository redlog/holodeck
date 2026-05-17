"""SceneryAgent: paints a room background image from a text prompt.

No spatial constraints — purely atmospheric backgrounds. Takes the
location's image_prompt (which the DM wrote during the creation phase)
and renders a 960x600 PNG.
"""

import io
import sys
from pathlib import Path

from PIL import Image

from agents.base import BaseAgent
from config import GEMINI_IMAGE_MODEL, INTERNAL_WIDTH, INTERNAL_HEIGHT


def _log(msg):
    print(f"[SCENERY] {msg}", file=sys.stderr, flush=True)


BACKGROUND_PROMPT_TEMPLATE = """{visual_style}.

A painted background for a graphical text adventure game. Paint this scene:

{scene}

The image is 16:10 widescreen. No characters or people unless the description explicitly says so. No text, labels, UI elements, borders, or watermarks. Treat the camera as a fixed three-quarter overhead view typical of point-and-click adventure games.

Render the entire frame with care — every region should be finished painted artwork.
"""


class SceneryAgent(BaseAgent):
    def __init__(self, cache_dir):
        super().__init__(model=GEMINI_IMAGE_MODEL, temperature=0.7)
        self._cache_dir = Path(cache_dir)
        self._pending = {}

    @property
    def pending(self):
        return bool(self._pending)

    def generate_room(self, location_id, location_def, visual_style):
        if location_id in self._pending:
            return
        self._pending[location_id] = True
        _log(f"Starting room paint for '{location_id}'")
        self._run_threaded(self._pipeline, location_id, location_def, visual_style)

    def _pipeline(self, location_id, location_def, visual_style):
        try:
            scene = (location_def.get("image_prompt")
                     or location_def.get("summary")
                     or location_def.get("name", "an empty room"))

            _log(f"[{location_id}] painting scene...")
            prompt = BACKGROUND_PROMPT_TEMPLATE.format(
                visual_style=visual_style or "painterly adventure-game art",
                scene=scene,
            )
            image_bytes = self._call_image(prompt, aspect_ratio="16:10")
            if not image_bytes:
                self._result_queue.put(("error", location_id, "Image model returned nothing"))
                return

            path = self._save_room(location_id, image_bytes)
            self._result_queue.put(("room_complete", location_id, {
                "image_path": str(path),
            }))
            _log(f"[{location_id}] saved -> {path}")

        except Exception as e:
            _log(f"[{location_id}] Pipeline error: {e}")
            self._result_queue.put(("error", location_id, str(e)))
        finally:
            self._pending.pop(location_id, None)

    def _save_room(self, location_id, image_bytes):
        path = self._cache_dir / "rooms" / f"{location_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)

        img = Image.open(io.BytesIO(image_bytes))
        img = img.resize((INTERNAL_WIDTH, INTERNAL_HEIGHT), Image.LANCZOS)
        img = img.convert("RGB")
        img.save(path, "PNG")
        return path
