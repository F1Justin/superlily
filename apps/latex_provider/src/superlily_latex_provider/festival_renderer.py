"""Apply packaged, text-free festival ornaments around the TeX-rendered body."""
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import json

from superlily_contracts.festival_themes import PALETTES

ASSETS = Path(__file__).with_name("festival_assets")

@lru_cache(maxsize=16)
def _ornament(theme_id: str, edge: str) -> bytes:
    name = f"{theme_id}-{edge}.png"
    manifest = json.loads((ASSETS / "manifest.json").read_text())
    content = (ASSETS / name).read_bytes()
    if sha256(content).hexdigest() != manifest[name]:
        raise ValueError("festival ornament checksum mismatch")
    return content


def decorate_document(content: bytes, theme_id: str) -> bytes:
    if theme_id == "default":
        return content
    from PIL import Image
    background = "#" + PALETTES[theme_id][0]
    with Image.open(BytesIO(content)) as decoded:
        body = decoded.convert("RGB")
    w, h = body.size
    # Poppler can round the PDF page up to a final white pixel row/column.
    # The TeX template has an 8pt content margin; remove only fully white edges.
    for box in ((0, 0, w, 1), (0, h-1, w, h), (0, 0, 1, h), (w-1, 0, w, h)):
        if body.crop(box).getextrema() == ((255, 255),) * 3:
            body.paste(background, box)
    side, top, bottom = (max(1, round(v * w / 2048)) for v in (70, 280, 150))
    width, height = w + 2 * side, h + top + bottom
    result = Image.new("RGBA", (width, height), background)
    result.paste(body, (side, top))
    for edge, target_height, y in (("top", top, 0), ("bottom", bottom, top+h)):
        with Image.open(BytesIO(_ornament(theme_id, edge))) as decoded:
            overlay = decoded.convert("RGBA").resize((width, target_height), Image.Resampling.LANCZOS)
        result.alpha_composite(overlay, (0, y))
    # The transport bounds apply to the complete decorated document.
    result.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
    output = BytesIO()
    result.convert("RGB").save(output, format="PNG")
    return output.getvalue()
