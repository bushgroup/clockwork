# The clockwork daemon: `clockwork serve` and its ZeroMQ protocol

Exactly one process may own the instrument: each MIPS box is one COM port, and the digitizer is
driven by one acquisition console. `clockwork serve` is a process that takes that ownership and
offers it to clients over a loopback socket, so that the window, a command line and an MCP
server can all drive the same boxes and the same console without any of them opening a port.
This document is what a client has to know. The Python client is
`clockwork.owner.remote.RemoteOwner`, which implements the same owner protocol as the in-process
owner the window uses, so a caller written against one runs against the other unchanged.

## Starting it

```
clockwork serve [--fake] [--output DIR] [--library DIR] [--instrument PATH] [--limits PATH]
                [--project-limits PATH] [--console PATH] [--kept DIR] [--errors-log PATH]
                [--detached]
```

The window starts the daemon itself when it opens and none is answering (under The window as a
client), so on a trainee's day nobody types this. To start one by hand, run that command at a
terminal on the instrument PC. In order, it

1. takes the instrument lock (below), and exits with the lock's sentence if another owner holds it;
2. stops an acquisition console left running by an owner that died, if one is answering on the
   console's command port (under Failure modes);
3. binds its two sockets, and exits with one sentence if either port is taken;
4. finds the boxes, as the window does when it opens, and starts the acquisition console, found
   through `--console`, then `$CLOCKWORK_CONSOLE`, then the directory the installer puts it in;
5. serves until Ctrl-C at its terminal or a `shutdown` request from any client.

`--output` is the directory a run's files go to when a job names none, the working directory by
default. `--library` is the method library a client may list, reported by `hello` and otherwise
unused by the daemon. It may name several directories joined by `;` (`:` off Windows), searched
in order, and defaults to the `golden` directory of the
[instrument directory](mcp-server.md#the-instrument-directory) when that resolves; the daemon
logs the library it serves when it starts.

`--instrument`, `--limits` and `--project-limits` are the sitting: the instrument document runs
are acquired under, the instrument's [standing limits](instrument-limits.md) and a project's
limits combined into them. The daemon loads them when it starts, logs them, and reports them in
`hello`, and the MCP server and every verb of the command line take the daemon's wherever their
own flags are absent, so every client of one daemon shows and enforces one set of limits. The
instrument document defaults, except under `--fake`, to the
[instrument directory](mcp-server.md#the-instrument-directory)'s `instrument.toml`, and the
standing limits to the `limits.toml` beside it. A document or limits that are named or found and
do not load are one sentence in the log and exit code 1, before anything else is done.
`--fake` runs everything above over simulated boxes
and a simulated
console: it takes no lock, stops nothing, opens no port but its own two, and nothing it reports is
evidence about a MIPS box or a digitizer. `--kept` is the folder a failed run's files are copied
into, the `$CLOCKWORK_REPORTS` or per-user default otherwise, and `--errors-log` is a front end's
error log to copy with them; the window passes its own of both when it starts the daemon, since
the daemon has no error log of its own.

The acquisition console the daemon starts is tied to the daemon's life. On Windows the daemon
places itself and everything it starts in a job object that ends its members when the daemon's
process ends, however it ends: closed with its console window, killed from Task Manager, or
crashed. A console can therefore not outlive the daemon and hold the digitizer against the next
one.

Starting the daemon at a terminal is the authorization for what its clients then do. There is no
login and no token; the sockets accept connections from this machine only.

## Sockets

| Socket | Address | Pattern | Carries |
|---|---|---|---|
| Command | `tcp://127.0.0.1:5570` | ROUTER; client uses DEALER or REQ | One request frame, one reply frame, both JSON |
| Events | `tcp://127.0.0.1:5571` | PUB; client uses SUB | Two frames per message: a topic, then a JSON payload |

Both are bound to the loopback address, never to `*`, and both ports sit clear of the console's
5554 and 5555. The command socket answers each request within milliseconds: nothing a request
asks for waits on the hardware, since a job is queued and its progress read later. The one
exception is `events` for a long job, whose reply grows with the job's history, up to tens of
megabytes for a series of a thousand repetitions. The daemon builds that reply beside its command
loop, a piece at a time and resting between pieces, so that it delays no other request and holds
the acquisition, which runs in the same process, for no more than about 15 ms. Answered at once
on the loop, as before clockwork 1.2.0, one such reply stalled the acquisition for most of a
second and a `status` asked meanwhile for three. Replies therefore carry the request's `id` and
may arrive in a different order from the requests, for a client that sends several without
waiting. A DEALER client sends an empty delimiter frame before the request, as it would to any
ROUTER, and reads one after it.

## Requests and replies

A request is one UTF-8 JSON object:

```json
{"id": 7, "cmd": "submit", "args": {"job": {"@type": "Discover", "label": "finding boxes"}}}
```

and its reply is another, with the same `id`:

```json
{"id": 7, "ok": true, "result": {"@type": "Handle", "id": 3, "kind": "Discover", "label": "finding boxes", "owner": "5f1c0a9e27d4"}, "session": "5f1c0a9e27d4"}
{"id": 7, "ok": false, "error": {"kind": "refused", "message": "clockwork serve is shutting down"}, "session": "5f1c0a9e27d4"}
```

`id` is the client's and is echoed unexamined. `args` may be omitted where a command takes none.
`session` names this run of the daemon and changes when it restarts, which is how a client learns
that every handle it holds has gone with the old one. `error.kind` is one of four words:

| Kind | Meaning |
|---|---|
| `bad-request` | The frame was not a JSON object with a `cmd`, or an argument could not be decoded |
| `unknown-command` | `cmd` is not in the table below |
| `refused` | The request was understood and declined: a submit during shutdown, a handle from another session, something that is not a job |
| `failed` | The daemon raised while answering; the message is one sentence and its log has the rest |

`error.message` is always one sentence meant to be shown to a person as it stands.

## Commands

One command per call of the owner protocol, and `hello`. Four of them, `pause`, `resume`,
`note` and `guard`, are a person's at the window ([A person's pause](#a-persons-pause),
[the beam guard](#the-beam-guard)).

| `cmd` | `args` | `result` |
|---|---|---|
| `hello` | none | An object naming the daemon: `program`, `version` (clockwork's), `protocol` (1), `session`, `pid`, `started`, `fake`, `events` (the event socket's address), `output`, `library`, `holder`, the lock holder's own description or null under `--fake`, and the sitting, `instrument`, `limits` and `project_limits`, each an absolute path or empty for none |
| `submit` | `job`, `origin` (optional) | A `Handle`. The job is queued and runs after every job before it, one at a time. `origin` is a few words naming the client, such as `the clockwork window (pid 5120)` or `Claude, through clockwork mcp`, and is carried unexamined in the handle's own `origin` so that every other client can say whose job it is |
| `events` | `handle`, `after` (default 0), `limit` (default 0, none) | That job's progress numbered after `after`, oldest first, as a list of `Progress`, and at most `limit` entries of it where `limit` is not 0. A client reading a long history asks for a page at a time, each after the last number of the page before, until a reply holds fewer than `limit`; the Python client asks for 2000 at a time, about 5 MB of a long series' history, which the daemon answers in about 0.35 s |
| `summary` | `handle` | A `JobSummary`, where the job has got to, in a reply whose size does not grow with the job: `last`, the number of its newest event; `job`, the job as it started, or null before; `runs_done`, the runs it has finished; `scans_so_far`, what the console frame in flight has published; `point`, the newest `PointStarted` of a series, or null; and `ended`, the `JobFinished` or `JobFailed` that ended it, or null. The daemon keeps it as the events arrive, so `runs_done` counts every run of a job whose oldest events have been let go |
| `stop` | `reason`, `by`, `client` (all optional) | null. The run in flight ends after its current repetition and its fold, and a paused series ends where it rests. `by` is the initials of a person who asked and `client` a few words naming the client, as `origin` does; both are written into the run's record with the stop |
| `pause` | `grain` (`point` or `repetition`, default `point`), `by`, `client` (optional) | A `Hold`, at once. The series running rests after its point or its repetition in flight |
| `resume` | `by`, `client` (optional) | null. The pause is lifted and the series goes on |
| `note` | `text`, `by`, `client` (optional) | A few words saying where the note was kept: the run record of the job running, or the day record |
| `guard` | `guard` (a `BeamGuard`, or null to turn it off), `by`, `client` (optional) | A few words saying where the change was recorded. The daemon's beam guard is armed, changed or turned off from the next repetition of every acquisition |
| `snapshot` | none | The `Snapshot` the next run will be stamped with, or null before any send |
| `status` | none | An `OwnerStatus`: console, boxes, ports held, running and queued handles, stopping, the lock holder, the lock's refusal if there is one, `armed`, what the last send that finished left the boxes holding (the method's name, what went on the wire, and the stem the send named), or null before any send and while one is under way, `issued`, the id of the newest handle this session has issued, 0 before the first, `hold`, a pause on the series running, a person's or the beam guard's, or null, and `guard`, the beam guard with who armed it, or null |
| `shutdown` | `reason` (optional) | null, sent before the shutdown begins (under Shutting down) |

A client checks `protocol` in the `hello` reply and refuses to go on if it is not the version it
was written for. This document describes protocol 1. `origin`, `issued` and the sitting's three
paths were added to it without a new version: each has a default, an empty string or 0, that a
client written before it never reads and a daemon written before it never sends. `pause`,
`resume`, `note`, the `by` and `client` of `stop`, and `hold` were added in the same way, with
`hold` null by default, and so were `guard` and the status's `guard`, null by default. A daemon
written before any of the four commands answers it with `unknown-command`, which the window
shows as one sentence saying that the daemon is older than the window. `summary` was added in the
same way; a daemon written before it answers `unknown-command`, and the Python client then works
the summary out from the job's whole history, as a follower did before it. So was `limit`; a
daemon written before it refuses the argument as a `bad-request`, and the Python client then asks
for the whole history in one request. A daemon written before `stop` took `by` and `client` refuses them as a
`bad-request`, and the Python client then sends the reason alone.

Handle ids are counted from 1 for each session with no gaps, which with `issued` lets a client
follow every job whatever client submitted it: every id up to `issued` is a job, and `events`
answers for any of them by its id and the session. A job's kept history is dropped some time after
it finishes, and `events` then answers with an empty list.

## Wire forms

Every job, event, handle and result crosses as the JSON form `clockwork.owner.wire` gives it: a
dataclass is an object whose `@type` key is its class name and whose other keys are its fields,
and a list comes back as a tuple. Four classes cross differently. A method crosses as its
canonical TOML text plus its load-time warnings, and an instrument document as its text. A batch
of scans crosses without its summed spectrum, which is hundreds of kilobytes about fifteen times
a second and which no client draws, since mainspring is the only viewer; its total ion counts and
time stamps cross. A box found by a scan crosses as its name, port and firmware version, never as
the open port, which stays with the daemon that opened it.

An event the codec cannot carry is replaced by a `Said` event of the same number saying so, so a
new kind of event can never stall the stream. The event that ends a job is the exception: a
`JobFinished` whose result cannot cross is sent without its result, since a client following the
job is waiting for exactly that event.

## The event stream

Each event a job reports is published once, on the event socket, as it happens:

| Topic | Payload |
|---|---|
| `handle/<id>/` | One `Progress`: `seq`, the daemon's number for the event, and `event` |
| `heartbeat/` | `{"session": ..., "seq": ..., "time": ...}`, once a second |

To follow one job, subscribe to its topic; the trailing slash keeps `handle/1/` from matching
`handle/12/`. To follow everything, subscribe to the empty string. Events reported outside any job
are published under `handle/0/`.

The sequence numbers are the daemon's, one counter for every event of every job, and they are
published in the order they were assigned. A subscriber to everything therefore sees them
consecutively, 1, 2, 3 and on, and a gap means messages were lost: at the publisher's high-water
mark, or across a reconnection. The heartbeat carries the last number published, so a quiet
subscriber can tell that it missed the end of a job. Two consecutive `BatchSeen` events of one job
are one entry in `events`, the second replacing the first under a new number; a subscriber that
wants the same list applies the same rule.

A subscriber that connects late, or loses messages, catches up over the command socket: `events`
with `after` 0 returns the job's whole kept history, and the stream supplies everything after it.
Publish-subscribe drops what it cannot deliver and says nothing, so the stream is the fast path
and `events` is the record; a client that cannot tell whether it has seen everything asks. The
Python client keeps a copy of each job's progress from the stream, fills it by one `events` call
the first time a job is read or after any gap, and falls back to asking every time until its first
heartbeat arrives. It fills the copy from the number it was asked after, not from 0, and fills it
again from further back only when asked for earlier events.

To follow a job, ask `summary` for where it has got to and `events` for what is new since the last
number read. That costs the daemon the same at the job's first repetition as at its thousandth.
A follower that instead asks for the whole history every few seconds makes the daemon build the
largest reply it has, again and again. On 2026-11-19, a follower that started a new process every
two seconds and asked that way slowed AUKLET's `TBLSTRT` acknowledgements until the gate guard
dropped repetitions in 13 of 19 files, and kept the window's `status` waiting until the window
took the daemon for gone. A reply built beside the loop no longer stalls the
acquisition, but it still costs the daemon a few seconds of work each time.

## A person's pause

To hold a series without ending it, a person at the window sends `pause`. A pause is the daemon's
state and not the window's, so it outlives the window that asked for it, and every client reads
it in `status` as `hold`:

| Key | What it holds |
|---|---|
| `job` | The id of the series job held |
| `grain` | `point` or `repetition` |
| `by`, `client` | The initials of the person who asked, and the client the pause came through |
| `asked` | When it was asked for, local time |
| `since`, `where` | When the series came to rest, and where in words, such as `after point 3 of 12 (planned 7)`; both are empty until it has |

A pause is accepted only while a series job is running and has started its first point. `point`
rests the series once the point in flight has acquired its last file, at which time no file is
open and the console's acquisition is closed. `repetition` rests it once the repetition in flight
has ended, inside the file, with the file and the console's acquisition left open; when that
repetition is the last of its file, the series rests before its next file instead. The daemon
refuses a pause that has nowhere to rest, with the sentence the window shows on its greyed button.
It refuses a job that is not a series, for which `stop` is the only intervention. It refuses
`point` on a series' last point. It refuses `repetition` on a point whose method is
`single_frame`, since that method's repetitions are inside the sequencer's table and its file has
no boundary between them. A pause asked for while another is still on its way replaces it.

Nothing bounds a pause. The job remains the one running, every job queued behind it waits, and
the series rests until `resume`, `stop` or `shutdown`. While it rests inside a file the daemon
keeps reading the boxes' serial ports and the console's data stream and asks nothing of either.
`resume` lifts the pause. After a rest inside a file, the next repetition is prepared as the
first repetition of a run is: the method's reset sequence is sent, the declared enable line is
lowered by command, and the frame is held open for the gate check before the start sequence
releases it. That repetition's record says how long the rest lasted, and the file records the
same ([template file](template-file-format.md#what-a-rendered-run-records)). `stop` ends a paused
series where it rests, as a series stopped while paused.

Four events report a pause within the job's own progress. `PauseAsked` and `Held` carry the
`Hold`, `Resumed` adds who lifted it and the seconds rested, and `StopAsked` carries a stop's
reason, its `by` and `client`, and whether the series was paused. `note` adds a `Noted` event,
with the text and the person's initials, to the job running. A note is a record and not a
command: it reaches every client that follows the job as something to read. With no job running,
or one that has no run record, the note goes to the day record instead
([run record](run-record.md#the-day-record)).

The daemon writes each of these acts into the run record of the job acted on, with `by` and
`client`, under `interventions` ([run record](run-record.md#the-sections)). Every `stop` of an
acquisition is written there too, whichever client sent it, so that a stop pressed at the window
and a stop sent by an agent's tool can be told apart afterwards.

Only the window sends `pause`, `resume` and `guard`. The command line has no verb for any of
them, the MCP server has no tool for any of them, and while `hold` is not null their tools read and note only
([MCP server](mcp-server.md#while-a-person-has-paused-the-run)). This is capability by absence
and not a lock. The socket is bound to the loopback address and has no login, so any program on
the instrument PC that speaks this protocol could send either command. What the arrangement
guarantees is that no part of clockwork that an agent drives contains a way to lift a person's
pause or to arm, change or turn off the beam guard.

## The beam guard

The beam guard ends a file whose beam was lost at the source: a file whose repetitions count fewer
ions than a floor, a set number of times in a row, ends after the current repetition and its
fold, and says `failed` with the guard's sentence. It is the daemon's state, armed by a person at
the window with `guard` and reported to every client in `status` as `guard`: the `guard` itself
(`floor`, `repetitions` and `source`, the floor's origin in words), `by`, `client` and `since`.
Every acquisition the daemon makes watches it from its next repetition, whichever client
submitted the job, until a person turns it off or the daemon stops. A `guard` sent while a job
runs adds a `GuardChanged` event to that job, and every change is written with `by` and `client`,
as a `guard` entry in the `interventions` of the running job's run record, or with nothing
running as a `guard` line in the day record ([run record](run-record.md)).

A file the guard ends inside a series job, a plan or a queue, then holds the series. The daemon
sets `hold` itself, with `by` `the beam guard`, `grain` `point`, the guard's sentence as
`beam_lost`, and `where` naming the point and the file, and writes a `pause` intervention by the
beam guard into the record. The series rests as at a person's pause after the point, until a
person sends `resume` or any client's `stop` ends it, under the same rules: the agent's tools
read the hold and cannot lift it. On `resume` the daemon acquires the point again from its start,
as new files under the same planned index at the next positions, adds an entry to the record's
`retakes` naming the files it replaced, and goes on. The point's `PointStarted` event then
carries `retake`, 1 for the first retake. A single `Acquire` has no series to hold, so a file the
guard ends there ends the job, which finishes with that file's `Run` carrying `beam_lost`.

## The instrument lock

The daemon holds the same per-user lock the window takes, `%LOCALAPPDATA%\clockwork\instrument.lock`,
for as long as it runs. A second daemon, or a window started after it, is refused before it opens
a port, with a sentence of the form

> the instrument is already owned by clockwork serve (pid 4812, since 2026-09-23 14:02:11); close
> that one before using the hardware from here

and a daemon started after a window is refused the same way, naming the window. The operating
system releases the lock with the process however it ends, so a daemon killed from Task Manager
leaves nothing to clean up. A program that does not take the lock, such as the MIPS host
application or a bench script, is refused by the COM port itself as it always was. Under `--fake`
no lock is taken, so a second `--fake` daemon is refused by its port instead.

## Shutting down

Any client may send `shutdown`, and Ctrl-C at the daemon's terminal does the same. The reply comes
first; then the daemon stops the run in flight after its current repetition, folds it, closes its
files, fails every job still queued without starting it, closes the boxes and the console, and
lets go of the lock last, so that a second owner taking it at once finds every port already
closed. While that happens the command socket keeps answering `status` and `events` and refuses
`submit`. A second Ctrl-C abandons the wait: the console is stopped and the process exits, and
the run in flight is left as its last completed repetition. What a stopped run leaves on disk is
a short experiment, not a broken one. A series that a person has paused does not hold a shutdown
up: it ends where it rests, as stopped while paused.

## Failure modes

**The daemon dies while a table is streaming to a box.** A MIPS box abandons a table after three
seconds of silence between its tokens, so the box is left as it was before the table began, and
harmlessly so. The run is lost: its file holds the frames completed before the death, and the next
owner must send the method again before acquiring.

**The console dies.** The daemon checks every two seconds whether the console it started is still
running, and restarts it once, with a line in its log, when it has stopped on its own while no job
was running. A restart that fails is not retried; the next job that needs the console says so.

**A console is already running when the daemon starts.** A console outlives an owner that was
killed, and keeps its command port against the next one's start. The daemon looks for a process
listening on the console's command port once it holds the lock, and stops it when it is the
acquisition console, rather than adopting it: a console the daemon did not start has no captured
output, so neither its startup record of what it read from `config.txt` nor anything it writes
afterwards could reach a transcript. A port held by any other program is left alone and reported.

**The daemon is not there.** A request that goes unanswered within the client's timeout, five
seconds by default, fails with a sentence saying that no daemon answered and how to start one; the
client discards its socket and makes another, so the late reply cannot be read as the answer to
the next request. When the daemon comes back, on the same address, the next request reaches it,
its new `session` tells the client that the old handles are gone, and asking for the progress of
one of them is refused.

## The window as a client

The clockwork window is a client of the daemon like any other, and on an instrument PC it is the
usual way a daemon starts. When it opens it asks `hello` on the command socket. If a daemon
answers, the window follows it. If none does, the window starts `clockwork serve` itself, the
installed executable with the subcommand, passing its output directory, method library, console,
kept-files folder, error log and sitting (its instrument document, and the limits files where it
has any) on the command line, and waits for `hello`. The daemon it starts
has no console window, runs in a process group of its own, breaks away from any job the window is
in, and logs to `serve.log` alone; it is stopped by `shutdown` and never by a console signal.
`--detached` on its command line keeps its log out of the console of whatever started it, which
matters when that is `clockwork warm-up` in a shortcut's console window: without it, the daemon's
later lines printed after the verb's prompt. `warm-up` starts a daemon the same way. If
the daemon ends before it answers, which is what happens when another program holds the
instrument lock, the window shows the last line of `serve.log`, which names the holder.

The window follows every job in the daemon, whichever client submitted it. It reads `status` a few
times a second and the progress of every handle up to `issued`, and shows another client's job in
its run log and status bar exactly as it shows its own, with the job's `origin` beside it, and
lists another client's running and queued jobs under its run queue. A daemon that is `--fake`
is followed in the same way, and the window says so in its title and its run log. A daemon that
does not answer within the window's two seconds is gone only if its command port no longer takes
connections, its process has ended, or the instrument lock no longer names it, which under
`--fake` it never holds. Otherwise it is
busy, and the window says so once in its run log, goes on following it, and offers no Find boxes
until it answers again. It greys out its sending and
acquiring buttons while any job runs, since the daemon runs one job at a time, and its Stop button
stops whichever run is in flight. Its two pause buttons, Resume and the note field act on
whichever job is running in the same way, each under the initials typed in "At the window".

Closing the window leaves the daemon to its clients:

| When the window closes and | it does this |
|---|---|
| a send, acquisition or series it submitted is running or queued | asks whether to stop it after the current repetition and close once its files are closed, leave it running in the daemon and close now, or not close |
| a series it submitted is paused | asks whether to leave it paused in the daemon and close now, stop it where it rests and close once its files are closed, or not close |
| another client's job is running or queued | says so and closes, leaving the daemon and the job running, and a paused series paused |
| nothing is running or queued | shuts the daemon down if this window started it, and leaves one it found running as it was |

The window's run queue is a series job like a plan's, submitted with every row's document read
at Start, so it outlives the window as any job does, and a paused queue stays paused. Before
clockwork 1.2.0 the queue belonged to the window, and its waiting rows were skipped whichever way
the window closed. Under `--fake` the window runs its simulated instrument in its
own process and starts no daemon.

Two verbs of the [command line](command-line.md#the-two-ends-of-a-day) start and stop a daemon
the same way. `clockwork warm-up` starts one as the window does when none answers, with the
window's saved output directory, library and console, and leaves it running, so a window opened
later finds it and leaves it running when it closes. `clockwork stand-down` sends `shutdown` once
it has zeroed the boxes, then waits until `hello` goes unanswered, the daemon's process has ended,
the instrument lock no longer names it and nothing listens on the console's command port.

## Logs

The daemon writes one line per event worth a person's reading to its terminal, unless it was
started `--detached`, and to `%LOCALAPPDATA%\clockwork\serve.log`, appended: its start and what
it found, every request that changes something (`submit`, `stop`, `pause`, `resume`, `note`,
`guard`, `shutdown`) with the job it named and the client that sent it, each job's start and end, the console's starts and restarts, and anything it stopped. The window's `errors.log` in the same
directory is left to the window. Each send and each run also leaves its wire transcript and its
send log beside its files in the output directory, exactly as a run started from the window does.

`serve.log` is the daemon's own account, kept on the PC and never beside the data. Two records
the daemon writes do go beside the data, in its output directory, and are described in
[the run record](run-record.md). A run started at the window leaves a run record that the daemon
begins when the job starts, since the window begins none of its own: a `submit` whose
`Acquire` names `source` `window` and no request has the daemon mint the request, stamp it into
every file, and record who was at the window and what the run was for. It reports the
request's identifier and the record's path to every client in a `RecordBegun` event, after the
job's `JobStarted`. The day record,
`day-record.jsonl`, holds the sitting the daemon was started with, with the SHA-256 of each
document, the daemon's own start and stop, and each console start, restart, stop and failure with
the client whose job caused it, so that a console restart between two runs is recorded beside the
runs it fell between.
