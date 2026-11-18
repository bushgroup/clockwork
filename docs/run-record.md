# The run record and the day record

Every run clockwork acquires belongs to a request, and every request leaves one small JSON
document beside its files, the run record. A run record says what a request was for and who asked
for it, how it was planned and sent, which files it wrote, and what people said about them
afterwards. The files remain the primary record: each one stamps its request's identifier, and
the run record is what joins a request's files and says why each was made. Beside the run records,
the daemon keeps one day record per output directory for what happens under no request: the
sitting it was started with, its own starts and stops, and the acquisition console's.

This document is the format of both. It is the reference for anything that reads them back, such
as a manifest, a notebook or a published account of a day's work, and it changes only by
additions within one `record_schema`.

## Where a run record comes from

A run record is begun on the route the request came by, and that route is its `source`:

| `source` | The request came from | Who begins the record |
|---|---|---|
| `mcp` | a session's tool call through `clockwork-mcp` | the MCP server, as it arms or submits the job |
| `cli` | a verb of `clockwork-cli`, typed by a person or run from an agent's shell | the command line, as it arms or submits the job |
| `window` | a person pressing Acquire, or starting a series plan, at the window | the daemon, when it starts the job |

Records written before clockwork 1.2.0 say `agent` for both `mcp` and `cli`, without saying which;
the audit log's `via` tells them apart. A request continued under its identifier from another
route keeps the source its record was begun under.

Before clockwork 1.2.0 a run started at the window left no run record, and a `verdict` on one of
its files began a record for it with `window` as the source. That still happens for a file
acquired before 1.2.0. From 1.2.0 every run started at the window has a record of its own from the
moment its job starts, written by the daemon that acquires it, so a window run's record is whole
however the window ends. One press of Acquire is one request and one record, whatever its replicate
count; a press of Replicate is another, and so is each Start of a series plan, however many points
it has.

The daemon enters each file in its request's record as the file is written, on every route, before
it reports the job's end. A client that waits for a job to end and then reads the record therefore
finds every file in it.

## The file

`<stem>.request.json`, in the directory the request's files go to, where the stem is that of the
request's first file (`YYMMDD_INITIALS_NNN`). Should another request already have begun a record
under that stem, the new one is `<stem>-<request id>.request.json`. The document is UTF-8 with LF
line endings, and is written whole on every change, through a temporary file
(`<stem>.request.json.tmp`) and a rename, so that a reader never sees half of it. Neither file is
counted as one of the stem's files when the next file number is chosen.

The top level is an object:

| Key | What it holds |
|---|---|
| `record_schema` | `1`. Additions to the format keep the number; a change that a reader of the number would misread moves it |
| `request` | The request block, below |
| `plans`, `arms`, `acquisitions`, `files`, `notes`, `routines`, `verdicts` | One list per section, each entry stamped with its `time`, oldest first |
| `interventions` | A person's acts on a running job; present only once there is an entry |

Every `time` is local time, ISO 8601 to the second, as `2026-11-18T14:02:11`. A reader ignores a key
it does not know: later releases add keys to the request block and to entries without moving
`record_schema`.

## The request block

| Key | What it holds |
|---|---|
| `id` | The request's identifier, `YYMMDD-HHMMSS-xxxxxx`, the time it was minted and six hexadecimal digits. Every file of the request stamps it as `ClockworkSeriesId` |
| `text` | What the request is for, in the words of the person it was for. For a window run, the purpose line beside Start, recorded empty when it was left empty |
| `source` | The route, above |
| `initials` | The initials that name the request's files |
| `opened` | When the record was begun |
| `stem` | The stem it was begun at |
| `at_window` | A window run only: the initials of the person at the window who pressed Start |

The file initials and the person at the window are separate fields because they answer separate
questions. The initials in a file name are whatever the lab's naming convention puts there, often
the code of the ion under study, whereas `at_window` names a person.

## The sections

`plans` holds what a request set out to do. For `arm`, it is the agent's stated plan, `{text}`.
For a series, it is the whole plan: `text` (the plan's description), `job`, `plan` (the plan's
TOML text), `path`, `template` and `template_hash`, `labels`, `seed` and `shuffled`, `first_place`,
`acquisitions`, `files` and `minutes`, `points` (each point's index, knobs, repetitions,
replicates, reference and minutes), and `order`, the planned indices in the order acquired. Then:

| Key | What it holds |
|---|---|
| `sha256` | The SHA-256 of `plan`, the text with Windows line endings made Unix ones, as every `series` line of the audit log carries it |
| `checked` | The latest `series` call made with `check` true on a plan of this hash, read from the audit log in the request's directory when the series started: `{time, via, session}`, the line's time, its route (`mcp` or `cli`) and its daemon session. Null where there is none, which for a series started at the window means nobody checked this text through an agent |
| `outside_limits` | Each point outside the limits the daemon's sitting carries, `{index, problems}`, in the words an agent's refusal would use. Empty when none is, and null where there are no limits. A series an agent started has none outside, since its tool refuses such a plan, whereas a series a person starts at the window runs such points and lists them here |

A series started at the window has the same `plans` entry, written by the daemon when the job
starts, with `path` the file the person opened, and the same `acquisitions` entry less
`cold_start`, which the window's series says in the run log as each point is sent.

`arms` holds each send an agent's `arm` made: `stem`, `method` (its name), `hash` (the method's
SHA-256, cut to its first characters), `template` (as below, or null for a hand-written method),
`setup`, `conditions`, `cold_start` (the cautions the cold-start check raised), `finished`, and for
a send that finished, `seconds` and `send_log`, or for one that did not, its `job`.

`acquisitions` holds each acquisition job, one entry per job:

| Key | What it holds |
|---|---|
| `job` | The daemon's job number |
| `replicates` | How many files the job was asked for, the first and its technical replicates |
| `first_place` | The first file's place in the request, counted from 1; each later file is one more |
| `method` | The method's name, or for a series the template's |
| `method_hash` | A window run only: the SHA-256 of the method acquired, as its files stamp it |
| `template` | For a rendered run, `{path, hash, knobs, labels}`: the template's path where one is known, its SHA-256, and every knob value and label it was rendered at. A window run's adds `method_hash`, the rendered method's SHA-256. Null for a hand-written method |
| `outside_limits` | A rendered window run only: one sentence for each knob value outside the limits the daemon's sitting carries, in the words an agent's refusal would use, or for a template those limits do not admit at all. Empty when every value lies inside them, and null when the daemon carries no limits |
| `cold_start` | An agent's acquisition only: the cautions the cold-start check raised |
| `series`, `points` | A series only: `true`, and how many points it planned |

A person at the window is never held to the agent's limits. `outside_limits` records where a
person's values went beyond them, so that a reader can tell a run an agent could have made from
one only a person could.

`files` holds one entry per file written, replaced rather than repeated if the same stem is
entered twice: `stem`, `raw` and `summed` (the two files' paths, either empty where it was not
written), `complete`, `stopped_early`, `replicate` (whether the run was a replicate of the one
before it), `seconds`, and `summary`, the file's frame count, provisional frames, scans per frame,
accumulations, total counts, base peak, pusher period and its ratio to the declared one, and
saturated fraction, or `{problem}` with the sentence that says why the file could not be read.

`notes` holds free text: `{text, by}`. `by` is `session` for a note an agent added with the `note`
tool, and `server` for one the daemon or a client wrote, such as the sentence that says a job
failed, where in a series it failed and why, or that a series stopped after some of its points.

`routines` holds each routine's result: `routine` (its name), `hash`, `path`, `verdict`, `reason`,
`criteria`, `values`, `files`, `job` and `text`, the report as the routine words it.

`verdicts` holds each person's judgement of one run: `stem`, `verdict`, `words` and `by`, the
person's initials. A verdict is one of `worked`, `no_signal`, `saturated`, `wrong_sample` and
`other`, and `other` carries the person's `words`. A later verdict on the same stem supersedes the
earlier, which is kept. A verdict is distinct from a run's outcome, which the file stamps and which
says how the acquisition ended (`completed`, `stopped`, `failed` or `incomplete`).

`interventions` holds a person's acts on a job already running, each in the record of the job
acted on, whatever client started that job:

| Key | What it holds |
|---|---|
| `kind` | What was done: `pause`, `resume`, `stop` or `guard` |
| `grain` | For a pause, where the job holds: `point` (after the point in flight) or `repetition` (after the repetition in flight); empty otherwise |
| `by` | The initials of the person who acted, or the client's name where no person did |
| `client` | The client the act came through, as the daemon names it: `the clockwork window (pid 5120)`, or the route of an agent's `stop` |
| `job` | The daemon's job number |
| `reason` | The words given with the act, or empty |

The `interventions` section is part of the format from clockwork 1.2.0; the controls at the window
that fill it arrive later in the same release, and a record with none of them has no
`interventions` key.

## The day record

`day-record.jsonl`, in the daemon's output directory, appended one JSON object per line, UTF-8,
LF. The daemon writes it, and it holds what belongs to the day rather than to any request. Every
line has `time` and `kind`, and `by` where a person or a client caused it:

| `kind` | When | What else the line holds |
|---|---|---|
| `sitting` | First in every segment of the day record | `version`, `session`, `pid`, `fake`, `output`, `library`, and `instrument`, `limits` and `project_limits`, each `{path, sha256}` of the document in force, or null for none |
| `daemon` | The daemon started or stopped | `event` (`started` or `stopped`), `session`, `reason` |
| `console` | The acquisition console started, restarted or stopped | `event` (`started`, `restarted`, `stopped` or `failed`), `job`, `by` (the client whose job it was, or the daemon itself), `detail` |

The SHA-256 of each document is what lets a reader confirm, months later, which limits a day's
runs were checked against, since the files on disk may have changed since. A console restart
between two runs, which belongs to neither, is recorded here. Later releases add `note` lines, for
a note typed at the window with nothing running, and `guard` lines, for a change to the beam guard
with nothing running.

The day record is kept in segments in the same way as the audit log, so that a folder once listed
never changes. `day-record.jsonl` is the live segment, the only one written to. The two calls that
seal the audit log, `seal_calls` and `stand_down`, seal the day record too, and answer its
segment under `sealed_day`: the live file is renamed `day-record.<YYYYMMDD-HHMMSS>.jsonl`, with
`-2`, `-3` and so on added if that name is taken, and nothing writes to a sealed segment again. The daemon's next line starts a new live file,
which opens with the sitting again, so every segment can be read on its own. A reader reads every
`day-record*.jsonl` in the folder, in the order of each file's first line, with the live file last.

## What the record does not hold

The audit log, `mcp-calls.log`, is a line per tool call and nothing else: a run started at the
window leaves no line in it, and its record is the run record above. The wire transcript and the
send log beside each file hold every string sent to a box and every command sent to the console.
A request's words in its run record are the only copy of them beside the data, so a run record
is kept, and published, with its files.
