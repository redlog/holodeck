"""Small PIL helpers shared by the imagery agents.

Kept transport- and game-agnostic: pure image geometry, no game state.
"""

import io

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
