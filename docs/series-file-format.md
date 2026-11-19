# The series plan

A series plan is a list of experiments to be acquired as one: the points of a grid over a
template's knobs, or a list of points whose knobs move together, each acquired the same number of
times, with the template's defaults as references at the start, the end or both. Where the
template makes its repetition count a knob, the plan may set the count point by point. It exists because
a series is acquired in shuffled order, so that drift in the source or the instrument is spread
across the knob values rather than confounded with them, and a shuffled series can only be read
back if every file says where it sat in the plan and what shuffled it. Clockwork stores a plan as a
small [TOML](https://toml.io) document, reads it with `clockwork.series`, and acquires it with the
`series` tool of the [MCP server](mcp-server.md), `clockwork series PLAN` at the
[command line](command-line.md), or Start in the window's Queue tab, where a person opens the
plan, reads its check and starts it ([at the window](#at-the-window)). All three routes check the
plan with one function and submit the same job.

## A grid

```toml
plan_schema = 1
description = "Guard amplitude against hold duration, two files a point, defaults either side"

template = "bradykinin-clock/template.toml"
labels = { sample = "bradykinin" }
replicates = 2
references = ["start", "end"]

[grid]
amplitude_v = [30, 45]
duration_ms = [100, 200, 300]
```

This plan is eight points: the start reference, the six points of the grid, and the end
reference. At two files a point it is 16 files in eight acquisitions, one for each point, as
eight `acquire` calls of two replicates each would make.

## Coupled points

```toml
plan_schema = 1
description = "Longer holds at lower amplitudes, the kinetics slowing as the ions cool"

template = "bradykinin-clock/template.toml"
labels = { sample = "bradykinin" }
references = ["start", "end"]
seed = 1804289383

[[points]]
amplitude_v = 45
duration_ms = 100

[[points]]
amplitude_v = 37
duration_ms = 200

[[points]]
amplitude_v = 30
duration_ms = 300
```

Each `[[points]]` table is one point, with the knobs it sets. A knob a point does not name takes
the template's default, so an empty table is the defaults. This plan is five points of one file
each, and the seed makes its order the same every time it is acquired.

## Repetitions per point

How many repetitions a point acquires is a knob of the template, not a key of the plan. A
template that lets a series set the count writes `accumulations = "{repetitions}"` in its
`[acquisition]` table and declares a `repetitions` knob
([template file](template-file-format.md#holes)). A plan then sets the count as it sets any other
knob: in `[grid]`, to acquire every point of the grid at each count, or in `[[points]]`, point by
point, where a point that does not name it takes the template's default.

```toml
[[points]]
amplitude_v = 37
duration_ms = 200

[[points]]
amplitude_v = 37
duration_ms = 10000
repetitions = 20
```

The first point acquires the template's default count of a 200 ms hold and the second 20
repetitions of a 10 s hold, a fifth of the time the default of 100 would take. Because the count
is a knob, it is checked against the template's range and the standing limits with the rest,
stamped in every file as `ClockworkKnobRepetitions`, and rendered again with the other knobs
whenever a file's provenance is checked, so a file acquired at 20 repetitions reproduces as one
acquired at the default does. A template without the knob acquires every point at its own count.
Two points that differ only in their count put the same strings on the wire, so the second is
acquired without a send.

## Keys

| Key | Required | Meaning |
|---|---|---|
| `plan_schema` | yes | The version of this format; this document describes 1 |
| `template` | yes | The template's path in the method library, as `list_templates` gives it |
| `labels` | as the template requires | The template's labels, such as the sample, the same for every point |
| `replicates` | no, 1 | Files per point: 2 is a run and one technical replicate of it |
| `references` | no, none | `["start"]`, `["end"]` or `["start", "end"]`: a point at the template's defaults before the others, after them, or both |
| `[grid]` | one of the two | A list of values for each knob, of which the plan takes every combination |
| `[[points]]` | one of the two | One table per point, each with the knob values it sets |
| `shuffle` | no, true | Whether the points between the references are acquired in shuffled order |
| `seed` | no | The seed of the shuffle, from 0 to 2147483647; drawn at random when not given |
| `description` | no | What the series is for, in a sentence, kept in the run record |

A key not in this table is refused, and so is a plan with both `[grid]` and `[[points]]` or
neither, a knob the template does not have, a grid list that is empty or repeats a value, and a
seed in a plan with `shuffle = false`, since there is then no shuffle for it to order.

## The planned order

The points are numbered from 1 in the order the plan writes them, i.e., the planned index: the
start reference first, then the grid or the points, then the end reference. A grid takes its
knobs in the order the plan lists them, with the last knob varying fastest, so the grid above
is (30, 100), (30, 200), (30, 300), (45, 100), (45, 200), (45, 300) as indices 2 to 7. A point
written twice in `[[points]]` is two points, each with an index of its own.

## The order acquired

With `shuffle` true, the points between the references are acquired in an order drawn from the
seed, and the references stay where they are, so the defaults open and close the series whatever
the draw. The shuffle is Python's `random.Random(seed).shuffle` over the planned order, so a
plan acquired twice under one seed is acquired in one order. A plan that gives no seed has one
drawn for it, which the tool answers before the first file exists and which every file records.
With `shuffle` false the points are acquired in planned order and no seed is drawn.

The replicates of a point are acquired one after another, since a replicate is the same method
again and needs nothing sent to the boxes. A point whose knob values are those of the point
acquired before it is acquired without sending anything either. Every other point is sent to the
boxes first, with its `setup` phase when that phase differs from the one last sent and with its
table and mode change alone when it does not.

## Before anything is sent

A series is refused whole or acquired, never cut off by a refusal part of the way through. Every
point is rendered and checked against the template's ranges, the acquisition loop's refusals and
the standing limits before the first string goes to a box, and a plan with one point outside them
is refused naming that point. The boxes are then
read back once, and every distinct method in the plan is compared with what they hold, as `arm`
compares one ([MCP server](mcp-server.md#the-interlock)).

The tool also says how long the series will acquire for, in minutes, for each point and in all.
Each repetition costs its frame, counted at the pusher period the instrument document states or,
where it states none, at the template's `tick_us`, and then the acquisition console's re-arm
before the next frame, which measures 0.337 s on our instrument and is as long as the frame
itself at the shortest holds. The sends and the folds between points are not counted. Where
neither document gives a pusher period, the minutes are left empty rather than guessed. Asked
with `check` true (`clockwork series PLAN --check` at the command line), the tool does all of
the above up to the read-back of the boxes and stops there: it sends nothing, starts no job and
writes no record, and answers each point's knob values, repetitions, files and minutes in
planned order, or the refusal the series would meet. A plan's cost can therefore be seen, and
the plan cut, before anyone is asked to run it.

## At the window

The window's run queue is acquired by the same job: its points are saved method documents rather
than a template at knob values, read and hashed when the person presses Start, and its files stamp
a series identifier, index, position and seed as a plan's do ([user guide](user-guide.md#the-run-queue)).

To run a plan an agent wrote, or one written by hand, a person opens it in the window's Queue tab
with Open plan… and starts it there. The window checks the plan as the `series` tool does with
`check` true and shows the answer as a table: each point in planned order with its knob values,
repetitions, files and minutes, and the totals beneath. It then reads the output directory's
[audit log](mcp-server.md#the-audit-log) for a `series` call made with `check` true whose plan has
the SHA-256 of the plan opened, and says under the table what it found: "checked through mcp at
14:02, unchanged since", or "no check of this text on record". A plan edited by one character
after an agent checked it therefore shows as unchecked, and it still runs.

A person at the window is held to the template's ranges and the acquisition loop's refusals, as
every route is, but not to the agent's limits. A point outside the limits the daemon's sitting
carries is marked in the table with the sentence an agent's refusal would have used, the plan can
still be started, and the run record lists every such point under `outside_limits`; where the
daemon carries no limits, nothing is marked and the record holds null. What the boxes read back
after each point's send is compared with what the point declares, and a difference is said in
the run log but never refuses the point.

Start needs the person's initials in "At the window", and the purpose line beside it is filled
from the plan's `description` until the person types over it. The window draws the seed where
the plan gives none, as the tool does, and submits the series as one job of the daemon with
`source` `window`. The daemon mints the request id and begins the run record, whose `plans` entry
holds what the `series` tool writes for the same plan, together with the plan's hash and the
check it matched ([run record](run-record.md#the-sections)). The files, their stamps and the order
acquired are those the tool leaves for the same plan and seed.

The window follows every series the daemon runs, whichever client started it, in a table beneath
the plan: each point's state, the point in flight with its knob values and which of its files is
being acquired, the minutes left as the sum of the remaining points' planned minutes, and who
started it, i.e., the client and, for a series started at the window, the person. Only a point
that failed, was stopped, was skipped or is held is coloured. Each point counts the warnings the
run log gave while it ran, and the run log can be narrowed to the series' own lines. Stop ends a
series whichever client started it. A person can also pause any series after its point or its
repetition in flight, and only a person at the window resumes it
([daemon protocol](daemon-protocol.md#a-persons-pause)).

## What each file records

Every file of a series stamps the series parameters that the
[template file](template-file-format.md#what-a-rendered-run-records) defines, with the request's
id as the series id:

| Parameter | For a series file |
|---|---|
| `ClockworkSeriesId` | The id of the request the series served |
| `ClockworkSeriesIndex` | The point's planned index; the replicates of one point share it |
| `ClockworkSeriesPosition` | The file's place in the order acquired, counted from 1 across the series |
| `ClockworkSeriesSeed` | The seed, or absent for a series acquired with `shuffle = false` |

The grid above, shuffled, might acquire index 5 at positions 3 and 4, and the manifest's
`series_index`, `series_position` and `series_seed` columns then say so for each file. A series
that continues an earlier request by its id takes its numbers after the request's last file, so
a request's indices and positions never repeat.

A series stopped part way leaves the files it finished, each stamped as above, and the run record
says after which point it stopped and why. The record's `plans` section holds the plan's text and
its SHA-256, the seed, the order drawn, and each point's knob values, repetitions and minutes, so
the plan can be set beside the files it produced and beside the audit log's line for each call
that was given it.

The hash is taken of the plan's text as clockwork reads it, with Windows line endings made
Unix ones, whichever way the plan arrived. A plan passed inline and the same plan saved by a
Windows editor and passed as a path therefore hash alike, whereas any other difference, a
trailing space included, changes the hash.
