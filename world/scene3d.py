"""3D scene construction and deterministic priority map generation.

A Scene3D is built from a structured JSON description (floor polygon, walls, objects).
The geometry is the source of truth — priority maps and walkability are computed
from it deterministically, never guessed from generated art.

Coordinate system:
    X — left/right (world meters)
    Y — up (world meters)
    Z — into screen (world meters, away from viewer)

Projection: oblique parallel (Sierra/SCUMM-style 3/4 overhead).
A point (x, y, z) projects to game-space pixel coordinates as:
    screen_x = ORIGIN_X + x * SCALE
    screen_y = ORIGIN_Y - y * SCALE - z * SCALE

This keeps the math invertible: given a walkable floor pixel and y=0,
we can recover world (x, z) — needed for depth banding.
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from config import (
    INTERNAL_WIDTH, INTERNAL_HEIGHT,
    PRIORITY_MAP_WIDTH, PRIORITY_MAP_HEIGHT,
    PRIORITY_BLOCK, PRIORITY_WALKABLE_MIN, PRIORITY_WALKABLE_MAX, PRIORITY_FOREGROUND,
)


def _log(msg):
    print(f"[SCENE3D] {msg}", file=sys.stderr, flush=True)


# A standing adult is 1.8 m and should render at 96 px tall, so 1 m = 53.33 px.
PIXELS_PER_METER = 96.0 / 1.8

# Where world origin (0, 0, 0) lands on the game-space 960x600 canvas.
# Origin sits centered horizontally and near the bottom (floor front edge).
ORIGIN_SCREEN_X = INTERNAL_WIDTH / 2
ORIGIN_SCREEN_Y = INTERNAL_HEIGHT - 50

# Character heights at front (z=0) and back of typical room. Empirically this
# gives a comfortable point-and-click feel without dramatic perspective.
SCALE = PIXELS_PER_METER


def project(x, y, z):
    """World point -> game-space pixel (sx, sy). Float, not clamped."""
    sx = ORIGIN_SCREEN_X + x * SCALE
    sy = ORIGIN_SCREEN_Y - y * SCALE - z * SCALE
    return sx, sy


def unproject_floor(sx, sy):
    """Game-space pixel -> world (x, z) assuming y=0 (on the floor)."""
    x = (sx - ORIGIN_SCREEN_X) / SCALE
    z = (ORIGIN_SCREEN_Y - sy) / SCALE
    return x, z


class Scene3D:
    """A 3D scene built from a structured room description.

    JSON schema:
        {
            "id": "<room_id>",
            "floor": [[x, z], [x, z], ...],          # polygon in world meters
            "walls": [                                 # optional
                {"from": [x, z], "to": [x, z], "height": h}
            ],
            "objects": [                               # optional
                {
                    "id": "...",
                    "type": "box",
                    "position": [x, z],                # ground footprint center
                    "size": [width, depth, height],    # meters
                }
            ],
        }
    """

    def __init__(self, description):
        self.id = description.get("id", "scene")
        self.floor = [tuple(p) for p in description.get("floor", [])]
        self.walls = list(description.get("walls", []))
        self.objects = list(description.get("objects", []))

    # ---- projection helpers ----

    def _project_polygon(self, world_pts_xz, y=0.0):
        """Project a list of (x, z) world points (all at given y) to game pixels."""
        return [project(x, y, z) for (x, z) in world_pts_xz]

    def _object_footprint(self, obj):
        """Return the 4 ground-plane corners of a box object as (x, z) tuples."""
        cx, cz = obj["position"]
        w, d, _h = obj["size"]
        hw, hd = w / 2.0, d / 2.0
        return [
            (cx - hw, cz - hd),
            (cx + hw, cz - hd),
            (cx + hw, cz + hd),
            (cx - hw, cz + hd),
        ]

    def _object_silhouette(self, obj):
        """Project the full 3D box silhouette to a screen-space polygon.

        Returns the convex hull of the 8 projected corners — what the object
        actually occludes on screen. Used to mark foreground overlay regions.
        """
        cx, cz = obj["position"]
        w, d, h = obj["size"]
        hw, hd = w / 2.0, d / 2.0
        # 8 corners of the box
        corners = []
        for dx in (-hw, hw):
            for dz in (-hd, hd):
                for dy in (0.0, h):
                    corners.append(project(cx + dx, dy, cz + dz))
        return _convex_hull(corners)

    # ---- priority map rendering ----

    def render_priority_map(self):
        """Render a deterministic priority map as a numpy uint8 array
        of shape (PRIORITY_MAP_HEIGHT, PRIORITY_MAP_WIDTH)."""

        # Work in game-space (960x600) then downscale to map size for crispness.
        canvas = Image.new("L", (INTERNAL_WIDTH, INTERNAL_HEIGHT), color=PRIORITY_BLOCK * 16)
        draw = ImageDraw.Draw(canvas)

        # 1. Floor: fill as walkable. We'll re-band by depth afterward.
        if len(self.floor) >= 3:
            floor_screen = self._project_polygon(self.floor, y=0.0)
            draw.polygon(floor_screen, fill=255)  # temporary marker for "walkable floor"

        # 2. Object ground footprints: carve out of walkable.
        for obj in self.objects:
            footprint = self._object_footprint(obj)
            footprint_screen = self._project_polygon(footprint, y=0.0)
            draw.polygon(footprint_screen, fill=PRIORITY_BLOCK * 16)

        # 3. Walls: carve out where the wall meets the floor.
        for wall in self.walls:
            x1, z1 = wall["from"]
            x2, z2 = wall["to"]
            # Walls are thin — give them a small thickness in world units
            # by drawing a line in screen space.
            p1 = project(x1, 0, z1)
            p2 = project(x2, 0, z2)
            draw.line([p1, p2], fill=PRIORITY_BLOCK * 16, width=4)

        # Floor pass marked walkable pixels as 255. Now band them by depth.
        arr = np.array(canvas, dtype=np.uint8)
        walkable_mask = arr == 255

        if walkable_mask.any():
            # Compute world-z for every game-pixel row (y=0 floor plane).
            row_indices = np.arange(INTERNAL_HEIGHT, dtype=np.float32)
            world_z_per_row = (ORIGIN_SCREEN_Y - row_indices) / SCALE  # shape (H,)

            # Map z range across walkable pixels onto bands [WALKABLE_MIN, WALKABLE_MAX].
            # Closer to camera (smaller z) -> higher band (renders in front).
            z_walkable = world_z_per_row[np.where(walkable_mask.any(axis=1))]
            if len(z_walkable) > 0:
                z_min = float(z_walkable.min())
                z_max = float(z_walkable.max())
                z_span = max(z_max - z_min, 1e-6)

                # Per-pixel z
                z_grid = np.broadcast_to(
                    world_z_per_row[:, None], (INTERNAL_HEIGHT, INTERNAL_WIDTH)
                )
                # Normalize: front (z_min) -> 1.0, back (z_max) -> 0.0
                t = 1.0 - (z_grid - z_min) / z_span
                bands = (PRIORITY_WALKABLE_MIN
                         + t * (PRIORITY_WALKABLE_MAX - PRIORITY_WALKABLE_MIN))
                bands = np.clip(bands, PRIORITY_WALKABLE_MIN, PRIORITY_WALKABLE_MAX)
                pixel_vals = (bands.astype(np.uint8) * 16) + 8  # center of band

                arr = np.where(walkable_mask, pixel_vals, arr)

        # 4. Foreground overlay for objects taller than the character's head.
        # If an object's full silhouette extends above the character's head height
        # (~1.8m), the part of the silhouette above the floor footprint is foreground.
        # For step 1 keep it simple: skip foreground; we'll add this when sprites land.

        # Downscale to priority map size.
        img = Image.fromarray(arr, mode="L")
        img = img.resize((PRIORITY_MAP_WIDTH, PRIORITY_MAP_HEIGHT), Image.NEAREST)
        return np.array(img, dtype=np.uint8)

    def save_priority_map(self, path):
        arr = self.render_priority_map()
        Image.fromarray(arr, mode="L").save(path, "PNG")
        _log(f"Saved priority map to {path}")
        return path

    # ---- 3D reference render ----

    def render_reference(self):
        """Render a flat-shaded 3D preview of the scene as a PIL Image (RGB).

        Drawn at game resolution (960x600). Serves two purposes:
          - debug visualization so we can see what the LLM built
          - conditioning image for the art pass (step 3)

        The camera is the same oblique projection used for the priority map,
        so the 3D preview is spatially consistent with the priority map.
        """
        SKY = (170, 195, 220)
        FLOOR = (190, 175, 145)
        FLOOR_EDGE = (110, 95, 70)
        TOP = (200, 200, 200)
        FRONT = (140, 140, 145)
        OUTLINE = (40, 40, 50)

        img = Image.new("RGB", (INTERNAL_WIDTH, INTERNAL_HEIGHT), color=SKY)
        draw = ImageDraw.Draw(img)

        # Floor
        if len(self.floor) >= 3:
            floor_screen = self._project_polygon(self.floor, y=0.0)
            draw.polygon(floor_screen, fill=FLOOR, outline=FLOOR_EDGE)

        # Objects: painter's algorithm — draw back ones first (larger cz)
        ordered = sorted(self.objects, key=lambda o: -o["position"][1])
        for obj in ordered:
            self._draw_box(draw, obj, TOP, FRONT, OUTLINE)

        return img

    def _draw_box(self, draw, obj, top_color, front_color, outline):
        """Render one axis-aligned box as a flat-shaded top + front quad."""
        cx, cz = obj["position"]
        w, d, h = obj["size"]
        hw, hd = w / 2.0, d / 2.0

        # The 8 corners (x_left/x_right, y_bot/y_top, z_front/z_back)
        # In screen space, front face is a rectangle, top face is a rectangle
        # stacked above it (because the projection has no horizontal angle).
        x_left = cx - hw
        x_right = cx + hw
        z_front = cz - hd
        z_back = cz + hd

        # Front face corners
        fl_bot = project(x_left,  0, z_front)
        fr_bot = project(x_right, 0, z_front)
        fr_top = project(x_right, h, z_front)
        fl_top = project(x_left,  h, z_front)
        front_quad = [fl_bot, fr_bot, fr_top, fl_top]

        # Top face corners
        bl_top = project(x_left,  h, z_back)
        br_top = project(x_right, h, z_back)
        top_quad = [fl_top, fr_top, br_top, bl_top]

        draw.polygon(top_quad, fill=top_color, outline=outline)
        draw.polygon(front_quad, fill=front_color, outline=outline)

    def save_reference(self, path):
        img = self.render_reference()
        img.save(path, "PNG")
        _log(f"Saved reference render to {path}")
        return path


def _convex_hull(points):
    """Andrew's monotone chain convex hull. Input: list of (x, y) tuples."""
    pts = sorted(set(points))
    if len(pts) <= 1:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]
