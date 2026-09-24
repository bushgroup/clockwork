# The command line: `clockwork <verb>`

Every tool the [MCP server](mcp-server.md) offers an agent is also a subcommand of `clockwork`,
so a person at a terminal, a script or a continuous-integration job can drive the instrument with
no MCP client at all. `clockwork status` answers what the `status` tool answers, `clockwork arm`
sends what the `arm` tool sends, and so on for all 18 tools. Each verb calls the same function as
its tool, through the same owner of the hardware, so it is refused in exactly the places the tool
is refused: the interlock, the [standing limits](instrument-limits.md) and the cold-start check
all live inside the tools, and a verb cannot reach a box around them.

## Starting a session

The verbs are clients of `clockwork serve` ([daemon protocol](daemon-protocol.md)), which owns the
boxes and the acquisition console. To work at a terminal, start the daemon in one window and type
verbs in another:

```
clockwork serve
clockwork status
```

A verb that finds no daemon exits with one sentence saying so. Each verb is a process of its own,
and everything that must outlast one verb is kept by the daemon or on disk: which method the boxes
were last sent, each job and its progress, the audit log and the run record. `arm` in one command
and `acquire` in the next therefore behave exactly as the two tools do in one MCP session.

To rehearse with no instrument, start `clockwork serve --fake` instead. Unlike `clockwork mcp`,
a verb has no `--fake` of its own, since a simulated rack that lived only as long as one verb could
hold nothing for the next. Nothing a `--fake` daemon reports is evidence about a MIPS box or a
digitizer, and `status` says `fake: true` throughout.

From a source checkout, run each verb as `uv run clockwork <verb>`.

## Options every verb takes

| Option | Meaning |
|---|---|
| `--endpoint ADDRESS` | The daemon's command socket, `tcp://127.0.0.1:5570` by default |
| `--library DIR` | The method and template library, by default the daemon's `--library` |
| `--output DIR` | Where runs are written and read back, by default the daemon's `--output` |
| `--instrument PATH` | The [instrument file](instrument-file-format.md) runs are acquired under |
| `--limits PATH` | The standing limits, by default the `limits.toml` beside `--instrument` |

`--instrument` and `--limits` matter to `arm` and `acquire` and are accepted by every verb, so a
shell alias or a script can pass them once for a whole session. Against the instrument, a verb
given no limits is refused every send, as the MCP server is.

## Flags

Each argument of a tool is a flag of the same name with hyphens for underscores: the tool's
`request_id` is `--request-id`, and the tool's `wait_s` is `--wait-s`. A flag left out takes the
tool's own default. `clockwork <verb> --help` prints the tool's description and every flag.

| The tool takes | The flag is written |
|---|---|
| text, a whole number or a number | `--template bradykinin-clock/template.toml`, `--replicates 3` |
| true or false | `--setup` or `--no-setup` |
| a list | `--frames 1 2 3` |
| a mapping | `--knobs duration_ms=100 --knobs amplitude_v=30`, repeated, or one JSON object |
| a name or a mapping | `--windows bradykinin`, or `--windows precursor=[530.2,531.4]` repeated |
| a name or a list | `--mz [530.2,531.4]`, or a window name |

A mapping is written as `KEY=VALUE` pairs first because Windows PowerShell 5.1 removes the double
quotes from a JSON argument before a program receives it, so `{"duration_ms": 100}` arrives as
text that is not JSON. A value is read as a number or a list where the tool wants one and as text
where it wants text, so `--labels "sample=bradykinin, 1 uM"` carries only the quotes the shell
itself needs for the spaces.

## What comes back

Standard output carries JSON and nothing else: the tool's answer, indented, exactly as the MCP
server returns it to an agent. A verb therefore composes with `ConvertFrom-Json` in PowerShell or
with `jq`. Anything a person watching needs, such as the events of a run in progress, goes to
standard error.

| Exit status | Meaning |
|---|---|
| 0 | The tool answered. An answer that reports problems as data, such as `validate-method` with `ok: false`, is still 0 |
| 1 | The tool refused, or the job it waited for failed: one sentence on standard error, nothing on standard output |
| 2 | The command line itself was wrong: a missing flag, or a value that is not a number |
| 130 | Ctrl-C while waiting on a run. The run carries on, and `clockwork stop` ends it |

Every call is written to the audit log in the output directory, as the MCP server's calls are,
and the line records `via: cli`, so a person reading the log can tell a shell's calls from an
agent's.

## Following a run

To acquire from a shell, run `acquire` and let it finish. It prints its answer, the job number,
the request id and the run record's path, at once, then stays until the run ends, writing each
event's line to standard error and entering each file in the run record as it is written. It
exits 0 when the run is done and 1 if the job failed.

`acquire --no-wait` answers at once instead, for a script that starts an acquisition and follows
it separately. `progress --job N --follow` follows a job from any process, the one that started
it or not: it writes one compact JSON line per event as the events arrive and a last line with
the answer, which lists the files the run wrote under `runs`. A follower also enters each file it
sees in the run record, so a run started with `--no-wait` and followed to its end leaves the same
record as one that waited. Note that a run started with `--no-wait` and never followed has files
that stamp their request but a run record that lists none of them. `progress --job N` without
`--follow` answers once, after waiting up to `--wait-s` seconds for news, exactly as the tool
does.

## A request from a shell

```
clockwork discover-boxes --template bradykinin-clock/template.toml --labels sample=bradykinin
clockwork render-template --template bradykinin-clock/template.toml --labels sample=bradykinin --knobs duration_ms=100
clockwork arm --template bradykinin-clock/template.toml --labels sample=bradykinin --knobs duration_ms=100 --request "Run bradykinin once with a 100 ms hold" --initials AB --plan "one acquisition at 100 ms, the other knobs at their defaults"
clockwork acquire --template bradykinin-clock/template.toml --labels sample=bradykinin --knobs duration_ms=100 --request "Run bradykinin once with a 100 ms hold" --initials AB
clockwork list-files
clockwork summarize-file --path 260924_AB_001.summed.uimf
```

`acquire` names the same template, knobs and labels as `arm`, and is refused unless the boxes are
holding exactly the method they render. The same words in `--request` continue the same request
within one daemon session, so both commands above serve one request and its files share one
series; `--request-id` continues a request from an earlier session.

## The verbs

Each verb is its tool, described in full by `clockwork <verb> --help` and in the
[MCP server](mcp-server.md) document.

**Methods and templates**

- `list-templates`: every template in the library, with its knobs, labels and marks, and what the standing limits allow of each.
- `list-methods`: every hand-written method in the library.
- `load-method`: one method's canonical text and fields.
- `diff-methods`: two methods compared field by field and line by line.
- `validate-method`: whether a method, or a template at some knob values, can be acquired.
- `render-template`: a template rendered at some knob values, the method that would be sent.

**The boxes**

- `discover-boxes`: which MIPS boxes answer, on which port.
- `read-box-state`: every box's persistent settings, read back with getters only.
- `arm`: send a method to every box and leave them armed for `acquire`.

**Acquisition**

- `acquire`: acquire the armed method, one file per replicate, and wait for the run unless `--no-wait`.
- `progress`: what a job has done since an event number, or with `--follow` until it ends.
- `stop`: end the acquisition in flight after its current repetition and its fold.
- `status`: the daemon, the console, the boxes, the job running, what was last armed, the limits and the budget.
- `note`: add a note to a request's run record.

**Reading the files back**

- `list-files`: the runs in the output directory, newest first, with the request each served.
- `summarize-file`: what one UIMF file holds, as numbers.
- `windowed-intensities`: summed intensity in named m/z windows, and ratios to a reference.
- `arrival-time-distribution`: intensity against scan in one m/z window, with its peak.

The verbs are generated from the server's tool registry when `clockwork` starts, so a tool added
to the server is a verb of the command line the same day.
