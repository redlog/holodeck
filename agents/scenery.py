"""SceneryAgent: paints a room background image from a text prompt.

No spatial constraints — purely atmospheric backgrounds. Takes the
location's image_prompt (which the DM wrote during the creation phase)
and renders a 960x600 PNG.
"""

import io
import sys
import time
from pathlib import Path

from PIL import Image

from agents.base import BaseAgent
from agents.prompts import SCENERY_NEGATIVE_PROMPT, SCENERY_TEMPLATE
from config import GEMINI_IMAGE_MODEL, SCENERY_MODEL


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
            n = len(npc_descs)
            people = "person" if n == 1 else "people"
            lines.append(
                f"The scene must contain EXACTLY {n} distinct {people} — no more, no fewer. "
                f"Paint all {n} of them, each one clearly visible and recognizable:"
            )
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
    # The image model occasionally returns nothing (a transient server-side
    # hiccup). Retry a few times in-thread so the room fills in within the
    # session instead of staying blank until the next reload.
    MAX_PAINT_ATTEMPTS = 3
    RETRY_BACKOFF_SECONDS = 2

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
                scene = (location_def.get("image_prompt")
                         or location_def.get("summary")
                         or location_def.get("name", "an empty room"))
                prompt = (
                    f"Regenerate this scene with one small update. "
                    f"The scene is: {scene}. "
                    f"Visual style: {visual_style or 'painterly illustration'}. "
                    f"Only change: {change}. "
                    f"Everything else — the full wide-angle composition, camera distance, "
                    f"lighting, colour palette, art style, and all other room details — "
                    f"must remain identical to the reference image. "
                    f"Do not zoom in. Show the complete room."
                )
                # Edits must go through a Gemini image model: the Imagen API
                # path ignores reference images, so the agent's default
                # SCENERY_MODEL (an imagen-* model) would regenerate from the
                # text prompt alone and lose fidelity to the prior image.
                paint = lambda: self._call_image(
                    prompt,
                    reference_images=[existing_bytes],
                    aspect_ratio="16:9",
                    context=f"room:{location_id}",
                    model=GEMINI_IMAGE_MODEL,
                )
            else:
                scene = (location_def.get("image_prompt")
                         or location_def.get("summary")
                         or location_def.get("name", "an empty room"))
                scenery_ctx = _build_scenery_context(game_context)
                _log(f"[{location_id}] painting scene from scratch...")
                prompt = SCENERY_TEMPLATE.format(
                    visual_style=visual_style or "painterly illustration",
                    scene=scene,
                    context=scenery_ctx,
                )
                # Combine the global UI-chrome exclusions with any per-location
                # negative the DM wrote (e.g. "fire, flames, doors") to push back
                # on model priors that contradict the scene's actual state.
                negative = SCENERY_NEGATIVE_PROMPT
                loc_negative = (location_def.get("negative_visual") or "").strip()
                if loc_negative:
                    negative = f"{negative}, {loc_negative}"
                paint = lambda: self._call_image(
                    prompt,
                    aspect_ratio="16:9",
                    context=f"room:{location_id}",
                    negative_prompt=negative,
                )

            image_bytes = None
            for attempt in range(1, self.MAX_PAINT_ATTEMPTS + 1):
                image_bytes = paint()
                if image_bytes:
                    break
                if attempt < self.MAX_PAINT_ATTEMPTS:
                    _log(f"[{location_id}] image model returned nothing "
                         f"(attempt {attempt}/{self.MAX_PAINT_ATTEMPTS}), retrying...")
                    time.sleep(self.RETRY_BACKOFF_SECONDS)

            if not image_bytes:
                self._result_queue.put((
                    "error", location_id,
                    f"Image model returned nothing after {self.MAX_PAINT_ATTEMPTS} attempts"))
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
