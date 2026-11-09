"""One-time helper (needs Pillow, which the app itself does not): makes the app icons in web/images from the logo. Run: python tools/make_icons.py
The output files are committed, so a server never needs Pillow."""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
BG = (43, 26, 18, 255)  # the dark brown of the site


def icon(size, pad):
    logo = Image.open(ROOT / "web" / "images" / "logo-transparent.png").convert("RGBA")
    box = int(size * (1 - 2 * pad))
    logo.thumbnail((box, box))
    img = Image.new("RGBA", (size, size), BG)
    img.alpha_composite(logo, ((size - logo.width) // 2, (size - logo.height) // 2))
    return img.convert("RGB")


for size in (192, 512):
    icon(size, 0.14).save(ROOT / "web" / "images" / f"icon-{size}.png", optimize=True)
icon(512, 0.26).save(ROOT / "web" / "images" / "icon-maskable-512.png", optimize=True)  # extra padding: Android crops maskable icons to a circle or squircle
print("icons written")
