"""Self-check for a fresh clone: no MIPS box, no digitizer, no console, no lab repo needed.

Every check here must pass in a bare public clone. Checks that need something a clone does
not ship -- a serial port with a box on it, a running acquisition console, the lab
repository -- are reported as SKIPPED when it is absent, never as FAIL.

What it covers today: the package imports, the version declarations agree, the three lower
layers stay free of Qt, the module layout is complete, lab-directory resolution behaves, a
method document round-trips through its phases, start sequence and repetition modes, the
MIPS sender drives a simulated box through a table load, a TBLRPT round trip, arming and a
rejection, the console client drives a simulated console from `info` through a whole
frame to `finished acquire`, and a frame that published nothing and a frame the console
reported an error on are both refused rather than reported as successes. Tasks add
sections as they land code.

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
        f"schema_version = {method.SCHEMA_VERSION}\n\n"
        'start = [["box2", "TARBTRG"], ["box1", "TBLSTRT"]]\n'
        'reset = [["box1", "SMOD,LOC"], ["box1", "SMOD,TBL"]]\n\n'
        '[metadata]\nname = "check_public-sample"\ncreated = 2026-09-06\n\n'
        "[acquisition]\nframes = 1\nscans = 100\naccumulations = 10\n"
        'repetition_mode = "per_repetition"\nkeep_raw = true\n'
        'file_stem = "check_public-sample"\n\n'
        '[[boxes]]\nname = "box1"\nport = "COM3"\nsetup = ["STBLCLK,EXT"]\n'
        'load = ["STBLDAT;0:[A:1,100:];"]\narm = ["SMOD,TBL"]\n\n'
        '[[boxes]]\nname = "box2"\nport = "COM4"\nsetup = ["SWFREQ,1,15000"]\n'
    )
    m = method.loads(sample)
    check_true("a sample method document loads", m.metadata.name == "check_public-sample")
    check_true("dumps then loads round-trips the method", method.loads(method.dumps(m)) == m)
    check_true(
        "the start sequence keeps its cross-box order",
        [(step.box, step.command) for step in m.start]
        == [("box2", "TARBTRG"), ("box1", "TBLSTRT")],
    )
    check_true(
        "one console frame per repetition is one ion mobility experiment long "
        f"(frame_length {m.acquisition.frame_length}, {m.acquisition.console_frames} frames)",
        m.acquisition.frame_length == m.acquisition.scans
        and m.acquisition.console_frames == m.acquisition.accumulations,
    )
    single = method.loads(sample.replace("per_repetition", "single_frame"))
    check_true(
        "and one frame per method frame is the whole thing "
        f"(frame_length {single.acquisition.frame_length})",
        single.acquisition.frame_length
        == single.acquisition.scans * single.acquisition.accumulations
        and single.acquisition.console_frames == 1,
    )
    warned = method.loads(sample.replace('"SWFREQ,1,15000"', '"SWFREQ,1,15000\\t"'))
    check_true(
        "a string with trailing whitespace is stripped and warned about",
        warned.boxes[1].setup == ("SWFREQ,1,15000",) and len(warned.warnings) == 1,
    )
    stamp = method.stamp(m, console_version="0.0.0-check")
    check_true(
        "stamp() carries a hash, text and versions",
        stamp["method_hash"] and stamp["method_text"] and stamp["clockwork_version"],
    )
    check_true(
        "the stamp hash covers the start sequence",
        method.stamp(method.loads(sample.replace('["box2", "TARBTRG"], ', "")))["method_hash"]
        != stamp["method_hash"],
    )
    try:
        method.loads(f"schema_version = {method.SCHEMA_VERSION}\n")
        check_true("an incomplete method document is rejected", False)
    except method.MethodError:
        check_true("an incomplete method document is rejected", True)
    try:
        method.loads(
            sample.replace(f"schema_version = {method.SCHEMA_VERSION}", "schema_version = 1")
        )
        check_true("a schema-1 method document is rejected", False)
    except method.MethodError as exc:
        check_true(
            "a schema-1 method document is rejected, saying why",
            "not supported" in str(exc),
        )

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

    section("acquisition console")
    from clockwork import acq

    request = acq.FrameRequest(
        frame_length=5000, file_name="check_public-sample.uimf", frame_number=2,
        nbr_accumulations=100, offset_bins=20000,
    )
    check_true(
        "a frame request round-trips through protobuf and Snappy "
        f"({len(request.encode())} bytes on the wire)",
        acq.FrameRequest.decode(request.encode()) == request,
    )
    check_true(
        "a frame past the ScanNum column's declared range is warned about, not refused",
        len(acq.FrameRequest(frame_length=500_000, offset_bins=20000).warnings()) == 1,
    )
    # The instrument's own numbers at 2 GS/s: a 129.0036 us pusher period, the
    # 10 us post-trigger delay and the 2.048 us rearm dead time.
    record = acq.record_size_samples(258007, 20000, 4096)
    check_true(
        f"the record size follows the console's arithmetic ({record} samples, "
        f"{record + 20000} with the post-trigger delay)",
        record == 233888 and record % acq.GATE_GRANULARITY_SAMPLES == 0,
    )

    with acq.FakeConsole(subscriber_wait_s=2.0) as fake:
        with acq.DataStream(fake.data_endpoint) as stream, \
                acq.Console(fake.command_endpoint, timeout=5.0) as console:
            info = console.info()
            check_true(
                f"a simulated console answers info ({info.model}, serial {info.serial})",
                info.model == "SA220P" and info.is_fork,
            )
            check_true("and num instruments", console.num_instruments() == 1)
            try:
                console.acquire_frame(acq.FrameRequest(frame_length=10))
                check_true("acquire frame before acquire is refused by the client", False)
            except acq.ConsoleStateError:
                check_true("acquire frame before acquire is refused by the client",
                           not fake.died)
            try:
                console.request("reset timestamps", timeout=0.1)
                check_true("a command the console never answers is refused, not waited on",
                           False)
            except acq.ConsoleStateError:
                check_true("a command the console never answers is refused, not waited on",
                           True)

            # The other two ways to break the ordering rule, checked without
            # sending anything: a start while an acquisition is unstopped
            # destroys a thread the console never joined, and kills it.
            sent_so_far = len(fake.commands)
            with acq.Console(fake.command_endpoint, timeout=1.0) as guard:
                guard.acquiring = guard.running = True
                refused = 0
                for attempt in (lambda: guard.acquire(timeout=1.0),
                                lambda: guard.acquire_frame(acq.FrameRequest(frame_length=10))):
                    try:
                        attempt()
                    except acq.ConsoleStateError:
                        refused += 1
                check_true(
                    "a start while an acquisition is unstopped is refused, both ways",
                    refused == 2 and not fake.died
                    and len(fake.commands) == sent_so_far,
                )

            console.configure(offset_v=0.251)
            check_true(
                "configure sends init, horizontal, vertical, invert and the enable input",
                [name for name, *_ in fake.commands[-5:]]
                == ["init", "horizontal", "vertical", "invert", "enable io port"],
            )
            width = acq.start_chain(console, stream, timeout=5.0, settle=2.0)
            check_true(
                f"acquire replies with a period ({width.pusher_pulse_width} samples) "
                f"that passes its own SHA-256, and a record of {width.num_samples}",
                width.num_samples == fake.num_samples,
            )
            check_true(
                "and the open-ended acquisition it starts is stopped and cleared away",
                console.acquiring and not console.running and stream.poll(0.1) is None,
            )

            batches: list[acq.Batch] = []
            end = acq.run_frame(
                console, stream, acq.FrameRequest(frame_length=250, offset_bins=20000),
                timeout=10.0, on_batch=batches.append,
            )
            check_true(
                f"one frame's {len(batches)} batches all arrive, then finished on its own "
                "topic",
                end.is_finished and end.topic == acq.TOPIC_STATUS
                and sum(batch.scans for batch in batches) == 250,
            )
            check_true(
                "and the frame was stopped, so the next one may start",
                not console.running and not fake.died and fake.ignored_frames == 0,
            )

            # A frame that acquired nothing ends with exactly the `finished` a
            # whole frame ends with, so a client that takes that at face value
            # reports a dead acquisition as a good one (lab record, task 20).
            fake.frame_batches = 0
            try:
                acq.run_frame(console, stream, acq.FrameRequest(frame_length=250),
                              timeout=10.0, settle=0.5)
                check_true("a frame that published no scans is not called a success", False)
            except acq.EmptyFrameError:
                check_true("a frame that published no scans is not called a success",
                           not console.running and not fake.died)
            fake.frame_error = "Invalid value (1000) for parameter nbrElementsToFetch"
            try:
                acq.run_frame(console, stream, acq.FrameRequest(frame_length=250),
                              timeout=10.0, settle=0.5)
                check_true("and an error the console publishes is raised in its own words",
                           False)
            except acq.ConsoleAcquisitionError as exc:
                check_true("and an error the console publishes is raised in its own words",
                           "nbrElementsToFetch" in str(exc))
            fake.frame_batches, fake.frame_error = None, None

            console.stop_acquire()
            check_true(
                "and stop acquire is followed by finished acquire",
                stream.wait_for_status(
                    acq.FINISHED_ACQUIRE, timeout=10.0
                ).is_finished_acquire,
            )

            # A command that fails inside the console answers with one `error`
            # frame in place of its reply, and the session goes on. A console
            # without that boundary exits instead, and the client meets the
            # exit as a request that timed out (lab record, task 21).
            fake.refuse["tof width"] = "measured pusher period is outside the believable band"
            try:
                console.tof_width(timeout=10.0)
                check_true("a command the console refuses is raised, not taken for a reply",
                           False)
            except acq.ConsoleCommandError as exc:
                check_true("a command the console refuses is raised, not taken for a reply",
                           "believable band" in str(exc))
            fake.refuse.clear()
            check_true(
                "and the refusal cost the session nothing: the next command is answered",
                console.tof_width(timeout=10.0).pusher_pulse_width
                == fake.pusher_period_samples,
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
