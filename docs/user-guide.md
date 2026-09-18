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
refusal at install time, and what the acquisition console prints when it cannot open the card is
worth reading once before it happens for the first time on an acquisition day.

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
3. **Acquire.** Runs the first acquisition, then the method's reset list and one replicate for each
   further count in the Replicates field, each into its own file, on one worker thread so the
   window stays responsive throughout.

**Acquire is greyed out until the boxes agree with the method in the panes.** clockwork keeps a
fingerprint of what the last Send setup or Load and arm actually put on each box, covering setup,
load, arm and the acquisition's own declarations; Acquire is disabled until that fingerprint
matches what the panes now hold, with the reason named in the button's tooltip: "the boxes have
not been loaded and armed with this method" the first time, or "the panes have changed since the
boxes were armed" after an edit. This exists because a box nobody sent to answers `TBLSTRT` with a
refusal partway through a series, which used to cost a trainee three frames and an `abort_after`
before anyone noticed the setup step had been skipped.

**Stop** ends a run after its current repetition and fold rather than mid-flight, so what a stopped
run leaves on disk is a short experiment and not a broken one.

## Replicates and naming

The **Name** field is filled automatically: one past the highest number these initials already
use in the output directory, so two people acquiring into the same folder never collide. It is
editable before Acquire starts, and the **Next** button beside it re-reads the directory on
demand, which matters if a file landed there from somewhere else since the window opened.

**Replicates** is a count of technical replicates, run unattended after the first acquisition: the
method's reset list, then the same acquisition again, into a file of its own each time. A
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
  a disagreement.

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

## Opening the result in mainspring

**Open in mainspring** opens the last run's file: the summed companion if the run kept one, the
raw per-repetition file otherwise. mainspring's own installer registers itself for `.uimf`, which
is what clockwork tries first; where that registration is not in force, set mainspring's path in
this window's settings and the button uses that instead. mainspring is the only viewer clockwork
carries an opinion about. Nothing here plots data.
