"""One-off asset baker. Run by hand, never in CI; its output is committed to generator/data.

usage: bake_assets.py <fonts_dir> <icons_dir> <measured_languages.json>
needs: fonttools, brotli, numpy, pillow
"""
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from PIL import Image

OUT = Path(__file__).resolve().parents[1] / "data"
CHARS = "".join(chr(c) for c in range(32, 127)) + "·—–öüÖÜçşğıİŞĞ"
FONTS = {"mono": "jetbrains-mono-400", "monob": "jetbrains-mono-700", "sans": "space-grotesk-500", "sansb": "space-grotesk-700"}
ICONS = {
    "java": "openjdk", "kotlin": "kotlin", "typescript": "typescript", "python": "python", "javascript": "javascript",
    "csharp": "dotnet", "mongodb": "mongodb", "mariadb": "mariadb", "mysql": "mysql", "redis": "redis",
    "postgresql": "postgresql", "docker": "docker", "linux": "linux", "git": "git", "github": "github",
    "actions": "githubactions", "nginx": "nginx", "gradle": "gradle", "grafana": "grafana", "nextjs": "nextdotjs",
    "pterodactyl": "pterodactyl",
}


def bake_glyphs(fonts_dir):
    result = {}
    for key, name in FONTS.items():
        font = TTFont(fonts_dir / f"{name}.woff2")
        scale = 1000 / font["head"].unitsPerEm
        glyphset, cmap, hmtx = font.getGlyphSet(), font.getBestCmap(), font["hmtx"]
        table = {}
        for ch in CHARS:
            glyph = cmap.get(ord(ch))
            if glyph is None:
                print(f"{key}: no glyph for {ch!r}")
                continue
            pen = SVGPathPen(glyphset, ntos=lambda v: str(round(v)))
            glyphset[glyph].draw(TransformPen(pen, (scale, 0, 0, -scale, 0, 0)))
            table[ch] = [pen.getCommands(), round(hmtx[glyph][0] * scale)]
        result[key] = table
    (OUT / "glyphs.json").write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def bake_icons(icons_dir):
    result = {}
    for key, name in ICONS.items():
        svg = (icons_dir / f"{name}.svg").read_text(encoding="utf-8")
        result[key] = re.search(r'\sd="([^"]+)"', svg).group(1)
    (OUT / "icons.json").write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")


def bake_languages(path):
    """Bytes measured across the owner's repositories, public and private. SQL dumps and HTML are excluded."""
    measured = json.loads(path.read_text(encoding="utf-8"))["bytes"]
    keep = {k: v for k, v in measured.items() if k not in ("SQL", "HTML")}
    (OUT / "languages.json").write_text(json.dumps(keep, indent=1), encoding="utf-8")


def smooth_noise(n, knots, rng):
    """Periodic-free 1D value noise with cosine interpolation."""
    pts = rng.random(knots + 2)
    x = np.linspace(0, knots, n)
    i = x.astype(int)
    f = (1 - np.cos((x - i) * math.pi)) / 2
    return pts[i] * (1 - f) + pts[i + 1] * f


def aurora(w, h, seed, phase):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    v = yy / h

    # Night sky.
    top, bottom = np.array([5, 8, 18.0]), np.array([10, 17, 34.0])
    img = top[None, None, :] * (1 - v[..., None]) + bottom[None, None, :] * v[..., None]

    # Stars thin out towards the horizon.
    star_rng = np.random.default_rng(7)
    for _ in range(420):
        sx, sy = star_rng.integers(0, w), int(star_rng.random() ** 1.6 * h * 0.72)
        img[sy, sx] += star_rng.uniform(40, 190)

    # Curtains: a wandering lower edge, long upward decay, and fine vertical rays.
    curtains = (
        (0.62, 0.20, 0.42, (46, 230, 170), 1.00),
        (0.44, 0.15, 0.30, (56, 189, 248), 0.70),
        (0.70, 0.12, 0.34, (167, 139, 250), 0.55),
    )
    for base, amp, decay, colour, gain in curtains:
        edge = (base + amp * (smooth_noise(w, 5, rng) - 0.5) * 2
                + 0.05 * np.sin(xx[0] / w * 11 + phase + rng.random() * 6)) * h
        rays = smooth_noise(w, 150, rng) * 0.55 + smooth_noise(w, 46, rng) * 0.45
        rays = np.clip(rays * 1.5 - 0.15, 0, 1) ** 1.3
        envelope = np.clip(smooth_noise(w, 4, rng) * 1.7 - 0.25, 0, 1)
        depth = (edge[None, :] - yy) / (decay * h)
        profile = np.where(depth > 0, np.exp(-depth * 1.5) * (1 - 0.55 * np.exp(-depth * 3)), 0.45 * np.exp(depth * 5))
        light = profile * (0.28 + 0.72 * rays[None, :]) * envelope[None, :] * gain
        img += light[..., None] * np.array(colour, dtype=np.float32) * 0.92

    # Stepped voxel ridges in three depth layers.
    for layer, (level, relief, step, shade) in enumerate(((0.78, 0.17, 28, (20, 38, 64)), (0.86, 0.12, 22, (12, 22, 42)), (0.94, 0.07, 16, (6, 10, 20)))):
        ridge_rng = np.random.default_rng(100 + layer)
        ridge = smooth_noise(w, 9 + layer * 4, ridge_rng) * 0.65 + smooth_noise(w, 26, ridge_rng) * 0.35
        stepped = np.repeat(ridge[::step], step)[:w]
        line = np.round(((level - relief * (stepped - 0.5) * 2) * h) / step) * step
        mask = yy >= line[None, :]
        below = np.clip((yy - line[None, :]) / (h * 0.22), 0, 1)[..., None]
        img = np.where(mask[..., None], np.array(shade, dtype=np.float32) * (1 - below * 0.45), img)
        rim = mask & (yy < line[None, :] + 3)
        img[rim] += np.array((30, 74, 84), dtype=np.float32) * (1 - layer * 0.35)

    # Vignette plus grain, which keeps the dark gradients from banding.
    d = ((xx / w - 0.5) ** 2 * 1.3 + (v - 0.5) ** 2 * 0.8)
    img *= (1 - np.clip(d, 0, 1) * 0.75)[..., None]
    img += rng.normal(0, 1.5, img.shape)
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")


if __name__ == "__main__":
    fonts, icons, languages = map(Path, sys.argv[1:4])
    OUT.mkdir(parents=True, exist_ok=True)
    bake_glyphs(fonts)
    bake_icons(icons)
    bake_languages(languages)
    for name, seed, phase in (("aurora-a", 21, 0.0), ("aurora-b", 21, 1.1)):
        aurora(1400, 616, seed, phase).save(OUT / f"{name}.webp", "WEBP", quality=72, method=6)
    for f in sorted(OUT.iterdir()):
        print(f.name, f.stat().st_size)
