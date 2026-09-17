"""A trainee's pane of strings, to a method's phases and back.

The window's panes *are* the method (lab record, task 48, and task 50's decision 2):
each box has one plain-text pane, one string per line, exactly what used to be
pasted into the MIPS host app's terminal, and Save writes schema-2 TOML. So one
module has to turn that text into `BoxMethod` phases and `Step` sequences and
turn them back into the same text. Nothing here is Qt and nothing here opens a
port.

**Classification is not interpretation.** `classify` decides *when* a string is
sent -- which phase it belongs to -- from the command word alone. It never reads
a table's ticks, a compression table's ops or a channel number; a string it does
not recognise is `setup`, which is the phase that sends everything once and
changes nothing about the run. The public format document commits the method to
carrying no interpretation of its strings and that commitment is intact: what
the strings *mean* is still the deferred compiler's, and the counts a run is
checked against are read in `clockwork.acq.loop`, above this seam.

The five phases a line can land in are `setup`, `load`, `arm`, `start` and
`reset`; a comment is a sixth tag and belongs to whichever phase it precedes.
`start` and `reset` are method-level, so a pane yields its own box's steps and
`start_order` puts the boxes' steps in the order the experiment needs -- which
is what the window displays and never lets a trainee type.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from . import BoxMethod, RfChannel, Step, is_comment

PHASES: tuple[str, ...] = ("setup", "load", "arm", "start", "reset")
"""The phases a pane's lines are divided into, in the order a pane renders them."""

TAGS: tuple[str, ...] = PHASES + ("comment", "directive", "blank", "unplaced")
"""Every value a `Line.tag` can take, which is what a window's margin shows.

A command's tag is the phase it lands in. `comment` is a trainee's note,
`directive` a `# clockwork:` line that is consumed rather than stored, `blank` a
separator, and `unplaced` a line that is not a command at all. The last four say
nothing about a phase, which is what `Line.phase` is for: a comment belongs to
the phase it labels and a margin wants to show both facts.
"""

LOAD_COMMANDS = frozenset({"STBLDAT", "SARBCTBL"})
"""The command words that load something: a pulse-sequence table, a compression table.

Both are re-sent for every acquisition and neither survives a power cycle, which
is what `load` means. `SARBCTBL` is wire format section 6; `STBLDAT` is section 2.
"""

START_COMMANDS: tuple[str, ...] = ("TARBTRG", "TBLSTRT")
"""The command words that start something, in the cross-box order they are sent.

`TARBTRG` before `TBLSTRT` is the experiment's own requirement rather than a
convenience: a compression table told to run waits at its first hold for the
release edge, and a box that has not reached that hold when the first edge
arrives misses the whole first repetition. `start_order` sorts on this tuple.
"""

TAG_PATTERN = re.compile(r"#\s*clockwork\s*:\s*([A-Za-z]+)\s*\Z", re.IGNORECASE)
"""`# clockwork: <phase>`, the one directive a pane understands.

It tags the group of lines that follows, and is the answer to the one thing the
golden trainee file states as prose rather than as strings: "to reset for a
technical replicate, send `SMOD,LOC` to MIPS A, then start again". A `reset` is
the *complete* list a replicate sends (lab record, task 14), and no command word
says "this one is a reset" -- `SMOD,LOC` is an ordinary setter in every other
context. The same directive overrides the classifier anywhere else, which is
what decision 8 of the window design (lab record, task 50) means by "a line the
classifier cannot place gets a manual tag, never a refusal".

A directive line is consumed by `parse_pane` and re-emitted by `render_pane`, so
it never becomes a string in a phase. It is the one line in a pane that is
clockwork's rather than the trainee's.
"""

_COMMAND_WORD = re.compile(r"[A-Z][A-Z0-9]*\Z")
_LABEL = re.compile(r"MIPS[\s-]*([A-Z])\b")


def head(command: str) -> str:
    """The command word of a method string, however its arguments are punctuated.

    `,` for an ordinary setter and `;` for the table upload's payload (wire
    format sections 1 and 2). Upper-cased, because the firmware's parser is
    case-insensitive on the word and trainees are inconsistent about it.
    """
    return command.split(",", 1)[0].split(";", 1)[0].strip().upper()


def argument(command: str) -> str:
    """A one-argument command's argument, upper-cased: `SMOD,TBL` -> `TBL`."""
    return command.partition(",")[2].strip().upper()


def is_command_like(text: str) -> bool:
    """Whether a line could be a MIPS command at all, by its word alone.

    A trainee's paste file is not all commands. Both golden files end in a
    sentence of prose, and one carries three lines of FALKOR settings written as
    `Ion Mobility Scans = 5000`. Left to the classifier's "anything unknown is
    `setup`" rule those would be sent to a box, which is the one thing a
    classifier must not do quietly. So a line whose word is not a bare
    alphanumeric token is not placed in a phase at all: it comes back in
    `PaneResult.unplaced` for the window to tag by hand or leave alone.

    Deliberately weak -- it tests the shape of the word and nothing else, so a
    command this package has never heard of still passes.
    """
    stripped = text.strip()
    if not stripped or is_comment(stripped):
        return False
    return _COMMAND_WORD.fullmatch(head(stripped)) is not None


def classify(command: str) -> str:
    """Which phase a string belongs to: `setup`, `load`, `arm`, `start`, `comment`.

    The whole command table, and it is short on purpose:

    - `STBLDAT`, `SARBCTBL` -> `load`. Sent once per acquisition.
    - `SMOD` -> `arm`, unless its argument is `LOC`. `TBL`, `ONCE` and `SMOD,<n>`
      all put the box in table mode (wire format section 3); `SMOD,LOC` takes it
      out, which is a `setup` string on its own and the first line of a `reset`
      group in context.
    - `TARBTRG`, `TBLSTRT` -> `start`. Method-level, once per console frame.
    - anything else -> `setup`. Including a word this package has never seen:
      `setup` sends it once, in the order written, and changes nothing else, so
      guessing wrong there costs a trainee nothing. Guessing wrong towards
      `load` or `start` would re-send it per acquisition or per frame.

    `reset` is not here, and cannot be: nothing about `SMOD,LOC` says whether it
    is a reset or an ordinary drop to local. That is decided in `parse_pane`,
    from the `# clockwork: reset` tag or from where the group sits.
    """
    if is_comment(command):
        return "comment"
    word = head(command)
    if word in LOAD_COMMANDS:
        return "load"
    if word == "SMOD":
        return "setup" if argument(command) == "LOC" else "arm"
    if word in START_COMMANDS:
        return "start"
    return "setup"


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a pane as the parser read it, for the window's margin.

    `number` is 1-based in the text as given, so a window can put the tag beside
    the line a trainee is looking at. `text` is stripped, and is `""` for a blank
    line. `tag` is one of `TAGS`.
    """

    number: int
    text: str
    tag: str
    phase: str = ""
    """The phase this line is stored in: its own, or for a comment the one it labels.

    Empty for a blank, a directive and an unplaced line, none of which is stored.
    """

    @property
    def sent(self) -> bool:
        """Whether this line goes on a wire. Comments, blanks and unplaced do not."""
        return self.tag in PHASES


@dataclass(frozen=True, slots=True)
class PaneResult:
    """One box's pane, classified.

    The three phases are strings, comments among them; `start` and `reset` are
    `Step`s already named with this box, so a caller can concatenate the panes'
    lists without re-attributing them. `lines` is every line in order for the
    margin, and `unplaced` is the subset that is not a command and not a comment
    -- prose a trainee left in the pane. `warnings` carries the repairs, in the
    shape `clockwork.method` uses.
    """

    box: str
    setup: tuple[str, ...] = ()
    load: tuple[str, ...] = ()
    arm: tuple[str, ...] = ()
    start: tuple[Step, ...] = ()
    reset: tuple[Step, ...] = ()
    lines: tuple[Line, ...] = field(default_factory=tuple, compare=False)
    unplaced: tuple[Line, ...] = field(default_factory=tuple, compare=False)
    warnings: tuple[str, ...] = field(default_factory=tuple, compare=False)

    def box_method(
        self,
        port: str,
        *,
        dc_bias: Sequence[tuple[int, float]] = (),
        rf: Sequence[RfChannel] = (),
    ) -> BoxMethod:
        """This pane as a `BoxMethod`. The analog declarations are not pane text."""
        return BoxMethod(
            name=self.box,
            port=port,
            setup=self.setup,
            load=self.load,
            arm=self.arm,
            dc_bias=tuple(dc_bias),
            rf=tuple(rf),
        )


@dataclass(slots=True)
class _Entry:
    """One line on the way through the parser, before its phase is settled."""

    number: int
    text: str
    group: int
    tag: str
    phase: str = ""
    tagged: bool = False
    """Whether the phase came from a `# clockwork:` directive rather than the table."""


def parse_pane(text: str, box: str) -> PaneResult:
    """One box's pane text as phases, start steps and reset steps.

    Blank lines separate groups and are not kept (lab record, task 50, decision 11): they
    are how a trainee's paste file has always been punctuated, and what they
    punctuate is which strings belong to which comment. Surrounding whitespace on
    a command is stripped and reported, as `clockwork.method._command` does, and
    on a comment stripped silently.

    A group's phase is settled line by line, with two rules over the top of
    `classify`:

    - A leading `# clockwork: <phase>` directive forces the whole group. That is
      how a `reset` is written, and how decision 8's manual tag (lab record,
      task 50) is expressed.
    - Without a directive, a group that sits after the last `start` line and
      begins with `SMOD,LOC` is the reset group. That is the one place this
      module reads a trainee's *layout* rather than their words, and it exists
      because the golden CLOCK file states its reset as prose ("to reset for a
      technical replicate, send `SMOD,LOC` to MIPS A, then start again at
      `SMOD,TBL`") and schema 2 wants it as strings.

    **The reset group is completed with the box's `arm` phase where it does not
    already end in it**, which is the one string this module supplies that the
    trainee did not type. A `reset` is the complete list a replicate sends, and a
    box dropped to local that is not armed again cannot be started; the golden
    transcription writes both lines for exactly that reason. Text that
    `render_pane` wrote carries the directive and both lines, so nothing that has
    been through the window ever depends on the completion.

    A comment takes the phase of the next placed line in its own group, so that
    it labels the strings under it; a comment with nothing under it takes the
    phase of the last placed line before it, and `setup` if the pane has none.
    """
    entries = _entries(text)
    warnings = [
        f"line {entry.number}: stripped surrounding whitespace from {raw!r}"
        for entry, raw in _stripped(text, entries)
    ]
    _tag_groups(entries)
    _tag_reset(entries)
    _tag_comments(entries)

    phases: dict[str, list[str]] = {phase: [] for phase in PHASES}
    for entry in entries:
        if entry.phase:
            phases[entry.phase].append(entry.text)
    _complete_reset(phases)
    return PaneResult(
        box=box,
        setup=tuple(phases["setup"]),
        load=tuple(phases["load"]),
        arm=tuple(phases["arm"]),
        start=tuple(Step(box, command) for command in phases["start"]),
        reset=tuple(Step(box, command) for command in phases["reset"]),
        lines=tuple(Line(entry.number, entry.text, entry.tag, entry.phase)
                    for entry in entries),
        unplaced=tuple(Line(entry.number, entry.text, entry.tag, entry.phase)
                       for entry in entries if entry.tag == "unplaced"),
        warnings=tuple(warnings),
    )


def _entries(text: str) -> list[_Entry]:
    """Every line, stripped, with the group it belongs to and its own classification."""
    entries: list[_Entry] = []
    group = 0
    started = False
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped:
            if started:
                group += 1
                started = False
            entries.append(_Entry(number, "", group, "blank"))
            continue
        started = True
        if is_comment(stripped):
            tag = "comment"
        elif not is_command_like(stripped):
            tag = "unplaced"
        else:
            tag = classify(stripped)
        entries.append(_Entry(number, stripped, group, tag,
                              phase=tag if tag in PHASES else ""))
    return entries


def _stripped(text: str, entries: list[_Entry]) -> list[tuple[_Entry, str]]:
    """The commands whose whitespace was repaired, with what they were written as."""
    raws = text.splitlines()
    return [(entry, raws[entry.number - 1]) for entry in entries
            if entry.tag not in ("blank", "comment")
            and raws[entry.number - 1] != entry.text]


def _tag_groups(entries: list[_Entry]) -> None:
    """Apply each group's leading `# clockwork:` directive to the group."""
    for group in {entry.group for entry in entries}:
        members = [entry for entry in entries if entry.group == group and entry.text]
        if not members:
            continue
        match = TAG_PATTERN.fullmatch(members[0].text)
        if match is None:
            continue
        phase = match.group(1).lower()
        if phase not in PHASES:
            # Not a phase this schema has. Left as an ordinary comment rather than
            # refused: a trainee's `# clockwork: maybe` is a note, not an error.
            continue
        members[0].tag = "directive"
        members[0].phase = ""
        for entry in members[1:]:
            if entry.tag != "comment":
                entry.tag = phase
                entry.phase = phase
                entry.tagged = True


def _tag_reset(entries: list[_Entry]) -> None:
    """The untagged reset group: after the last start line, beginning `SMOD,LOC`."""
    starts = [entry.number for entry in entries if entry.tag == "start"]
    if not starts:
        return
    last = max(starts)
    for group in sorted({entry.group for entry in entries}):
        members = [entry for entry in entries
                   if entry.group == group and entry.tag in PHASES and not entry.tagged]
        if not members or members[0].number <= last:
            continue
        if not (head(members[0].text) == "SMOD" and argument(members[0].text) == "LOC"):
            continue
        for entry in members:
            entry.tag = "reset"
            entry.phase = "reset"
        return


def _tag_comments(entries: list[_Entry]) -> None:
    """A comment labels what follows it: the next placed line in its own group."""
    previous = "setup"
    pending: list[_Entry] = []
    for entry in entries:
        if entry.tag == "comment":
            pending.append(entry)
            continue
        if entry.tag not in PHASES:
            continue
        for waiting in pending:
            # Only a comment in this line's own group is a label for it. One in an
            # earlier group labelled nothing, so it keeps company with what it
            # followed rather than jumping a blank line forward.
            waiting.phase = entry.phase if waiting.group == entry.group else previous
        pending = []
        previous = entry.phase
    for waiting in pending:
        waiting.phase = previous


def _complete_reset(phases: dict[str, list[str]]) -> None:
    """A reset that does not end in the box's `arm` phase gains it.

    See `parse_pane`. The comparison is on the commands alone, so a comment
    between the trainee's `SMOD,LOC` and the arming this appends does not make
    the list look incomplete.
    """
    arm = [command for command in phases["arm"] if not is_comment(command)]
    reset = [command for command in phases["reset"] if not is_comment(command)]
    if not reset or not arm or reset[-len(arm):] == arm:
        return
    phases["reset"].extend(arm)


def render_pane(
    box: BoxMethod,
    start: Sequence[Step] = (),
    reset: Sequence[Step] = (),
) -> str:
    """One box's phases as the pane text a trainee reads and edits.

    `start` and `reset` are the method's own cross-box lists; this filters them
    to `box`. `dc_bias` and `rf` render as nothing: they are fields the window
    edits in a form, not strings a trainee types (`method-file-format.md`, "the
    analog state a method may declare").

    Phases go in the order `setup`, `load`, `arm`, `start`, `reset`, one blank
    line between groups, with a group beginning wherever a comment follows a
    command -- which is how the trainees' own paste files are punctuated and how
    `parse_pane` reads them back. A phase whose first command would classify as
    something else gains a `# clockwork: <phase>` directive and is rendered as
    one group, so that the directive covers all of it; in practice that is every
    `reset` and nothing else in either golden method.

    `parse_pane(render_pane(x)) == x` for any method whose strings a trainee
    could have typed. It is not a two-way round trip: text is free to put a
    `setup` line after its `load`, and re-rendering gathers it back into the
    `setup` block.
    """
    blocks: list[list[str]] = []
    for phase, commands in (
        ("setup", tuple(box.setup)),
        ("load", tuple(box.load)),
        ("arm", tuple(box.arm)),
        ("start", tuple(step.command for step in start if step.box == box.name)),
        ("reset", tuple(step.command for step in reset if step.box == box.name)),
    ):
        blocks.extend(_render_phase(phase, commands))
    return "\n\n".join("\n".join(block) for block in blocks)


def _render_phase(phase: str, commands: Sequence[str]) -> list[list[str]]:
    """One phase as blocks of lines, a blank line between each pair."""
    if not commands:
        return []
    if any(classify(command) != phase for command in commands
           if not is_comment(command)):
        return [[f"# clockwork: {phase}", *commands]]
    blocks: list[list[str]] = [[]]
    for command in commands:
        if is_comment(command) and blocks[-1] and not is_comment(blocks[-1][-1]):
            blocks.append([])
        blocks[-1].append(command)
    return blocks


def start_order(panes: Mapping[str, PaneResult]) -> tuple[Step, ...]:
    """Every pane's start steps in the order the experiment needs them sent.

    `TARBTRG` before `TBLSTRT` (`START_COMMANDS`), box order within each, and
    anything else a pane classified as `start` after both, in box order. The
    mapping's order is the box order, which is the method's.

    This is what the window displays above the panes and what a trainee never
    types: the cross-box order is the experiment's, derived here from the
    classification, and decision 2 of the window design (lab record, task 50) keeps
    it out of the panes for the same reason `send_phases` is the only sender.

    Each command carries the comments written above it in its own pane, so a
    trainee's note about which box a step goes to survives the reordering. A
    comment with no command under it in a pane goes last among that pane's steps.
    """
    units = {name: _units(pane.start) for name, pane in panes.items()}
    ordered: list[Step] = []
    for word in START_COMMANDS:
        for name in panes:
            for leading, step in units[name]:
                if step is not None and head(step.command) == word:
                    ordered += [*leading, step]
    for name in panes:
        for leading, step in units[name]:
            if step is None:
                ordered += leading
            elif head(step.command) not in START_COMMANDS:
                ordered += [*leading, step]
    return tuple(ordered)


def _units(steps: Sequence[Step]) -> list[tuple[tuple[Step, ...], Step | None]]:
    """A sequence as (leading comments, command) units; a trailing unit has no command."""
    units: list[tuple[tuple[Step, ...], Step | None]] = []
    leading: list[Step] = []
    for step in steps:
        if is_comment(step.command):
            leading.append(step)
            continue
        units.append((tuple(leading), step))
        leading = []
    if leading:
        units.append((tuple(leading), None))
    return units


def split_trainee_file(
    text: str, boxes: Mapping[str, str] | None = None
) -> dict[str | None, str]:
    """A multi-box paste file as one pane per box, for opening an old file once.

    The trainees' files name their boxes in comments -- "Send to MIPS A", "sent
    to MIPS B" -- so a group of strings under such a comment, and a command whose
    own trailing comment names a box, can be attributed. `boxes` maps a label's
    letter form (`"MIPS A"`, matched case-insensitively) to a box name; without
    it each label is its own key, which is what a caller with no map wants.

    Everything the file does not attribute comes back under `None`, whole and in
    order, for the trainee's clipboard. That is not a failure of the heuristic:
    one golden file gives its travelling-wave frequency and amplitude block once,
    unlabelled, for two boxes, and the other names no box anywhere. A rule that
    guessed would put strings on a box the file never said to put them on.

    A trailing comment on a command (`TARBTRG #sent to MIPS B.`) is moved onto
    its own line above it, because a pane is one command per line and the box
    would reject the line as written. Nothing else is rewritten.
    """
    labels = {key.upper(): value for key, value in (boxes or {}).items()}
    panes: dict[str | None, list[list[str]]] = {}
    for group in _groups(text):
        lines = [line for raw in group for line in _split_trailing(raw)]
        label = next((found for line in lines if is_comment(line)
                      and (found := _label(line, labels)) is not None), None)
        blocks: dict[str | None, list[str]] = {}
        for index, line in enumerate(lines):
            if is_comment(line):
                own = _label(line, labels) or label
            elif index and is_comment(lines[index - 1]):
                # A command's own trailing comment, now the line above it, is what
                # names its box: the CLOCK start group is three commands under one
                # unlabelled heading, each going to a different box.
                own = _label(lines[index - 1], labels) or label
            else:
                own = label
            blocks.setdefault(own, []).append(line)
        for name, block in blocks.items():
            panes.setdefault(name, []).append(block)
    return {name: "\n\n".join("\n".join(block) for block in blocks)
            for name, blocks in panes.items()}


def _groups(text: str) -> list[list[str]]:
    """The file's blank-line-separated groups, stripped, blanks dropped."""
    groups: list[list[str]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            if groups and groups[-1]:
                groups.append([])
            continue
        if not groups:
            groups.append([])
        groups[-1].append(stripped)
    return [group for group in groups if group]


def _split_trailing(line: str) -> list[str]:
    """`TARBTRG #sent to MIPS B.` -> the comment, then the command.

    Only where what precedes the `#` is command-like: `Ion Mobility Scans = 5000
    #number of pushes in frame` is prose with a note on it and stays one line,
    which leaves it unplaced rather than half-placed.
    """
    stripped = line.strip()
    if is_comment(stripped):
        return [stripped]
    at = stripped.find("#")
    if at <= 0:
        return [stripped]
    command = stripped[:at].strip()
    if not is_command_like(command):
        return [stripped]
    return [stripped[at:].strip(), command]


def _label(comment: str, labels: Mapping[str, str]) -> str | None:
    """The box a comment names, mapped through `labels`; None where it names none."""
    match = _LABEL.search(comment)
    if match is None:
        return None
    found = f"MIPS {match.group(1)}"
    return labels.get(found.upper(), found)


__all__ = [
    "LOAD_COMMANDS",
    "PHASES",
    "START_COMMANDS",
    "TAGS",
    "TAG_PATTERN",
    "Line",
    "PaneResult",
    "argument",
    "classify",
    "head",
    "is_command_like",
    "parse_pane",
    "render_pane",
    "split_trainee_file",
    "start_order",
]
