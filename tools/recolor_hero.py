"""Recolor the hero banner: maroon/pink kolam -> gold on warm brown, figure kept in original colours.
Usage: python tools/recolor_hero.py   (reads web/images/hero-final-braid.png, writes hero-banner.png + hero-figure.png)"""
import numpy as np
from PIL import Image
from scipy import ndimage as ndi

SRC, DIR = "web/images/hero-final-braid.png", "web/images/"
BG_OLD = np.array([107, 29, 28.0]); LINE_OLD = np.array([200, 160, 152.0])
GOLD = np.array([217, 172, 98.0]); PATTERN_OPACITY = 0.42

src = np.array(Image.open(SRC).convert("RGB")).astype(float)
h, w, _ = src.shape

# pattern strength a (0 = bg, 1 = line) by projecting each pixel on the bg->line axis
d = LINE_OLD - BG_OLD
a = np.clip(((src - BG_OLD) @ d) / (d @ d), 0, 1)
resid = np.linalg.norm(src - (BG_OLD + a[..., None] * d), axis=2)

# figure mask: pixels that are neither bg nor a bg/line blend, inside the figure's bounding area
box = np.zeros((h, w), bool); box[70:410, 730:1030] = True
m = (resid > 14) & box
m = ndi.binary_opening(m, iterations=3)          # drop thin pattern-line leftovers
m = ndi.binary_closing(m, iterations=8)
lab, n = ndi.label(m)
m = lab == (1 + np.argmax(ndi.sum(m, lab, range(1, n + 1))))
m = ndi.binary_fill_holes(ndi.binary_closing(m, iterations=10))   # beard/white details inside
m = ndi.binary_erosion(m, iterations=1)           # shave the maroon fringe
alpha = ndi.gaussian_filter(m.astype(float), 0.8)

# new background: vertical brown gradient + gold pattern
t = np.linspace(0, 1, h)[:, None, None]
c1, c2 = np.array([90, 53, 34.0]), np.array([58, 36, 23.0])
bg = c1 + (c2 - c1) * t
out = bg + (a * PATTERN_OPACITY)[..., None] * (GOLD - bg)
out = out * (1 - alpha[..., None]) + src * alpha[..., None]
Image.fromarray(out.round().astype(np.uint8)).save(DIR + "hero-banner.png", optimize=True)

fig = np.dstack([src, alpha * 255]).round().astype(np.uint8)
ys, xs = np.where(m); Image.fromarray(fig).crop((xs.min() - 4, ys.min() - 4, xs.max() + 5, ys.max() + 5)).save(DIR + "hero-figure.png")
print("figure bbox", xs.min(), ys.min(), xs.max(), ys.max())
