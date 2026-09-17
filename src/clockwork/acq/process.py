"""The acquisition console as a process: start it, watch it, restart it, stop it.

`console.py` talks to a console that is already running. Nothing has ever started
one: every bench day launched it by hand in a terminal and watched it scroll, and
the two things that went wrong with that arrangement are the reasons this module
exists.

*A `config.txt` change is invisible to a running console.* The console reads that
file once, in `configure_settings()`, before it opens the card, and nothing in the
ZeroMQ protocol reports back what it read except the full scale. So between an edit
and the next restart the file and the process say different things and only the
process is true: a timeout bisection that ended at 250 ms, restored the file to
2000 and left the console up handed the next run a console configured for an
experiment that was over, and a 64.5 s frame was aborted three seconds in, twice,
with nothing in the results saying so (lab record, tasks 42 and 47). `ConsoleConfig`
reads and writes the file; `ConsoleProcess.needs_restart()` compares it with the
block the running console logged at startup, which is the only statement of what
the process actually holds.

*There is no stop command.* Nothing in the protocol ends the process: both forms of
`stop` end an acquisition and leave the console running, and a console exits on its
own only when it cannot start or when the driver fails under it
(`docs/console-protocol.md`, under the command set). So `stop()` kills it, and that
is not a shortcut -- the fork flushes each log line as it is written and once a
second besides, precisely so that a killed console still leaves its record behind.

Two classes and a function:

    ConsoleConfig    the `config.txt` key table: read, write, range-check, and
                     say whether the file has drifted from a running console
    ConsoleProcess   the process: start, wait until it answers, restart, stop,
                     with its stdout and stderr in the transcript
    prepare_console  the one place `console.configure` is called from an
                     instrument document, for the window and for a bench script

and `FakeConsoleProcess`, which puts `FakeConsole` behind the same interface so
that `--fake` exercises the status bar, the restart path and the whole of the
window without an executable anywhere.

## Why the launch looks the way it does

`main` calls `disable_quick_edit()` before anything else, which reads the console
mode off `STD_INPUT_HANDLE` and throws if that handle is not a console. Redirecting
the child's handles the way `subprocess` does it sets `STARTF_USESTDHANDLES`, which
replaces **all three** handles including stdin, and a launch from a process that
has no console of its own -- a PyInstaller window, or a session driving this from a
pipe -- then gives the console a stdin it cannot query. It prints
`Error getting console mode` and exits 1 before it has read `config.txt`, let alone
opened the card. Measured on MASSTRO, all five arrangements (lab record, task 49):

| Launch | Starts? |
|---|---|
| inherit everything, from a parent with no console | no |
| `stdout`/`stderr` pipes | no |
| `CREATE_NEW_CONSOLE` + `SW_HIDE`, no redirection | yes |
| `CREATE_NEW_CONSOLE` + `SW_HIDE` + pipes | no |
| `CREATE_NO_WINDOW`, no redirection | yes |

So on Windows the console is launched through `cmd.exe /c` with `1>` and `2>`:
`cmd` gets the new console under `CREATE_NO_WINDOW`, the child inherits its stdin,
and the shell's redirection touches only the other two. That is the one arrangement
that both starts and gives up its output -- and it is what the trainee decision
needs, since `CREATE_NO_WINDOW` means no window is ever drawn (lab record, task 50,
decision 4). On anything else the streams are redirected directly, because nothing
else has `disable_quick_edit()` to satisfy.

**The output goes to files and is tailed, not piped.** A pipe would be the same
work and would not survive the supervisor: a file holds what a console wrote in the
seconds before it died, and is still there to read afterwards. Both are tailed line
by line onto `clockwork.acq.console_process`, which `clockwork.transcript` collects,
so a bench day's transcript carries the console's own narrative interleaved with
the commands that provoked it.

**The startup block is read out of that captured stdout.** The existing bench
helper scrapes the newest file in the console's `logs/` directory for the last
`Logger initialized` block, which is a guess about which process wrote it; reading
the stdout of the process this object started is not a guess.

Blocking, and Qt-free. `start()` and `wait_ready()` take seconds -- the card open
alone measured 5.4 s on the instrument PC with the card present -- so nothing here
may be called from the UI thread.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from ..instrument import Instrument
from ..transcript import CONSOLE as _CONSOLE
from ..transcript import DECIDED as _DECIDED
from ..transcript import sent as _sent
from .console import DEFAULT_TIMEOUT_S, Console, ConsoleTimeout
from .wire import (
    COMMAND_PORT,
    DATA_PORT,
    SECONDS_PER_SAMPLE_2GSPS,
    AcqError,
    ConsoleInfo,
)

_LOG = logging.getLogger("clockwork.acq.console_process")
"""The transcript's name for the console process: its stdout, its stderr, and
this supervisor's own decisions. Documented in `clockwork.transcript`."""

CONFIG_NAME = "config.txt"
"""What the console looks for, in its working directory, and nowhere else.

`Config("config.txt")` is a relative path, so the file the console reads is the
one beside wherever it was started from. That is why `ConsoleProcess` sets the
working directory to the executable's own, which is where the console's own
post-build step puts a copy (lab record, task 17), rather than inheriting the
caller's.
"""

EXECUTABLE_NAME = "AqMD3_console.exe" if os.name == "nt" else "AqMD3_console"

STARTUP_TIMEOUT_S = 40.0
"""How long `wait_ready` allows before it gives up on a console that is running.

The card open is the slow part and everything else is noise beside it: 5.4 s
from launch to a bound command socket on the instrument PC with the SA220P
present, 5.2 s when task 17 first measured it. This is not that number with a
margin, it is room for a machine whose PXI enumeration is having a bad day, and
it is bounded so that a console wedged in the driver is eventually reported as
wedged rather than waited on for ever (lab record, task 49).
"""

STOP_GRACE_S = 5.0
"""How long a polite stop is given before the process is killed.

There is nothing polite to do on Windows -- the protocol has no stop command and a
console application with no window has nothing to send a close to -- so this is the
grace period on platforms where `terminate()` means SIGTERM, and on Windows the
time allowed for the tree kill itself to take effect.
"""

READY_POLL_S = 0.1
TAIL_POLL_S = 0.15
"""How often the port is probed and the output files are read forward.

Both are fast enough to be invisible next to a card open and slow enough to cost
nothing: the tail is two `read()` calls at the end of two files.
"""

CREATE_NO_WINDOW = 0x08000000
"""`CREATE_NO_WINDOW`. The child gets a console -- which is what
`disable_quick_edit()` requires -- with no window drawn for it, which is what a
trainee never seeing a console window requires (lab record, task 50, decision 4).
Measured, not assumed: `CREATE_NEW_CONSOLE` with `SW_HIDE` also works and this is
the one that never draws anything at all.
"""


class ConsoleProcessError(AcqError):
    """The console process could not be started, or died, or would not answer."""


# -- config.txt ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Key:
    """One `config.txt` setting, as the console reads it.

    `kind` is how the console parses the value, which is what decides whether two
    spellings of it are the same setting: `std::stod` reads `0.5`, `0.50` and
    `5e-1` as one number, so a comparison between the file and a running console
    has to be numeric for those keys and textual for the rest.
    """

    name: str
    kind: str
    """`"float"`, `"int"` or `"text"`."""

    default: str
    what: str
    """One line saying what the setting does, for a window that offers it."""

    low: float | None = None
    high: float | None = None
    """The range the fork refuses outside, inclusive, or None where it checks none."""

    allowed: tuple[str, ...] = ()
    """The exact values the fork accepts, for the two keys checked that way."""


KEYS: tuple[Key, ...] = (
    Key("PostTriggerDelay", "float", "0.00001",
        "seconds of delay between the trigger and the first digitized sample"),
    Key("TriggerRearmDeadTime", "float", "0.000002048",
        "seconds the card is allowed to re-arm between triggers"),
    Key("ResourceName", "text", "PXI0::0::0::INSTR",
        "the VISA resource the card answers on"),
    Key("NotifyOnScansCount", "int", "500",
        "scans per published batch, and the width of the enable-gate allowance"),
    Key("AcquisitionTimeoutMs", "int", "100",
        "milliseconds one batch's fetch is allowed before the acquisition is abandoned"),
    Key("AcquisitionInitialBufferCount", "int", "40",
        "buffers the acquisition pool starts with"),
    Key("AcquisitionMaxBufferCount", "int", "100",
        "buffers the acquisition pool may grow to"),
    Key("AcquisitionBufferReserveElementsCount", "int", "2048",
        "elements reserved past the end of each buffer"),
    Key("LogLevel", "text", "info",
        "spdlog level: trace, debug, info, warn, err, critical or off"),
    Key("TriggerLevel", "float", "2.0",
        "volts at which the external trigger fires"),
    Key("TriggerSlope", "text", "rising",
        "which edge of the trigger fires it", allowed=("rising", "falling")),
    Key("FullScaleRange", "float", "0.5",
        "volts across channel 1's window", allowed=("0.5", "2.5")),
    Key("ZeroSuppressThreshold", "int", "-32667",
        "the code a sample must reach for its gate to be kept",
        low=-32768, high=32767),
    Key("ZeroSuppressHysteresis", "int", "100",
        "how far a sample must fall back before the gate closes", low=100, high=1023),
    Key("ControlIoPort", "int", "2",
        "which Control I/O port becomes the acquisition enable input", low=1, high=3),
)
"""Every key the console reads, in the order it reads them.

`docs/console-protocol.md`, "Configuration", is the source: the keys, the defaults
and the five the fork refuses before they reach the driver are all stated there, and
a disagreement between this table and that document is fixed in the document first.
What is here and not there is the one line of `what` each key does, which is for a
window that offers the setting to put beside it.

`default` is the literal the console compiles in and falls back to when the file does
not carry the key, which is why a key can be absent from a file and still be in
force; the values actually in force on this instrument are the lab record's, not this
table's.

**The last six exist only on the fork.** A stock build ignores every one of them
without a word, which is what `ConsoleInfo.is_fork` is for.
"""

KEYS_BY_NAME = {key.name: key for key in KEYS}

FORK_ONLY = ("TriggerLevel", "TriggerSlope", "FullScaleRange",
             "ZeroSuppressThreshold", "ZeroSuppressHysteresis", "ControlIoPort")
"""The keys a stock console reads nothing from. `config.txt` may state them; only the
fork acts on them (`docs/console-protocol.md`, "Settings the fork moves out of the
source")."""

RESTART_REQUIRED = tuple(key.name for key in KEYS)
"""Which keys need a restart to take effect: all of them.

Spelled out rather than implied because the whole of this module's reason for
existing is that the answer is "all of them and nothing says so".
"""


def _parsed(key: Key, text: str) -> float | str:
    """One value in the form the console holds it in, for comparison.

    Numeric keys come back as floats so that `0.5` and `0.50` are one value, and
    everything else as stripped text; a numeric key whose text will not parse comes
    back as text too, which lets a bad value be compared and reported rather than
    raising in the middle of a comparison that is about something else.
    """
    text = text.strip()
    if key.kind in ("float", "int"):
        try:
            return float(text)
        except ValueError:
            return text
    if key.name == "TriggerSlope":
        # The console lower-cases and right-trims this one before it looks at it.
        return text.lower()
    return text


TO_STRING_DECIMALS = 6
"""How many decimal places `std::to_string(double)` writes, which is a fixed six.

It is the reason `differences` compares printed forms rather than numbers. The
console's startup block is written with `std::to_string`, so a value needing more
than six decimals is logged rounded: this instrument's `TriggerRearmDeadTime` of
`0.000002048` appears in the log as `0.000002`, and a numeric comparison against
the file it was read from reports a disagreement that is only a printing artefact
(measured on the instrument PC, lab record, task 49).
"""


def _as_console_prints(key: Key, value: float | str) -> str:
    """One value as the console's own startup block would write it.

    `print_config` passes each setting through `std::to_string`, which is `%f` for a
    double and plain digits for an integer, and prints the two text settings as it
    holds them -- `TriggerSlope` normalised to `rising` or `falling`. Rendering both
    sides this way is what makes the comparison mean "would this console have logged
    what the file now says", which is the only question its log can answer.
    """
    if isinstance(value, str):
        return value.strip()
    if key.kind == "int":
        return str(int(value))
    return f"{value:.{TO_STRING_DECIMALS}f}"


class ConsoleConfig:
    """The console's `config.txt`, read and written with its own formatting kept.

    A trainee never edits this file (lab record, task 50, decision 4), so something
    has to edit it for them, and that something must not reformat the lab's copy
    on the way past: a run that changed one number and left every line of the file
    looking modified is a run whose diff says nothing. Comments, blank lines, key
    order, spacing and the line ending are all preserved; only the values of the
    keys asked for change, and a key the file does not carry is appended.

    Values are text, because text is what the console reads and what its log
    prints back. `value_of` gives the parsed form where a comparison needs one.
    """

    __slots__ = ("_ending", "_lines", "path")

    def __init__(self, text: str = "", path: str = "") -> None:
        self.path = path
        self._ending = "\r\n" if "\r\n" in text else "\n"
        self._lines = text.splitlines()

    # -- reading ----------------------------------------------------------

    @classmethod
    def load(cls, path: str) -> ConsoleConfig:
        """Read the file, keeping its line endings as they are on disk.

        `newline=""` on the way in and out: without it a file read as LF is written
        back as CRLF on Windows, and the copy of record grows a diff of itself.
        """
        with open(path, encoding="utf-8", newline="") as handle:
            return cls(handle.read(), path=path)

    @classmethod
    def beside(cls, executable: str) -> ConsoleConfig:
        """The `config.txt` the console at this path will read when it starts.

        Which is the one in its own directory and no other: the console opens
        `config.txt` by a relative path, so the file of record in a source tree is
        not the file it reads unless somebody has copied it across.
        """
        return cls.load(os.path.join(os.path.dirname(os.path.abspath(executable)),
                                     CONFIG_NAME))

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def get(self, name: str, default: str | None = None) -> str | None:
        """What the file says for a key, or `default` if it does not say.

        Not the value in force: a key the file omits is in force at the literal the
        console compiles in, which is `KEYS_BY_NAME[name].default`. `in_force` is
        the one that answers that question.
        """
        for line in self._lines:
            if line.lstrip().startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == name:
                return value.strip()
        return default

    def in_force(self, name: str) -> str:
        """What a console started on this file would hold for a key.

        The file's value, or the literal the console compiles in when the file is
        silent. A stock console holds its own literal for the six fork-only keys
        whatever the file says, which this does not model: `ConsoleInfo.is_fork`
        is how a client tells the two builds apart.
        """
        key = KEYS_BY_NAME.get(name)
        return self.get(name, key.default if key else None) or ""

    def value_of(self, name: str) -> float | str:
        """`in_force`, parsed the way the console parses it."""
        key = KEYS_BY_NAME.get(name)
        text = self.in_force(name)
        return _parsed(key, text) if key else text.strip()

    def as_dict(self) -> dict[str, str]:
        """Every key the file states, in file order. Keys it omits are not here."""
        out: dict[str, str] = {}
        for line in self._lines:
            if line.lstrip().startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
        return out

    # -- the settings clockwork has an opinion about ----------------------

    @property
    def full_scale_v(self) -> float | None:
        return _as_float(self.value_of("FullScaleRange"))

    @property
    def trigger_level_v(self) -> float | None:
        return _as_float(self.value_of("TriggerLevel"))

    @property
    def trigger_slope(self) -> str:
        return str(self.value_of("TriggerSlope"))

    @property
    def notify_on_scans_count(self) -> int | None:
        return _as_int(self.value_of("NotifyOnScansCount"))

    @property
    def acquisition_timeout_ms(self) -> int | None:
        return _as_int(self.value_of("AcquisitionTimeoutMs"))

    @property
    def post_trigger_delay_s(self) -> float | None:
        return _as_float(self.value_of("PostTriggerDelay"))

    @property
    def control_io_port(self) -> int | None:
        return _as_int(self.value_of("ControlIoPort"))

    @property
    def zero_suppress_threshold(self) -> int | None:
        return _as_int(self.value_of("ZeroSuppressThreshold"))

    @property
    def zero_suppress_hysteresis(self) -> int | None:
        return _as_int(self.value_of("ZeroSuppressHysteresis"))

    @property
    def resource_name(self) -> str:
        return str(self.value_of("ResourceName"))

    # -- writing ----------------------------------------------------------

    def set(self, **values: object) -> ConsoleConfig:
        """Change these keys in place and hand the object back.

        A key already in the file keeps its line's position; one that is not is
        appended, which is what the fork's own six keys need on a `config.txt`
        written before they existed. A commented-out line is left alone and the key
        appended, because a commented key is a note and not a setting.

        **This changes nothing about a running console.** The file is read once, at
        startup: call `ConsoleProcess.restart()` afterwards, or the process keeps
        the value it started with and nothing says so.
        """
        left = {name: _as_text(value) for name, value in values.items()}
        for index, line in enumerate(self._lines):
            if line.lstrip().startswith("#") or "=" not in line:
                continue
            name = line.split("=", 1)[0].strip()
            if name in left:
                self._lines[index] = f"{name}={left.pop(name)}"
        for name, text in left.items():
            self._lines.append(f"{name}={text}")
        return self

    def dumps(self) -> str:
        return self._ending.join(self._lines) + self._ending

    def save(self, path: str | None = None) -> str:
        """Write the file back, with the line endings it came with. Returns the path."""
        where = path or self.path
        if not where:
            raise ValueError("this ConsoleConfig has no path; pass one to save()")
        with open(where, "w", encoding="utf-8", newline="") as handle:
            handle.write(self.dumps())
        self.path = where
        return where

    # -- what the console will refuse -------------------------------------

    def problems(self) -> list[str]:
        """Every value the fork refuses to start on, in the words it refuses in.

        `reject_bad_settings()` throws on the first one it meets and the console
        exits 1 before it opens the card, having logged the complaint and nothing
        else. Checking here means a trainee is told which value is wrong, and told
        all of them at once, before a console is launched and fails to answer for a
        reason that looks like a missing card.

        A value that will not parse at all is a problem too: the console reads these
        with `std::stod` and `std::stoi`, which throw on a value with no digits, and
        that exception takes the same path.
        """
        found: list[str] = []
        for key in KEYS:
            text = self.get(key.name)
            if text is None:
                continue
            parsed = _parsed(key, text)
            if key.kind in ("float", "int") and isinstance(parsed, str):
                found.append(f"{key.name} must be a number, got {text!r}")
                continue
            if key.allowed:
                if parsed not in [_parsed(key, one) for one in key.allowed]:
                    found.append(f"{key.name} must be "
                                 + " or ".join(key.allowed) + f", got {text!r}")
            elif key.low is not None and key.high is not None and not (
                    key.low <= float(parsed) <= key.high):
                found.append(
                    f"{key.name} must be between {_as_text(key.low)} and "
                    f"{_as_text(key.high)}, got {text!r}")
        return found

    # -- the file against a running console -------------------------------

    def differences(self, startup: dict[str, str]) -> dict[str, tuple[str, str]]:
        """Where this file and the block a console logged at startup disagree.

        `{key: (what the console read, what the file says)}`, empty when they agree.

        **Both sides are rendered as the console prints them**, rather than compared
        as numbers or as text. As text, a file saying `0.00001` differs from a log
        saying `0.000010` and every float in the table is a disagreement. As numbers,
        the opposite fault appears and it is the one that bites: `std::to_string`
        writes six decimal places, so this instrument's `TriggerRearmDeadTime` of
        `0.000002048` is logged as `0.000002`, and a console started from that very
        file reports as having drifted from it. Comparing printed forms asks the only
        question the log can answer -- would this console have logged what the file
        now says -- and answers it exactly.

        The cost is `blind_spots()`: a change too small to survive the printing is
        invisible here, and that method says which keys are in that position.

        Keys the console did not log are skipped rather than reported as
        disagreements -- `LogLevel` is read and never printed, so nothing can check
        it -- and a stock build logs the same block, so this works against both.
        """
        differs: dict[str, tuple[str, str]] = {}
        for key in KEYS:
            if key.name not in startup:
                continue
            read = _parsed(key, startup[key.name])
            if _as_console_prints(key, read) != _as_console_prints(
                    key, self.value_of(key.name)):
                differs[key.name] = (startup[key.name], self.in_force(key.name))
        return differs

    def blind_spots(self) -> dict[str, str]:
        """Keys whose value this file states more precisely than the console can log.

        `{key: what the console would print}`. Everything in `differences` rests on
        the startup block, and the block is `std::to_string` output, so a key stating
        more than six decimal places is checkable only down to that: an edit below it
        takes effect on the card and shows up nowhere. One key on this instrument is
        in that position, `TriggerRearmDeadTime`, and its value is a card setting
        nobody has had a reason to move.

        Not a fault to fix here. It is a property of the console's own logging, and
        the honest thing is for a window that offers these settings to know which of
        them it cannot read back.
        """
        out: dict[str, str] = {}
        for key in KEYS:
            if key.kind != "float":
                continue
            stated = self.get(key.name)
            if stated is None:
                continue
            value = _parsed(key, stated)
            if isinstance(value, float) and float(_as_console_prints(key, value)) != value:
                out[key.name] = _as_console_prints(key, value)
        return out

    def __repr__(self) -> str:
        return f"ConsoleConfig({self.path!r}, {len(self.as_dict())} keys)"


STARTUP_MARK = "Logger initialized"
"""The line the console logs immediately before its config block, and the only
thing that marks where one process's block begins."""

CONFIG_LINE = re.compile(
    r'Config value "(?P<key>[^"]+?)"? (?:found, value set to|'
    r'not found, value defaulted to) (?P<value>.*?)\s*$')
"""`print_config_value`'s two message shapes, which differ in one place.

The key is quoted on both sides in the "found" message and only on the left in the
"not found" one -- `"Config value \\"" + key` with `"\\" found, ..."` or
`" not found, ..."` appended -- so the closing quote is optional here rather than
two patterns being kept in step.
"""


def read_startup_block(lines: list[str] | tuple[str, ...]) -> dict[str, str]:
    """The `Config value ...` block a console logged at startup, as a dict.

    One line per key, immediately after `Logger initialized`, saying the value the
    console parsed -- whether it came from the file or from the literal the console
    compiles in, which is the distinction between the two message shapes and one
    this does not keep: what a client needs is what the console holds, and it holds
    both the same way.

    The **last** block in `lines` is taken, so a file that a supervisor has appended
    to across a restart answers for the process now running. Reading forward stops
    at the first line past the block, since the console gets on with its day there
    and reading on would merge two starts into one.
    """
    marks = [n for n, line in enumerate(lines) if STARTUP_MARK in line]
    if not marks:
        return {}
    values: dict[str, str] = {}
    for line in lines[marks[-1] + 1:]:
        found = CONFIG_LINE.search(line)
        if found:
            values[found.group("key")] = found.group("value")
        elif values:
            break
    return values


# -- the process -----------------------------------------------------------------


class ConsoleSupervisor:
    """What the window and a bench script need from a console, real or simulated.

    `ConsoleProcess` runs the executable; `FakeConsoleProcess` runs `FakeConsole` in
    this process. Both answer the same six questions, so `--fake` exercises the
    status bar, the restart path and everything above them without an executable
    anywhere, and nothing above this line knows which it has.

    The two endpoints are attributes rather than constants because the stand-in
    binds whatever ports it is given: a caller that hardcodes 5555 works against the
    instrument and against nothing else.
    """

    command_endpoint: str
    data_endpoint: str

    def start(self) -> ConsoleSupervisor:
        raise NotImplementedError

    def wait_ready(self, timeout: float = STARTUP_TIMEOUT_S) -> float:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def restart(self, timeout: float = STARTUP_TIMEOUT_S) -> float:
        raise NotImplementedError

    @property
    def alive(self) -> bool:
        raise NotImplementedError

    def __enter__(self) -> ConsoleSupervisor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class ConsoleProcess(ConsoleSupervisor):
    """One console executable, started and owned by this object.

    Start it, wait for it to answer, and stop it on the way out:

        with ConsoleProcess(find_console()) as console_process:
            console_process.start()
            console_process.wait_ready()
            with Console(console_process.command_endpoint) as console:
                ...

    `start()` returns as soon as the process has been created, which is well before
    it is listening -- the card open takes about five seconds on its own -- so
    `wait_ready()` is a separate call and a caller with something else to do may do
    it in between. Everything the console writes reaches the transcript either way.
    """

    def __init__(
        self,
        command: str | list[str] | tuple[str, ...],
        *,
        directory: str | None = None,
        host: str = "127.0.0.1",
        command_port: int = COMMAND_PORT,
        data_port: int = DATA_PORT,
        output_dir: str | None = None,
        label: str = "console",
    ) -> None:
        self.command: tuple[str, ...] = (
            (command,) if isinstance(command, str) else tuple(command))
        if not self.command:
            raise ValueError("a ConsoleProcess needs something to run")
        self.directory = directory or os.path.dirname(os.path.abspath(self.command[0]))
        """Where the process runs, and so which `config.txt` it reads.

        The executable's own directory by default, because the console opens
        `config.txt` by a relative path and the build puts a copy beside the
        executable; inheriting the caller's directory would have it read whatever
        happened to be there, or nothing, and a console that finds no config runs
        on its compiled-in defaults and says so in one line nobody is watching.
        """

        self.host = host
        self.command_endpoint = f"tcp://{host}:{command_port}"
        self.data_endpoint = f"tcp://{host}:{data_port}"
        self.command_port = command_port
        self.label = label

        self.output_dir = output_dir or os.path.join(
            tempfile.gettempdir(), "clockwork-console")
        self.stdout_path = os.path.join(self.output_dir, f"{label}-{os.getpid()}-stdout.log")
        self.stderr_path = os.path.join(self.output_dir, f"{label}-{os.getpid()}-stderr.log")
        """Where the two streams land, why stderr has a file of its own, and why the
        name carries a process id.

        `CstContext::acquire` writes `wrong header -- cst acq (not zero sp)` to
        `std::cerr` when a marker header is wrong, and `std::cerr` is not one of
        spdlog's sinks: that line is in neither the console's log file nor anything
        a client sees, and it is the one thing that says the marker stream has
        desynchronised (lab record, task 21). It is tailed into the transcript from
        here, which is the first time it has reached a client at all.

        **The id is this process's, not the console's**, and it is there because the
        default directory is shared. Two clockworks on one machine -- a window and a
        bench script, or two sessions -- would otherwise pick the same two paths, and
        the second to start fails outright: Windows opens a `1>` redirection target
        without sharing write access, so the first console's capture cannot even be
        truncated by the second. Found by two self-checks colliding (lab record,
        task 49).
        """

        self.info: ConsoleInfo | None = None
        """The `info` reply that ended the last `wait_ready`, for the run header."""

        self.startup: dict[str, str] = {}
        """What this process logged that it read from `config.txt`, at startup.

        Empty until `wait_ready` has seen the block. This is the authority on what
        the running console holds -- the file is not (lab record, task 47).
        """

        self.started_seconds: float | None = None
        """How long the last start took to answer, which is mostly the card open."""

        self._proc: subprocess.Popen[bytes] | None = None
        self._tail: threading.Thread | None = None
        self._stop_tailing = threading.Event()
        self._lines: list[str] = []
        self._lines_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> ConsoleProcess:
        """Launch it, truncating this run's output files first. Does not wait."""
        if self.alive:
            raise ConsoleProcessError(
                "this ConsoleProcess is already running; stop() or restart() it")
        if not os.path.isfile(self.command[0]):
            raise ConsoleProcessError(f"no console executable at {self.command[0]}")
        os.makedirs(self.output_dir, exist_ok=True)
        try:
            for path in (self.stdout_path, self.stderr_path):
                with open(path, "w", encoding="utf-8"):
                    pass
        except OSError as exc:
            # Almost always a console of ours still holding the file: a `1>` target is
            # opened without write sharing, so this is the first thing that fails when
            # one was left running. Say that, rather than a bare errno.
            raise ConsoleProcessError(
                f"could not clear {exc.filename or self.stdout_path} for this console's "
                f"output ({exc}). A console started from here may still be running and "
                "holding it; stop it, or pass an output_dir of this run's own.") from exc
        with self._lines_lock:
            self._lines = []
        self.info = None
        self.startup = {}

        decided(f"starting the console: {' '.join(self.command)} in {self.directory}")
        if os.name == "nt":
            # The command line is built by hand and handed to Popen as a string: the
            # redirection has to reach `cmd`, and list quoting would escape the inner
            # quotes with backslashes, which cmd does not read.
            inner = " ".join(f'"{part}"' for part in self.command)
            line = f'cmd.exe /c "{inner} 1> "{self.stdout_path}" 2> "{self.stderr_path}""'
            self._proc = subprocess.Popen(  # noqa: S602 - no shell; cmd is the redirector
                line, cwd=self.directory, creationflags=CREATE_NO_WINDOW)
        else:
            out = open(self.stdout_path, "wb")  # noqa: SIM115 - owned by the child
            err = open(self.stderr_path, "wb")  # noqa: SIM115
            try:
                self._proc = subprocess.Popen(
                    list(self.command), cwd=self.directory, stdout=out, stderr=err)
            finally:
                out.close()
                err.close()
        self._stop_tailing.clear()
        self._tail = threading.Thread(
            target=self._tail_output, name=f"{self.label}-output", daemon=True)
        self._tail.start()
        return self

    def wait_ready(self, timeout: float = STARTUP_TIMEOUT_S) -> float:
        """Block until the console answers `info`, and say how long that took.

        Two stages, because they fail differently. The command port is probed with
        a plain TCP connect, which is cheap enough to do ten times a second and says
        when the console has got past opening the card; then one `info` is asked,
        which is what proves it is a console answering and not something else on the
        port, and gives the run header its provenance string in the same breath.

        Raises `ConsoleProcessError` as soon as the process exits, rather than
        waiting out the timeout: a console that has refused a `config.txt` value is
        gone within a second and its complaint is already in the transcript, so
        reporting it at once turns a forty-second hang into a sentence.
        """
        if self._proc is None:
            raise ConsoleProcessError("this ConsoleProcess has not been started")
        started = time.perf_counter()
        deadline = started + timeout
        while time.perf_counter() < deadline:
            code = self._proc.poll()
            if code is not None:
                self._drain()
                raise ConsoleProcessError(
                    f"the console exited with code {code} before it answered. Its "
                    f"output is in {self.stdout_path} and in the transcript"
                    + (f": {self.last_error}" if self.last_error else ""))
            if _port_answers(self.host, self.command_port):
                break
            time.sleep(READY_POLL_S)
        else:
            raise ConsoleProcessError(
                f"nothing was listening on {self.command_endpoint} after {timeout:g} s, "
                "and the process is still running: the console is wedged rather than "
                "gone, and killing it is what restart() is for")
        with Console(self.command_endpoint, timeout=max(1.0, DEFAULT_TIMEOUT_S)) as client:
            try:
                self.info = client.info()
            except ConsoleTimeout as exc:
                raise ConsoleProcessError(
                    f"the command port {self.command_port} is open and nothing answered "
                    f"info: {exc}") from exc
        self.started_seconds = time.perf_counter() - started
        self._drain()
        self.startup = read_startup_block(self.output_lines)
        decided(f"the console answered after {self.started_seconds:.1f} s: "
                f"{self.info.text}")
        if not self.info.is_fork:
            decided("this is a stock console: it ignores TriggerLevel, TriggerSlope, "
                    "FullScaleRange, ZeroSuppressThreshold, ZeroSuppressHysteresis and "
                    "ControlIoPort without saying so")
        return self.started_seconds

    def stop(self) -> None:
        """End the process, politely where that means anything, then by force.

        The protocol has no stop command, so this is a kill however it is dressed
        up. On Windows the process this object holds is `cmd`, and the console is
        its child, so the tree goes together or the console is orphaned and keeps
        port 5555 against the next start.

        Safe to call on a process that has already died, and on one that was never
        started: both are the state this leaves behind.
        """
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            decided(f"stopping the console (pid {proc.pid})")
            if os.name == "nt":
                subprocess.run(  # noqa: S603, S607
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, text=True, check=False)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    decided("the console did not die; something else is holding it")
        elif proc is not None:
            proc.wait()
        self._stop_tailing.set()
        if self._tail is not None:
            self._tail.join(timeout=2.0)
            self._tail = None
        self._drain()

    def restart(self, timeout: float = STARTUP_TIMEOUT_S) -> float:
        """Stop and start, and wait for the new one. Returns its startup time.

        The only way a `config.txt` change reaches the console, and the only way a
        crashed one comes back. A restart that fails leaves no console at all, which
        is the right way round: what follows says nothing is listening and stops,
        where a console still holding the old value would have quietly run.
        """
        decided("restarting the console")
        self.stop()
        self.start()
        return self.wait_ready(timeout=timeout)

    # -- what it is doing --------------------------------------------------

    @property
    def alive(self) -> bool:
        """Whether the process is running. Not whether it answers.

        The two differ for the five seconds the card takes to open, and differ again
        for a console wedged in the driver, which is why `wait_ready` asks the other
        question separately.
        """
        return self._proc is not None and self._proc.poll() is None

    @property
    def returncode(self) -> int | None:
        """The exit code, once it has one. The console exits 1 on a bad `config.txt`."""
        return self._proc.poll() if self._proc is not None else None

    @property
    def output_lines(self) -> list[str]:
        """Every line this process has written to stdout or stderr, in arrival order."""
        self._drain()
        with self._lines_lock:
            return list(self._lines)

    @property
    def last_error(self) -> str:
        """The last critical or error line the console logged, for a message.

        What a caller wants when a start failed is the console's own complaint --
        `ZeroSuppressHysteresis must be between 100 and 1023, got 4` -- and not the
        forty lines of config block in front of it.
        """
        for line in reversed(self.output_lines):
            if "[critical]" in line or "[error]" in line or "Error " in line:
                return line.strip()
        return ""

    def config(self) -> ConsoleConfig:
        """The `config.txt` this console reads, as it is on disk now.

        Which is what the *next* start will read. What the running one holds is
        `startup`, and `needs_restart()` is the comparison between the two.
        """
        return ConsoleConfig.load(os.path.join(self.directory, CONFIG_NAME))

    def needs_restart(self) -> dict[str, tuple[str, str]]:
        """Where `config.txt` has drifted from what the running console read.

        `{key: (what the console holds, what the file says)}`, empty when they
        agree and empty when there is nothing running to disagree with. **Empty is
        not a promise that the console is configured as the lab intends** -- that is
        `ConsoleConfig` against the copy of record -- only that the process and the
        file beside it say the same thing.
        """
        if not self.startup or not self.alive:
            return {}
        try:
            return self.config().differences(self.startup)
        except OSError:
            return {}

    # -- the output --------------------------------------------------------

    def _tail_output(self) -> None:
        """Read both files forward and put every new line in the transcript.

        A thread rather than a pipe reader because the streams are files (see the
        module docstring), and a daemon thread because a supervisor that failed to
        stop must not be what keeps the process alive.
        """
        marks = {self.stdout_path: 0, self.stderr_path: 0}
        while not self._stop_tailing.is_set():
            self._read_forward(marks)
            time.sleep(TAIL_POLL_S)
        self._read_forward(marks)

    def _read_forward(self, marks: dict[str, int]) -> None:
        """One pass over both files, from where the last pass stopped.

        Bytes rather than text, so that the mark is a file offset a `seek` can use:
        a text handle's `tell()` is opaque on Windows, where the newline translation
        makes the number it returns not the number of characters read, and mixing
        the two loses lines at every pass boundary.
        """
        for path, mark in list(marks.items()):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size < mark:
                # Truncated by a restart: read it from the top rather than never again.
                marks[path] = mark = 0
            if size <= mark:
                continue
            try:
                with open(path, "rb") as handle:
                    handle.seek(mark)
                    raw = handle.read()
            except OSError:
                continue
            # A trailing partial line is left for the next pass, so a line never
            # reaches the transcript in two halves.
            keep = raw.rfind(b"\n")
            if keep == -1:
                continue
            marks[path] = mark + keep + 1
            where = "stderr" if path == self.stderr_path else "stdout"
            fresh = [line.strip() for line in
                     raw[:keep].decode("utf-8", "replace").splitlines()]
            fresh = [line for line in fresh if line]
            with self._lines_lock:
                self._lines += fresh
            for line in fresh:
                _LOG.debug("%s %s", where, line)

    def _drain(self) -> None:
        """Let the tail catch up, for a caller about to read `output_lines`."""
        if self._tail is not None and self._tail.is_alive():
            time.sleep(TAIL_POLL_S * 2)

    def __repr__(self) -> str:
        return (f"ConsoleProcess({self.command[0]!r}, {self.command_endpoint}, "
                f"{'running' if self.alive else 'not running'})")


class FakeConsoleProcess(ConsoleSupervisor):
    """`FakeConsole` behind the supervisor interface, for `--fake`.

    There is no process: `FakeConsole` runs on threads in this one and binds
    whatever ports it is given, which is why the endpoints are attributes. Every
    method a window calls is here and does the honest thing -- `restart()` really
    does stop and rebind, so the status bar's restart path is exercised -- and
    `needs_restart()` is always empty, because a stand-in reads no `config.txt`.

    What it cannot show is the whole of what this module is for: a console that
    refuses a setting, one that dies mid-frame, and the five seconds a card open
    takes. Those need an executable, and `ConsoleProcess` against the stand-in
    executable in the tests is where the launch path itself is exercised.

    `startup` is filled from the stand-in's own numbers rather than left empty,
    because it is the answer to "what is the *running* console holding", and that
    question has an answer here too (lab record, task 50).
    """

    def __init__(self, **console: object) -> None:
        from .fake import FakeConsole

        self._make = lambda: FakeConsole(**console)  # type: ignore[arg-type]
        self._fake: object | None = None
        self.command_endpoint = ""
        self.data_endpoint = ""
        self.info: ConsoleInfo | None = None
        self.startup: dict[str, str] = {}
        """What the stand-in is running with, in the three keys a caller reads off a
        real console's startup block. Filled by `start()`; see the note there."""

        self.started_seconds: float | None = None

    @property
    def fake(self) -> object | None:
        """The `FakeConsole` itself, for a test that wants to assert on it."""
        return self._fake

    def start(self) -> FakeConsoleProcess:
        if self._fake is not None:
            raise ConsoleProcessError("this FakeConsoleProcess is already running")
        decided("starting a simulated console in this process")
        fake = self._make()
        fake.start()
        self._fake = fake
        self.command_endpoint = fake.command_endpoint
        self.data_endpoint = fake.data_endpoint
        # The stand-in reads no `config.txt`, but a caller reading `PostTriggerDelay`
        # off `startup` is reading what the *running* console holds, and that is as
        # true here as it is of a process: the stand-in binds its own numbers, so a
        # caller that fell back to the compiled-in default would build every scan's
        # leading zero run at a delay the stand-in is not using. Written in the form
        # the console prints, seconds, so the two supervisors answer alike.
        self.startup = {
            "PostTriggerDelay": repr(fake.post_trigger_samples
                                     * SECONDS_PER_SAMPLE_2GSPS),
            "TriggerRearmDeadTime": repr(fake.rearm_samples
                                         * SECONDS_PER_SAMPLE_2GSPS),
            "NotifyOnScansCount": str(fake.notify_on_scans_count),
        }
        return self

    def wait_ready(self, timeout: float = STARTUP_TIMEOUT_S) -> float:
        if self._fake is None:
            raise ConsoleProcessError("this FakeConsoleProcess has not been started")
        started = time.perf_counter()
        with Console(self.command_endpoint, timeout=max(1.0, min(timeout, 5.0))) as client:
            self.info = client.info()
        self.started_seconds = time.perf_counter() - started
        decided(f"the simulated console answered: {self.info.text}")
        return self.started_seconds

    def stop(self) -> None:
        fake, self._fake = self._fake, None
        if fake is not None:
            decided("stopping the simulated console")
            fake.stop()

    def restart(self, timeout: float = STARTUP_TIMEOUT_S) -> float:
        decided("restarting the simulated console")
        self.stop()
        self.start()
        return self.wait_ready(timeout=timeout)

    @property
    def alive(self) -> bool:
        return self._fake is not None

    def needs_restart(self) -> dict[str, tuple[str, str]]:
        return {}

    def __repr__(self) -> str:
        return (f"FakeConsoleProcess({self.command_endpoint or 'not started'}, "
                f"{'running' if self.alive else 'not running'})")


# -- preparing a console from the instrument document ----------------------------


@dataclass(frozen=True, slots=True)
class Prepared:
    """What `prepare_console` sent, and what it noticed on the way.

    Kept rather than printed so that a bench script's results file and a window's
    status line are the same three numbers, and so that a run's stamp and the
    warnings the loop raises about the same settings cannot come from two readings.
    """

    offset_v: float
    inverted: bool
    full_scale_v: float | None = None
    """What the console reports it is using, which is the only half of the window
    it states back (lab record, task 24). None from a stock or older build."""

    info: ConsoleInfo | None = None
    warnings: tuple[str, ...] = ()
    """Lines for a `Warned` event. Warnings, never refusals: every one of them is a
    document that has drifted from the machine, and which of the two is wrong is not
    something this can know."""


def prepare_console(
    console: Console,
    instrument: Instrument,
    *,
    config: ConsoleConfig | None = None,
    info: ConsoleInfo | None = None,
    io_port: int | None = 2,
    seconds_per_sample: float | None = None,
) -> Prepared:
    """`console.configure` from an instrument document, in the one place it happens.

    `clockwork.acq.loop` never configures the console: it is handed one already
    configured, because the offset and the inversion are the instrument's and the
    loop is the experiment's (lab record, task 38). That left every caller to read
    the document itself, and there was one caller. This is that code, lifted, so
    that the window and a bench script send the same three settings from the same
    document and a file's stamp cannot disagree with the card it was acquired on.

    Three things it does that a bare `configure` does not.

    *It refuses a document with no offset, by name.* `offset_v` is optional in the
    schema and one instrument document leaves it out on purpose, so the failure
    without this check was `float(None)` raising a bare `TypeError` from inside
    `vertical`, with the console already initialised and the message naming neither
    the document nor the setting (lab record, task 30).

    *Inversion defaults to false when the document states none*, which is what the
    console does with a channel nobody has inverted, so a document that is silent
    and a document that says `false` configure the same card.

    *It checks the full scale against the machine* -- against `config.txt` where one
    is given, and against what `info` reports, which is the console's own authority.
    A disagreement is a warning and names which value the file will carry, because
    the full scale is a `config.txt` key and a restart: nothing here can change it,
    and a caller that could would only be able to make the two agree by typing.
    """
    vertical = instrument.vertical
    if vertical.offset_v is None:
        raise ConsoleProcessError(
            f"the instrument document for {instrument.name or 'this machine'} states no "
            "channel offset, and the offset is sent to the console and stamped into the "
            "file as one number. Write `offset_v` into its [vertical] table, or pass a "
            "document that has one.")
    offset = float(vertical.offset_v)
    inverted = bool(vertical.inverted)
    if info is None:
        info = console.info()

    warnings: list[str] = []
    if not info.is_fork:
        warnings.append(
            "this is a stock acquisition console: it ignores every setting config.txt "
            "moves out of source, including TriggerLevel and FullScaleRange, without "
            "saying so")
    reported = info.full_scale_v
    declared = vertical.full_scale_v
    if declared is not None and reported is not None and declared != reported:
        warnings.append(
            f"the console is using a full scale of {reported} V and the instrument "
            f"document says {declared} V; the file will be stamped with the console's "
            "value, so one of the two is wrong")
    if config is not None:
        from_file = config.full_scale_v
        if reported is not None and from_file is not None and from_file != reported:
            warnings.append(
                f"the console is using a full scale of {reported} V and the config.txt "
                f"beside it says {from_file} V; the console reads that file only at "
                "startup, so it is holding a value the file no longer states and a "
                "restart is what changes it")

    if seconds_per_sample is None:
        console.configure(offset_v=offset, inverted=inverted, io_port=io_port)
    else:
        console.configure(offset_v=offset, inverted=inverted, io_port=io_port,
                          seconds_per_sample=seconds_per_sample)
    for message in warnings:
        decided(message)
    return Prepared(offset_v=offset, inverted=inverted, full_scale_v=reported,
                    info=info, warnings=tuple(warnings))


# -- finding one -----------------------------------------------------------------

CONSOLE_ENV = "CLOCKWORK_CONSOLE"
"""An executable, or a directory holding one. The first place `find_console` looks,
and the one a machine with the console somewhere unusual sets."""


def find_console(extra: str | None = None) -> str | None:
    """Where this machine's console executable is, or None.

    In order: `extra` if given, `$CLOCKWORK_CONSOLE`, a `console/` directory beside
    the installation (which is the shape the installer ships, lab record, task 50,
    decision 9), and last a lab checkout if one resolves, searched rather than
    assumed so that no build-tree layout is written down here.

    None is an ordinary answer and not a fault: a public clone has no console, and
    every code path that wants one has to say so rather than assume it.
    """
    from .. import ROOT, lab_dir

    for candidate in (extra, os.environ.get(CONSOLE_ENV)):
        if not candidate:
            continue
        found = _executable_in(candidate)
        if found:
            return found
    for root in (os.path.join(ROOT, "console"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "..", "..", "console")):
        found = _executable_in(root)
        if found:
            return found
    lab = lab_dir("console")
    return _executable_in(lab) if lab else None


def _executable_in(where: str) -> str | None:
    """The executable at, or anywhere under, this path. Deepest match ignored:
    the first one found wins, and a tree with two builds in it is a machine whose
    `$CLOCKWORK_CONSOLE` should say which."""
    if not where:
        return None
    where = os.path.abspath(where)
    if os.path.isfile(where):
        return where
    if not os.path.isdir(where):
        return None
    direct = os.path.join(where, EXECUTABLE_NAME)
    if os.path.isfile(direct):
        return direct
    for root, _dirs, files in os.walk(where):
        if EXECUTABLE_NAME in files:
            return os.path.join(root, EXECUTABLE_NAME)
    return None


# -- helpers ---------------------------------------------------------------------


def decided(message: str) -> None:
    """One line of this supervisor's own narrative, into both logs.

    Marked as the run's decision rather than as traffic, because starting, waiting
    for, restarting and stopping a console are things clockwork did and not things
    the console said. A trainee reading the send log beside a file should see that
    the console was restarted between two runs; that is the whole failure task 47
    recorded.
    """
    _LOG.debug("%s", message, extra=_sent(_DECIDED, _CONSOLE, message))


def _port_answers(host: str, port: int, timeout: float = 0.25) -> bool:
    """Whether anything accepts a TCP connection there.

    Cheap enough to poll, and it answers the question `wait_ready` needs first:
    the console binds its command socket only after the card is open, so a port
    that refuses is a console still starting rather than one that is broken.
    """
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def _as_float(value: object) -> float | None:
    return value if isinstance(value, float) else None


def _as_int(value: object) -> int | None:
    return int(value) if isinstance(value, float) and value == int(value) else None


def _as_text(value: object) -> str:
    """A value as `config.txt` should carry it.

    Floats are written out rather than in exponent form, since `std::stod` reads
    both but a file a trainee may one day open should not say `1e-05` where it used
    to say `0.00001`; booleans never occur in this file and are not special-cased.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.18f}".rstrip("0")
        return text + "0" if text.endswith(".") else text
    return str(value)


__all__ = [
    "CONFIG_NAME",
    "CONSOLE_ENV",
    "EXECUTABLE_NAME",
    "FORK_ONLY",
    "KEYS",
    "KEYS_BY_NAME",
    "RESTART_REQUIRED",
    "STARTUP_TIMEOUT_S",
    "STOP_GRACE_S",
    "ConsoleConfig",
    "ConsoleProcess",
    "ConsoleProcessError",
    "ConsoleSupervisor",
    "FakeConsoleProcess",
    "Key",
    "Prepared",
    "find_console",
    "prepare_console",
    "read_startup_block",
]
