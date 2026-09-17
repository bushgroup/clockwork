"""Rasterise `packaging/icon/clockwork.svg` into the multi-resolution
`src/clockwork/app/resources/clockwork.ico` that the `.exe`, the installer and the
placeholder window all use.

Run:  uv run tools/make_icon.py            # rewrite the .ico
      uv run tools/make_icon.py --check    # exit nonzero if the .ico is out of date

One source drawing, unlike mainspring's two-tier scheme (`../mainspring/tools/make_icon.py`):
this is placeholder art -- a plain clock face, task 08's successors' to replace -- with no
fine detail that a wider gap at small sizes would protect.

Qt does the rendering (QSvgRenderer, already a dependency through PySide6) and this module
writes the ICO container itself, because Qt's ICO writer emits one frame per file and an
icon that Windows can pick a size from needs all of them in one. Frames at or below 64 px
are stored as 32-bit BGRA DIBs, the form every Windows version reads; the 128 and 256 px
frames are stored as PNG, as they have been since Vista, which keeps the file to a few tens
of kilobytes.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SOURCE = os.path.join(ROOT, "packaging", "icon", "clockwork.svg")
TARGET = os.path.join(ROOT, "src", "clockwork", "app", "resources", "clockwork.ico")

# 24 px is here because Windows asks for it at 125% and 150% display scaling in
# Explorer's list views.
SIZES = (256, 128, 64, 48, 32, 24, 16)


def _render(path: str, size: int) -> QImage:  # noqa: F821 - Qt import is deferred
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(path)
    if not renderer.isValid():
        raise SystemExit(f"not a readable SVG: {path}")
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def _dib(image: QImage) -> bytes:  # noqa: F821
    """A frame in the BMP form an ICO expects: BITMAPINFOHEADER, then bottom-up BGRA
    rows, then an AND mask.

    The header's height is doubled because the format counts the mask's rows as well as
    the image's. The mask itself is left all zeros -- "opaque everywhere" -- since the
    32-bit frames carry their own alpha and Windows honours it; the bytes are still
    written because the length in the header promises them.
    """
    width, height = image.width(), image.height()
    pixels = bytearray()
    for y in range(height - 1, -1, -1):  # bottom-up
        for x in range(width):
            pixel = image.pixel(x, y)
            alpha, red = (pixel >> 24) & 0xFF, (pixel >> 16) & 0xFF
            green, blue = (pixel >> 8) & 0xFF, pixel & 0xFF
            pixels += bytes((blue, green, red, alpha))
    mask_stride = ((width + 31) // 32) * 4  # 1 bit per pixel, rows padded to 4 bytes
    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # header size
        width,
        height * 2,  # image rows + mask rows
        1,  # planes
        32,  # bits per pixel
        0,  # BI_RGB, uncompressed
        len(pixels) + mask_stride * height,
        0,
        0,
        0,
        0,
    )
    return header + bytes(pixels) + bytes(mask_stride * height)


def _png(image: QImage) -> bytes:  # noqa: F821
    from PySide6.QtCore import QBuffer, QByteArray

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def build() -> bytes:
    """The whole .ico as bytes."""
    frames = [_render(SOURCE, size) for size in SIZES]
    encoded = [_png(image) if size >= 128 else _dib(image)
               for size, image in zip(SIZES, frames, strict=True)]

    directory = struct.pack("<HHH", 0, 1, len(encoded))  # reserved, type 1 = icon, count
    offset = len(directory) + 16 * len(encoded)
    entries = b""
    for size, frame in zip(SIZES, encoded, strict=True):
        entries += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,  # 256 is stored as 0: the field is one byte
            size if size < 256 else 0,
            0,  # palette size, 0 for a direct-colour frame
            0,  # reserved
            1,  # planes
            32,  # bits per pixel
            len(frame),
            offset,
        )
        offset += len(frame)
    return directory + entries + b"".join(encoded)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the .ico differs from the source",
    )
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # no display needed to rasterise
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    data = build()

    if args.check:
        current = open(TARGET, "rb").read() if os.path.exists(TARGET) else b""
        if current == data:
            print(f"{os.path.relpath(TARGET, ROOT)} is up to date")
            return 0
        print(
            f"{os.path.relpath(TARGET, ROOT)} is out of date -- run `uv run tools/make_icon.py`",
            file=sys.stderr,
        )
        return 1

    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    with open(TARGET, "wb") as handle:
        handle.write(data)
    print(
        f"wrote {os.path.relpath(TARGET, ROOT)}: "
        f"{len(SIZES)} frames ({', '.join(str(s) for s in SIZES)}), {len(data) / 1024:.0f} KiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
