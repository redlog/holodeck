# Priority Map Exploration — Design Memo

> **Status:** Exploration paused. Architecture decision pending.
> **Date:** 2026-05-17
> **Branch:** `main` (commits `8a132bb`, `bf9a209`, `163193c`)

## What we were trying to fix

The current scenery pipeline generates a painted background, then asks the
same image model to "look at this picture and produce a grayscale walkability
map." This is asking a generative model to do something it's fundamentally
bad at — produce pixel-precise spatial metadata from an artistic image.

Specific failure modes we observed in the original pipeline:

- **Prompt-system mismatch:** the prompt asks for three values (0, 128, 255),
  but the engine uses a 16-band system (`bands 4–14` walkable, band 15 foreground).
  We were only ever populating 3 of 16 bands.
- **No depth banding:** every walkable pixel landed in the same band. Z-ordering
  of multiple characters fell apart because everyone sorted identically.
- **Anti-aliasing and gradients:** image models can't reliably produce flat
  pixel-perfect color regions; the boundary between walkable and impassable
  was fuzzy.
- **No correction loop:** if the model produced garbage, the room was broken
  with no fallback.

The walkability mask drives where the player can go, the depth bands drive
sprite z-ordering, and band 15 drives foreground overlays. All three need to
be geometrically accurate. Generative models can't deliver that.

## The 3D-first idea

User insight: **the painted image and the priority map should both be derived
from a single 3D scene — neither should try to infer the other.**

If you have a 3D model of the room, "take a photograph" gives you the art
and "take a LIDAR scan" gives you the depth map. They're guaranteed
spatially consistent because they're two views of the same geometry.

We explicitly avoided "do everything in the LLM" and instead split
responsibilities:

- **LLM's job:** creative — what kind of room, what objects, rough sizes,
  aesthetic. Pure narrative reasoning.
- **Deterministic code's job:** all spatial math — projection, rasterization,
  depth, collision.
- **Generative image model's job:** purely aesthetic surface treatment —
  texture, lighting, atmosphere. Never spatial decisions.

## What we built

### Step 1 — Scene3D + deterministic priority map ([commit `8a132bb`](#))

**Files:**
- [world/scene3d.py](../world/scene3d.py) — geometry construction, oblique
  projection, priority map rasterizer
- [scripts/test_scene3d.py](../scripts/test_scene3d.py) — hardcoded tavern test

**Approach:**
- World coordinates: X (left/right, meters), Y (up, meters), Z (depth into
  screen, meters). Floor at y=0.
- Oblique parallel projection (Sierra/SCUMM style — no horizontal foreshortening):
    ```
    screen_x = 480 + x * 53.33
    screen_y = 550 - y * 53.33 - z * 53.33
    ```
    The 53.33 px/m comes from "1.8 m human → 96 px sprite."
- Floor polygon → screen-space polygon → rasterized as walkable
- Object bottom-face footprints → cut out of walkable as impassable
- Depth bands 4–14 assigned based on world Z (closer to camera → higher band)

**Result:** Deterministic, fast (<1 second per scene), pixel-precise. The
hardcoded tavern produced a clean priority map with bars cut out for the
bar/tables/barrel and 10 distinct depth bands across the floor. Loads
correctly through the existing `PriorityMap` class.

![priority map](../cache/test_scene3d/preview.png)

### Step 2 — LayoutAgent ([commit `bf9a209`](#))

**Files:**
- [agents/layout.py](../agents/layout.py) — Gemini-driven scene designer
- [scripts/test_layout.py](../scripts/test_layout.py) — text → JSON → priority map

**Approach:**
- Strict JSON schema enforced via Gemini's `response_schema` (floor polygon,
  list of objects with id/category/position/size).
- Detailed system prompt with worked examples and explicit "placement idioms":
  - **Flush with back wall:** `cz = Z_BACK - depth/2`
  - **Chair offset from table:** `chair.cz = table.cz - table.d/2 - chair.d/2 - 0.05`
  - **Realistic size table:** chair 0.5×0.5×0.9, bar 6×0.8×1.1, etc.
- Post-processing sanitization: clamp out-of-bounds values, clip object
  footprints inside the floor bounding box.

**Initial output (before prompt improvements):** model placed chairs at the
same Z as tables (overlapping), and bars stuck out past the back wall by
their full depth.

**After improved prompting:** the LLM applied the math exactly. Chair_1 at
z=2.1 sat precisely `3.0 - 0.6 - 0.25 - 0.05` in front of a table at z=3.0.
Bars sat flush with back walls. Fireplaces flush with side walls. Bookshelves
ran along walls instead of floating mid-room.

Three test rooms all produced coherent layouts:

| Room          | Vertices | Objects | Notes |
|---------------|----------|---------|-------|
| Cozy tavern   | 4        | 9       | Bar+fireplace flush, table-chair pairs aligned |
| Forest glade  | 5        | 5       | Irregular polygon clearing, trees at the edges |
| Wizard's study| 4        | 6       | Bookshelves running along back AND left walls |

![tavern preview](../cache/test_layout/cozy_tavern/preview.png)

### Step 2b — 3D reference render ([commit `163193c`](#))

Added `Scene3D.render_reference()` that draws each object as a stacked
top+front quad (visible faces in the oblique projection). Painter's algorithm
back-to-front by world Z. Serves both as a debug visualization and as the
intended conditioning image for step 3.

![tavern reference](../cache/test_layout/cozy_tavern/reference.png)

### Step 3 — Reference-conditioned art (the wall we hit)

**File:** [scripts/test_paint.py](../scripts/test_paint.py)

**Attempt 1: pass reference + structured prompt to Gemini image model.**
The model painted *inside* the floor and object regions but **preserved the
blue sky background** of the reference at the borders. Specifically, the
corner pixels of the output had RGB `(170, 195, 218)` — exactly our
placeholder sky color. The model treated uniform background regions as a
mask: "don't touch."

**Attempt 2: make the reference visually drab so it doesn't look like
"finished background, leave alone."** Changed sky to dark gray. Same behavior
— now with gray borders instead of blue. The model also preserved the *light
gray top faces* of object boxes as gray rectangles in the painted output.
The image-to-image preservation bias is strong and doesn't yield to color
choices or prompt strength.

**Attempt 3: drop the reference image entirely, use a detailed text prompt
with explicit screen-pixel positions.** The output was **beautiful** — full
frame, atmospheric tavern with fireplace, candlelit bar, wooden ceiling
beams, barrels. But spatial precision was lost: the painter added extra
tables, moved things around, took its own artistic liberties.

![text-only paint](../cache/test_layout/cozy_tavern/painted.png)

## The architectural problem we ran into

We have two incompatible properties we want simultaneously:

1. **Beautiful, atmospheric art** (only image generation can really do this
   for arbitrary new rooms).
2. **Pixel-precise spatial alignment** between the art and the walkability
   map (only deterministic rendering can guarantee this).

The image model will not produce (2) on demand. Conditioning images don't
constrain its layout decisions tightly enough — it preserves uniform regions
as masks, ignores spatial constraints in prompts when they conflict with
"looking good," and rearranges objects freely.

This is the wall. Generative image models are fundamentally bad at producing
spatially-grounded gameplay assets.

## Architecture options we considered

### Option A — Tighter conditioning, somehow

Keep trying with different reference styles (priority map as conditioning,
edge-only outlines, noisy textures to defeat the "uniform region = mask"
heuristic, multi-step refinement passes). Risk: may never work robustly
with current image generation models; we'd be fighting the tool.

### Option B — Paint first, derive layout from art

Reverse the pipeline. Painter produces a full image first, then a vision
pass analyzes the painted result and infers object footprints and depth.
The priority map becomes a derivative of the art rather than the other way
around. Pro: art looks great. Con: every room needs a vision pass that's
itself error-prone; closer to where we started.

### Option C — Generous painted layout, vision pass to find actual positions

Paint with loose spatial guidance (text prompt only), then use a vision
model to detect where things actually ended up in the painted image, and
build the priority map from that. Same fundamental fragility as B.

### Option D — Pure 3D pipeline (Blender or equivalent)

Drop generative image models for the gameplay-critical rendering entirely.
Use a real 3D engine that renders both art and depth from the same camera.
The two are guaranteed spatially consistent because they're two outputs of
one render pass.

**Where the artistic look comes from in this world:**

- **D1. Stylized shaders on programmatic geometry.** Toon shading, painted
  textures, post-processing for hand-painted look. Materials are authored
  once, reused everywhere. Geometry stays box-primitive.
- **D2. Pre-made asset library.** Author (or commission) ~50 standard stylized
  models — `table_round.glb`, `chair_wooden.glb`, `fireplace_stone.glb` etc.
  LLM-designed scenes place them. This is what real adventure games do.
- **D3. AI-generated textures on deterministic geometry.** Use image gen for
  wood grain, stone surfaces, fabric patterns — UV-mapped onto a 3D scene.
  Geometry is pixel-precise; surfaces look hand-painted. The hybrid that
  plays to each tool's strengths.
- **D4. Sprite composition (classic adventure-game approach).** Background
  art is just the empty room shell (no furniture). Each object is rendered
  separately (in 3D or 2D) as a sprite with transparency and known depth.
  Composited at runtime by depth. This is what Sierra/LucasArts actually did
  — the painted background was just the room shell.

## Where we landed (tentatively)

Option **D4** is the most consistent with what's been discussed throughout:

> "we actually want sprites for most things. if we're rendering a room we
> just want to render the room as a static image. We'd create the table,
> chairs, bar, as separate sprites that have locations on them. then
> characters can walk around them"

The architecture would be:

1. **LLM designs scene** (same as our current step 2 — JSON of floor + objects).
2. **Background art = empty room shell.** Painted by the image model, prompted
   to show only walls, floor, sky, doorways. No furniture. Spatial precision
   matters less here because the player never collides with walls and sky.
3. **Each object rendered as a sprite** with transparent background. Could be:
   - 3D-rendered from a stylized model library (consistent style, deterministic
     position/depth metadata)
   - Generated by image model on a transparent canvas (riskier but unique
     per scene)
4. **Runtime composites** background + object sprites + character sprites,
   sorted by depth (Y-position or explicit depth band).
5. **Priority map computed deterministically** from the JSON scene description
   — same code as our current step 1.

The painted background and the priority map are no longer required to align
pixel-precisely because the priority map's "impassable" zones come from the
object sprites' positions, not from features painted into the background.

## What we kept that's still useful

Even if we pivot to D4, the work isn't wasted:

- **`Scene3D` + the oblique projection** ([world/scene3d.py](../world/scene3d.py))
  becomes the metadata layer for sprite compositing. It still computes the
  priority map from object footprints, and it still has the projection math
  that tells us where each sprite should render on screen.
- **`LayoutAgent`** ([agents/layout.py](../agents/layout.py)) keeps doing
  exactly what it does — producing structured scene JSON from text. Its
  output schema may grow (sprite reference IDs, foreground overlay hints)
  but the core stays.
- **The reference render** is dead as conditioning, but lives as a debug
  visualization for verifying layouts.
- **The placement idioms in the prompt** (flush-with-wall math, chair-table
  offsets) carry over verbatim.

## What we'd need to build next

For D4:

- Authoring or generating a starter asset library of 3D models for common
  objects (table, chair, fireplace, bookshelf, tree, boulder, bar, bed, ...).
  This is a one-time cost paid in advance.
- A sprite renderer: take a 3D asset + camera angle, render to PNG with
  alpha. Probably Blender headless or pyrender.
- A simpler background-art pipeline: prompt the image model for empty room
  shells only, no objects.
- A runtime sprite layer in the existing pygame rendering code that places
  sprites by world position, sorts by depth, and stacks them above/below
  the character based on band.
- The character renders behind/in-front of object sprites based on the
  same depth banding the priority map uses — but now the sprite *is* the
  visible obstacle, not painted features in the background.

For D1 or D3 (alternative D-paths if we want fewer pre-made assets):

- Material library: 5–10 stylized shaders for common surface types.
- For D3: pipeline for AI-generating tileable textures, UV-mapping onto
  3D primitives.
- More work upfront, more visual variety at the end.

## Alternatives worth revisiting later

- **ControlNet-style conditioning.** Open-source image models support depth-
  map conditioning that's far stronger than what Gemini exposes. If we self-
  host, we might be able to make option A work.
- **Real-time image-to-3D services.** Meshy, Tripo, etc. can produce 3D models
  from text. If their output quality reaches "drop into Blender scene," the
  asset-library cost of D2 drops dramatically.
- **AR/VR-style asset libraries.** Quaternius, Kenney, Synty — large free
  asset libraries of stylized models. Could bootstrap D2 in days, not months.

## Files in the repo from this exploration

```
world/
  scene3d.py                       # 3D scene + priority map + reference render

agents/
  layout.py                        # LLM scene designer (text -> JSON)

scripts/
  test_scene3d.py                  # hardcoded tavern, deterministic
  test_layout.py                   # end-to-end LLM layout pipeline
  test_paint.py                    # reference-conditioned art (the failed step)

design/
  priority_map_exploration.md      # this document

cache/                             # gitignored, run scripts to regenerate
  test_scene3d/                    # outputs from step 1
  test_layout/                     # outputs from step 2
    cozy_tavern/
    forest_glade/
    wizards_study/
```

## TL;DR

We tried to derive the priority map from a 3D scene description rather than
from generated art. The 3D-first half worked beautifully — clean
deterministic priority maps with proper depth bands, produced in <1 second
from a JSON scene description that an LLM happily generates.

The art half hit a wall: Gemini's image model can't be reliably conditioned
to paint a scene *at specific coordinates from a reference image*. It either
preserves the reference as a mask, or paints beautifully but loosely. Neither
is acceptable for gameplay where the priority map must match the visible art.

The most likely path forward is to abandon image generation for the
gameplay-critical layout entirely. Use a real 3D pipeline (Blender or
similar) to render objects as sprites — each pixel-precise and depth-aware.
Use image generation only for the empty-room background, where spatial
precision doesn't matter. Composite at runtime by depth. This is closer to
how classic adventure games actually worked.

This is a larger project than the spike we were on, so we're stopping here
to capture the state before committing to it.
