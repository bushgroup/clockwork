"""Self-check for a fresh clone: no MIPS box, no digitizer, no console, no lab repo needed.

Every check here must pass in a bare public clone. Checks that need something a clone does
not ship -- a serial port with a box on it, a running acquisition console, the lab
repository -- are reported as SKIPPED when it is absent, never as FAIL.

What it covers today: the package imports, the version declarations agree, the three lower
layers stay free of Qt, the module layout is complete, lab-directory resolution behaves, a
method document round-trips, and the MIPS sender drives a simulated box through a table load,
a TBLRPT round trip, arming and a rejection. Tasks add sections as they land code.

Run:  uv run tools/check_public.py
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

FAIL: list[str] = []
SKIPPED: list[str] = []


def check_true(name: str, cond: object) -> None:
    print("{:4s} {}".format("OK" if cond else "FAIL", name))
    if not cond:
        FAIL.append(name)


def skip(name: str, why: str) -> None:
    print(f"SKIP {name} ({why})")
    SKIPPED.append(name)


def section(title: str) -> None:
    print()
    print("--- " + title + " " + "-" * max(3, 72 - len(title)))


def declared_versions() -> dict[str, str]:
    """The version as each file that hand-carries it states it.

    Nothing derives one from another; they only agree because someone keeps them
    agreeing, which is why this is checked rather than trusted. The Inno Setup
    script joins the set once packaging exists.
    """
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as handle:
        pyproject = tomllib.load(handle)["project"]["version"]
    out = {"pyproject.toml": pyproject}
    iss = os.path.join(ROOT, "packaging", "clockwork.iss")
    if os.path.isfile(iss):
        found = re.search(r'^#define\s+MyAppVersion\s+"([^"]+)"',
                          open(iss, encoding="utf-8").read(), re.MULTILINE)
        out["packaging/clockwork.iss"] = found.group(1) if found else "(not found)"
    return out


LOWER_LAYERS = ("clockwork.mips", "clockwork.acq", "clockwork.method")
QT_PREFIXES = ("PySide6", "PyQt", "pyqtgraph", "shiboken")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    section("package and versions")
    import clockwork

    check_true("clockwork imports and carries a version", bool(clockwork.__version__))
    declared = declared_versions() | {"clockwork.__version__": clockwork.__version__}
    check_true(
        "every version declaration agrees ("
        + ", ".join(f"{where} {what}" for where, what in declared.items()) + ")",
        len(set(declared.values())) == 1,
    )

    section("module layout and the no-Qt seam")
    for name in LOWER_LAYERS + ("clockwork.app",):
        try:
            importlib.import_module(name)
            check_true(f"{name} imports", True)
        except Exception as exc:  # noqa: BLE001 -- reporting, not handling
            check_true(f"{name} imports ({exc!r})", False)
    probe = (
        "import importlib, sys\n"
        f"for n in {LOWER_LAYERS!r}: importlib.import_module(n)\n"
        f"print(','.join(sorted(m for m in sys.modules if m.startswith({QT_PREFIXES!r}))))\n"
    )
    leaked = subprocess.run([sys.executable, "-c", probe], check=True,
                            capture_output=True, text=True).stdout.strip()
    check_true("the lower layers import without pulling in Qt"
               + (f" (leaked: {leaked})" if leaked else ""), leaked == "")

    section("lab-directory resolution")
    lab = clockwork.lab_dir()
    if lab is None:
        skip("lab repo resolves",
             "no lab checkout beside this clone; a public clone is expected to lack it")
    else:
        check_true(f"lab repo resolves to a task system ({lab})",
                   os.path.isfile(os.path.join(lab, "tasks", "README.md")))
        for sub in ("tasks", "notes"):
            check_true(f"lab_dir({sub!r}) resolves", clockwork.lab_dir(sub) is not None)
    try:
        clockwork.lab_dir("not-a-lab-directory")
        check_true("lab_dir rejects an unknown directory name", lab is None)
    except ValueError:
        check_true("lab_dir rejects an unknown directory name", True)

    section("method file")
    from clockwork import method

    sample = (
        "schema_version = 1\n\n"
        '[metadata]\nname = "check_public-sample"\ncreated = 2026-09-06\n\n'
        "[acquisition]\nframes = 1\nscans = 100\naccumulations = 10\n"
        'file_stem = "check_public-sample"\n\n'
        '[[boxes]]\nname = "box1"\nport = "COM3"\nstrings = ["STBLCLK,EXT"]\n'
    )
    m = method.loads(sample)
    check_true("a sample method document loads", m.metadata.name == "check_public-sample")
    check_true("dumps then loads round-trips the method", method.loads(method.dumps(m)) == m)
    stamp = method.stamp(m, console_version="0.0.0-check")
    check_true(
        "stamp() carries a hash, text and versions",
        stamp["method_hash"] and stamp["method_text"] and stamp["clockwork_version"],
    )
    try:
        method.loads("schema_version = 1\n")
        check_true("an incomplete method document is rejected", False)
    except method.MethodError:
        check_true("an incomplete method document is rejected", True)

    section("MIPS serial")
    from clockwork import mips

    fake = mips.FakeBox(name="check_public-box")
    box = mips.Box(transport=fake, name="fake")
    check_true(f"a simulated box answers GVER ({box.version()})", bool(box.version()))
    check_true("and GNAME", box.box_name() == "check_public-box")

    example = "STBLDAT;25:[A:10,10:A:1,25:A:0:5:34.5,100:];"
    load = box.send_table(example)
    check_true(
        f"a table loads and is ACKed ({load.bytes_sent} bytes, "
        f"{load.predicted.byte_size} on the box)",
        load.prediction_error is None,
    )
    check_true("TBLRPT reads back the table that was sent", box.verify_table(load) == [])

    # A long table has to be chunked: the box's 4096-byte input buffer drops
    # what overruns it without saying so (docs/mips-wire-format.md §1).
    long_events = ",".join(f"{tick}:A:1" for tick in range(100, 2000, 2))
    long_table = f"STBLDAT;0:[A:1,{long_events},4000:];"
    long_load = box.send_table(long_table)
    check_true(
        f"a table of {len(long_table)} bytes streams without losing characters",
        fake.dropped_bytes == 0 and box.verify_table(long_load) == [],
    )

    box.arm()
    box.trigger()
    seen = box.drain(0.05)
    check_true(
        "arming and a pass report TBLRDY, TBLTRIG, TBLCMPLT and the re-arm ("
        + ", ".join(event.name for event in seen) + ")",
        seen == [mips.TableEvent.TRIGGERED, mips.TableEvent.COMPLETE, mips.TableEvent.READY],
    )
    try:
        box.command("NOSUCHCMD")
        check_true("a bad command is rejected with the box's own error code", False)
    except mips.BoxRejected as exc:
        check_true(
            f"a bad command is rejected with the box's own error code ({exc.code})",
            exc.code == 1,
        )

    section("hardware")
    skip("a MIPS box answers GVER", "no serial hardware in a self-check; lab record, task 04")
    skip("the acquisition console answers info", "no console in a self-check; lab record, task 03")

    print()
    print(f"{len(FAIL)} failed, {len(SKIPPED)} skipped")
    for name in FAIL:
        print("  FAIL", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
