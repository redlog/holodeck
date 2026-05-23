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
from agents.prompts import SCENERY_TEMPLATE
from config import SCENERY_MODEL


def _log(msg):
    print(f"[SCENERY] {msg}", file=sys.stderr, flush=True)



def _build_scenery_context(game_context):
    """Build extra context lines for the image prompt from game state."""
    if not game_context:
        return ""
    lines = []

    tone = game_context.get("tone", "")
    if tone:
        lines.append(f"Mood/tone: {tone}")

    # NPCs visually present — their appearance affects the scene
    npcs = game_context.get("present_npcs") or []
    if npcs:
        npc_descs = []
        for npc in npcs:
            name = npc.get("name", "")
            desc = npc.get("description", "")
            intent = npc.get("current_intent", "")
            if name:
                npc_descs.append(f"{name}: {desc}. Currently: {intent}".strip())
        if npc_descs:
            lines.append("Characters who should be visible in the scene:")
            lines.extend(f"  - {d}" for d in npc_descs)

    # Visual clues from secrets — things the observant player should notice
    visual_clues = game_context.get("visual_clues") or []
    if visual_clues:
        lines.append("Important visual details to include (these are plot-relevant clues):")
        lines.extend(f"  - {c}" for c in visual_clues)

    # Discovered features already known
    features = game_context.get("discovered_features") or []
    if features:
        lines.append("Known features that should be visible: " + ", ".join(features))

    # Events that changed the scene
    events = game_context.get("events_log") or ""
    if events:
        lines.append(f"Recent events here: {events}")

    return "\n".join(lines)


class SceneryAgent(BaseAgent):
    def __init__(self, cache_dir):
        super().__init__(model=SCENERY_MODEL, temperature=0.7, game_dir=cache_dir)
        self._cache_dir = Path(cache_dir)
        self._pending = {}

    @property
    def pending(self):
        return bool(self._pending)

    def generate_room(self, location_id, location_def, visual_style,
                      game_context=None, change=None):
        if location_id in self._pending:
            return
        self._pending[location_id] = True
        _log(f"Starting room paint for '{location_id}'")
        self._run_threaded(self._pipeline, location_id, location_def,
                           visual_style, game_context, change)

    def _pipeline(self, location_id, location_def, visual_style,
                  game_context=None, change=None):
        try:
            existing_path = location_def.get("image_path")
            existing_bytes = None
            if change and existing_path:
                try:
                    existing_bytes = Path(existing_path).read_bytes()
                except OSError:
                    pass

            if existing_bytes:
                _log(f"[{location_id}] applying delta to existing image: {change}")
                prompt = (
                    f"This is an existing scene. Apply exactly this change and nothing else: {change}. "
                    f"Keep the composition, lighting, art style, camera angle, and all other details "
                    f"completely identical to the reference image."
                )
                image_bytes = self._call_image(prompt, reference_images=[existing_bytes], aspect_ratio="16:9", context=f"room:{location_id}")
            else:
                scene = (location_def.get("image_prompt")
                         or location_def.get("summary")
                         or location_def.get("name", "an empty room"))
                scenery_ctx = _build_scenery_context(game_context)
                _log(f"[{location_id}] painting scene from scratch...")
                prompt = SCENERY_TEMPLATE.format(
                    visual_style=visual_style or "painterly adventure-game art",
                    scene=scene,
                    context=scenery_ctx,
                )
                image_bytes = self._call_image(prompt, aspect_ratio="16:9", context=f"room:{location_id}")
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
        """Save at native 16:9 dimensions; PlayMode handles crop/fit at render."""
        path = self._cache_dir / "rooms" / f"{location_id}.png"
        path.parent.mkdir(parents=True, exist_ok=True)

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img.save(path, "PNG")
        return path
