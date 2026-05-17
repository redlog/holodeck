"""LayoutAgent: produces a structured 3D scene description from a room text.

The LLM's job here is purely *creative* — decide what objects go where, rough
sizes, the floor shape. It is NOT asked to produce precise pixel coordinates
or paint anything. Deterministic code (world.scene3d) turns the structured
output into geometry, priority maps, and (later) art references.

World coordinates the model must use:
    X — left/right, meters. Origin centered.
    Z — depth into screen, meters. Front edge of floor near z=0.5.
    Y — up, meters. Floor at y=0.
    A standing adult is 1.8 m. The camera is a fixed 3/4 overhead view.
"""

import json
import sys

from google.genai import types

from agents.base import BaseAgent
from config import GEMINI_AUTHOR_MODEL


def _log(msg):
    print(f"[LAYOUT] {msg}", file=sys.stderr, flush=True)


# Reasonable bounds the LLM should stay within. Used both in the prompt and
# in post-output sanitization.
FLOOR_X_MIN, FLOOR_X_MAX = -6.0, 6.0
FLOOR_Z_MIN, FLOOR_Z_MAX = 0.3, 8.0
OBJECT_MIN_DIM = 0.2
OBJECT_MAX_DIM = 8.0


LAYOUT_SYSTEM_PROMPT = """You are a 3D scene layout designer for an adventure game.
Given a description of a room, output a structured JSON scene that defines its
walkable floor and the major objects inside it.

COORDINATE SYSTEM (all units in METERS):
- X axis: left/right. Negative is left, positive is right. Origin is center.
- Z axis: depth into the screen. The FRONT edge of the floor (closest to camera)
  is near z=0.5. The BACK wall is somewhere around z=5–7. Larger z = further away.
- Y axis: up. Floor at y=0. You do not emit Y; positions are on the floor.

CAMERA: fixed three-quarter overhead. A standing human is 1.8 m tall.

═══ FLOOR ═══
- A polygon (3+ vertices) describing the WALKABLE region.
- Vertices in clockwise or counter-clockwise order, no self-intersection.
- Typical rooms: 6–10 m wide (X), 4–6 m deep (Z).
- The floor edges are the walls. Objects can sit AGAINST these edges but must
  not extend past them.

═══ OBJECTS ═══
Each object is an axis-aligned box. You emit:
  - position: [x, z] — the CENTER of the object's footprint on the floor
  - size: [width_x, depth_z, height_y] — extents in meters

THE FOOTPRINT RULE:
An object centered at (cx, cz) with size (w, d, h) occupies the rectangle
  x ∈ [cx - w/2, cx + w/2],  z ∈ [cz - d/2, cz + d/2]
This whole rectangle MUST fit inside the floor polygon. THINK THROUGH THE MATH
before emitting each object.

PLACEMENT IDIOMS — apply these explicitly:

1. AGAINST THE BACK WALL (bar, bookshelf, fireplace lining back wall):
   If the back wall is at z = Z_BACK, set the object's center z to:
       cz = Z_BACK - d/2
   so the BACK face of the object sits flush against the wall.

2. AGAINST THE LEFT/RIGHT WALL (sideboard, bookshelf along side):
   If left wall is at x = X_LEFT, set cx = X_LEFT + w/2.
   If right wall is at x = X_RIGHT, set cx = X_RIGHT - w/2.

3. CHAIR PAIRED WITH A TABLE:
   Tables are commonly approached from the front (the camera side).
   Place the chair OFFSET TOWARD THE CAMERA from the table center:
       chair.cz = table.cz - (table.d/2) - (chair.d/2) - 0.05
       chair.cx = table.cx
   This puts the chair just in front of the table.
   For multiple chairs around one table, place them on different sides:
       in front: cz = table.cz - table.d/2 - chair.d/2 - 0.05
       behind:   cz = table.cz + table.d/2 + chair.d/2 + 0.05
       left:     cx = table.cx - table.w/2 - chair.w/2 - 0.05
       right:    cx = table.cx + table.w/2 + chair.w/2 + 0.05

4. WALKING SPACE:
   Leave at least ~1.0 m between major objects, and at least ~0.6 m between
   any object and a wall (unless the object is intentionally flush with that
   wall per rules 1–2).

SIZES — use realistic dimensions:
  chair          0.5 × 0.5 × 0.9
  small table    0.9 × 0.9 × 0.8
  round table    1.2 × 1.2 × 0.8
  long table     2.0 × 1.0 × 0.8
  bar counter    width × 0.8 × 1.1   (width often 4–8 m)
  bookshelf      1.5 × 0.4 × 2.0     (or longer along a wall)
  fireplace      1.5 × 0.8 × 1.8
  bed (single)   1.0 × 2.0 × 0.6
  bed (double)   1.6 × 2.0 × 0.6
  barrel         0.7 × 0.7 × 1.0
  large tree     1.5 × 1.5 × 4.0+
  boulder        1.0 × 1.0 × 0.8

CONVENTIONS:
- 3–8 objects per room is typical. Adventure-game scenes are sparse, not crowded.
- Lowercase snake_case ids. Each object also has a category (table, chair, …).
- Multiple objects of the same type: id them table_1, table_2, etc.

═══ WORKED EXAMPLE ═══
Description: "A simple tavern with a bar at the back, one round table with two chairs, and a barrel by the door."

Floor: rectangle 8 m wide, 5 m deep:
  [[-4.0, 0.5], [4.0, 0.5], [4.0, 5.5], [-4.0, 5.5]]
  (X_LEFT=-4, X_RIGHT=4, Z_FRONT=0.5, Z_BACK=5.5)

Bar (against back wall, 6 m long):
  size = [6.0, 0.8, 1.1]
  cz = Z_BACK - 0.8/2 = 5.1
  position = [0.0, 5.1]            ✓ back face at z=5.5, flush

Round table in the middle:
  size = [1.2, 1.2, 0.8]
  position = [0.0, 2.8]            ✓ leaves ~2 m to bar, ~1.5 m to front

Chair in front of table:
  size = [0.5, 0.5, 0.9]
  cz = 2.8 - 0.6 - 0.25 - 0.05 = 1.9
  position = [0.0, 1.9]            ✓ just in front of table

Chair behind table:
  cz = 2.8 + 0.6 + 0.25 + 0.05 = 3.7
  position = [0.0, 3.7]            ✓ just behind table

Barrel near front-right (door side):
  size = [0.7, 0.7, 1.0]
  position = [3.0, 1.2]            ✓ tucked into front-right corner

Output ONLY the JSON, no commentary."""


SCENE_SCHEMA = {
    "type": "object",
    "properties": {
        "floor": {
            "type": "array",
            "description": "Polygon vertices [x, z] in meters, ordered.",
            "items": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
            },
            "minItems": 3,
        },
        "objects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "position": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "size": {
                        "type": "array",
                        "description": "[width_x, depth_z, height_y] in meters",
                        "items": {"type": "number"},
                        "minItems": 3,
                        "maxItems": 3,
                    },
                },
                "required": ["id", "category", "position", "size"],
            },
        },
    },
    "required": ["floor", "objects"],
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _floor_bbox(floor):
    xs = [p[0] for p in floor]
    zs = [p[1] for p in floor]
    return min(xs), max(xs), min(zs), max(zs)


def _clip_object_to_bbox(obj, bbox):
    """Push the object's footprint inside the floor bounding box if it leaks past.

    Returns (clipped_obj, was_clipped). Does not resize unless the object is
    bigger than the bbox in some axis (in which case the size is reduced).
    """
    x_min, x_max, z_min, z_max = bbox
    cx, cz = obj["position"]
    w, d, h = obj["size"]

    bbox_w = x_max - x_min
    bbox_d = z_max - z_min

    new_w = min(w, bbox_w)
    new_d = min(d, bbox_d)

    new_cx = _clamp(cx, x_min + new_w / 2, x_max - new_w / 2)
    new_cz = _clamp(cz, z_min + new_d / 2, z_max - new_d / 2)

    changed = (abs(new_cx - cx) > 1e-4 or abs(new_cz - cz) > 1e-4
               or abs(new_w - w) > 1e-4 or abs(new_d - d) > 1e-4)
    clipped = dict(obj)
    clipped["position"] = [new_cx, new_cz]
    clipped["size"] = [new_w, new_d, h]
    return clipped, changed


def sanitize_scene(raw):
    """Clamp and normalize an LLM-produced scene into safe ranges.

    Returns (scene, warnings) where scene is the cleaned dict.
    """
    warnings = []

    # Floor
    floor_raw = raw.get("floor") or []
    floor = []
    for pt in floor_raw:
        if not (isinstance(pt, (list, tuple)) and len(pt) == 2):
            warnings.append(f"dropped malformed floor point {pt!r}")
            continue
        x = _clamp(float(pt[0]), FLOOR_X_MIN, FLOOR_X_MAX)
        z = _clamp(float(pt[1]), FLOOR_Z_MIN, FLOOR_Z_MAX)
        floor.append([x, z])
    if len(floor) < 3:
        warnings.append("floor has <3 vertices; using default rectangle")
        floor = [[-4.5, 0.5], [4.5, 0.5], [4.5, 6.0], [-4.5, 6.0]]

    bbox = _floor_bbox(floor)

    # Objects
    objects = []
    seen_ids = set()
    for obj in raw.get("objects") or []:
        try:
            obj_id = str(obj["id"]).strip().lower().replace(" ", "_")
            pos = list(obj["position"])
            size = list(obj["size"])
            assert len(pos) == 2 and len(size) == 3
        except (KeyError, AssertionError, TypeError):
            warnings.append(f"dropped malformed object {obj!r}")
            continue

        # Dedup ids
        base_id = obj_id
        i = 2
        while obj_id in seen_ids:
            obj_id = f"{base_id}_{i}"
            i += 1
        seen_ids.add(obj_id)

        # Clamp to global bounds first
        pos = [_clamp(float(pos[0]), FLOOR_X_MIN, FLOOR_X_MAX),
               _clamp(float(pos[1]), FLOOR_Z_MIN, FLOOR_Z_MAX)]
        size = [_clamp(float(size[0]), OBJECT_MIN_DIM, OBJECT_MAX_DIM),
                _clamp(float(size[1]), OBJECT_MIN_DIM, OBJECT_MAX_DIM),
                _clamp(float(size[2]), OBJECT_MIN_DIM, OBJECT_MAX_DIM)]

        clean = {
            "id": obj_id,
            "category": str(obj.get("category", "object")),
            "description": str(obj.get("description", "")),
            "position": pos,
            "size": size,
        }

        # Then clip footprint inside the floor bounding box
        clipped, was_clipped = _clip_object_to_bbox(clean, bbox)
        if was_clipped:
            warnings.append(
                f"clipped '{obj_id}' to fit floor "
                f"(pos {pos}->{clipped['position']}, size {size}->{clipped['size']})"
            )
        objects.append(clipped)

    return {"floor": floor, "objects": objects}, warnings


class LayoutAgent(BaseAgent):
    def __init__(self):
        super().__init__(model=GEMINI_AUTHOR_MODEL, temperature=0.7)

    def design_room(self, room_id, room_def, visual_style=None):
        """Synchronous: produce a sanitized scene dict for a single room.

        Returns (scene_dict, warnings). Caller can hand `scene_dict` straight
        to world.scene3d.Scene3D after attaching the room id.
        """
        description = room_def.get("description", "")
        name = room_def.get("name", room_id)

        user_msg = (
            f"Room name: {name}\n"
            f"Visual style: {visual_style or 'painterly adventure game art'}\n"
            f"Description: {description}\n\n"
            f"Design this room. Apply the placement idioms exactly as shown in the worked example."
        )

        _log(f"[{room_id}] requesting layout for: {description[:80]}...")
        raw_text = self._call_text(
            system_prompt=LAYOUT_SYSTEM_PROMPT,
            contents=user_msg,
            response_mime="application/json",
        )

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as e:
            _log(f"[{room_id}] JSON parse error: {e}")
            _log(f"[{room_id}] Raw response: {raw_text[:200]}")
            raise

        scene, warnings = sanitize_scene(raw)
        scene["id"] = room_id
        for w in warnings:
            _log(f"[{room_id}] warning: {w}")
        _log(f"[{room_id}] floor verts={len(scene['floor'])} objects={len(scene['objects'])}")
        return scene, warnings

    def _call_text(self, system_prompt, contents, response_mime="application/json"):
        # Override: pass response_schema so structured output is enforced.
        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=self._temperature,
                response_mime_type=response_mime,
                response_schema=SCENE_SCHEMA,
                safety_settings=self._safety_off(),
            ),
        )
        return response.text
