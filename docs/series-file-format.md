# The series plan

A series plan is a list of experiments to be acquired as one: the points of a grid over a
template's knobs, or a list of points whose knobs move together, each acquired the same number of
times, with the template's defaults as references at the start, the end or both. It exists because
a series is acquired in shuffled order, so that drift in the source or the instrument is spread
across the knob values rather than confounded with them, and a shuffled series can only be read
back if every file says where it sat in the plan and what shuffled it. Clockwork stores a plan as a
small [TOML](https://toml.io) document, reads it with `clockwork.series`, and acquires it with the
`series` tool of the [MCP server](mcp-server.md) or `clockwork series PLAN` at the
[command line](command-line.md). The window's run queue is a separate thing and does not read
plans.

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
reference. At two files a point it is 16 files, and it spends eight acquisitions of the standing
budget ([instrument limits](instrument-limits.md)), one for each point, as eight `acquire` calls
of two replicates each would.

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
is refused naming that point. The budget is counted the same way: a series of eight points on a
session with five acquisitions left is refused, not stopped after the fifth. The boxes are then
read back once, and every distinct method in the plan is compared with what they hold, as `arm`
compares one ([MCP server](mcp-server.md#the-interlock)).

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
says after which point it stopped and why. The record's `plans` section holds the plan's text, the
seed and the order drawn, so the plan can be set beside the files it produced.
