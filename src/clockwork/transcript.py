"""A timestamped record of everything said to a box and to the acquisition console.

Nothing else in `clockwork` keeps the conversation. A script keeps what it chose
to compute, the loop keeps the `Event`s it decided to report, and the wire itself
is gone the moment it has been parsed. That is affordable on a desk and not on a
bench day: a box is on the rig once, the digitizer is cabled to the instrument on
one prepared day, and a surprise that leaves only a summary number costs the next
visit (lab record, task 29).

So every layer under the Qt seam emits its traffic to a logger, and this module is
the one helper that turns those loggers into a file:

    from clockwork import transcript

    with transcript.to_file("bench-2026-09-11.transcript.log"):
        box.send_table(table_string)
        run_acquisition(method, boxes=boxes, console=console, stream=stream, ...)

Four logger names, all children of `clockwork`, so one handler catches every link
and a caller who wants one of them alone attaches a handler to that name instead:

    clockwork.mips.wire     every byte written to and read from a box, the
                            `STBLDAT` chunk structure, the asynchronous status
                            lines as they are classified
    clockwork.acq.wire      every ZeroMQ command frame and its reply, refusals,
                            timeouts and the socket resets that follow them
    clockwork.acq.stream    every `status` message, and one summary line per
                            published batch
    clockwork.acq.loop      the loop's own `Event` narrative, which is what it
                            decided rather than what the wire carried

**Off costs nothing.** The package configures no handlers and sets no levels; the
effective level of `clockwork.*` is whatever the application left it at, which by
default is the root's `WARNING`. Every call site in the package is therefore a
level check that fails and no `LogRecord` is ever built. `to_file` is the only
thing that changes that, and it puts the level back on the way out.

**What is deliberately not recorded.** A batch's payload -- `mz`, `tic` and
`time_stamps` -- is hundreds of kilobytes of display product per message, and it
is not the data path: the acquired data goes from the console into the UIMF file
and never passes through this client at all. Nor do the digitizer's samples, which
never reach Python. A batch gets one line saying how many scans it carried, over
what trigger timestamps, in how many bytes. At the instrument's 500-scan batch
that is about fifteen lines a second, so there is no case for keeping up by
dropping records, and a transcript that dropped them would be worth less than one
that counted them.

Nothing is redacted. Nothing on either wire is secret: a MIPS command is a pulse
sequence and a console command is a digitizer setting.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import time

ROOT_LOGGER = "clockwork"
"""The logger every name below descends from, and the one a handler goes on."""

LOGGERS = (
    "clockwork.mips.wire",
    "clockwork.acq.wire",
    "clockwork.acq.stream",
    "clockwork.acq.loop",
)
"""The four names this module documents. Public: a caller may attach to any of
them directly, and a name that disappeared would break such a caller."""

NOTE_LOGGER = "clockwork.note"
"""Where `note()` puts a caller's own line, so that a script doing something the
package cannot see -- reading a reply straight off a transport, turning a knob --
can put it in the same file in the same order."""

MAX_BYTES = 256
"""How much of one read or write is rendered before it is elided.

A `TBLRPT` dump is one line per byte and a real table runs to thousands of them,
so an untruncated transcript of one is tens of thousands of characters that say
nothing the parsed report does not. The one thing never truncated is the
`STBLDAT` string itself, which is the evidence any correction to
`docs/mips-wire-format.md` would be argued from.
"""

FORMAT = "%(asctime)s.%(msecs)03d %(shortname)-16s %(message)s"
DATE_FORMAT = "%H:%M:%S"
"""Time of day to the millisecond. The date is in the file's header: a transcript
that spans midnight is a bench day that went badly wrong."""

_LOG = logging.getLogger(NOTE_LOGGER)


def render(data: bytes, limit: int = MAX_BYTES) -> str:
    """Bytes as `repr`, control characters visible, elided past `limit`.

    `repr` rather than a decode because the framing *is* the question on the MIPS
    side: the LF-CR after a set-style ACK, the CR-LF after a value, the doubled
    newline after a status line and the `?` after a NAK are four conventions in
    one firmware (§1), and a transcript that normalised them away would be
    useless for the one job it has there.
    """
    if len(data) <= limit:
        return repr(data)
    return f"{data[:limit]!r} (+{len(data) - limit} more)"


def default_name(stem: str, *, when: _dt.date | None = None) -> str:
    """`<stem>-<date>.transcript.log`, the name every caller should use.

    Here rather than in each caller so that a bench script, the loop and later the
    window cannot drift apart on it: a transcript is meant to be found beside the
    `results.json` or the UIMF file it belongs to, by someone who was not there.
    """
    return f"{stem}-{(when or _dt.date.today()).isoformat()}.transcript.log"


def note(message: str, *args: object) -> None:
    """Put one line of a caller's own into the transcript, in its own place.

    For the traffic the package cannot see. `%`-style arguments are formatted only
    if a transcript is open, so an expensive message costs nothing when none is.
    """
    _LOG.debug(message, *args)


class _ShortName(logging.Filter):
    """`clockwork.mips.wire` -> `mips.wire` in the column, in full nowhere else."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        record.shortname = name[len(ROOT_LOGGER) + 1:] if name.startswith(ROOT_LOGGER + ".") \
            else name
        return True


class Transcript:
    """An open transcript file. Made by `to_file`, closed by `close` or a `with`.

    Closing puts back the logger's level and its propagation, and removes the
    handler, so a process that opens one per run leaves nothing behind between
    them.
    """

    __slots__ = ("_handler", "_level", "_logger", "_propagate", "_started", "path")

    def __init__(self, path: str, handler: logging.Handler, logger: logging.Logger,
                 level: int, propagate: bool) -> None:
        self.path = path
        self._handler = handler
        self._logger = logger
        self._level = level
        self._propagate = propagate
        self._started = time.monotonic()

    def close(self) -> None:
        if self._handler is None:
            return
        self._write(f"--- closed after {time.monotonic() - self._started:.1f} s\n")
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._level)
        self._logger.propagate = self._propagate
        self._handler.close()
        self._handler = None  # type: ignore[assignment]

    def note(self, message: str, *args: object) -> None:
        """`transcript.note`, for a caller that has the object to hand."""
        note(message, *args)

    def _write(self, text: str) -> None:
        stream = getattr(self._handler, "stream", None)
        if stream is not None:
            stream.write(text)
            stream.flush()

    def __enter__(self) -> Transcript:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Transcript({self.path!r})"


def to_file(
    path: str | os.PathLike[str],
    *,
    mode: str = "w",
    level: int = logging.DEBUG,
    propagate: bool = False,
    header: str = "",
) -> Transcript:
    """Attach a file handler to the `clockwork` logger and hand back the handle.

    The file is flushed per record, so it is a record of a run that died as much
    as of one that finished, which is the whole point of having it on a bench day.

    `propagate` is false by default, and that is not a detail. Raising the
    package's level to `DEBUG` would otherwise send every byte of the transcript
    up to whatever the application has on the root logger, which for a bench
    script is the terminal it is also printing its own results to. A caller who
    does want both passes `propagate=True` and arranges the levels itself.

    `mode` defaults to `"w"`, which suits a caller whose file is named for one run.
    Pass `"a"` where a day's runs merge into one results file -- a bench script run
    once per step with a knob turned between them -- and the header block written
    on the way in separates one run from the next inside the file.
    """
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    handler = logging.FileHandler(path, mode=mode, encoding="utf-8")
    handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATE_FORMAT))
    handler.addFilter(_ShortName())

    from . import __version__

    logger = logging.getLogger(ROOT_LOGGER)
    was_level, was_propagate = logger.level, logger.propagate
    stream = handler.stream
    stream.write(
        f"clockwork {__version__} wire transcript, "
        f"{_dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        f"{', '.join(LOGGERS)}\n"
        "bytes are repr, control characters visible, elided past "
        f"{MAX_BYTES} bytes except a table string\n"
        "batch payloads and digitizer samples are not here and never were: "
        "the data goes console to file\n"
        + (header.rstrip("\n") + "\n" if header else "")
        + "---\n"
    )
    stream.flush()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = propagate
    return Transcript(path, handler, logger, was_level, was_propagate)


__all__ = [
    "FORMAT",
    "LOGGERS",
    "MAX_BYTES",
    "NOTE_LOGGER",
    "ROOT_LOGGER",
    "Transcript",
    "default_name",
    "note",
    "render",
    "to_file",
]
