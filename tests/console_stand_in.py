"""A console-shaped executable, for the tests that drive the real `subprocess` path.

`FakeConsole` runs on threads in the test's own process, which proves everything
about the protocol and nothing about launching, watching, restarting or killing an
executable -- and launching one is the whole of `clockwork.acq.process`. So this is
a separate program: it prints the startup block a console prints, binds the command
port, answers `info`, and dies when it is killed.

Run as

    python tests/console_stand_in.py <port> [--fail <message>] [--silent] [--slow <s>]

`--fail` prints a complaint and exits 1 without binding, which is what the console
does when `config.txt` carries a value it refuses. `--silent` binds nothing and sits
there, which is a console wedged in the driver. `--slow` waits before binding, which
is the card open.

It deliberately does not import clockwork: what it stands in for is an executable
this repository does not own.
"""

from __future__ import annotations

import sys
import time

import zmq

_STAMP = "[2026-09-16 22:48:32.812] [aqmd3] [info] "
STARTUP = "\n".join(_STAMP + line for line in (
    "Logger initialized",
    'Config value "PostTriggerDelay" found, value set to 0.000010',
    'Config value "NotifyOnScansCount" found, value set to 500',
    'Config value "AcquisitionTimeoutMs" found, value set to 100',
    'Config value "TriggerLevel" found, value set to 0.400000',
    'Config value "TriggerSlope" found, value set to rising',
    'Config value "FullScaleRange" found, value set to 0.500000',
    'Config value "ZeroSuppressHysteresis" not found, value defaulted to 100',
    'Config value "ControlIoPort" found, value set to 2',
))
"""The block `print_config` writes, with both of its message shapes in it.

Copied in shape, not in content: the numbers are the instrument's as
`console/README.md` tabulates them and the formatting is `std::to_string`'s, which
is the reason `ConsoleConfig.differences` compares numerically.
"""

INFO = ("Digitizer Model: SA220P / Digitizer Serial No.: AQ00000000 / "
        "Digitizer Firmware Version: stand-in / App: AqMD3_console / "
        "App Version: 1.2.0-standin / Fork: bushgroup/AqMD3-Acquisition-Console@clockwork "
        "/ Full Scale: 0.5")


def main(argv: list[str]) -> int:
    port = int(argv[0])
    fail = argv[argv.index("--fail") + 1] if "--fail" in argv else ""
    silent = "--silent" in argv
    slow = float(argv[argv.index("--slow") + 1]) if "--slow" in argv else 0.0

    if fail:
        print("[critical] config.txt is not usable, application exiting", flush=True)
        print(f"[critical] {fail}", flush=True)
        return 1

    print(STARTUP, flush=True)
    print("a line on stderr, which no client has ever seen", file=sys.stderr, flush=True)
    if slow:
        time.sleep(slow)
    if silent:
        while True:
            time.sleep(0.2)

    context = zmq.Context()
    socket = context.socket(zmq.ROUTER)
    socket.bind(f"tcp://127.0.0.1:{port}")
    print(f"[info] listening on {port}", flush=True)
    while True:
        parts = socket.recv_multipart()
        identity, frames = parts[0], [p for p in parts[1:] if p]
        command = frames[0].decode("utf-8", "replace") if frames else ""
        if command == "info":
            socket.send_multipart([identity, b"", INFO.encode()])
        else:
            socket.send_multipart([identity, b"", b"ack"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
