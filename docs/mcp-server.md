# The MCP server: `clockwork mcp`

`clockwork mcp` offers the instrument to an agent, such as a Claude Code session on the instrument
PC, as a set of typed tools over the Model Context Protocol (MCP). The tools turn a person's
request into an experiment: find a method template and choose its knob values, check the method
that results, send it to the MIPS boxes, acquire, follow the run, and read the files back as
numbers. Each tool is a thin layer over a function the window already uses, reached through the
same owner of the hardware, so an agent drives exactly what a trainee drives and is refused in
the same places. The server itself carries the guardrails: the interlock that decides what is sent
to the boxes, the audit log of every call, and, through the daemon, the rule that one process owns
the instrument.

## Starting it

```
clockwork mcp [--fake] [--library DIR] [--output DIR] [--instrument PATH] [--limits PATH]
              [--endpoint ADDRESS]
```

The server speaks MCP over its standard input and output, so it is started by the MCP client
rather than at a terminal. Without `--fake` it is a client of `clockwork serve`
([daemon protocol](daemon-protocol.md)) at `--endpoint`, `tcp://127.0.0.1:5570` by default, and
exits with one sentence on standard error if no daemon answers. The daemon owns the boxes and the
acquisition console; the MCP server owns nothing, and any number of servers, windows and command
lines can drive one daemon.

`--library` is the directory of method documents and templates the tools list, and `--output` is
where runs are written and where the data tools read by default. Both default to the daemon's own
`--library` and `--output`. `--instrument` is the instrument document runs are acquired under
([instrument file](instrument-file-format.md)); the acquisition loop refuses a document that
states no channel offset, exactly as it does from the window. `--limits` is the instrument's
[standing limits](instrument-limits.md), by default the `limits.toml` beside the instrument
document; limits named and not found, or found and not valid, stop the server with the problems
on standard error.

`--fake` needs no daemon. The server holds simulated boxes and a simulated acquisition console of
its own, starts the console at once, and writes its files to
`%LOCALAPPDATA%\clockwork\fake-runs` unless given `--output`. With no `--instrument` it acquires
under a stand-in document named "simulated instrument". Nothing a `--fake` session reports is
evidence about a MIPS box or a digitizer, and `status` says `fake: true` throughout.

### Connecting Claude Code

To make the tools available to a Claude Code session, put an `.mcp.json` in the directory the
session is started from. For a rehearsal from a checkout of this repository:

```json
{
  "mcpServers": {
    "clockwork": {
      "command": "uv",
      "args": ["run", "clockwork", "mcp", "--fake"]
    }
  }
}
```

For the instrument, start `clockwork serve` at a terminal first, then point the session at it:

```json
{
  "mcpServers": {
    "clockwork": {
      "command": "uv",
      "args": ["run", "clockwork", "mcp", "--instrument", "C:/lab/slimphony.instrument.toml"]
    }
  }
}
```

Claude Code starts a project's servers in the project directory, which is where `uv run` finds
this package. The instrument path above is an example; use the document the window uses.

## The tools

Every tool answers a JSON object, or fails with one sentence meant to be read as it stands, never
a traceback. A path to a method or template is relative to the library, and a path to a file is
relative to the output directory, unless it is absolute.

| Group | Tool | Arguments | Answers |
|---|---|---|---|
| Method | `list_templates` | none | Every template: its knobs (unit, default, range, integer or not, description), labels and marks, and what the standing limits allow of it |
| | `list_methods` | none | Every hand-written method: name, hash, date, description, or the problem that stops it loading |
| | `load_method` | `method` | The method's canonical text and the same as structured fields |
| | `diff_methods` | `a`, `b` | The two compared field by field and line by line, and whether anything a box is sent differs |
| | `validate_method` | `method`, or `template` with `knobs` and `labels` | `problems` (does not load or render), `refusals` (clockwork would not acquire it), `cautions` (strings it cannot read well enough to check), and `ok` |
| | `render_template` | `template`, `knobs`, `labels` | Every knob's value, the derived values, the marks in ms and as expected scans, the method's text and hash, its refusals, and where it lies outside the standing limits |
| Hardware | `discover_boxes` | a method or template under `--fake` | Which boxes answered, on which port, with which firmware |
| | `read_box_state` | `boxes` (optional) | Every box's persistent settings read back, as the window's state panel shows them |
| | `arm` | `request`, `initials`, a method or template, `setup`, `conditions`, `request_id`, `plan` | The request id, the stem the first file takes, what the boxes read back, the cold-start cautions, and the run record's path |
| Acquisition | `acquire` | as `arm` without `setup` and `conditions`, plus `replicates` | A job number, at once; the run proceeds on the owner |
| | `progress` | `job`, `after`, `wait_s` | The job's events since number `after`, how many files are done of how many were asked for, and whether it is done |
| | `stop` | `reason` | Whether a run was in flight; it ends after its current repetition and fold |
| | `status` | none | Simulated or real, the console, the boxes, the running and queued jobs, the method this server last armed, why sends are refused if they are, the standing limits in force and the budget left |
| | `note` | `request_id`, `text` | Adds one note to the request's run record |
| Data | `list_files` | `directory` (optional) | Each run's files with sizes and times, its logs, the request it served, and whether its summed file was written |
| | `summarize_file` | `path`, `frames`, `points`, `texts` | Frames, scans, total counts, total ion current, base peak, calibration, pusher period, saturation, every clockwork stamp, and the request the run served |
| | `windowed_intensities` | `path`, `windows`, `reference`, `scans`, `frames` | Summed intensity in named m/z windows and each window's ratio to a reference |
| | `arrival_time_distribution` | `path`, `mz`, `preset`, `frames`, `points` | Intensity against scan in one m/z window, with the peak in scans and in ms |

A method is named in one of two ways wherever a tool sends or checks one: `method`, a document, or
`template` with `knobs` and `labels`, a template rendered at those values
([template file](template-file-format.md)). Knobs not given take their defaults, and a knob
outside its declared range is refused by the render before anything else happens.

### Arming and acquiring

To acquire, first `arm`, then `acquire` with the same method or template and the same knob values.
`arm` sends every box its `setup`, `load` and `arm` strings, as the window's Send setup does, and
waits for the send to finish; `setup: false` sends only the table and the mode change, as Load and
arm does. `acquire` refuses a method the boxes are not holding, i.e., one that differs in any
string from what the last `arm` put on the wire. The two are separate so that one request arms
once and acquires as often as it needs to, as the window's Replicate button does, and so that a
failed send is reported before any acquisition begins. The `conditions` given to `arm`, free text
about the sample and source, are stamped into every file acquired from that arming, which is why
`acquire` takes none of its own.

`acquire` answers at once with a job number, and `progress` follows it. Pass the `last` number of
one answer as `after` in the next to read only what is new. `progress` waits up to `wait_s`
seconds, 10 by default and never more than 30, and answers early only when something other than
the scan counter happens, so a session following a run makes one call every several seconds
rather than asking continuously; `wait_s: 0` answers at once. The events are the loop's own, each
with a line of text, and the scan counter appears only at its latest value, as it does in the
window's run log. Every answer carries `position`: the files done, the files asked for, and the
scans the frame in progress has published. When the job ends, `done` is true and the answer lists
each file the run wrote under `runs`, with any failed frame and the reason, or says why the job
failed under `failed`.

## Requests

Every `arm` and `acquire` carries `request`, what the experiment is for in the words of the person
it is for, and `initials`, theirs, which name the files (`YYMMDD_INITIALS_NNN`, as the window names
them). The first use of a set of words mints a request id, such as `260923-141502-3fa2c1`, which
`arm` and `acquire` both answer. Using the same words again, or passing `request_id`, continues the
same request, so one request can span several acquisitions and several sessions.

The request travels with every file it produces. Each run stamps the request id as its series,
`ClockworkSeriesId`, and its place in the request, counted from 1 across every acquisition of the
request, as `ClockworkSeriesIndex` ([template file](template-file-format.md)). The words
themselves go into the head of each run's wire transcript and send log, beside the file, and into
the audit log. `list_files` reads the id and place from each file's own stamp and the words from
the audit log. A run rendered from a template also stamps the template, every knob, the labels
and the marks, exactly as a render made in the window does.

## The interlock

Before `arm` or `acquire` submits anything, the server decides whether the method may be sent.
Every send must carry a request, under `--fake` as well. Every send must also lie inside the
instrument's [standing limits](instrument-limits.md): a template they list, at knob values inside
their ranges, on boxes they allow, inside the budget of the daemon session. Against the
instrument, a server with no limits refuses every send and a hand-written method is refused
outright; the window is the surface for those. `status` reports a refusal that applies to every
send under `sends_refused`, and the budget left under `budget`.

`arm` then reads back every box the method names, with getters only, and compares what each box
holds with what the method declares. A setting the method leaves as found is refused or returned
as a caution by the limits' cold-start rule, so a template that leaves a DC bias channel, a live
RF head or an ARB module's frequency or range to whatever the last experiment left is refused
before any string is sent. `acquire` repeats the comparison against what `arm`'s send read back,
which also catches a declared value a box did not take. Cautions come back under `cold_start`,
and an agent reports them to the person the request is for.

The interlock is the server's decision, not the agent's. Every refusal comes before any job that
sends a string reaches the owner, and it is the same sentence whichever client asks. Under
`--fake` with no limits nothing is refused but an empty request, and cold-start findings come
back as cautions, so the whole tool set can be exercised on a machine with no instrument; a
rehearsal given `--limits` is held to them exactly as the instrument would be.

## The run record

Every request leaves `<stem>.request.json` beside its files, named after the request's first
file: the request in the person's words, the `plan` passed to `arm`, each arming with the
template's hash and knob values, each acquisition, each file with a summary of its numbers, and
the notes added with `note`. The server writes it and nothing edits it by hand;
[the standing limits](instrument-limits.md#the-run-record) describe it in full. `list_files` names
each run's record.

## The audit log

Every call is one line of `mcp-calls.log` in the output directory, appended: the time, the tool,
its arguments, the request it served, a summary of what it answered, how long it took, the
error sentence if it failed, and the daemon session it was made in, which is what the standing
budget is counted from. A long text argument, such as a method's text, is recorded as its
SHA-256 and length rather than quoted, and a result is summarised to its numbers and the lengths
of its lists. The files and their logs hold everything else, so a person who was away can read
what an agent did, and why, from the audit log and the files alone.

## What the server does not do

The server draws nothing; mainspring remains the only viewer, and a file being acquired can be
followed live in mainspring as it can from the window. The server does not start or stop the
daemon, change the acquisition console's settings, or send a string that is not part of a method.
It serves one client over standard input and output; an HTTP transport, for a session on another
machine, is a later addition.
