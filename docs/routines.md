# Instrument routines

A routine is an experiment an instrument needs run anyway, written down so that nobody has to
choose anything to run it: whether the beam reaches the detector, whether an activation
experiment still behaves as it did, whether the MIPS boxes hold the voltages they should. Each
routine names a [method template](template-file-format.md) at fixed knob values and a number of
files, or a set of documents to read the boxes back against; the summary functions to run on what
it produces; the criteria that turn those numbers into a verdict; and what else its report shows.
A routine is a request with no free parameters, so it is sent through the same `arm` and `acquire`
as any other request ([MCP server](mcp-server.md)): the [standing limits](instrument-limits.md),
the cold-start check, the budget, the audit log and the run record all apply to it unchanged. The
one thing a routine adds is the judgement at the end, which is `pass`, `fail`, or `could not
judge` with the reason.

The format is public; the routines an instrument runs are not part of this repository, because
the numbers they compare against are that instrument's own. A routine is written once by whoever
keeps the instrument, beside the method library, and changed when the instrument changes.

## Where routines are kept

Routines are TOML documents in one directory, by default the `routines` directory beside the
method library: a library at `C:\lab\golden` has its routines in `C:\lab\routines`. The
`--routines` option of `clockwork mcp` and of every verb names another. Each document carries
`routine_schema` at its top, which is what distinguishes a routine from a method or a template in
the same tree; any other document in the directory is ignored.

## Document

A routine that acquires:

```toml
routine_schema = 1

[routine]
name = "beam-check"
description = "Is there beam, and is the single-ion response where it was?"
unattended = false

[acquire]
template = "detection-response/template.toml"
replicates = 1

[[measure]]
name = "events"
function = "ion_events"
file = "raw"

[[criterion]]
name = "beam"
value = "events.events_per_push"
at_least = 0.02
unmet = "no beam"
source = "beam off stores no events at the operating offset"

[[criterion]]
name = "single-ion height"
value = "events.height.p50"
reference = "golden"
golden = 5240
factor = 1.25
source = "median single-ion height at the operating point"

[[criterion]]
name = "single-ion height, last pass"
value = "events.height.p50"
reference = "last-passing"
factor = 1.10

[report]
show = ["events.occupancy", "events.width_bins.mode"]
```

A routine that reads the boxes back and acquires nothing:

```toml
routine_schema = 1

[routine]
name = "stack-audit"
description = "Do the boxes hold the stack the instrument runs on?"
unattended = true

[[audit]]
name = "stack"
document = "detection-response/method.toml"
settings = ["dc_bias", "rf"]

[[criterion]]
name = "stack held"
value = "stack.differences"
at_most = 0
```

Every number in these two examples is illustrative; a real routine states where each of its
numbers came from in `source`.

### `[routine]`

- `name` is what `list_routines` shows and `run_routine` takes. It is unique in the directory.
- `description` says in a sentence what the routine asks. It goes into the request's words.
- `unattended` says whether a session with nobody at the instrument may run the routine. The
  server does not enforce it, since it cannot see who is present; it tells the session that
  runs routines, which does. A routine that only reads the boxes back is safe to run unattended;
  one that sprays sample needs a person who has said the source is on.

### `[acquire]`

- `template` is a path in the method library, or an absolute path. The template must be listed
  in the standing limits by its hash, as any template an agent runs must be, and must declare
  the whole stack the experiment depends on, or the cold-start check refuses it.
- `knobs` and `labels` are fixed values, given as tables; a knob not named takes its default.
- `replicates` is the number of files, from 1. A `pair` criterion needs at least 2.
- `conditions` is free text stamped into every file, beside whatever the person running the
  routine adds.

A routine either acquires or has `[[audit]]` entries, never both.

### `[[measure]]`

Each measure is one summary function run on every file the routine acquires.

- `name` is how criteria and the report refer to its result.
- `function` is one of the summary functions the data tools use: `summarize`, `windowed`,
  `atd` or `ion_events`.
- `file` is `raw` or `summed`, which file of each run's pair it reads; `summed` by default.
  `ion_events` needs single pushes, which every raw file holds.
- `arguments` is a table passed to the function as it stands, for example
  `arguments = { windows = "bradykinin-clock" }` for `windowed`.

### `[[audit]]`

Each audit entry is one document the boxes are read back against, with getters only.

- `name` is how criteria and the report refer to its result.
- `document` is a method or a template in the library. A template is rendered at its defaults,
  or at `knobs` if the entry gives them, and a required label the entry does not give is filled
  with a placeholder, since a label renders nothing.
- `settings` narrows what is compared to some of `dc_bias`, `rf` and `arb`; all three by
  default.

### `[[criterion]]`

Each criterion takes one number and makes one comparison.

- `value` is a dotted path into a result: the measure's or audit's name, then keys, such as
  `events.height.p50` or `ratios.ratios.fragments`. For a routine that acquires several files,
  the value is taken from each.
- `at_least` and `at_most` are bounds of the criterion's own. Every file's value must lie inside
  them.
- `reference` compares the value with another number, within `factor` either way: a value of
  0.8 is within a factor of 1.25 of 1.0, and a value of 0.79 is not. The references are:
  - `golden`, the number given as `golden`;
  - `last-passing`, the mean of the same value in the last run of this routine that passed,
    found in the run records in the output directory. With no earlier pass the criterion is
    skipped, not failed;
  - `pair`, the run's own files against each other: the largest value over the smallest must
    not exceed `factor`;
  - `file`, a reference UIMF file named by `file`, relative to the routine's own directory,
    measured with the same measure and arguments.
- `unmet` is what it means when the criterion is not met: `fail`, the default, or a reason the
  routine could not judge, such as `no beam` or `saturation`.
- `judge = false` makes a comparison that the report shows and the verdict ignores.
- `source` says where the criterion's numbers came from.

### `[report]`

- `show` lists further values, as dotted paths, that the report gives without judging them.

## The verdict

A routine is judged in three steps. First, a criterion that is not met and whose `unmet` names a
reason makes the verdict `could not judge` with that reason: when there is no beam, a pulse height
means nothing. Then a criterion that could not be evaluated at all, because a number is missing
from a result or a reference file is not on disk, makes the verdict `could not judge` with what
was missing, since a routine that could not look must not say it passed. Only then do the
ordinary criteria decide: `fail` if any is not met, `pass` otherwise. A refused `arm` or
`acquire`, an acquisition that failed, and a file that was not acquired whole are all `could not
judge`, with the refusal or the failure as the reason.

## Running one

```
clockwork routine beam-check --initials MB
```

`clockwork routine NAME` is the command-line form of `run_routine` ([command line](command-line.md)),
and `clockwork run-routine --name NAME` is the same verb spelled like every other. It prints each
step on standard error as it happens, then the report as JSON on standard output. An MCP client
calls `run_routine` with `name`, `initials` and optionally `conditions`, and `list_routines` shows
every routine with its criteria in words.

The routine's run is a request of its own, worded `routine NAME: DESCRIPTION (run TIME)`, so every
run mints a new request id. It is armed with a plan that states the template, the knob values, the
number of files and every criterion, and each file stamps the request's id as its series exactly as
any request's files do. Its acquisitions count against the daemon session's budget. A routine that
acquires answers once its files are written and judged, which for a run of several minutes means
the call takes several minutes; `progress` on the job it names follows it meanwhile.

The report answers `verdict`, `reason`, `text` (the report in a few lines, for a person), `criteria`
(each criterion's values, what it was compared with, whether it was met and why), `values` (every
judged and shown number, per file), `files`, `request_id` and `record`. The same report is added to
the request's run record under `routines`, which is where a later run finds its last pass. An audit
adds `audit`, each entry's table of differences, and `read_back`, the boxes as they were read.

## What an audit compares

For each box a document names, an audit reads the box back and compares every setting the
document declares: each DC bias channel against the box's setpoint, to within 0.02 V; each RF
head's frequency to within 1 %, its drive to within 0.05 % and its mode exactly; and each ARB
module setting the document's `setup` strings name, numerically to within 1 % where both sides are
numbers and as text otherwise. The 1 % on an ARB setting is the RF frequency's tolerance, for the
same reason: a box quantises a requested frequency. The result counts `differences`, lists them
as `rows` of box, setting, index, declared value and held value, and names under `unread` any box
the document declares settings for and nobody read. A setting the document does not declare is not
a difference; the cold-start check of the standing limits is what judges those.
