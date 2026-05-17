"""End-to-end test for step 2: text description -> JSON scene -> priority map.

Run from the project root:
    python scripts/test_layout.py [room_index]

Generates layouts for a few sample rooms, rasterizes their priority maps,
and saves outputs under cache/test_layout/<room_id>/.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image

from agents.layout import LayoutAgent
from world.scene3d import Scene3D
from scripts.test_scene3d import make_debug_preview


SAMPLE_ROOMS = [
    {
        "id": "cozy_tavern",
        "name": "The Bent Tankard",
        "description": "A cozy tavern with a long wooden bar at the back, "
                       "several round tables with chairs scattered through the middle, "
                       "a stone fireplace on the left wall, and a few barrels near the entrance.",
    },
    {
        "id": "forest_glade",
        "name": "Sunlit Glade",
        "description": "A small clearing in an old forest. A few large trees stand around "
                       "the edges. There is a mossy boulder in the center and a fallen log "
                       "on the right side.",
    },
    {
        "id": "wizards_study",
        "name": "Wizard's Study",
        "description": "A cluttered study. A massive desk dominates one side, bookshelves "
                       "line the walls, and there is a globe and a reading chair near the window.",
    },
]


def run_one(room_def, out_dir):
    agent = LayoutAgent()
    if not agent.connected:
        print("ERROR: No GEMINI_API_KEY configured")
        return False

    scene_dict, warnings = agent.design_room(room_def["id"], room_def)

    room_out = out_dir / room_def["id"]
    room_out.mkdir(parents=True, exist_ok=True)

    # Save the JSON the LLM produced (after sanitization)
    with open(room_out / "scene.json", "w") as f:
        json.dump(scene_dict, f, indent=2)
    print(f"  scene.json saved ({len(scene_dict['floor'])} floor verts, "
          f"{len(scene_dict['objects'])} objects)")
    if warnings:
        for w in warnings:
            print(f"    warning: {w}")

    # List object summary
    for obj in scene_dict["objects"]:
        pos = obj["position"]
        size = obj["size"]
        print(f"    - {obj['id']:25s} ({obj['category']:12s}) "
              f"pos=({pos[0]:+.2f}, {pos[1]:+.2f})  "
              f"size=({size[0]:.2f}×{size[1]:.2f}×{size[2]:.2f})")

    # Build geometry and render priority map
    scene = Scene3D(scene_dict)
    arr = scene.render_priority_map()

    Image.fromarray(arr, mode="L").save(room_out / "priority_map.png", "PNG")
    make_debug_preview(arr).save(room_out / "preview.png", "PNG")
    scene.save_reference(room_out / "reference.png")

    bands = arr // 16
    total = bands.size
    walkable_pct = ((bands >= 4) & (bands <= 14)).sum() / total * 100
    print(f"  priority_map.png saved   walkable={walkable_pct:.1f}%   "
          f"bands_present={sorted(set(bands[(bands>=4)&(bands<=14)].tolist()))}")

    return True


def main():
    out_dir = Path(__file__).resolve().parent.parent / "cache" / "test_layout"
    out_dir.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) > 1:
        idx = int(sys.argv[1])
        rooms = [SAMPLE_ROOMS[idx]]
    else:
        rooms = SAMPLE_ROOMS

    for room in rooms:
        print(f"\n=== {room['name']} ({room['id']}) ===")
        print(f"  {room['description']}")
        run_one(room, out_dir)

    print(f"\nAll outputs in: {out_dir}")


if __name__ == "__main__":
    main()
