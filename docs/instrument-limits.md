# The instrument's standing limits

The standing limits bound what an agent may send to one instrument through
[`clockwork mcp`](mcp-server.md). They are a flat [TOML](https://toml.io) document, `limits.toml`,
kept beside the [instrument file](instrument-file-format.md) and written once by whoever is
responsible for the instrument. For each [method template](template-file-format.md) an agent may
run, the document narrows the ranges its knobs may take and fixes the knobs that should not move.
It also lists the boxes an agent may address and sets a budget for work done without a person
present. Before `arm` or `acquire` puts anything on the wire, the server checks the method
against these limits and against what the boxes currently hold. A method outside either is
refused, in one sentence, before any string is sent.

Nothing here asks for approval. Starting `clockwork serve` at a terminal is the authorization:
the daemon holds the instrument lock, nothing reaches a box without it, and the budget is counted
from the moment it started. A budget that has been spent is renewed by a person restarting the
daemon, and by nothing else.

## Why a separate document

The limits could have been a table in the instrument file. They are kept apart because the two
documents answer to different readers. The instrument file describes what an acquired file
states about the machine, is read by every acquisition the window or the daemon makes, and
rejects any key it does not know. The limits are an authorization that only the agent surface
reads, change when the instrument or its templates change, and would stop the window from
acquiring if a mistake in them were a mistake in the instrument file.

## Document

```toml
schema_version = 1
description = "Standing limits for agent-driven runs on the instrument."
cold_start = "refuse"

[allow]
boxes = ["box1", "box2", "box3"]

[budget]
max_runs = 20
max_replicates_per_run = 5
max_hours = 8

[templates.clock]
hash = "3fa2c1d49b07"
name = "clock-activation"

[templates.clock.knobs]
duration_ms = { min = 50.0, max = 300.0 }
amplitude_v = { min = 20.0, max = 45.0 }

[templates.clock.fixed]
bias_hold_v = 10.0
```

- `schema_version` pins the document to the shape this section describes.
- `description` is free text, shown by `status`.
- `cold_start` is the rule for the cold-start check below: `refuse`, the default, `strict` or
  `caution`. A template entry may carry its own.
- `allow.boxes` lists the boxes, by the names a method uses, that an agent may address. A method
  that names any other box is refused.
- `budget` is what one daemon session may do unattended. `max_runs` counts acquisitions, i.e.,
  `acquire` calls, however many files each makes; `max_replicates_per_run` bounds the files one
  acquisition may ask for; and `max_hours` is the time from the daemon's start after which
  nothing more is sent. All three are required.
- `templates.<key>` is one template an agent may run. The key is a free name for the entry.
  `hash` is at least 8 hex digits of the template's hash, as `list_templates` shows it, and a
  template whose hash does not begin with these digits does not match the entry. Editing a
  template therefore takes it out of the limits until its entry is updated, which is deliberate:
  the limits were written against the strings as they stood. `name`, if given, must equal the
  template's `metadata.name`, a check that the hash was copied from the right template.
- `templates.<key>.knobs` narrows a knob's range with `min`, `max` or both. A bound outside the
  template's own range is an error in the limits, because limits narrow a range and never widen
  it. A knob the entry does not name keeps the template's own range.
- `templates.<key>.fixed` holds a knob at one value, which must lie within the template's range.
  A render at any other value is refused.

`clockwork.envelope.load()` collects every problem in a document before raising, so a document
with several mistakes reports all of them at once, and `clockwork mcp` exits with those problems
rather than run without the limits it was pointed at. An entry that does not fit its template, a
bound on a knob the template lacks or a range wider than its own, refuses that template with the
reason, and `status` lists every such problem across the library.

## Where the document lives

`clockwork mcp --limits PATH` names the document. Without the flag, the server looks for
`limits.toml` beside the document given to `--instrument`. Against the instrument, a server with no
limits refuses every send, and says so under `status`. Under `--fake` a server with no limits
refuses nothing, so that the tools can be exercised on a machine with no instrument, while a
rehearsal given limits is held to them exactly as the instrument would be.

## What is checked

Before `arm` or `acquire` submits a job, the server refuses:

1. a request with no words, i.e., a send that cannot say what it is for;
2. against the instrument, a hand-written method, which has no knobs for the limits to bound
   and is run from the window instead;
3. a template the limits do not list;
4. a knob outside its narrowed range, and a fixed knob at any value but its own;
5. a method that addresses a box outside `allow.boxes`;
6. any send once the session has made `max_runs` acquisitions or `max_hours` have passed since
   the daemon started, and an acquisition asking for more than `max_replicates_per_run` files.

`render_template` and `validate_method` report items 3 to 5 under `limits` rather than clamping a
value into range, so an agent learns why a choice would be refused before it tries to send it.

The budget is counted from the audit log in the output directory: the `acquire` calls it accepted
under the daemon's session identifier. Every server and every restart of a server over the same
daemon therefore spends one budget. Note that two servers writing to different output directories
keep separate logs, and so separate counts.

## The cold-start check

A method template declares what its experiment varies, and it may leave the rest of an
instrument's state to whatever the previous experiment, or a person at a front panel, left
behind. A run started from a cold instrument then acquires under settings nobody chose: one run of
a CLOCK method that declared none of its ion optics acquired with sixteen DC bias channels at
0 V (lab record, task 64). An agent starting experiments for people who do not know the
instrument's recent history cannot rely on that history, so the server compares every template's
declared stack against the boxes themselves.

`arm` reads back every box the method names, using getters only, as `read_box_state` does, before
it sends anything. `acquire` compares against what that `arm`'s send read back after its `setup`
phase. Each comparison produces findings:

| Finding | When | Rule under `refuse` |
|---|---|---|
| A DC bias channel the method does not set | Always, whatever it holds, 0 V included | Refuse |
| An RF head the method does not set | Only when its drive is above 0 % | Refuse |
| An ARB module's frequency or range left as found (`WFREQ`, `WFVRNG`) | Always | Refuse |
| An ARB module's direction, mode or alternate waveform left as found (`WFDIR`, `ARBMODE`, `ALTWFM`, `ALTENA`, `ALTHWD`) | Always | Caution |
| A box the method names that did not answer the read-back | Always | Refuse |
| A declared setpoint or RF setting the box does not hold after the send | `acquire` only | Refuse |
| A DC bias monitor that disagrees with its setpoint | `acquire` only | Caution |

A method sets a DC bias channel by declaring it (`[boxes.dc_bias]`, see the
[method file](method-file-format.md)) or with an `SDCB` or `SDCBALL` string in its `setup`
phase, and an RF head by declaring it or with an `SRF` setter there. A channel that only a table
moves is not set, because between table events it holds whatever it held before. The RF rule
counts only a head that is on, because a box with no RF board fitted still answers two heads at
0 % drive, and a rule that counted those would refuse every run on such a box.

`cold_start` chooses what the findings do. `refuse` refuses the findings the experiment is known
to depend on, i.e., the optics, the RF and the traveling-wave frequency and amplitude, and returns
the rest as cautions. `strict` refuses every finding. `caution` refuses none. Cautions come back
under `cold_start` in the answers of `arm` and `acquire`, for the agent to report, and are written
to the request's run record. A refusal names every finding and sends nothing.

The check is about the boxes. The person at the instrument still owns the sample and the source:
whether the sample is spraying, and what it is, are theirs to confirm, and nothing here reads
either.

## The run record

Every request leaves one small document beside its files, `<stem>.request.json`, where the stem is
that of the request's first file. The server writes it as the request proceeds and nothing edits
it by hand. It holds the request in the words of the person it was for, their initials and its
identifier; the plan the agent stated when it armed; each arming, with the template's hash, every
knob value and label, and the cold-start cautions; each acquisition; each file written, with a
summary of its frames, counts, base peak, pusher period and saturation; and the notes the agent
added with the `note` tool. A request continued in a later session by its identifier finds its
record by that identifier and appends to it. A run started from the window records `window` as its
source in the same document.

The file remains the primary record. Every file stamps its request's identifier, and the run
record is what joins a request's files and says why each was made.
