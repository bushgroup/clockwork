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

## Replicates and naming

The **Name** field is filled automatically: one past the highest number these initials already
use in the output directory, so two people acquiring into the same folder never collide. It is
editable before Acquire starts, and the **Next** button beside it re-reads the directory on
demand, which matters if a file landed there from somewhere else since the window opened.

**Replicates** is a count of technical replicates, run unattended after the first acquisition,
each the same acquisition again into a file of its own. A
**Conditions** note, typed once, is stamped into every file a run writes and into its send log's
header. It is the one part of the record no getter can read back: sample, MCP voltage, pusher
period, collision energy, whatever the day's method does not already state as a setting.

The lone **Replicate** button beside Acquire runs one more acquisition off the last run's own
readback, for the acquisition taken after the fact rather than counted up front.

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
