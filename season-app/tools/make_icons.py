#!/usr/bin/env python3
"""Generate the app's icons from the crest.

Kept as a script, with the source alongside it, so the icons in `static/` have
a provenance: a binary blob nobody can regenerate is a blob nobody can change.
Run it after editing the crest.

    python3 tools/make_icons.py

Two shapes, because Android crops icons and iOS does not:

- **any** — the crest cropped to its own ink, so the ring reaches the edge.
  This is the favicon and the iOS home-screen icon, both of which are shown
  exactly as given.
- **maskable** — the same crest at 70% on a solid ground. Android masks an
  icon to a circle, a squircle or a rounded square depending on the launcher,
  and only the central 80% is guaranteed to survive. An `any` icon used as
  maskable gets its ring shaved off; this one has room to be cropped.
"""
from pathlib import Path
from PIL import Image

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "app" / "static"
SOURCE = HERE / "crest-1024.png"
# White, and specifically not the app's --bg. The crest is drawn on its own
# white field, so any other ground shows as a hard square seam behind a round
# crest. Dark is out for the same reason it can't be a dark icon at all: the
# ink would vanish into it.
GROUND = (255, 255, 255)


def ink_bounds(im):
    """The crest's own edges, ignoring the white margin around it."""
    mask = im.convert("L").point(lambda v: 255 if v < 200 else 0)
    return mask.getbbox()


def main():
    src = Image.open(SOURCE).convert("RGB")
    crest = src.crop(ink_bounds(src))

    for size in (180, 192, 512):
        crest.resize((size, size), Image.LANCZOS).save(
            STATIC / f"icon-{size}.png", optimize=True)

    # 70% leaves the whole crest inside the 80% safe circle with a little air.
    side = 512
    inner = round(side * 0.70)
    canvas = Image.new("RGB", (side, side), GROUND)
    off = (side - inner) // 2
    canvas.paste(crest.resize((inner, inner), Image.LANCZOS), (off, off))
    canvas.save(STATIC / "icon-maskable-512.png", optimize=True)

    for f in sorted(STATIC.glob("icon-*.png")):
        print(f"{f.name}: {Image.open(f).size[0]}px, {f.stat().st_size // 1024}KB")


if __name__ == "__main__":
    main()
