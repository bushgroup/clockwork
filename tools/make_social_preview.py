"""Draw `packaging/icon/social-preview.png`, the 1280x640 card GitHub shows when a link
to the repository is unfurled in a browser tab, a chat client or a search result.

Run:  uv run tools/make_social_preview.py
      uv run tools/make_social_preview.py --open    # and show it

GitHub has no API for this image: the file this writes has to be uploaded by hand, under
Settings > General > Social preview. It is drawn here rather than kept as a binary nobody
can regenerate, so a change of wording or of the mark is an edit and a re-run.

The mark is `packaging/icon/clockwork.svg` itself, rendered through Qt (QSvgRenderer, a
PySide6 dependency already) at the size it is drawn, not scaled up from the .ico. The
palette is the icon's own eight-stop viridis ramp, on a near-black card that holds up
against both of GitHub's themes.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SOURCE = os.path.join(ROOT, "packaging", "icon", "clockwork.svg")
TARGET = os.path.join(ROOT, "packaging", "icon", "social-preview.png")

WIDTH, HEIGHT = 1280, 640

# The ramp's two ends and the green a third of the way down from the top, which is the
# one stop that stays legible as small text on black.
VIRIDIS_DARK = "#440154"
VIRIDIS_GREEN = "#4ac16d"
VIRIDIS_YELLOW = "#fde725"

WORDMARK = "clockwork"
TAGLINE = "Control software for ion mobility mass spectrometry"
HARDWARE = "MIPS controllers   ·   Keysight SA220P   ·   AqMD3 console   ·   UIMF"

# A centred stack: the mark, the wordmark, a rule, the tagline, the hardware. Centred
# rather than set beside the mark because the tagline is fifty characters and wraps
# against anything narrower than the full card. Four elements and not five: an unfurled
# card is often drawn around 500 px wide, where a fifth line of small print is a grey
# smear, and the repository's name and licence are printed beside the image anyway.
MARK_BOX = (WIDTH / 2 - 112, 60, 224, 224)
RULE_WIDTH = 300


def _font(families: list[str], size: int, weight: int, spacing: float = 0.0):
    """The first of `families` this machine actually has, at `size` pixels.

    Qt substitutes silently for a missing family, which on a card whose whole job is to
    be read would go unnoticed until it was published, so the families are tried in turn
    and a machine with none of them is an error rather than a substitution.
    """
    from PySide6.QtGui import QFont, QFontDatabase

    available = set(QFontDatabase.families())
    chosen = next((name for name in families if name in available), None)
    if chosen is None:
        raise SystemExit(f"none of these fonts is installed: {', '.join(families)}")
    font = QFont(chosen)
    font.setPixelSize(size)
    font.setWeight(QFont.Weight(weight))
    if spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return font


def draw() -> None:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import (QColor, QImage, QLinearGradient, QPainter, QPen,
                               QRadialGradient)
    from PySide6.QtSvg import QSvgRenderer

    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    # The card: near-black, lifted very slightly towards the ramp's dark end at the top
    # left so the mark does not sit on a flat field.
    backdrop = QLinearGradient(0, 0, WIDTH, HEIGHT)
    backdrop.setColorAt(0.0, QColor("#12151c"))
    backdrop.setColorAt(1.0, QColor("#08090c"))
    painter.fillRect(0, 0, WIDTH, HEIGHT, backdrop)

    glow = QRadialGradient(MARK_BOX[0] + MARK_BOX[2] / 2,
                           MARK_BOX[1] + MARK_BOX[3] / 2, 420)
    warm = QColor(VIRIDIS_DARK)
    warm.setAlpha(90)
    glow.setColorAt(0.0, warm)
    glow.setColorAt(1.0, QColor(0, 0, 0, 0))
    painter.fillRect(0, 0, WIDTH, HEIGHT, glow)

    renderer = QSvgRenderer(SOURCE)
    if not renderer.isValid():
        raise SystemExit(f"not a readable SVG: {SOURCE}")
    renderer.render(painter, QRectF(*MARK_BOX))

    centred = int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

    painter.setPen(QPen(QColor("#f2f4f7")))
    painter.setFont(_font(["Segoe UI Semibold", "Segoe UI", "Arial"], 112, 600, -1.0))
    painter.drawText(QRectF(0, 314, WIDTH, 120), centred, WORDMARK)

    # A rule in the ramp itself, under the wordmark rather than through it: the text is
    # drawn centred in a box, so the rule is placed below that box and not at a baseline
    # guessed from the pixel size.
    rule = QLinearGradient((WIDTH - RULE_WIDTH) / 2, 0, (WIDTH + RULE_WIDTH) / 2, 0)
    rule.setColorAt(0.0, QColor(VIRIDIS_GREEN))
    rule.setColorAt(1.0, QColor(VIRIDIS_YELLOW))
    painter.fillRect(QRectF((WIDTH - RULE_WIDTH) / 2, 444, RULE_WIDTH, 3), rule)

    painter.setPen(QPen(QColor("#c3ccd7")))
    painter.setFont(_font(["Segoe UI", "Arial"], 38, 400))
    painter.drawText(QRectF(0, 466, WIDTH, 56), centred, TAGLINE)

    painter.setPen(QPen(QColor(VIRIDIS_GREEN)))
    painter.setFont(_font(["Cascadia Mono", "Consolas", "Courier New"], 24, 400, 0.6))
    painter.drawText(QRectF(0, 534, WIDTH, 36), centred, HARDWARE)

    painter.end()
    if not image.save(TARGET, "PNG"):
        raise SystemExit(f"could not write {TARGET}")
    print(f"{os.path.relpath(TARGET, ROOT)}  {WIDTH}x{HEIGHT}  "
          f"{os.path.getsize(TARGET) / 1024:.0f} KiB")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--open", action="store_true",
                    help="open the written file in the default viewer")
    args = ap.parse_args()

    # Qt needs a QGuiApplication before a font database exists. The platform plugin is
    # left to Qt rather than forced to `offscreen`: that plugin carries no font database
    # at all, and a card drawn under it comes out with every glyph a tofu box and no
    # error to say so. The check below is what refuses to write one.
    from PySide6.QtGui import QFontDatabase, QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])
    if not QFontDatabase.families():
        raise SystemExit(
            "Qt loaded no fonts under the "
            f"{os.environ.get('QT_QPA_PLATFORM', 'default')} platform plugin, so every "
            "glyph would be drawn as an empty box; unset QT_QPA_PLATFORM and re-run")
    draw()
    del app

    if args.open:
        os.startfile(TARGET)  # noqa: S606 - Windows, and the path is this module's own
    return 0


if __name__ == "__main__":
    sys.exit(main())
