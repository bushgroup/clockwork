"""The seam: the three lower layers never import Qt, even transitively.

Checked in a fresh interpreter so nothing this test process already imported
can mask a leak.
"""

import subprocess
import sys

LOWER_LAYERS = ("clockwork.mips", "clockwork.acq", "clockwork.method")

PROBE = """
import importlib, sys
for name in {names!r}:
    importlib.import_module(name)
prefixes = ("PySide6", "PyQt", "pyqtgraph", "shiboken")
leaked = sorted(m for m in sys.modules if m.startswith(prefixes))
print(",".join(leaked))
"""


def test_lower_layers_do_not_import_qt() -> None:
    out = subprocess.run(
        [sys.executable, "-c", PROBE.format(names=LOWER_LAYERS)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert out == "", f"Qt modules leaked into the data layers: {out}"
