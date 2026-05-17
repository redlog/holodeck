"""Spike: build a hardcoded 3D room, generate a priority map deterministically.

Run from the project root:
    python scripts/test_scene3d.py

Outputs:
    cache/test_scene3d/priority_map.png   — the generated priority map
    cache/test_scene3d/preview.png        — debug visualization (bands as grayscale)
"""

import sys
from pathlib import Path

# Add project root to path so we can import world.* and config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image

from world.scene3d import Scene3D
from config import (
    PRIORITY_BLOCK, PRIORITY_WALKABLE_MIN, PRIORITY_WALKABLE_MAX, PRIORITY_FOREGROUND,
)


# A small tavern. Floor is a rectangle 9m wide and 6m deep, front edge at z=0.5,
# back wall at z=6.5. Objects: a bar along the back wall, two tables in the middle.
ROOM = {
    "id": "test_tavern",
    "floor": [
        [-4.5, 0.5],   # front-left
        [ 4.5, 0.5],   # front-right
        [ 4.5, 6.5],   # back-right
        [-4.5, 6.5],   # back-left
    ],
    "objects": [
        # The bar — long thin box along the back wall
        {
            "id": "bar",
            "type": "box",
            "position": [0.0, 5.8],   # center (x, z)
            "size": [6.0, 0.8, 1.1],  # width, depth, height in meters
        },
        # Two tables
        {
            "id": "table_left",
            "type": "box",
            "position": [-2.2, 3.2],
            "size": [1.2, 1.2, 0.9],
        },
        {
            "id": "table_right",
            "type": "box",
            "position": [2.2, 3.2],
            "size": [1.2, 1.2, 0.9],
        },
        # A barrel near the front-right
        {
            "id": "barrel",
            "type": "box",
            "position": [3.2, 1.6],
            "size": [0.7, 0.7, 1.0],
        },
    ],
}


def make_debug_preview(arr):
    """Visualize bands: lighter = more walkable foreground depth,
    darker = further back, black = impassable."""
    rgb = np.zeros((arr.shape[0], arr.shape[1], 3), dtype=np.uint8)

    bands = arr // 16
    walkable = (bands >= PRIORITY_WALKABLE_MIN) & (bands <= PRIORITY_WALKABLE_MAX)
    block = bands == PRIORITY_BLOCK
    fg = bands == PRIORITY_FOREGROUND

    # Walkable: greenish, lighter toward the front
    t = np.zeros_like(arr, dtype=np.float32)
    t[walkable] = (bands[walkable] - PRIORITY_WALKABLE_MIN) / max(
        PRIORITY_WALKABLE_MAX - PRIORITY_WALKABLE_MIN, 1
    )
    rgb[..., 1] = (t * 200 + 30 * walkable).astype(np.uint8)

    # Block: red
    rgb[block, 0] = 180

    # Foreground: blue
    rgb[fg, 2] = 220

    return Image.fromarray(rgb, "RGB")


def main():
    out_dir = Path(__file__).resolve().parent.parent / "cache" / "test_scene3d"
    out_dir.mkdir(parents=True, exist_ok=True)

    scene = Scene3D(ROOM)
    arr = scene.render_priority_map()

    pm_path = out_dir / "priority_map.png"
    Image.fromarray(arr, mode="L").save(pm_path, "PNG")
    print(f"Priority map: {pm_path}  shape={arr.shape}  unique={np.unique(arr).tolist()}")

    preview = make_debug_preview(arr)
    preview_path = out_dir / "preview.png"
    preview.save(preview_path, "PNG")
    print(f"Debug preview: {preview_path}")

    # Summary stats
    bands = arr // 16
    total = bands.size
    walkable_pct = ((bands >= PRIORITY_WALKABLE_MIN) & (bands <= PRIORITY_WALKABLE_MAX)).sum() / total * 100
    block_pct = (bands == PRIORITY_BLOCK).sum() / total * 100
    print(f"Walkable: {walkable_pct:.1f}%   Impassable: {block_pct:.1f}%")
    print(f"Bands present in walkable area: "
          f"{sorted(set(bands[(bands >= PRIORITY_WALKABLE_MIN) & (bands <= PRIORITY_WALKABLE_MAX)].tolist()))}")


if __name__ == "__main__":
    main()
