"""Draw the JARVIS icon (assets/jarvis.ico, 16-256 px) in the style of the HUD orb.

    .\\.venv\\Scripts\\python.exe scripts\\make_icon.py

Each size is drawn separately (supersampled 8x, then reduced) so small sizes
stay crisp instead of being a blurry shrink of the 256 px image.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "jarvis.ico"
SIZES = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]

BG = (3, 10, 22, 255)
BLUE = (42, 140, 255)
CYAN = (95, 227, 255)
CORE = (169, 242, 255)


def draw(size: int) -> Image.Image:
    ss = 8
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = s / 2
    # Rounded dark tile.
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=s * 0.22, fill=BG)

    # Soft glow behind the orb.
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    r = s * 0.30
    gd.ellipse([c - r, c - r, c + r, c + r], fill=CYAN + (110,))
    glow = glow.filter(ImageFilter.GaussianBlur(s * 0.08))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img)

    small = size <= 24
    # Outer dashed HUD ring (solid at tiny sizes, where dashes turn to mush).
    ro = s * 0.36
    w_outer = max(ss, int(s * (0.045 if small else 0.03)))
    if small:
        d.ellipse([c - ro, c - ro, c + ro, c + ro], outline=BLUE + (255,), width=w_outer)
    else:
        segments = 16
        for i in range(segments):
            a0 = i * 360 / segments + 4
            d.arc([c - ro, c - ro, c + ro, c + ro], a0, a0 + 360 / segments * 0.62, fill=BLUE + (255,), width=w_outer)
        # Tick marks at the four quarters.
        for k in range(4):
            a = math.radians(k * 90 - 45)
            x0, y0 = c + math.cos(a) * ro * 1.08, c + math.sin(a) * ro * 1.08
            x1, y1 = c + math.cos(a) * ro * 1.2, c + math.sin(a) * ro * 1.2
            d.line([x0, y0, x1, y1], fill=CYAN + (220,), width=w_outer)

    # Inner ring.
    ri = s * 0.235
    d.ellipse([c - ri, c - ri, c + ri, c + ri], outline=CYAN + (255,), width=max(ss, int(s * 0.045)))

    # Bright core.
    rc = s * 0.11
    d.ellipse([c - rc, c - rc, c + rc, c + rc], fill=CORE + (255,))
    hl = rc * 0.45
    d.ellipse([c - hl - rc * 0.25, c - hl - rc * 0.25, c + hl - rc * 0.25, c + hl - rc * 0.25], fill=(235, 252, 255, 255))

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    images = [draw(n) for n in SIZES]
    images[-1].save(OUT, format="ICO", sizes=[(n, n) for n in SIZES], append_images=images[:-1])
    images[-1].save(OUT.with_suffix(".png"))
    print(f"Wrote {OUT} ({', '.join(map(str, SIZES))} px)")


if __name__ == "__main__":
    main()
