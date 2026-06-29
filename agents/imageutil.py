"""Small PIL helpers shared by the imagery agents.

Kept transport- and game-agnostic: pure image geometry, no game state.
"""

import io
import os
import time
import uuid
from pathlib import Path

from PIL import Image


def crop_to_aspect(img, w_ratio, h_ratio):
    """Center-crop a PIL image to the given aspect ratio (no scaling).

    The Gemini image model ignores the aspect_ratio request and tends to
    return roughly square frames, so callers normalize the framing here:
    rooms to 16:9, portraits/items to 1:1.
    """
    target = w_ratio / h_ratio
    w, h = img.size
    if h <= 0 or w <= 0:
        return img
    if w / h > target:
        # Too wide — trim the sides.
        new_w = max(1, int(round(h * target)))
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    # Too tall — trim top and bottom.
    new_h = max(1, int(round(w / target)))
    top = (h - new_h) // 2
    return img.crop((0, top, w, top + new_h))


def to_png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def save_png_atomic(img, path):
    """Write a PNG so concurrent readers never see a half-written file.

    Room images are overwritten in place on delta regen, and the FastAPI
    `/media` mount serves them with a static FileResponse: if a browser fetch
    stats the old (smaller) file and then PIL grows it under the same path
    mid-send, Starlette raises "Response content longer than Content-Length".
    Writing to a temp sibling and renaming makes the swap atomic — the reader
    holds a complete inode either way.

    On Windows `os.replace` fails (WinError 5 / sharing violation) while a
    reader still has the target open, so retry briefly; the reader finishes
    against the old file in the meantime.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    img.save(tmp, "PNG")
    last_err = None
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return path
        except PermissionError as e:  # Windows: target open by a /media reader
            last_err = e
            time.sleep(0.1)
    # Give up on the atomic swap but don't leak the temp file.
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last_err
