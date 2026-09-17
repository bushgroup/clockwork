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

Five logger names, all children of `clockwork`, so one handler catches every link
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
    clockwork.acq.console_process
                            the console process's own stdout and stderr, line
                            by line, and the supervisor's decisions about it --
                            started, answered, restarted, stopped

The last is not a wire. It is the only place the console's `std::cerr` reaches a
client at all: `wrong header -- cst acq (not zero sp)` is written there and
`std::cerr` is not one of spdlog's sinks, so before there was a supervisor to
capture it that line was in neither the console's log nor anything a client saw
(lab record, tasks 21 and 49).

**Off costs nothing.** The package configures no handlers and sets no levels; the
effective level of `clockwork.*` is whatever the application left it at, which by
default is the root's `WARNING`. Every call site in the package is therefore a
level check that fails and no `LogRecord` is ever built. `to_file` and `send_log`
are the only things that change that, and both put the level back on the way out.

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

**The send log is the same records, filtered.** `send_log(path)` attaches a second
handler to the same four loggers and keeps one line per string sent, per reply and
per status line, with the string as text rather than as `repr` and with the chunk
bookkeeping, the batch summaries and the byte reads dropped:

    with transcript.send_log(directory + "/" + stem + ".sent.txt", header=...):
        run_acquisition(...)

It is written beside the UIMF file the run produces, and it is what a trainee
reads at the bench: the strings that drove this file, in the order they went, with
what each box said back. Nothing new is recorded for it. Every call site that
belongs in it passes a `Sent` alongside its message (`sent()` below), which says
what the line is -- to a box, from a box, unprompted, or the run's own decision --
and how to print it; every other record carries none and the filter drops it. So
the wire transcript is unchanged and stays the forensic record, and the two files
of one run cannot disagree about what happened (lab record, task 39).
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - annotations only, and no import cycle
    from collections.abc import Iterable, Sequence

    from .instrument import Instrument
    from .method import Method

ROOT_LOGGER = "clockwork"
"""The logger every name below descends from, and the one a handler goes on."""

LOGGERS = (
    "clockwork.mips.wire",
    "clockwork.acq.wire",
    "clockwork.acq.stream",
    "clockwork.acq.loop",
    "clockwork.acq.console_process",
)
"""The five names this module documents. Public: a caller may attach to any of
them directly, and a name that disappeared would break such a caller."""

TO_BOX = ">"
FROM_BOX = "<"
UNPROMPTED = "!"
DECIDED = "*"
ASIDE = "#"
"""The five marks a send log's second column can carry.

`>` is a string this host put on a link and `<` is what answered it, which on the
MIPS side is `ACK`, `ACK` and a value, or the NAK with the number and text `GERR`
gave for it. `!` is a line a box raised on its own -- `TBLRDY`, `TBLTRIG`,
`TBLCMPLT`, `ABORTED` -- which arrives unprompted and is the box's own account of
what its table did. `*` is the run's own narrative, the phase a string belonged to
and what the loop decided. `#` is a line a caller wrote with `note`.

Four characters rather than words because the column has to be scannable down a
page of a hundred strings, and because the direction is the first thing a reader
wants: what was sent is what a trainee recognises, and what came back is where a
run went wrong.
"""

CONSOLE = "console"
RUN = "run"
"""What stands in the name column for something that is not a box.

The boxes name themselves -- a method gives each one a bird and a port -- so the
two correspondents with no name of their own get these: the acquisition console,
and the loop itself for a line that is about the run rather than about one link.
Spelled once here because a log is read by running an eye down that column.
"""


@dataclass(frozen=True, slots=True)
class Sent:
    """What one record is, for a reader rather than for a forensic analyst.

    Attached to a `LogRecord` under the name `sent` by the `sent()` helper below,
    and read by the send log's filter. A record without one never reaches a send
    log, which is how the chunk bookkeeping, the batch summaries and the raw byte
    reads stay in the wire transcript and out of the trainee's file.

    `text` empty means "the record's own message", which keeps a `%`-style call
    site lazy: nothing is formatted unless a handler is listening.
    """

    mark: str
    source: str
    """The box's name, `console`, or empty for the run itself."""

    text: str = ""

    only: bool = False
    """Whether this record exists for the send log alone.

    True on the handful that restate something the wire already carried in a form
    the wire cannot: a box's reply classified as `ACK`, a value, or the NAK with
    the number and text `GERR` gave for it. The bytes behind each are in the wire
    transcript already, so a derived line there would be length without evidence,
    and `to_file` drops them.
    """


def sent(mark: str, source: str, text: str = "", *, only: bool = False) -> dict[str, object]:
    """The `extra=` a call site passes to put its record in the send log.

    A function rather than a literal dict at each site so that the attribute name
    is written once: a typo in it would drop a line out of the trainee's file and
    leave the wire transcript looking correct.
    """
    return {"sent": Sent(mark, source, text, only)}


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

SEND_FORMAT = "%(asctime)s.%(msecs)03d %(sentsource)-11s %(sentmark)s %(senttext)s"
"""The send log's line: the same clock, then who, then which way, then the string.

Eleven characters of name holds every bird on this instrument (`bufflehead` is the
longest) and the word `console`, so the marks line up down the page and a reader
can follow one box without reading the others.
"""

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


def send_log_name(stem: str) -> str:
    """`<stem>.sent.txt`, beside `<stem>.uimf`.

    No date in it, unlike `default_name`: a send log belongs to one acquisition and
    the stem already carries that acquisition's timestamp, and the whole point of
    the name is that a file and its log sort together in a directory listing.
    """
    return f"{stem}.sent.txt"


def note(message: str, *args: object, source: str = "") -> None:
    """Put one line of a caller's own into the transcript, in its own place.

    For the traffic the package cannot see. `%`-style arguments are formatted only
    if a transcript is open, so an expensive message costs nothing when none is.

    `source` fills the name column, for a note that is about one correspondent
    rather than about the run: a box's state readback is written a line at a time
    under the box's own name, so that a reader following one box down the column
    sees what it was holding as well as what it was sent (lab record, task 40).
    """
    _LOG.debug(message, *args, extra=sent(ASIDE, source))


def note_block(text: str, *, source: str = "") -> None:
    """A multi-line note, one record a line, so every line keeps its columns.

    A block written as one record would put its first line in the columns and
    every line after it hard against the left margin, which is exactly the shape
    a reader scanning the mark column cannot follow. Blank lines are dropped.
    """
    for line in text.splitlines():
        if line.strip():
            note("%s", line, source=source)


def run_header(
    *,
    method: Method | None = None,
    method_path: str | os.PathLike[str] | None = None,
    instrument: Instrument | None = None,
    instrument_path: str | os.PathLike[str] | None = None,
    console: object = None,
    boxes: Iterable[Sequence[str]] = (),
    conditions: str = "",
) -> str:
    """The block that says what this run was, for the top of a log.

    One line each for the method and its hash, the instrument document and the
    window channel 1 acquired through, the console's `info` string, and a row per
    box naming the port, the box's own `GNAME` and the firmware `GVER` reported.
    Everything is optional; what is not given is left out rather than written as
    unknown, because a line saying "offset: unknown" reads as a measurement that
    failed and this is a caller that did not pass one.

    **Why a file needs it.** The strings below are what drove this acquisition, and
    the same strings through a different window, a different inversion or a
    different box are a different experiment. The method's hash is the link back to
    the document the strings came from; the box rows are what make a log name the
    boxes it drove rather than three ports (lab record, task 39).

    `boxes` is a sequence of `(name, port, identity, firmware)` rows, any field of
    which may be empty. A caller that has open `Box` objects has all four already:
    the port it opened, `box_name()` and `version()`.

    `conditions` is the trainee's own free text -- sample, MCP voltage, pusher
    period, pDRE, collision energy -- and goes last, indented under a heading of
    its own. **It is the piece no getter reads**: everything else in this block
    and every readback in the file came off a wire, and this is the part of the
    experiment that exists only if somebody typed it (lab record, task 40). It is
    in the header rather than written mid-run so that a replicate's log carries
    it too, without the run that wrote it having to say it twice.
    """
    lines: list[str] = []
    if method is not None:
        from .method import stamp as _stamp

        record = _stamp(method)
        where = os.path.basename(os.fspath(method_path)) if method_path else ""
        lines.append(
            f"method       {where + '  ' if where else ''}"
            f"{record['method_name']!r}  sha256 {str(record['method_hash'])[:16]}"
        )
    elif method_path:
        lines.append(f"method       {os.path.basename(os.fspath(method_path))}")
    if instrument is not None:
        vertical = instrument.vertical
        window = ", ".join(
            part for part in (
                f"full scale {vertical.full_scale_v} V"
                if vertical.full_scale_v is not None else "",
                f"offset {vertical.offset_v} V" if vertical.offset_v is not None else "",
                ("inverted" if vertical.inverted else "not inverted")
                if vertical.inverted is not None else "",
            ) if part
        )
        where = os.path.basename(os.fspath(instrument_path)) if instrument_path else ""
        lines.append(
            "instrument   " + "  ".join(
                part for part in (where, instrument.name, window) if part
            )
        )
    if console is not None:
        lines.append(f"console      {getattr(console, 'text', console)}")
    for row in boxes:
        name, rest = row[0], [str(part) for part in row[1:] if part]
        lines.append(f"{name:<11} {'  '.join(rest)}".rstrip())
    if conditions.strip():
        lines.append("conditions")
        lines += [f"  {line}".rstrip() for line in conditions.strip().splitlines()]
    return "\n".join(lines)


class _ShortName(logging.Filter):
    """`clockwork.mips.wire` -> `mips.wire` in the column, in full nowhere else.

    Also the one place a record marked `only` is dropped, which is what keeps the
    wire transcript exactly the byte-level record it was before there was a send
    log to serve: the few records a send log needs and the wire already carries
    are written for the send log and read by nothing else.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        marked = getattr(record, "sent", None)
        if marked is not None and marked.only:
            return False
        name = record.name
        record.shortname = name[len(ROOT_LOGGER) + 1:] if name.startswith(ROOT_LOGGER + ".") \
            else name
        return True


class _SendOnly(logging.Filter):
    """Keep the records a trainee needs, and spread their `Sent` into columns.

    The whole of the send log's selection rule: a record either carries a `Sent` or
    it does not, and the decision about which was made at the call site, where what
    the record *is* is known. Nothing here parses a message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        marked = getattr(record, "sent", None)
        if marked is None:
            return False
        record.sentmark = marked.mark
        record.sentsource = marked.source
        record.senttext = marked.text or record.getMessage()
        return True


class _LfFile(logging.FileHandler):
    """A file handler whose lines end `\\n` on every platform.

    `logging.FileHandler` opens in text mode with the platform's translation, which
    on Windows writes CRLF into a repo whose `.gitattributes` pins `eol=lf`: every
    transcript then reads as modified the moment it is touched, and a log committed
    beside its run shows up as a diff of itself (lab record, tasks 39 and 41). The
    files these write are read on Windows, Linux and in a browser, so the one that
    travels is the one to write.
    """

    def _open(self):  # noqa: ANN202 - the base class's signature
        return open(self.baseFilename, self.mode, encoding=self.encoding,
                    errors=self.errors, newline="\n")


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
    from . import __version__

    return _attach(
        path, mode=mode, level=level, propagate=propagate,
        line_format=FORMAT, keep=_ShortName(),
        preamble=(
            f"clockwork {__version__} wire transcript, "
            f"{_dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            f"{', '.join(LOGGERS)}\n"
            "bytes are repr, control characters visible, elided past "
            f"{MAX_BYTES} bytes except a table string\n"
            "batch payloads and digitizer samples are not here and never were: "
            "the data goes console to file\n"
        ),
        header=header,
    )


def send_log(
    path: str | os.PathLike[str],
    *,
    mode: str = "w",
    level: int = logging.DEBUG,
    propagate: bool = False,
    header: str = "",
) -> Transcript:
    """The same traffic as `to_file`, filtered down to what a trainee reads.

    One line per string sent to a box or to the console, per reply, per status line
    a box raised on its own, and per decision the loop made -- with a table string
    written out whole as text rather than as chunk bookkeeping and byte `repr`s.
    Attach it beside the UIMF file a run writes, named by `send_log_name`:

        stem = "bradykinin_clock-20260915-145701"
        with transcript.send_log(os.path.join(directory, send_log_name(stem)),
                                 header=transcript.run_header(method=method, ...)):
            run_acquisition(method, ..., stem=stem)

    **It is a view and not a second record.** Every line in it is a record the wire
    transcript holds too, so a question the send log raises is answered in the
    transcript beside it and the two cannot disagree. What it drops is the
    `STBLDAT` chunk structure, the per-batch summaries, the raw reads and the
    `GERR` round trip behind a rejection -- that last because the NAK line carries
    the number and the firmware's text already, and the firmware's text points the
    wrong way often enough that it belongs on the same line as the string it
    refused (lab record, task 39).

    Both this and `to_file` may be open at once, which is the arrangement a bench
    run uses: they are two handlers on the same logger and neither knows about the
    other. Whichever is closed last puts the logger's level and propagation back.
    """
    from . import __version__

    return _attach(
        path, mode=mode, level=level, propagate=propagate,
        line_format=SEND_FORMAT, keep=_SendOnly(),
        preamble=(
            f"clockwork {__version__} send log, "
            f"{_dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            "every string sent to a box and to the acquisition console, in the "
            "order it went\n"
            f"  {TO_BOX} sent by this host   {FROM_BOX} what answered it   "
            f"{UNPROMPTED} a line a box raised on its own   "
            f"{DECIDED} what the run did   {ASIDE} a note\n"
            "the wire transcript beside this file has the same traffic byte for "
            "byte, and more\n"
        ),
        header=header,
    )


def _attach(
    path: str | os.PathLike[str],
    *,
    mode: str,
    level: int,
    propagate: bool,
    line_format: str,
    keep: logging.Filter,
    preamble: str,
    header: str,
) -> Transcript:
    """Open one file, write its header block, and put a handler on `clockwork`.

    Shared by `to_file` and `send_log` so that the two files of one run are opened,
    flushed, closed and stamped the same way; what differs between them is the
    format, the filter and the block at the top.
    """
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    handler = _LfFile(path, mode=mode, encoding="utf-8")
    handler.setFormatter(logging.Formatter(line_format, datefmt=DATE_FORMAT))
    handler.addFilter(keep)

    logger = logging.getLogger(ROOT_LOGGER)
    was_level, was_propagate = logger.level, logger.propagate
    stream = handler.stream
    stream.write(preamble + (header.rstrip("\n") + "\n" if header else "") + "---\n")
    stream.flush()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = propagate
    return Transcript(path, handler, logger, was_level, was_propagate)


__all__ = [
    "ASIDE",
    "CONSOLE",
    "DECIDED",
    "RUN",
    "FORMAT",
    "FROM_BOX",
    "LOGGERS",
    "MAX_BYTES",
    "NOTE_LOGGER",
    "ROOT_LOGGER",
    "SEND_FORMAT",
    "TO_BOX",
    "UNPROMPTED",
    "Sent",
    "Transcript",
    "default_name",
    "note",
    "note_block",
    "render",
    "run_header",
    "send_log",
    "send_log_name",
    "sent",
    "to_file",
]
