"""Step 3: paint a background from a 3D reference render.

Pipeline:
    scene.json -> Scene3D -> reference.png -> Gemini image model -> painted.png

The reference render locks the spatial layout. The image model only fills in
texture, lighting, and atmosphere.

Usage:
    python scripts/test_paint.py [room_id]
"""

import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(override=True)

from google import genai
from google.genai import types
from PIL import Image

from world.scene3d import Scene3D
from config import GEMINI_SCENERY_MODEL, INTERNAL_WIDTH, INTERNAL_HEIGHT


# Default room descriptions for each test scene (matches scripts/test_layout.py).
ROOM_DESCRIPTIONS = {
    "cozy_tavern": (
        "A cozy tavern with a long wooden bar at the back, several round "
        "tables with chairs, a stone fireplace on the left wall, and a few "
        "barrels near the entrance.",
        "warm painterly adventure-game art, candlelit, rich wood tones",
    ),
    "forest_glade": (
        "A small sunlit clearing in an old forest. Large trees stand around "
        "the edges. A mossy boulder in the center and a fallen log on the right.",
        "lush painterly adventure-game art, dappled sunlight through leaves",
    ),
    "wizards_study": (
        "A cluttered wizard's study. A massive desk on one side, bookshelves "
        "lining the walls, a globe and a reading chair near the window.",
        "moody painterly adventure-game art, candlelight, leather and parchment",
    ),
}


PAINT_TEXT_TEMPLATE = """Paint a finished game background for a point-and-click adventure game.

Style: {visual_style}.

Scene: {description}

Camera: fixed three-quarter overhead perspective. Output is 960x600 pixels.

LAYOUT (in 960x600 screen coordinates, origin at top-left):
{layout}

Rules:
- The floor must occupy the screen region described above.
- Each listed object must appear at its screen position, sized as given.
- DO NOT include any people, characters, animals, or creatures.
- DO NOT include text, labels, UI, or borders.
- Paint EVERY pixel of the 960x600 frame. Above the floor, paint walls,
  ceiling, sky, or whatever the scene calls for.

Output ONLY the painted image."""


PAINT_PROMPT_TEMPLATE = """The attached image is a LAYOUT BLUEPRINT, not a starting canvas.
Every pixel is a drab placeholder color indicating geometry that you MUST
paint over completely. None of it should appear in your output.
  - DARK GREY region   = above-floor area (paint as walls, sky, or ceiling)
  - TAN region         = floor (paint as the actual floor surface)
  - LIGHT GREY boxes   = furniture/scenery (top faces lighter, front faces darker)

Your job is to PAINT THE ENTIRE FRAME FROM SCRATCH as a finished game background
in this style: {visual_style}.

EVERY pixel of the output must be painted artwork. NO part of the blueprint
should remain visible. The blue area becomes walls and/or sky as appropriate.
The tan area becomes a real painted floor. The grey boxes become the actual
furniture and scenery listed below.

Scene: {description}

Object inventory — paint each one EXACTLY where the matching grey box sits in
the blueprint, at the same height, width and depth:
{object_list}

CRITICAL RULES:
- The painted FLOOR must cover the same screen area as the tan region.
- Each painted object must sit in the same screen position as its grey box,
  with the same footprint and the same height.
- DO NOT add, remove, move, or resize anything.
- DO NOT include any people, characters, animals, or creatures.
- Paint NO text, labels, UI, debug graphics, or borders.
- The camera is a fixed three-quarter overhead view — match the projection
  of the blueprint exactly.

Output ONLY the finished painted background image."""


def describe_objects(scene_dict):
    lines = []
    for obj in scene_dict["objects"]:
        cat = obj["category"]
        desc = obj.get("description", "").strip()
        if desc:
            lines.append(f"  - {obj['id']}: a {cat} — {desc}")
        else:
            lines.append(f"  - {obj['id']}: a {cat}")
    return "\n".join(lines)


def describe_layout(scene_dict):
    """Produce a text description of object positions in screen-pixel terms,
    so the model can paint the scene without an image reference."""
    from world.scene3d import project, ORIGIN_SCREEN_X, ORIGIN_SCREEN_Y

    floor_pts = [project(x, 0, z) for (x, z) in scene_dict["floor"]]
    xs = [p[0] for p in floor_pts]
    ys = [p[1] for p in floor_pts]
    floor_desc = (f"Floor occupies roughly x={int(min(xs))}-{int(max(xs))}, "
                  f"y={int(min(ys))}-{int(max(ys))} (out of 960x600).")

    lines = [floor_desc, ""]
    for obj in scene_dict["objects"]:
        cx, cz = obj["position"]
        w, d, h = obj["size"]
        # Project center base and top
        base_sx, base_sy = project(cx, 0, cz)
        top_sx, top_sy = project(cx, h, cz)
        screen_w = int(w * (INTERNAL_WIDTH / 18.0))  # heuristic; PIXELS_PER_METER ≈ 53.3
        screen_h = int(base_sy - top_sy)
        desc = obj.get("description", "").strip()
        cat = obj["category"]
        lines.append(
            f"  - {cat} ({obj['id']}): center on floor at "
            f"screen ({int(base_sx)}, {int(base_sy)}), "
            f"about {screen_w}px wide and {screen_h}px tall."
            + (f" {desc}" if desc else "")
        )
    return "\n".join(lines)


def paint_room(room_id, use_reference=False):
    scene_file = Path(f"cache/test_layout/{room_id}/scene.json")
    if not scene_file.exists():
        print(f"ERROR: {scene_file} not found. Run scripts/test_layout.py first.")
        return False

    description, visual_style = ROOM_DESCRIPTIONS.get(
        room_id, ("a room", "painterly adventure-game art")
    )

    scene_dict = json.loads(scene_file.read_text())
    scene = Scene3D(scene_dict)

    layout_text = describe_layout(scene_dict)
    prompt = PAINT_TEXT_TEMPLATE.format(
        visual_style=visual_style,
        description=description,
        layout=layout_text,
    )

    contents = [prompt]
    if use_reference:
        ref_img = scene.render_reference()
        buf = io.BytesIO()
        ref_img.save(buf, "PNG")
        contents = [
            types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"),
            prompt,
        ]

    print(f"[paint] room={room_id}  ref={use_reference}")
    print(f"[paint] style={visual_style}")
    print(f"[paint] {len(scene_dict['objects'])} objects")
    print(f"[paint] calling {GEMINI_SCENERY_MODEL}...")

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set")
        return False

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=GEMINI_SCENERY_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["image", "text"],
            safety_settings=[
                types.SafetySetting(category=c, threshold="OFF") for c in [
                    "HARM_CATEGORY_HARASSMENT",
                    "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "HARM_CATEGORY_CIVIC_INTEGRITY",
                ]
            ],
        ),
    )

    for part in response.candidates[0].content.parts:
        if part.inline_data and part.inline_data.mime_type.startswith("image/"):
            painted = Image.open(io.BytesIO(part.inline_data.data))
            # Resize to internal game resolution for a fair comparison
            painted = painted.resize((INTERNAL_WIDTH, INTERNAL_HEIGHT), Image.LANCZOS)
            out_path = scene_file.parent / "painted.png"
            painted.save(out_path, "PNG")
            print(f"[paint] saved -> {out_path}")
            return True

    print("[paint] ERROR: no image returned")
    return False


def main():
    args = sys.argv[1:]
    use_reference = False
    if "--ref" in args:
        use_reference = True
        args.remove("--ref")
    rooms = args if args else list(ROOM_DESCRIPTIONS.keys())
    for room in rooms:
        print()
        paint_room(room, use_reference=use_reference)


if __name__ == "__main__":
    main()
