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
| `window` | a person pressing Acquire, or starting a series plan or the run queue, at the window | the daemon, when it starts the job |

Records written before clockwork 1.2.0 say `agent` for both `mcp` and `cli`, without saying which;
the audit log's `via` tells them apart. A request continued under its identifier from another
route keeps the source its record was begun under.

Before clockwork 1.2.0 a run started at the window left no run record, and a `verdict` on one of
its files began a record for it with `window` as the source. That still happens for a file
acquired before 1.2.0. From 1.2.0 every run started at the window has a record of its own from the
moment its job starts, written by the daemon that acquires it, so a window run's record is whole
however the window ends. One press of Acquire is one request and one record, whatever its replicate
count; a press of Replicate is another, and so is each Start of a series plan, however many points
it has, and so is each Start of the run queue, however many rows it has.

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
| `interventions` | A person's pauses and resumes of a running job, the beam guard's pauses, every change to the beam guard made while the job ran, and every stop of one; present only once there is an entry |
| `retakes` | Each series point the beam guard ended and a person's resume acquired again, with the files the retake replaced; present only once there is an entry |

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

A run queue started at the window has a `plans` entry with the same keys where a queue has the
same thing to say, and its own where it has not. `text` is the purpose line, `queue` holds the
queue's text as Start read it, in the [`.queue.toml`](user-guide.md#the-run-queue) form, `path`
the queue file it was last saved to or opened from, and `sha256` the hash of `queue`. `rows`
stands where a plan has `points`: each row's `index`, its `name` as the window shows it (with its
pass, as `(pass 2/3)`, for a randomized queue), `method` (the document's name), `path`, `sha256`
(the SHA-256 of the document's text as read at Start, Windows line endings made Unix ones),
`method_hash` (the hash its files stamp), `conditions`, `replicates`, and `template`, the
template's `{hash, knobs, labels}` for a document rendered from one, or null. `seed` and
`shuffled` are the randomization's, `order` is the indices in the order acquired, and `follows`
is the identifier of the queue whose unrun rows this one runs, or null. `template`,
`template_hash`, `minutes`, `checked` and `outside_limits` are null, since a queue has no template
and no check. Its `acquisitions` entry has `series` and `queue` both `true`, `points`, the number of
rows, and `replicates`, the number of files asked for.

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
A file that was paused between two of its repetitions also has `holds`, one entry per pause:
`after` (the `method_frame` and `repetition` it followed), `began`, `seconds` and `ended_by`,
which is `resumed` or `stopped`. A file that was never paused has no such key.

`notes` holds free text: `{text, by}`. `by` is `session` for a note an agent added with the `note`
tool, and `server` for one the daemon or a client wrote, such as the sentence that says a job
failed, where in a series it failed and why, or that a series stopped short of its plan. The
note for a series that stopped short counts points and files apart, because a retake's failed
file and the stopped point's file are files but not complete points. It gives the complete points
of those planned, the point that ended the series with its file and the reason, the planned
points never acquired with an end reference named as such, the files written, and the files a
retake replaced:

```
series job 10 stopped short, 17 of 19 points complete, point 18 of 19 (planned 12,
261119_ZZ_027): 3 frames in a row failed, ... Never acquired: planned 19 (the end reference).
19 files written, 1 of them failed and replaced by a retake (261119_ZZ_016).
```

A note a person typed at the window while the job ran has that person's initials as `by`, and
also carries `client`, the window it was typed at.

`routines` holds each routine's result: `routine` (its name), `hash`, `path`, `verdict`, `reason`,
`criteria`, `values`, `files`, `job` and `text`, the report as the routine words it.

`verdicts` holds each person's judgement of one run: `stem`, `verdict`, `words` and `by`, the
person's initials. A verdict is one of `worked`, `no_signal`, `saturated`, `wrong_sample` and
`other`, and `other` carries the person's `words`. A later verdict on the same stem supersedes the
earlier, which is kept. A verdict is distinct from a run's outcome, which the file stamps and which
says how the acquisition ended (`completed`, `stopped`, `failed` or `incomplete`).

`interventions` holds a person's acts on a job already running, each in the record of the job
acted on, whatever client started that job, and every stop of an acquisition, whichever client
sent it. The daemon writes each entry as the act is made:

| Key | What it holds |
|---|---|
| `kind` | What was done: `pause`, `resume`, `stop` or `guard`, the last a change to the beam guard |
| `grain` | For a pause and for its resume, where the job rests: `point` (after the point in flight) or `repetition` (after the repetition in flight); empty otherwise |
| `by` | The initials of the person who acted, `the beam guard` for the pause the guard set when it ended a file, or the client's name where no person did |
| `client` | The client the act came through, as the daemon names it: `the clockwork window (pid 5120)`, or `Claude, through clockwork mcp` for an agent's `stop` |
| `job` | The daemon's job number |
| `reason` | The words given with a stop, or for the beam guard's pause its sentence, or empty |
| `held_since`, `held_s`, `where` | A resume only: when the series came to rest, how many seconds it rested, and where in words. For a pause withdrawn before the series reached it, `held_s` is 0 and the other two are empty |
| `while_paused` | A stop only: `true` when it ended a paused series, and absent otherwise |
| `armed`, `floor`, `repetitions`, `source`, `was` | A `guard` entry only: whether the guard is now armed, its floor in ions a repetition, how many repetitions in a row under it end a file and where the floor came from, each null or empty once it is turned off, and `was`, the guard before the change as `{floor, repetitions, source}`, or null |

`retakes` holds one entry per point acquired again after the beam guard ended one of its files and
a person resumed the series: `index` (the planned index, which the new files stamp as the old ones
did), `number` (the point's place among the series' points as acquired), `retake` (1 for the
first time the point was acquired again, 2 for the second), `job`, `replaces` (the stems of the
files the guard ended, which stay on disk and in `files`, stamped `failed`), `beam_lost` (the
guard's sentence) and `first_place`, the position the retake's first file takes. A reader that
keeps one set of files per planned index keeps the files whose stems no `replaces` names.

A `pause` entry records the moment the pause was asked for, and the `resume` or `stop` that
follows it records how the pause ended. A pause that the series never reached, because the series
ended first, leaves a note by `server` saying so. A record with no intervention has no
`interventions` key, so a record of a run nobody paused or stopped is unchanged by this section.

## The day record

`day-record.jsonl`, in the daemon's output directory, appended one JSON object per line, UTF-8,
LF. The daemon writes it, and it holds what belongs to the day rather than to any request. Every
line has `time` and `kind`, and `by` where a person or a client caused it:

| `kind` | When | What else the line holds |
|---|---|---|
| `sitting` | First in every segment of the day record | `version`, `session`, `pid`, `fake`, `output`, `library`, and `instrument`, `limits` and `project_limits`, each `{path, sha256}` of the document in force, or null for none |
| `daemon` | The daemon started or stopped | `event` (`started` or `stopped`), `session`, `reason` |
| `console` | The acquisition console started, restarted or stopped | `event` (`started`, `restarted`, `stopped` or `failed`), `job`, `by` (the client whose job it was, or the daemon itself), `detail` |
| `note` | A person typed a note at the window with no job running, or while a job with no run record ran | `text`, `by` (the person's initials), `client` (the window), `job` (the job running, or null) |
| `guard` | A person armed, changed or turned off the beam guard with no job running, or while a job with no run record ran | `by`, `client`, `job`, and as a `guard` intervention, `armed`, `floor`, `repetitions`, `source` and `was` |

The SHA-256 of each document is what lets a reader confirm, months later, which limits a day's
runs were checked against, since the files on disk may have changed since. A console restart
between two runs, which belongs to neither, is recorded here, and so are a note typed and a
change to the beam guard made with nothing running.

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
