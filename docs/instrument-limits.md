# The instrument's standing limits

The standing limits bound what an agent may send to one instrument through
[`clockwork mcp`](mcp-server.md). They are a flat [TOML](https://toml.io) document, `limits.toml`,
kept beside the [instrument file](instrument-file-format.md) and written once by whoever is
responsible for the instrument. Their first job is physical: no method, rendered from a
[method template](template-file-format.md) or written by hand, may put two electrodes far enough
apart in potential to break down across the gap between them, or drive an RF head past what the
instrument has been run at. Their second is to say which templates an agent may run, how far each
knob may be turned and which knobs stay where they are, and which boxes an agent may address. A
research project may add a file of its own, given beside the standing one, that admits the
project's own templates and narrows the standing entries but never widens them. Before `arm` or
`acquire` puts anything on the wire, the server checks the method against the limits and against
what the boxes currently hold. A method outside either is refused, in one sentence, before any
string is sent.

Nothing here asks for approval, and nothing counts what is done. Starting `clockwork serve` at a
terminal is the authorization: the daemon holds the instrument lock, nothing reaches a box
without it, and it runs until the person who started it stops it.

## Why a separate document

The limits could have been a table in the instrument file. They are kept apart because the two
documents answer to different readers. The instrument file describes what an acquired file
states about the machine, is read by every acquisition the window or the daemon makes, and
rejects any key it does not know. The limits are an authorization that only the agent surface
reads, change when the instrument or its templates change, and would stop the window from
acquiring if a mistake in them were a mistake in the instrument file.

## Document

```toml
schema_version = 2
description = "Standing limits for agent-driven runs on the instrument."
cold_start = "refuse"

[allow]
boxes = ["box1", "box2", "box3"]

[caps]
amplitude_v = { max = 70.0 }

[templates.clock]
hash = "3fa2c1d49b07"
name = "clock-activation"

[templates.clock.knobs]
duration_ms = { min = 50.0, max = 300.0 }
amplitude_v = { min = 20.0, max = 45.0 }

[templates.clock.fixed]
bias_hold_v = 10.0

[electrodes]
max_v = 150.0
nets = [
  { name = "GUARD", dc = { box = "box1", channel = 16 } },
  { name = "RF1", volts = 0.0, rf = { box = "box1", channel = 1 }, rf_peak_v_per_pct = 2.0 },
  { name = "TW 1", dc = { box = "box1", channel = 15 }, arb = { box = "box2", module = 1 } },
  { name = "TW 2", dc = { box = "box1", channel = 15 }, arb = { box = "box2", module = 2 } },
]

[[rf_drive]]
box = "box1"
channel = 1
max_pct = 50.0
```

- `schema_version` pins the document to the shape this section describes, which is 2. A
  document at version 1, the shape that carried a `[budget]` and `allow.hand_written`, is refused
  with what to change.
- `description` is free text, shown by `status`.
- `cold_start` is the rule for the cold-start check below: `refuse`, the default, `strict` or
  `caution`. A template entry may carry its own.
- `allow.boxes` lists the boxes, by the names a method uses, that an agent may address. A method
  that names any other box is refused.
- `caps` bounds a knob by its name in every template that has it, with `min`, `max` or both. An
  entry, standing or a project's, whose bound or fixed value passes a cap is an error in the
  limits, and a render whose value passes a cap is refused wherever its entry leaves the knob
  open. A cap is a cheap check on the knobs templates share, such as an amplitude in volts; the
  electrodes below are the check on the potentials themselves.
- `templates.<key>` is one template an agent may run. The key is a free name for the entry.
  `hash` is at least 8 hex digits of the template's hash, as `list_templates` shows it, and a
  template whose hash does not begin with these digits does not match the entry. Editing a
  template therefore takes it out of the limits until its entry is updated, which is deliberate:
  the limits were written against the strings as they stood. `name`, if given, must equal the
  template's `metadata.name`, a check that the hash was copied from the right template.
- `templates.<key>.knobs` narrows a knob's range with `min`, `max` or both. A bound outside the
  template's own range is an error in the limits, because limits narrow a range and never widen
  it. A knob the entry does not name keeps the template's own range. Where a template makes its
  repetition count a knob ([template file](template-file-format.md#holes)), the entry bounds it
  as any other, so `repetitions = { min = 10, max = 100 }` lets a series run fewer repetitions
  than the default and none more.
- `templates.<key>.fixed` holds a knob at one value, which must lie within the template's range.
  A render at any other value is refused.
- `electrodes`, optional, names the nets that share the instrument's ion-optical volume and the
  largest potential difference allowed between any two of them, `max_v`, in volts. Each net has
  a `name` and one base, a DC bias channel (`dc`, by box and channel) or a fixed potential
  (`volts`, 0 for ground). At most one wave rides on the base: an ARB module's travelling wave
  (`arb`, by box and module) or an RF head's (`rf`, by box and channel, with
  `rf_peak_v_per_pct`, the head's peak volts per percent of drive). [The electrodes](#the-electrodes)
  below says how the check uses them.
- `rf_drive`, optional, caps the drive of each RF head it names at `max_pct`, in percent of full
  scale. Where the list is given, a method may not set a drive on a head it does not name, and
  may not put a named head in `AUTO`, where the box chooses the drive. The caps are the way to
  bound RF until a head's peak volts per percent is known well enough to give the head a net in
  `electrodes`; set them at the drives the instrument has run at. Write them as `[[rf_drive]]`
  blocks, which TOML accepts after any other table.

**A hand-written method is admitted where both `electrodes` and `rf_drive` are given.** It has no
knobs for an entry to narrow, so what bounds it is the check on the potentials its strings set.
Where the standing limits carry both tables, an agent may send a hand-written method to the boxes
`allow.boxes` lists, held to the electrodes, the RF caps and the cold-start check below. Where
either table is missing, a hand-written method is refused against the instrument and run from the
window instead.

`clockwork.envelope.load()` collects every problem in a document before raising, so a document
with several mistakes reports all of them at once, and `clockwork mcp` exits with those problems
rather than run without the limits it was pointed at. An entry that does not fit its template, a
bound on a knob the template lacks or a range wider than its own, refuses that template with the
reason, and `status` lists every such problem across the library.

## A project's limits

A project with templates of its own keeps a limits file of its own, in its repository, and the
standing document is not edited for it. The file is the same TOML with three keys only:

```toml
schema_version = 2
description = "Limits for the cold-hold project's sittings."

[templates.cold-hold]
hash = "9be0417c22d1"
name = "tandem-clock-crimp-cold"

[templates.cold-hold.knobs]
hold_ms = { min = 100.0, max = 600000.0 }

[templates.clock-narrower]
hash = "3fa2c1d49b07"

[templates.clock-narrower.knobs]
amplitude_v = { max = 40.0 }
```

An entry whose hash matches a standing entry narrows it. Its bounds must lie inside the standing
ones, a side it leaves open keeps the standing bound, a knob the standing entry fixes stays fixed
at the same value, and a knob the standing entry bounds may be fixed at a value inside that range.
An entry for any other template admits that template. Either way the standing `caps` hold, and an
entry's `cold_start` may be stricter than the standing rule but not looser. Everything else is
the instrument's: a project file that carries `allow`, `caps`, `cold_start`, `electrodes` or
`rf_drive` is refused, as is a file with any entry that would widen a standing one, and in either
case the server does not start. The two files are read together with `--project-limits PATH`, next to `--limits` or the
standing document found beside the instrument file; a project file with no standing limits to
narrow is refused. A refusal names whose limits refused, the standing or the project's.

## Where the document lives

`clockwork mcp --limits PATH` names the document. Without the flag, the server looks for
`limits.toml` beside the document given to `--instrument`, which is by default the instrument
directory's own ([MCP server](mcp-server.md#the-instrument-directory)), so an instrument keeps its
limits beside its document at the directory's root. `--project-limits PATH` adds a project's, and
has no default. The [command line](command-line.md)'s verbs take both flags. Against the
instrument, a server with no limits refuses every send, and says so under `status`. Under `--fake`
a server with no limits refuses nothing, so that the tools can be exercised on a machine with no
instrument, while a rehearsal given limits is held to them exactly as the instrument would be.

## What is checked

Before `arm` or `acquire` submits a job, the server refuses:

1. a request with no words, i.e., a send that cannot say what it is for;
2. against the instrument, a hand-written method where the standing limits do not carry both
   `electrodes` and `rf_drive`;
3. a template neither the standing limits nor the project's list;
4. a knob outside its narrowed range or past a cap, and a fixed knob at any value but its own;
5. a method that addresses a box outside `allow.boxes`;
6. a method, rendered or hand-written, that would put two nets of `electrodes` more than `max_v`
   apart at any moment, or whose table the check cannot read, and one that would drive an RF
   head past its `rf_drive` cap, drive a head the caps do not name, or put a capped head in
   `AUTO`.

`render_template` and `validate_method` report items 3 to 6 under `limits` rather than clamping a
value into range, so an agent learns why a choice would be refused before it tries to send it. A
refused `arm` or `acquire` is a failed call, and the audit log in the output directory records
the refusal word for word as its error. `status` reports how many acquisitions the daemon session
has made, by surface, under `session`: `mcp` for the MCP server and `cli` for the command line,
so a `clockwork routine` typed at the instrument PC during an agent's series is told apart. The
count bounds nothing.

## The electrodes

The one irreversible harm a method can do through the boxes is electrical: two electrodes far
enough apart in potential to break down across the gap between them at the operating pressure.
Knob ranges bound only the knobs a template exposes, so the electrode check reads the method
itself, the setpoints its strings would send, whether it was rendered from a template or written
by hand. Every pair of nets counts as neighbours, under one maximum.

A net's potential is its base plus whatever rides on it. An ARB module adds plus or minus half its
peak-to-peak range, taken at the widest range the method sets anywhere: `SWFVRNG`, `SALTRNG`
where `SALTRENA` enables it, and the `V`, `v`, `L` and `l` levels of a compression table, with
modules 1 and 2 sharing their levels under `M1` or `M2`. Its offsets, `SWFVOFF`, `SARBOFFA`,
`SARBOFFB` and table channels 105 to 108, count at every value the method gives them, and as 0 V
where it gives none. An RF head adds plus or minus `rf_peak_v_per_pct` times the highest drive the
method sets, through `[boxes.rf.<n>]`, `SRFDRV` or table channels 33 to 36.

The DC bias channels are where timing matters, so the check follows them moment by moment. Each
setter a box is sent makes a moment, and so does each table event that moves a channel, with every
loop run twice; a third pass sets nothing a second did not. A one-tick pulse therefore counts as
much as a ten-second hold. At each moment the highest potential any net reaches is compared with
the lowest any net reaches, and a method whose widest moment exceeds `max_v` is refused in one
sentence that names the two nets, their potentials and the moment, for example

> GUARD reaches 120 V and TW 1 2.5 V at tick 806 of box1's table, 117.5 V apart, more than the
> 60 V the standing limits allow between any two electrodes

A net whose DC channel, ARB range or RF drive the method never sets is left out, since what the
box holds there is the [cold-start check](#the-cold-start-check)'s to find. A table that will not
compile, or that ramps a DC channel with `STBLVDLT`, is refused, because a difference that cannot
be computed cannot be shown to be within the limit.

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
phase. A `series` reads the boxes back once and compares every distinct method in its plan before
anything is sent, and the daemon then compares what each of its sends read back as `acquire`
does, ending the series before a point it would refuse. Each comparison produces findings:

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
identifier; the plan the agent stated when it armed, and for a series the plan's text, its
seed and the order drawn from it; each arming, with the template's hash, every
knob value and label, and the cold-start cautions; each acquisition; each file written, with a
summary of its frames, counts, base peak, pusher period and saturation; the notes the agent
added with the `note` tool; and each person's verdict on a run given with the `verdict` tool, in
their words and under their initials. A request continued in a later session by its identifier finds its
record by that identifier and appends to it. A run started from the window records `window` as its
source in the same document.

The file remains the primary record. Every file stamps its request's identifier, and the run
record is what joins a request's files and says why each was made.
