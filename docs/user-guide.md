# User guide

This guide takes a trainee from a fresh instrument PC to a first acquisition: the two vendor
installs the driver and the acquisition console need, the clockwork installer, the window's panes
and buttons, and what a run leaves on disk. It assumes the SLIM boards, the MIPS controller boxes
and the SA220P digitizer are already cabled to the machine; that work is the instrument's own
commissioning record, not this guide's.

## Before installing clockwork

Two vendor products have to be on the machine first, in this order:

1. **Keysight IO Libraries Suite.** Supplies VISA, which the acquisition console uses to find the
   digitizer. Install it before the driver below, even if its own installer offers to put a
   different VISA on first. Two VISA implementations can coexist on one machine, but only one is
   primary, and Keysight's has no secondary mode: installed second, it forces itself into that
   role and the console stops finding the card.
2. **Acqiris MD3 software.** The IVI-C driver the console opens the SA220P through, plus the Soft
   Front Panel, which is how the card's `PXI…::INSTR` resource name is read the one time
   `config.txt` needs it.

A machine already used to bring the SA220P up has both. Nothing about the clockwork installer
checks for either: launching clockwork with the driver missing is a start_console failure, not a
refusal at install time. On a machine that has had neither install, the acquisition console
executable writes nothing at all and exits on its own, so a console that appears to do nothing is
the symptom of a skipped driver install rather than of a fault in clockwork.

**mainspring** is the third install, and it is the one that can wait. clockwork acquires and writes
its files without it, and what needs it is reading the result, so a machine that will only ever run
the instrument can be left without one. Install **1.6.0 or later** where a run is to be watched
while it is being acquired: every version registers itself for `.uimf` and answers *Open in
mainspring*, and 1.6.0 is the version that added the run pointer the live view rides on. Order does
not matter. clockwork reads the `.uimf` registration at the moment the button is pressed rather
than at the moment it is installed, so a mainspring installed afterwards is picked up by a
clockwork window that is already open, with nothing to restart and nothing to configure.

## Installing clockwork

Run the installer, `clockwork-<version>-setup.exe`. It is a per-user install and needs no
administrator rights, so it runs under whatever account normally signs in to the instrument PC.
One installer carries both clockwork and the acquisition console the lab's fork builds: they land
in the same folder, clockwork beside a `console` directory holding the console's own executable
and its `config.txt`, and clockwork finds the console there itself. Nothing further points one at
the other.

The wizard offers a desktop icon and, at the end, a chance to launch clockwork immediately. Both
are optional.

## First launch

Three fields matter before anything can be sent to a box:

- **Initials.** The middle field of every file name this session writes, remembered on this
  machine. Two letters is typical; clockwork takes no view of what goes here beyond cleaning it to
  what a file name can hold.
- **Output directory**, where the UIMF files, the transcript and the send log all land. Set once
  and remembered until changed.
- **Instrument document**, opened through the browse button beside it. This is the file that
  states the machine's own m/z calibration and the digitizer's vertical settings, the parts of an
  acquisition that describe the instrument rather than the experiment. Without one, every file this
  session writes has no calibration and mainspring shows flight time instead of mass. The status
  line under the field says what was read: the calibration date, the full scale and offset in
  force, and whether the document states a channel offset at all.

Everything else about a run, the boxes' strings and the acquisition settings, comes from a method,
loaded through **File > Open method…** or the method library (**File > Method library…**), not
typed by hand here.

## The panes and what the tags mean

One pane per box in the method holds the strings that box will be sent: setup commands, the
pulse-sequence table it loads, and how it is armed. It reads exactly like the pane one used to
paste into MIPS_QT6's terminal. What is new is the margin down the left, one tag per line, which is
clockwork's reading of what that line does and never a rewrite of it:

| Tag | Means |
|---|---|
| `setup` | An ordinary setter, sent whenever this box's setup phase runs |
| `load` | `STBLDAT` or `SARBCTBL`: a table this box loads |
| `arm` | Puts the box or a module into the mode the load and start commands need |
| `start` | `TARBTRG` or `TBLSTRT`: starts what was just loaded |
| `note` | A `#` comment, kept as text and never sent |
| `tag` | A `# clockwork: <phase>` directive: an explicit override where the reading below would guess wrong |
| *(blank)* | Not placed in any phase, drawn in the pane's warning colour rather than silently dropped |

An unplaced line is never refused. It stays in the pane exactly as typed, so a sentence clockwork
cannot classify is a question to answer with a `# clockwork:` tag, not a reason the pane will not
load.

## Sending a method

Three buttons walk a method onto the wire in the order MIPS boxes need, and getters are never
typed by hand: the state panel below is where a value already on a box is read back.

1. **Send setup.** Every phase for every box, in the method's order, plus a readback of what each
   box was holding before, after setup and once armed. Run this once per box after it powers up.
2. **Load and arm.** The table and the mode change only, skipping setup. Use this on a box that
   already had its setup sent this session and only needs its next table loaded.
3. **Acquire.** Runs the first acquisition, then one replicate for each further count in the
   Replicates field, each into its own file, on one worker thread so the window stays responsive
   throughout. Every one of them walks the method's reset list and puts the digitizer's enable
   down before its first frame, which costs a couple of seconds and is what makes a second press of
   Acquire as good as the first.

**Acquire is greyed out until the boxes agree with the method in the panes.** clockwork keeps a
fingerprint of what the last Send setup or Load and arm actually put on each box, and Acquire is
disabled until that fingerprint matches what the panes now hold, with the reason named in the
button's tooltip: "the boxes have not been loaded and armed with this method" the first time, or
"the panes have changed since the boxes were armed" after an edit. This exists because a box
nobody sent to answers `TBLSTRT` with a refusal partway through a series, which used to cost a
trainee three frames and an `abort_after` before anyone noticed the setup step had been skipped.
The fingerprint covers what the send actually delivered, so a Load and arm covers the table, the
mode change and the acquisition's declarations, and does not claim anything about the setup
strings it did not send. Editing a setup line after a Load and arm therefore leaves Acquire
available. Send setup again if the box needs that line.

**Stop** ends a run after its current repetition and fold rather than mid-flight, so what a stopped
run leaves on disk is a short experiment and not a broken one.

**A run whose pusher is not running at the method's period is refused before its first frame.**
Before each run's first frame the run log gives the pusher period the digitizer measured beside the
one the method was written for, which is the template's tick for a rendered method and the
instrument file's `pusher_period_us` for one written by hand. A method's tables count pushes, so
behind a pusher at 62 µs a method written for 129 µs runs every event at 0.48 of its time. Beyond
a 10% difference the run stops there with the reason named and leaves no data file; beyond 2% it
runs and the line is shown as a warning. Either means the time-of-flight's pusher setting has
changed, so ask whoever set up the instrument that day before acquiring again.

## What the progress bar counts, and the wait at the end

The bar counts **scans across the whole run**, not repetitions, and it advances about fifteen
times a second while the digitizer is publishing. The caption above it counts repetitions, which
is the number to match against the method. Both matter because the two golden methods are shaped
differently: a per-repetition method asks the console for one frame per accumulation and the
caption steps through them, while a single-frame method puts its accumulations inside the
sequencer's own table and asks for one frame of half a million scans. In the second case the
caption reads `repetition 1 of 1` for the whole minute the frame takes, and the bar is the only
thing that moves.

**Copy**, beside Clear above the log, puts every line on the clipboard as text, so what
happened on this machine can be pasted into a message rather than described. Collapsed groups
are copied open and a warning is marked with a leading `!`, since text carries no colour. The log
itself is not written to a file: the send log and the transcript beside the data are the record
of a run, and the log in the window is clockwork's own account of it.

No estimate of the time remaining is shown. The cost of a repetition depends on how much of the
detector's signal survives zero suppression, so the first repetition does not predict the
hundredth.

**A run is not over when its last repetition ends.** The repetitions are summed into the
`.summed.uimf` companion afterwards, and the run log says so before it starts: "summing 100
repetitions (1,310 MB) into the companion, about a minute; the run log is quiet until it is
done". Nothing is printed while it runs. The estimate is coarse and it is an estimate, but the
order of magnitude is right: a beam-on detection-response run of that size takes about a minute
to fold, and a larger file takes proportionally longer. Do not close the window or kill the
process during it. The per-repetition raw file is
complete on disk by then, and the companion is what a force-quit would lose.

## A repetition that comes up short

A repetition is short when the console says it has finished but fewer scans reach clockwork
than the method asked for, and the stream then stays quiet for three seconds. The console does
this when it loses the first batches of a frame, and the raw frame it leaves behind holds rows
that belong to no real scan, some with a base-peak m/z far outside any mass range. Rather than keep that frame, clockwork acquires the repetition again, once, into the
same frame of the raw file, after deleting every row the short attempt left there. The run log
shows the short frame and then a line such as `frame 1.97: 8500 of 10000 scans, acquired again`,
and the raw file ends the run with exactly one frame per repetition. The wire transcript is the
only record of the attempt that was discarded.

The same treatment applies to a repetition whose trigger timestamps start inside the previous
repetition's. The digitizer's clock runs on from one frame to the next, so a frame that starts
behind it is the previous frame's triggers read a second time, and its spectra are not this
repetition's. Every such frame seen so far also came up short, but one that counted out would
otherwise have been summed as a second copy of the repetition before it. Its line in the run log
ends `the previous frame's triggers read again`.

A repetition that comes up short a second time stops the run, as **Stop** does. The repetitions
before it are summed into the companion and that one is left out of the sum; it stays in the raw
file, marked unfinished, and a queue moves on to its next row.

A row of the run queue counts both events in its outcome: `2 repetition(s) acquired again` for
the repetitions that came up short, and `console reported 3 errors` for the `[error]` lines the
acquisition console logged while the row's runs were acquiring. Neither makes the row's files
unusable. A retried repetition is whole, and a run stopped by a second short end is a short
experiment rather than a broken one.

## Replicates and naming

The **Name** field is filled automatically: one past the highest number these initials already
use in the output directory, so two people acquiring into the same folder never collide. It is
editable before Acquire starts, and the **Next** button beside it re-reads the directory on
demand, which matters if a file landed there from somewhere else since the window opened.

A number is used up by any file written under it, the send log and the transcript as much as
the UIMF file, so a run that failed before its UIMF file existed still moves the counter on.
The field moves to the next free name whenever a send or an acquisition fails. A name that
is already taken is refused before any string goes to a box, and the refusal names the next
free one: typing a name by hand and leaving a stale one in the field get the same answer,
with no file overwritten and nothing sent. The name Send setup has just prepared is the one
exception, since the acquisition that follows it continues that send's log under the same
name.

**Replicates** is a count of technical replicates, run unattended after the first acquisition,
each the same acquisition again into a file of its own. A
**Conditions** note, typed once, is stamped into every file a run writes and into its send log's
header. It is the one part of the record no getter can read back: sample, MCP voltage, pusher
period, collision energy, whatever the day's method does not already state as a setting.

The lone **Replicate** button beside Acquire runs one more acquisition off the last run's own
readback, for the acquisition taken after the fact rather than counted up front.

## A method rendered from a template

A template is a method with holes in its strings and named knobs that fill them, such as a hold
time or a guard voltage ([`template-file-format.md`](template-file-format.md)). The window opens
methods, not templates, so a template becomes something to run by rendering it at chosen knob
values into a method file. There are two ways to get one.

- **Ask Claude**, for example for "the bradykinin CLOCK method with a 25 ms hold, as a file in
  my folder", or for one file per cell of a grid of holds and guard voltages.
- **From a shell**, with `render-template` and `--to`, as below. An existing file is replaced
  only with `--overwrite`.

```
clockwork render-template --template bradykinin-clock/template.toml --knobs duration_ms=25 --labels sample=bradykinin --to cells/25ms.toml
```

A rendered file opens in the window like any other method, into the same panes, and can be sent,
acquired, replicated and put in the run queue. What it adds is a record of where it came from:
the template, the knob values and the labels are embedded at the foot of the file. The line above
the panes names them, for example *Rendered from bradykinin-clock at duration_ms 25 ms*, and every
file acquired from it records each knob, label and mark as its own parameter, so a table of a
week's runs can be sorted by hold time without anyone reading the strings.

This holds while the method is **attached**, which is for as long as what the panes would send is
still what the template renders at those values. The first edit that changes that, a table tick,
a setup line, a comment or the number of frames, **detaches** it. The run log says so once, the
line above the panes goes away, and from then on the method runs and saves as an ordinary
hand-written one, whose files record no knobs. Nothing is refused either way. Reopening the file
attaches it again, as long as the file itself is unedited. A file whose strings were edited in a
text editor after it was rendered opens detached, with one warning in the run log saying why.

Changing the name, the output directory or the stem does not detach a method, and neither does a
box answering on a different COM port, since none of those changes what a box is sent.

Templates are listed in the method library too, marked as templates with their knobs and ranges.
**Open into panes** on a template explains how to render it instead of opening it.

Anyone may edit a template, in a text editor, and check it with
`clockwork validate-method --template PATH`. Note that an edited template has a new hash, and the instrument's standing limits
list templates by hash, so Claude cannot run an edited template until its entry in the limits is
updated. The window is not affected, because the limits apply only to what Claude sends.

## The run queue

To run a series of samples or conditions unattended, open **Run > Run queue** and add a row
for each method document. A row names a saved method, a conditions note and a replicate count,
and the document is read when the row starts, not when it was added, so a typo fixed in the
file before the row runs is the version that runs. Each row sends its method and then acquires
its replicates back to back. **setup** sends the whole setup, readback included; unticked, the
row loads and arms only, for a row whose method the boxes already have. A row that fails ends
the series and leaves the rows after it **skipped**, unless its **go on if it fails** box is
ticked. **Start the queue** runs every waiting row, skipped rows included, and **Stop** ends the
series after the current repetition and its fold.

To spread technical replicates over a series rather than acquire them back to back, press
**Randomize…**, choose a number of passes, and press OK. Each pass runs every waiting row once,
in its own shuffled order, and a row's own replicates stay together as one send and its
acquisitions. The result is ordinary rows, labelled in the **pass** column (`2/3` is the second
of three passes), so the table shows exactly what will run before **Start** is pressed, and any
row can still be edited, moved or removed. With **new order each pass** unticked, one shuffled
order is repeated in every pass, and one pass is a plain shuffle. Rows ticked **stays first**
open every pass, in the order they were entered, ahead of the shuffled rows, which suits a
blank or a calibrant. A closing wash is added by hand after randomizing. The run log records the
seed and every pass's order each time Randomize is applied.

Randomizing a queue that already has passes rebuilds them from pass 1, after asking. An edit
made to a row of pass 2 or later is lost; to change every pass, edit the row in pass 1 and
randomize again. A row added by hand after randomizing joins every new pass. Rows that have
already run stay where they are, above the new passes. Randomize is unavailable while the
queue runs.

To keep a series for another day, press **Save queue…**. The file, which ends in
`.queue.toml`, holds each row's method, note, replicate count, the three check boxes and its
pass, and the last seed. **Open queue…** replaces the queue with a saved one. Every row opens
waiting, because the file is a plan rather than a record; what the rows did is in the run log
and their files. Each method is stored by its full path and by its path relative to the queue
file, so a folder of methods copied to another PC with its queue file beside them still opens.
A row whose method is found by neither path fails when it starts, and says so in the run log.

## The state panel

**Run > Read the boxes' state**, or the disclosure arrow on a box's own pane, opens that box's
state panel: what it is actually holding, read back over the wire rather than assumed from what
was sent. A few things it reports are worth knowing before the first time they appear:

- A setting the method's `[[boxes]]` table does not declare is marked **left as found**, not
  flagged as missing. A clean, correctly-set-up box shows this for every ARB module setting a
  method does not bother naming; it is what told two otherwise-identical files apart once.
- A DC bias or RF setting the box disagrees with what the method declared is marked, with both
  numbers, and refuses nothing. Something moved it after the method was sent, at the front panel or
  from another session.
- **DC bias monitors freeze the moment a box enters table mode.** A reading taken while armed is
  reported as not converting rather than compared against anything, because a frozen monitor is not
  a disagreement. The panel keeps the last reading whose monitors were live and shows those
  figures on the rows instead, with the time they were taken, so pressing Read state during a run
  does not cost you the only measurement of those outputs you have.
- **An ARB module's waveform frequency reads back lower than you set it, and that is correct.**
  The module's output clock is an integer divider off a 42 MHz master clock, so it can only make
  the frequencies that divider produces. `SWFREQ,n,15000` is acknowledged and reads back as
  **14914 Hz**, on every module of both ARB boxes. The panel marks the row as declared and says
  which frequency the divider reached. A row marked as differing means the module is holding a
  frequency no request of yours would produce.

A box nothing has been read off yet says so plainly instead of showing empty rows.

## What a run leaves on disk

Every acquisition writes two UIMF files beside each other:

```
260910_BK_001.uimf          one frame per repetition
260910_BK_001.summed.uimf   the method frame's repetitions folded into one, Accumulations = N
```

alongside two more files a run always writes when the output directory is set:

- **`<stem>.sent.txt`**, the send log: every string this run sent, in order, with what each box
  said back, the boxes' state readback, and the conditions note, filtered down to what a trainee
  reads at the bench.
- **`<stem>-<date>.transcript.log`**, the wire transcript: every byte to and from every box and the
  acquisition console, the chunk structure a table was sent in, every ZeroMQ frame. Nothing is
  redacted or dropped from it, because nothing on either wire is secret.

Both UIMF files say how their run ended, in the global parameter `MainspringRunOutcome`:
`completed` when every repetition the method planned was acquired, `stopped` when you
pressed Stop, and `failed` when the run ended on an error or reached its end with
repetitions it did not acquire. A failed file carries the error in `MainspringRunReason`,
and every file carries `MainspringRepetitionsPlanned` and `MainspringRepetitionsAcquired`
beside the outcome, so a stopped run's twelve of fifty repetitions are stated rather than
inferred. A file reads `incomplete` from the moment it is created until its run closes it,
so a file left by a crash or a power cut says exactly that. The summed file carries the same
four values as the raw one, which is what keeps them readable where `keep_raw = false` has
removed the raw file. mainspring shows the outcome in its Info panel, and
`uimf-info <folder> --list --outcome completed,stopped` lists only the files whose runs
ended one of those ways; a file written before clockwork recorded outcomes reads `unknown`.

**Open the log**, beside the acquire buttons, opens both at once. When something goes wrong, the
send log is where to start: it is what a trainee reads, and it already carries the boxes' own
answers. Escalate with the transcript attached when the question is about the wire itself, a
timing fault, a chunk boundary, a reply that does not match what the send log shows; it is the
forensic record the send log was filtered from, so the two files of one run can never disagree
about what happened.

One further file sits outside any run. When something goes wrong inside the window itself,
the traceback is appended to `%LOCALAPPDATA%\clockwork\errors.log` and the run log says so and
names the path, because the installed clockwork opens no console window and has nowhere else to
print one. **Help > About** names the same path. Attach that file when reporting a button that
did nothing.

To report a problem, choose **Help > Report a problem...**. It opens a new bug report on the lab's
issue tracker in the browser with the version, the build commit, the PC's name, the operating
system, the method's name, and the last lines of the error log and of the current or last run's
wire transcript already filled in, so the report needs only what you did, what happened and what
you expected. The method's strings stay out: every string sent to a box or to the console is
removed from the transcript first. A transcript too long to fit is named by its path instead, to
be dragged into the report. `clockwork report` does the same from a terminal
([command line](command-line.md)).

To report a row of the run queue, right-click the row and choose **Report this run...**. The
report and its kept folder then carry that row's files, every replicate of it, together with the
row's method and its last replicate's transcript, whereas **Help > Report a problem...** takes
the run in progress or else the last one, which in a queue is usually a later row.

To ask for something clockwork should do, or do differently, choose **Help > Request a
feature...**. It opens a new feature request on the same tracker with the version, the build
commit and the PC's name already filled in, and asks what you are trying to do, how you do it
today and what would help. Nothing is copied and no log goes in. `clockwork request` does the same
from a terminal.

When a run fails, clockwork copies what the run left on disk into a folder of its own before the
failure is reported: both UIMF files, the send log and the wire transcript, which by then ends on
the `Stopped:` line giving the reason, together with the method file and the error log. The
folder sits under the kept-files root, `%LOCALAPPDATA%\clockwork\reports` unless **File > Kept
files folder...** or the `CLOCKWORK_REPORTS` environment variable names another, and the variable
takes precedence over the setting. The run log's failure line and the failed row of the run queue
both end by naming the folder. **Help > Report a problem...** makes the same copy, reusing the
failed run's folder when there is one, and the report carries the folder's report id and path, so
the files it names need not be attached. Each folder holds a `manifest.txt` giving why it was
kept, the version, the PC, the time, and the original path, size and SHA-256 hash of every file
copied. A file copied into the folder by hand is recorded the next time the folder is reported,
as `added by hand` with its own size and hash, and the rows already written are never changed,
so a kept copy that was edited afterwards still disagrees with its hash. A run ended with
**Stop** is not a failure and is not copied. Folders still named by their
bare report id are removed 90 days after they were made, when the window next starts; a folder
renamed after the report it belongs to is left alone. A failed run can therefore be deleted from
the output directory, or its stem used again, without losing anything a report points to.

## Opening the result in mainspring

**Open in mainspring** opens the last run's file: the summed companion if the run kept one, the
raw per-repetition file otherwise. mainspring's own installer registers itself for `.uimf`, and
that registration is what clockwork resolves, so a per-user install of mainspring answers the
button with nothing further to set. Where the button reports that nothing is registered for
`.uimf`, the repair is to reinstall mainspring rather than to configure clockwork. mainspring is
the only viewer clockwork carries an opinion about. Nothing here plots data.

The button is live during a run as well, from the moment that run's raw file exists. Pressed
then, it opens the file the console is filling, with mainspring following it and showing the view
the method's repetition mode asks for: the newest frame under `single_frame`, where one frame
fills for the whole run and a sum of finished frames would stay empty until the end, and the
rolling sum of the newest finished frames under `per_repetition`, where a frame finishes every few
hundred milliseconds. The tooltip says which of the two files the button will open. Carrying those
options needs the program rather than the document, so clockwork resolves the `.uimf` association
to the command registered behind it and runs that command on the file. Where no association
answers, the status bar says so and names what was tried, and the run carries on regardless.

**Open mainspring on Acquire**, the checkbox under those two buttons, does the same without the
click. It is off until you tick it, and it is remembered per machine. It opens one viewer for the
session rather than one for each run: a viewer opened this way ends with `Live` ticked, so it
moves to each later acquisition by itself and a replicate series or a run queue leaves one window
open instead of ten. Close that window and the next Acquire opens another. Note that the view is
chosen once, at launch, and rides into every run the viewer follows afterwards, so a queue mixing
the two repetition modes leaves the viewer in the view its first run asked for.

To watch a run as it is acquired without opening anything from here, leave a mainspring window
open with `Live` ticked before pressing Acquire. clockwork publishes the file it is writing when
the run starts and withdraws it when the run ends, and mainspring 1.6.0 and later read that every
two seconds, so the window moves to each acquisition of a session as it begins and says which run
it is following. No path is typed and nothing is configured. The window follows the raw per-repetition file, which is the
one that grows during a run; the summed companion is written at the end, and **Open in
mainspring** is how to reach it.
