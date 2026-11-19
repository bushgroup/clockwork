# User guide

This guide takes a trainee from a fresh instrument PC to a first acquisition: the two vendor
installs the driver and the acquisition console need, the clockwork installer, the window's five
tabs and the run strip beneath them, and what a run leaves on disk. It assumes the SLIM boards, the
MIPS controller boxes and the SA220P digitizer are already cabled to the machine; that work is the
instrument's own commissioning record, not this guide's.

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

The wizard offers desktop icons and, at the end, a chance to launch clockwork immediately. Both
are optional. The Start menu gets three entries whether or not the desktop icons are chosen:
clockwork itself, and **Warm up** and **Stand down**, which
[Beginning and end of a day](#beginning-and-end-of-a-day) describes.

## The window: five tabs over the run strip

The window has one tab for each way of using it, and a run strip with the run log beneath all
five, so whatever is running stays in view whichever tab is open:

| Tab | What it is for |
|---|---|
| **Template** | Set a template's knobs, see the method it renders, and acquire it, or save it as a method file |
| **Queue** | Run a list of saved methods, or a series plan an agent wrote and checked, and follow any series the instrument is running |
| **Method** | One pane per box with the strings it is sent, the acquisition settings, **Acquire** and **Send** |
| **Day** | Every request of the day, as its run record words it |
| **Setup** | Where the files go, the instrument document and limits, the boxes, the acquisition console and the kept files |

The **run strip** names the job in flight, who started it, where its files go and, for a series,
about how many minutes are left. Beside that are the **Purpose** line and the **At the window**
initials, the two pause buttons, **Resume** and **Stop**, the **Beam guard** button and the
**Note** field ([The run strip](#the-run-strip)). Under the strip, the bar counts the run in
progress and the run log holds every line clockwork has to say. The status bar says which boxes
answered and on which ports, which background service holds the instrument, and the acquisition
console's state.

The window opens on the tab it was left on, the Method tab the first time. A window opened while a
series or a queue is running opens on the Queue tab, which follows it.

## First launch: the Setup tab

Four settings in the Setup tab matter before anything can be sent to a box, and each is remembered
on the PC:

- **Output directory**, where the UIMF files, the transcript, the send log and the run records all
  land.
- **File initials.** The middle field of every file name this session writes. Two letters is
  typical; clockwork takes no view of what goes here beyond cleaning it to what a file name can
  hold.
- **Instrument document.** The file that states the machine's own m/z calibration and the
  digitizer's vertical settings, the parts of an acquisition that describe the instrument rather
  than the experiment. Without one, every file this session writes has no calibration and
  mainspring shows flight time instead of mass. The line under the field says what was read: the
  calibration, the full scale and offset in force, and whether the document states a channel
  offset at all.
- **Standing limits** and **Project limits**, the limits an agent is held to. A person at the window
  is never held to them, but the Template tab and the Queue tab show each value against them.

The instrument document and the two limits files are the **sitting**, and clockwork's background
service, `clockwork serve`, carries one sitting for every client: the window, an agent and the
command line are shown and held to the same limits. A window that starts the service hands it the
sitting in the Setup tab, and a change takes effect when the service next starts. A window that
finds the service already running shows that service's sitting instead, without the browse
buttons, and its runs carry that service's instrument document.

The rest of the Setup tab holds what is used less often. **Find boxes** asks every MIPS-class port
which box is behind it, and **Read the boxes' state** reads every box for the state panels.
**Start the console**, **Restart the console** and **Console settings…** act on the acquisition
console; the settings are the six `config.txt` keys that decide what the card records, and the
console is restarted in the same click, since it reads the file only at startup. The **Kept files
folder** and **Open mainspring when a run starts** are described under
[What a run leaves on disk](#what-a-run-leaves-on-disk) and
[Opening the result in mainspring](#opening-the-result-in-mainspring).

Everything else about a run, the boxes' strings and the acquisition settings, comes from a method
or a template, not typed by hand.

## Beginning and end of a day

**Warm up**, on the desktop or in the Start menu, is the first thing to run in the morning. It puts
the instrument's standing DC bias setpoints, RF drive levels and traveling-wave ranges on the boxes
and leaves them there, so that the RF heads and the supplies have settled by the time anything is
acquired. Stepping an RF head from cold to 50% drive and acquiring at once is the habit it
replaces. The voltages are ramped rather than stepped: five equal steps three seconds apart by
default, each printed as it is sent, about fifteen seconds in all. The window it opens ends with a
line saying whether every box holds the stack as declared, and lists any setting that does not.
Warm up loads no table and arms nothing, so the first **Acquire** of the day sends every phase
before it acquires. It also starts clockwork's background service when none is running, and the
window opened afterwards uses that service and leaves it running when the window closes. That
window asks the service for the boxes as it opens, or as soon as the service's current job ends,
so nothing needs **Find boxes** first.

The stack is whichever method the instrument document names under `[standing]`
([instrument file](instrument-file-format.md#standing)), and the method library, instrument
document and output directory are the ones this window last saved, so set those in the window once
before the first morning. Where nothing is saved, the method library and instrument document are
the [instrument directory](mcp-server.md#the-instrument-directory)'s.

**Stand down** is the last thing to run in the evening. It takes every box out of table mode, ramps
every DC bias channel, RF drive level and traveling-wave range down to zero in the same steps,
lowers every digital output, and then stops clockwork's background service and the acquisition
console with it. Its window ends by saying whether every setting reached zero and whether the
service and the console have stopped; after that, Task Manager shows neither, and the front panels
read zero. Stand down refuses to start while an acquisition is running, and names it: wait for the
run to finish, or stop it from the window. A window left open sees the service go and says so in
its status bar; it has nothing to reconnect to until the next Warm up, or until **Find boxes**
starts the service again.

Either can be interrupted with Ctrl-C in its window. The ramp stops after the step it is on, and
running the same shortcut again carries on from what the boxes then read back.

## The run strip

Every run started at the window records why it was started and who started it. Type what the run
is for in **Purpose**, which is kept as the request in the run record beside the files and may be
left empty, and your initials in **At the window**, which start out as the file initials and are
remembered on the PC. Acquire, the queue's and a plan's Start, every pause, resume, stop and note,
and every change to the beam guard need the initials, and each is recorded with them. They are
separate from the file initials because the two answer separate questions: the initials in a file
name are whatever the lab's naming puts there, often the code of the ion under study, whereas **At
the window** names a person.

The line beside the pause buttons names the job in flight, whose it is (this window, an agent
through `clockwork mcp`, the command line or another window), where its files go and, for a
series, about how many minutes remain, counted from the remaining points' planned minutes. The
controls act on that job whichever client started it.

**Stop** ends the run in flight after its current repetition and its fold, rather than mid-flight,
so what a stopped run leaves on disk is a short experiment and not a broken one.

### Pausing and resuming

To hold a series without ending it, press **Pause after this point** or **Pause after this
repetition**. Both buttons appear only while a series or a queue is running, whether you or an
agent started it. **Pause after this point** lets the point in flight finish its last file and
then rests the series before the next point, with no file open. **Pause after this repetition**
rests it as soon as the repetition in flight has ended, inside the file, which stays open. A button
that cannot be used is grey, and its tooltip says why: the last point of a series has no point
after it, and a `single_frame` method has no boundary between its repetitions, so its points offer
only the pause after the point. A single acquisition cannot be paused, and **Stop** is the only way
to end it early.

A paused series holds the instrument. The line under the controls says who paused it, or that the
[beam guard](#the-beam-guard) did, since when and where it rests, the table that follows the series
shows the point it rests at as **held**, and every sending and acquiring button stays grey, for you
and for an agent, until a person presses **Resume** or **Stop**. Nothing ends a pause on its own,
however long it lasts. **Resume** continues the series under the initials then in **At the
window**, which need not be those of the person who paused it. After a pause inside a file,
clockwork sends the method's reset sequence and checks the gate again before the next repetition,
as it does before the first repetition of any run, and marks that repetition in the file with the
length of the pause. **Stop** ends a paused series where it rests, and the run record says that it
was stopped while paused.

Closing the window while your series is paused asks whether to leave it paused or to stop it. A
series left paused stays paused in clockwork's background service until you open clockwork again
and press **Resume** or **Stop**. No command-line verb and no agent's tool pauses or resumes a run:
the background service takes those commands from the window alone, on a local connection with no
login, which is a capability withheld from the other clients rather than a lock.

### Notes

To add a remark to the record, type it in **Note** and press **Keep note**. The note is kept under
your initials in the run record of the job that is running, whichever client started it, or in the
day record when nothing is running. A note is kept with the data and may be published with it. A
note commands nothing. An agent whose run is in flight is shown the note and reports it without
acting on it, so use the buttons to pause or stop a run.

### The beam guard

To hold a run when the beam is lost, arm the beam guard. A beam lost at the source leaves every
frame counting out and every fold writing, so without the guard each remaining row reads **done**
and the loss is found only in the files. With the guard armed, every repetition counts its ions,
i.e., the sum of its scans' TIC as the console publishes them, and a file whose repetitions read
under a floor several times in a row ends after the current repetition and its fold. The file
says `failed`, with the guard's sentence as the reason, and keeps every repetition acquired
before it ended.

The guard belongs to `clockwork serve`, not to a tab or to the window, so it watches every
acquisition the service makes, whichever client started it: a queue, a series plan, an agent's
series or a single **Acquire**. In a queue or any series, a file the guard ends then holds the
series, paused by the guard, at the row or point it ended, and the run strip says so. The series
waits there until a person presses **Resume** or **Stop**. **Resume** acquires that row or point
again from its start, as new files under the same planned index and at the next positions, and
then goes on. The ended files stay on disk, stamped `failed`, and the run record lists them under
`retakes` as the files the retake replaced. An agent's tools report the guard's pause as they
report a person's and cannot lift it. A single acquisition has nothing to hold, so a file the guard
ends there simply ends the acquisition.

To arm the guard, press **Beam guard: off…** in the run strip, tick the box, and set the floor in
total ions a repetition. **From a run…** reads a run's per-repetition file, usually the day's gate
run on a beam known to be good, and sets the floor to the fraction beside it of that run's median,
0.10 by default; a number typed into the field replaces it. **In a row** is how many consecutive
repetitions under the floor end a file, 5 by default. The button is never greyed by a running job:
the guard can be armed, changed or turned off on any client's run at any time, and takes effect
from the next repetition. Each change is recorded with your initials, in the run record of the job
running or in the day record when nothing runs. The button then reads **Beam guard: armed…**, in
every window that follows the service, and the guard stays armed until a person turns it off or
the service stops. It is not saved in a queue file, because the floor is a reading of the day's
beam.

The defaults come from the 336 runs of one queued sweep, on which the beam was lost twice. At
0.10 of the gate run's median and 5 in a row, both losses ended the row they began in, on its
5th and 9th repetition without beam, whereas in none of the 277 runs acquired on a good beam did
5 consecutive repetitions all read under 0.34 of their gate run's median. Note that the count
starts again with each file, so a row with fewer repetitions than the count cannot trip it, and a
row whose repetitions gather ions far longer than the gate run's, such as a 40 s hold, can read
above the floor with no beam at all. In both cases the next ordinary row trips the guard.

## The Method tab

### The panes and what the tags mean

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
load. A method opens through **File > Open method…** or the method library (**File > Method
library…**). Each pane's header names the port the box answered on and its firmware, from the last
**Find boxes**, and keeps them when another method is opened. Where the method names more boxes
than fit, the panes scroll sideways.

### Acquire and Send

To acquire the method in the panes, press **Acquire**. clockwork's background service remembers
what it last sent the boxes, whichever window, agent or command line sent it, and Acquire sends
only what they do not already hold before it acquires:

- On boxes nothing is known to hold, every phase for every box, in the method's order, with a
  readback of what each box was holding before, after setup and once armed, then the acquisition.
- On boxes that hold another method, the table and the mode change for every box, with the setup
  phase only where it differs from the setup they were last sent, then the acquisition.
- On boxes that hold this method, nothing, and the first file is a replicate: the method's reset
  list, then the same acquisition again, exactly as a technical replicate is acquired.

**Replicates** beside the button is the number of files one press acquires, the first and its
technical replicates, run unattended, each into a file of its own. Every run walks the method's
reset list and puts the digitizer's enable down before its first frame, which costs a couple of
seconds and makes a second press of Acquire as good as the first.

**Send** is the same send with nothing acquired, for boxes that should hold the method before
anything is acquired, such as to read their state panels first. On boxes that already hold the
panes it sends nothing and says so in the run log.

The service compares what it sent with what the panes would send now, and a box that was switched
off and on since is not something it can know. To send every box its setup phase anyway, tick
**send setup again** before pressing Acquire or Send; the tick clears itself once sent.

The line above the panes says which of these cases applies before you press anything: "The panes
are what the boxes hold", "The boxes hold *name*, not the panes", or "Nothing is known to be on the
boxes since clockwork serve started". Acquire is grey only for a reason that would refuse the run
regardless, such as an instrument document with no channel offset, a table the acquisition loop
refuses, or another program holding the instrument, and the reason is listed under the buttons.

**A run whose pusher is not running at the method's period is refused before its first frame.**
Before each run's first frame the run log gives the pusher period the digitizer measured beside the
one the method was written for, which is the template's tick for a rendered method and the
instrument file's `pusher_period_us` for one written by hand. A method's tables count pushes, so
behind a pusher at 62 µs a method written for 129 µs runs every event at 0.48 of its time. Beyond
a 10% difference the run stops there with the reason named and leaves no data file; beyond 2% it
runs and the line is shown as a warning. Either means the time-of-flight's pusher setting has
changed, so ask whoever set up the instrument that day before acquiring again.

### The state panel

To see what a box is actually holding, open the disclosure arrow under its pane. The state panel
shows what the box answered when it was last read, marked against what the boxes were **last
sent**, whoever sent it, so editing the panes does not change the marks; the line above the panes
says whether the panes and that send agree. Every send fills the panels from its own readback, and
**Read** on a panel, or **Read the boxes' state** in the Setup tab, reads again. A few things the
panel reports are worth knowing before the first time they appear:

- A setting the last send did not declare is marked **left as found**, not flagged as missing. A
  clean, correctly-set-up box shows this for every ARB module setting a method does not bother
  naming; it is what told two otherwise-identical files apart once.
- A DC bias or RF setting the box disagrees with what was sent is marked, with both numbers, and
  refuses nothing. Something moved it after the send, at the front panel or from another session.
- **DC bias monitors freeze the moment a box enters table mode.** A reading taken while armed is
  reported as not converting rather than compared against anything, because a frozen monitor is not
  a disagreement. The panel keeps the last reading whose monitors were live and shows those
  figures on the rows instead, with the time they were taken, so reading the state during a run
  does not cost you the only measurement of those outputs you have.
- **An ARB module's waveform frequency reads back lower than you set it, and that is correct.**
  The module's output clock is an integer divider off a 42 MHz master clock, so it can only make
  the frequencies that divider produces. `SWFREQ,n,15000` is acknowledged and reads back as
  **14914 Hz**, on every module of both ARB boxes. The panel marks the row as declared and says
  which frequency the divider reached. A row marked as differing means the module is holding a
  frequency no request of yours would produce.

A box nothing has been read off yet says so plainly instead of showing empty rows.

### Naming and conditions

The **Name** field is filled automatically: one past the highest number the file initials already
use in the output directory, so two people acquiring into the same folder never collide. It is
editable before Acquire starts, and the **Next** button beside it re-reads the directory on
demand, which matters if a file landed there from somewhere else since the window opened. Each
replicate takes the next number without asking.

A number is used up by any file written under it, the send log and the transcript as much as the
UIMF file, so a run that failed before its UIMF file existed still moves the counter on. The field
moves to the next free name whenever a send or an acquisition fails. A name that is already taken
is refused before any string goes to a box, and the refusal names the next free one: typing a name
by hand and leaving a stale one in the field get the same answer, with no file overwritten and
nothing sent. The name a Send has just used is the one exception, since the acquisition that
follows it continues that send's log under the same name.

A **Conditions** note, typed once, is stamped into every file a run writes and into its send log's
header. It is the one part of the record no getter can read back: sample, MCP voltage, pusher
period, collision energy, whatever the day's method does not already state as a setting.

### A method rendered from a template

A method file can carry the template it was rendered from, the knob values and the labels,
embedded at its foot. Such a file is written by **Save as a method** in the Template tab, by an
agent asked for one, or by `render-template` with `--to`:

```
clockwork render-template --template bradykinin-clock/template.toml --knobs duration_ms=25 --labels sample=bradykinin --to D:\data\methods\25ms.toml
```

Give `--to` the full path of the file to write. A relative one is refused when the library is the
instrument's own, which keeps a rendered file out of the folder every user shares, and an existing
file is replaced only with `--overwrite`.

A rendered file opens in the Method tab like any other method, into the same panes, and can be
sent, acquired and put in the run queue. The line above the panes names where it came from, for
example *Rendered from bradykinin-clock at duration_ms 25 ms*, and every file acquired from it
records each knob, label and mark as its own parameter, so a table of a week's runs can be sorted
by hold time without anyone reading the strings.

This holds while the method is **attached**, which is for as long as what the panes would send is
still what the template renders at those values. The first edit that changes that, a table tick,
a setup line, a comment or the number of frames, **detaches** it. The run log says so once, the
line above the panes goes away, and from then on the method runs and saves as an ordinary
hand-written one, whose files record no knobs. Nothing is refused either way. Reopening the file
attaches it again, as long as the file itself is unedited. A file whose strings were edited in a
text editor after it was rendered opens detached, with one warning in the run log saying why.
Changing the name, the output directory or the stem does not detach a method, and neither does a
box answering on a different COM port, since none of those changes what a box is sent.

### The method library

**File > Method library…** lists every method and template a folder holds, named, hashed, dated
and described, with a template's knobs and ranges. **Open** puts a method into the Method tab and
a template into the Template tab. **Diff two methods…** compares two selected methods box by box
and phase by phase, with the acquisition settings and the declared tables side by side. A method
is compared with what the boxes hold by the state panels instead.

## The Template tab

A template is a method with holes in its strings and named knobs that fill them, such as a hold
time or a guard voltage ([`template-file-format.md`](template-file-format.md)). To acquire one at
values of your choosing, pick it in the Template tab, set each knob and fill in the labels. The
right of the tab shows the method the template renders at those values, and a second tab beside
it shows what differs from the template at its defaults. A required label left empty renders
nothing, and the line under the tab says which.

Each knob's field holds only the values the template's own range allows. Under the field, the
knob is shown against two ranges: the template's, and the agent's, from the limits in the
sitting, for example `template 100 to 520 ticks; the agent 200 to 450 ticks`. Where the sitting
carries no limits, each knob is shown against the template's range alone and the line above the
knobs says so; where the limits do not admit the template at all, the line says that an agent
could not run it at any setting. You are not held to the agent's range. A value outside it is
marked in the warning colour and the run goes ahead when Acquire is pressed. Its run record lists
each knob that lay outside, in the sentence an agent would have been refused with, and that list is
how a reader tells a run only a person was free to make from one an agent made.

**Acquire** in the Template tab acquires the render as it stands, with no method file on disk.
The boxes are sent what they do not already hold, as for Acquire in the Method tab, and every file
records the template's hash, each knob value and each label, as a run an agent acquires from the
template does. The run record keeps the template's hash, the knob values and the hash of the
method acquired. **Replicates** and **Conditions** in the tab apply to its own runs; the file name
is the next free one, as in the Method tab.

**Save as a method…** writes the render as a method file carrying the template and the knob
values, byte for byte the file `clockwork render-template --to` writes. **Open in the Method
tab…** writes the same file and opens it in the Method tab, attached, for a change the template
does not offer as a knob: edit a pane and the method detaches, as above.

The tab renders a template and cannot edit one. Anyone may edit a template, in a text editor, and
check it with `clockwork validate-method --template PATH`. Note that an edited template has a new
hash, and the instrument's standing limits list templates by hash, so an agent cannot run an
edited template until its entry in the limits is updated, whereas the window runs it regardless.

## The Queue tab

The Queue tab has two kinds of entry, **Rows**, a list of saved methods run one after another,
and **Series plan**, a [series plan](series-file-format.md) over a template's knobs, such as one
an agent wrote and checked. Under both, a table follows whatever series the instrument is running,
whichever client started it.

### The run queue

To run a series of samples or conditions unattended, add a row for each method document in
**Rows**. A row names a saved method, a conditions note and a replicate count. The table and the
run log call a row by its document's file name, or by the document's folder when the file is
`method.toml`, as every document in the method library is.

**Start the queue** reads every row's document, records the SHA-256 of each one's text, and hands
the whole queue to `clockwork serve` as one series, in the same way as a series plan. What runs is
therefore what the files said at the moment Start was pressed, and a document edited afterwards
changes nothing in the queue that is running. A row whose document does not open, or that
clockwork would refuse to acquire, keeps the queue from starting, and its outcome says why. Each
row is sent where the boxes do not already hold it, its `setup` strings only where they differ
from the ones last sent, and then its replicates are acquired back to back. Start needs your
initials in **At the window** and the file initials, and fills nothing else in for you: the
**Purpose** line is recorded as typed, empty included.

The queue runs on if the window is closed, and a window opened later follows it as it follows any
series. One Start is one request with one run record, `source` `window`, whose `plans` entry holds
the queue as started: the text of its queue file, each row's document and that document's
SHA-256, and the hash its files stamp ([run record](run-record.md#the-sections)). Every file the
queue writes stamps the request's identifier, its row's place in the queue as its planned index,
its position in the order acquired, and the seed for a randomized queue, as every file of a series
plan does.

While a queue runs, its rows are not edited, moved or added to. **Stop** ends it after the current
repetition and its fold: the row in flight keeps the files it has written, and the rows after it
are **skipped**. Pressing **Start the queue** again runs the skipped rows, after any edits, as a new
queue whose run record names the first one under `follows`. A row that fails ends the queue in the
same way, as a failed point ends a series plan, and the rows after it are skipped, because the next
row usually depends on the same instrument state that the failed one did not have. The pause
buttons, the note and **Stop** in the run strip act on a running queue exactly as they do on a
series plan.

To spread technical replicates over a series rather than acquire them back to back, press
**Randomize…**, choose a number of passes, and press OK. Each pass runs every waiting row once,
in its own shuffled order, and a row's own replicates stay together. The result is ordinary
rows, labelled in the **pass** column (`2/3` is the second of three passes), so the table shows
exactly what will run before **Start** is pressed, and any row can still be edited, moved or
removed. With **new order each pass** unticked, one shuffled order is repeated in every pass, and
one pass is a plain shuffle. Rows ticked **stays first** open every pass, in the order they were
entered, ahead of the shuffled rows, which suits a blank or a calibrant. A closing wash is added
by hand after randomizing. The run log records the seed and every pass's order each time
Randomize is applied, and a queue started with its passes stamps the seed into every file and its
run record.

Randomizing a queue that already has passes rebuilds them from pass 1, after asking. An edit made
to a row of pass 2 or later is lost; to change every pass, edit the row in pass 1 and randomize
again. A row added by hand after randomizing joins every new pass. Rows that have already run stay
where they are, above the new passes. Randomize is unavailable while the queue runs.

To keep a series for another day, press **Save queue…**. The file, which ends in `.queue.toml`,
holds each row's method, note, replicate count, **stays first** box and pass, and the last seed.
**Open queue…**, or **File > Open a run queue…**, replaces the queue with a saved one. Every row
opens waiting, because the file is a plan rather than a record; what the rows did is in the run
record and their files. Each method is stored by its full path and by its path relative to the
queue file, so a folder of methods copied to another PC with its queue file beside it still opens.
A queue file saved by a clockwork before 1.2.0 opens too, and the run log says that its **setup**
and **go on if it fails** values are ignored: the service sends `setup` only where it differs, and
a failure always ends the queue. A file saved now omits both, which an earlier clockwork reads as
**setup** ticked and **go on if it fails** unticked.

### Running a series plan

To run a plan, open the **Series plan** entry and press **Open plan…**, or choose **File > Open a
series plan…**. The table lists every point in planned order with its knob values, its
repetitions, its files and about how many minutes it takes, with the totals beneath, and is the
answer the agent's `series` tool gives to `check` for the same plan. The line under the totals
says whether an agent checked this same text: "checked through mcp at 14:02, unchanged since"
means a check of a plan with exactly this text is in the output folder's audit log, whereas "no
check of this text on record" means there is none, either because nobody checked it or because the
file has been edited since. Either way the plan can be run.

You are not held to the limits an agent is held to. A point outside them is marked in the
**limits** column with the sentence an agent would have been refused with, the line beneath says
how many there are, and Start runs them; the run record lists each one. A point the template's own
ranges or the acquisition loop refuse, such as a hold longer than the template allows, is marked
**refused** and keeps **Start the plan** grey until the plan is changed.

**Start the plan** needs your initials in **At the window**, and fills **Purpose** with the plan's
description unless you have typed a purpose of your own. The plan is read again from its file at
Start, and a file changed since it was opened is shown again rather than run, so that what runs is
what the table showed. The series then runs as one job: each point is sent and acquired in the
order its seed draws, and every file records its planned index, its position and the seed, as for
a series an agent starts. **Stop** ends it after the current repetition and its fold.

### Following a series

The table under the two entries follows any series, started here or by an agent: each point's
state, in the order acquired, with the point in flight in bold, how many of its files are done,
its minutes and how many warnings the run log gave while it ran. The line above it says who started
the series, the point in flight with its knob values and which of its files is being acquired, and
about how many minutes are left. Only a point that **failed**, was **stopped**, was **skipped** or
is **held** is coloured. Tick **Show only this series in the run log** to hide every other job's
lines, and untick it to see them again.

## The Day tab

To see what the day held, open the Day tab. It lists every request opened on the chosen day in the
output directory, read from the run records beside the files, in the order they were opened, and
the lines of the day record from the same day, such as a console restart or a note typed with
nothing running. The jobs the background service is running or holding in its queue follow, marked
**now**. Each request shows its time, its route (`window`, `mcp` or `cli`), the person at the
window or the file initials, its purpose, its files and how it ended: how many files say they are
complete or stopped early, a stop and who made it, a pause still standing, and the service's last
note, such as the sentence a failure ended on. A routine's result and a person's newest verdict on
each file are shown as the run record words them. The tab draws only what the records hold, and
judges nothing itself. Only a request that failed, was stopped or is held is coloured.

The tab reads the folder again when it is opened, when a job ends while it is open, and when
**Refresh** is pressed. Pick another date to read an earlier day of the same folder.

## What the progress bar counts, and the wait at the end

The bar counts **scans across the whole run**, not repetitions, and it advances about fifteen
times a second while the digitizer is publishing. The caption above the log counts repetitions,
which is the number to match against the method. Both matter because the two golden methods are
shaped differently: a per-repetition method asks the console for one frame per accumulation and
the caption steps through them, while a single-frame method puts its accumulations inside the
sequencer's own table and asks for one frame of half a million scans. In the second case the
caption reads `repetition 1 of 1` for the whole minute the frame takes, and the bar is the only
thing that moves.

**Copy**, beside Clear above the log, puts every line on the clipboard as text, so what happened on
this machine can be pasted into a message rather than described. Collapsed groups are copied open
and a warning is marked with a leading `!`, since text carries no colour. The log itself is not
written to a file: the send log and the transcript beside the data are the record of a run, and the
log in the window is clockwork's own account of it.

No estimate of a single run's remaining time is shown. The cost of a repetition depends on how much
of the detector's signal survives zero suppression, so the first repetition does not predict the
hundredth.

**A run is not over when its last repetition ends.** The repetitions are summed into the
`.summed.uimf` companion afterwards, and the run log says so before it starts: "summing 100
repetitions (1,310 MB) into the companion, about a minute; the run log is quiet until it is
done". Nothing is printed while it runs. The estimate is coarse, but the order of magnitude is
right: a beam-on detection-response run of that size takes about a minute to fold, and a larger
file takes proportionally longer. Do not close the window or kill the process during it. The
per-repetition raw file is complete on disk by then, and the companion is what a force-quit would
lose.

## A repetition that comes up short

A repetition is short when the console says it has finished but fewer scans reach clockwork than
the method asked for, and the stream then stays quiet for three seconds. The console does this when
it loses the first batches of a frame, and the raw frame it leaves behind holds rows that belong to
no real scan, some with a base-peak m/z far outside any mass range. Rather than keep that frame,
clockwork acquires the repetition again, once, into the same frame of the raw file, after deleting
every row the short attempt left there. The run log shows the short frame and then a line such as
`frame 1.97: 8500 of 10000 scans, acquired again`, and the raw file ends the run with exactly one
frame per repetition. The wire transcript is the only record of the attempt that was discarded.

The same treatment applies to a repetition whose trigger timestamps start inside the previous
repetition's. The digitizer's clock runs on from one frame to the next, so a frame that starts
behind it is the previous frame's triggers read a second time, and its spectra are not this
repetition's. Every such frame seen so far also came up short, but one that counted out would
otherwise have been summed as a second copy of the repetition before it. Its line in the run log
ends `the previous frame's triggers read again`.

A repetition that comes up short a second time ends the run after it, as **Stop** does, but as a
failure: the run's files say `failed` with the reason. The repetitions before it are summed into
the companion and that one is left out of the sum; it stays in the raw file, marked unfinished.
In the run queue the row is **failed** with the same reason, and the queue ends there. Every
other reason clockwork has to end a run early, such as three frames failing in a row, is a failed
row in the same way, and only **Stop** makes a row **stopped**.

A row of the run queue counts both events in its outcome: `2 repetition(s) acquired again` for the
repetitions that came up short, and `console reported 3 errors` for the `[error]` lines the
acquisition console logged while the row's runs were acquiring. Neither makes the row's files
unusable. A retried repetition is whole, and a run ended by a second short end is a short
experiment rather than a broken one.

## What a run leaves on disk

Every acquisition writes two UIMF files beside each other:

```
260910_BK_001.uimf          one frame per repetition, Accumulations = 1
260910_BK_001.summed.uimf   the method frame's repetitions folded into one, Accumulations = N
```

The first file holds single pushes: each of its scans is one pusher pulse, and a push that
crossed the zero-suppress threshold nowhere stores no scan at all. The summed file holds, per
scan, the sum over the method's N accumulations, which is the shape of a file FALKOR wrote. A
single-frame method's raw file holds one frame per method frame instead, its repetitions one after
another in the frame's scans. These are written alongside two more files a run always writes when
the output directory is set:

- **`<stem>.sent.txt`**, the send log: every string this run sent, in order, with what each box
  said back, the boxes' state readback, and the conditions note, filtered down to what a trainee
  reads at the bench. A run whose first file was a replicate says where the strings it did not
  send again went.
- **`<stem>-<date>.transcript.log`**, the wire transcript: every byte to and from every box and the
  acquisition console, the chunk structure a table was sent in, every ZeroMQ frame. Nothing is
  redacted or dropped from it, because nothing on either wire is secret.

Both UIMF files say how their run ended, in the global parameter `MainspringRunOutcome`:
`completed` when every repetition the method planned was acquired, `stopped` when you pressed
Stop, and `failed` when the run ended on an error or reached its end with repetitions it did not
acquire. A failed file carries the error in `MainspringRunReason`, and every file carries
`MainspringRepetitionsPlanned` and `MainspringRepetitionsAcquired` beside the outcome, so a stopped
run's twelve of fifty repetitions are stated rather than inferred. A file reads `incomplete` from
the moment it is created until its run closes it, so a file left by a crash or a power cut says
exactly that. The summed file carries the same four values as the raw one, which is what keeps
them readable where `keep_raw = false` has removed the raw file. mainspring shows the outcome in its
Info panel, and `uimf-info <folder> --list --outcome completed,stopped` lists only the files whose
runs ended one of those ways; a file written before clockwork recorded outcomes reads `unknown`.

Every acquisition also leaves a run record, `<stem>.request.json`, named after its first file. It
says what the run was for, in the words typed in **Purpose**, and who started it, from **At the
window**; with the file initials, the method's hash, for a method rendered from a template the
template's hash and every knob value, and an entry for each file with its frame count, total
counts, base peak and pusher period. One press of Acquire, in the Method tab or the Template tab,
is one record however many replicates it asks for, and so is one Start of a series plan or of the
run queue, however many points or rows it has. The record is written by `clockwork serve` as the
run proceeds, so it is whole even if the window is closed while the run goes on, and where a
template's knob value lies outside the limits an agent is held to, the record says so. An agent's
run leaves the same document, which [the run record](run-record.md) describes in full. The output
directory also holds `day-record.jsonl`, the service's record of the day: the instrument document
and limits it was started with, each start and restart of the service and the acquisition console,
and each note and change to the beam guard made while nothing was running. The Day tab reads both.

**Open the log**, under Acquire, opens the send log and the transcript at once. When something goes
wrong, the send log is where to start: it is what a trainee reads, and it already carries the
boxes' own answers. Escalate with the transcript attached when the question is about the wire
itself, a timing fault, a chunk boundary, a reply that does not match what the send log shows; it
is the forensic record the send log was filtered from, so the two files of one run can never
disagree about what happened.

One further file sits outside any run. When something goes wrong inside the window itself, the
traceback is appended to `%LOCALAPPDATA%\clockwork\errors.log` and the run log says so and names
the path, because the installed clockwork opens no console window and has nowhere else to print
one. **Help > About** names the same path. Attach that file when reporting a button that did
nothing.

To report a problem, choose **Help > Report a problem...**. It opens a new bug report on the lab's
issue tracker in the browser with the version, the build commit, the PC's name, the operating
system, the method's name, and the last lines of the error log and of the current or last run's
wire transcript already filled in, so the report needs only what you did, what happened and what
you expected. The method's strings stay out: every string sent to a box or to the console is
removed from the transcript first. A transcript too long to fit is named by its path instead, to be
dragged into the report. `clockwork report` does the same from a terminal
([command line](command-line.md)), and there it also names the research project whose data folder
the run was written to; the window's report names none.

To report a row of the run queue, right-click the row and choose **Report this run...**. The report
and its kept folder then carry that row's files, every replicate of it, together with the row's
method and its last replicate's transcript, whereas **Help > Report a problem...** takes the run in
progress or else the last one, which in a queue is usually a later row.

To ask for something clockwork should do, or do differently, choose **Help > Request a
feature...**. It opens a new feature request on the same tracker with the version, the build commit
and the PC's name already filled in, and asks what you are trying to do, how you do it today and
what would help. Nothing is copied and no log goes in. `clockwork request` does the same from a
terminal.

When a run fails, clockwork copies what the run left on disk into a folder of its own before the
failure is reported: both UIMF files, the send log and the wire transcript, which by then ends on
the `Stopped:` line giving the reason, together with the method file and the error log. The folder
sits under the kept-files root, `%LOCALAPPDATA%\clockwork\reports` unless the **Kept files folder**
in the Setup tab or the `CLOCKWORK_REPORTS` environment variable names another, and the variable
takes precedence over the setting. A folder chosen there takes effect when clockwork's background
service next starts. The run log's failure line and the failed row of the run queue both end by
naming the folder. **Help > Report a problem...** makes the same copy, reusing the failed run's
folder when there is one, and the report carries the folder's report id and path, so the files it
names need not be attached. Each folder holds a `manifest.txt` giving why it was kept, the version,
the PC, the time, and the original path, size and SHA-256 hash of every file copied. A file copied
into the folder by hand is recorded the next time the folder is reported, as `added by hand` with
its own size and hash, and the rows already written are never changed, so a kept copy that was
edited afterwards still disagrees with its hash. A run ended with **Stop** is not a failure and is
not copied. Folders still named by their bare report id are removed 90 days after they were made,
when the window next starts; a folder renamed after the report it belongs to is left alone. A
failed run can therefore be deleted from the output directory, or its stem used again, without
losing anything a report points to.

## Opening the result in mainspring

**Open in mainspring**, under Acquire, opens the last run's file: the summed companion if the run
kept one, the raw per-repetition file otherwise. mainspring's own installer registers itself for
`.uimf`, and that registration is what clockwork resolves, so a per-user install of mainspring
answers the button with nothing further to set. Where the button reports that nothing is registered
for `.uimf`, the repair is to reinstall mainspring rather than to configure clockwork. mainspring is
the only viewer clockwork carries an opinion about. Nothing here plots data.

The button is live during a run as well, from the moment that run's raw file exists. Pressed then,
it opens the file the console is filling, with mainspring following it and showing the view the
method's repetition mode asks for: the newest frame under `single_frame`, where one frame fills for
the whole run and a sum of finished frames would stay empty until the end, and the rolling sum of
the newest finished frames under `per_repetition`, where a frame finishes every few hundred
milliseconds. The tooltip says which of the two files the button will open. Carrying those options
needs the program rather than the document, so clockwork resolves the `.uimf` association to the
command registered behind it and runs that command on the file. Where no association answers, the
status bar says so and names what was tried, and the run carries on regardless.

**Open mainspring when a run starts**, in the Setup tab, does the same without the click, for any
client's run: one you start, an agent's series or another window's queue. It is off until you tick
it, and it is remembered per machine. It opens one viewer for the session rather than one for each
run: a viewer opened this way ends with `Live` ticked, so it moves to each later run by itself and
a replicate series or a run queue leaves one window open instead of ten. Close that window and the
next run opens another. Note that the view is chosen once, at launch, and rides into every run the
viewer follows afterwards, so a day mixing the two repetition modes leaves the viewer in the view
its first run asked for; switching it is mainspring's to do.

To watch a run as it is acquired without opening anything from here, leave a mainspring window
open with `Live` ticked before the run starts. clockwork publishes the file it is writing when the
run starts and withdraws it when the run ends, and mainspring 1.6.0 and later read that every two
seconds, so the window moves to each acquisition of a session as it begins and says which run it is
following. No path is typed and nothing is configured. The window follows the raw per-repetition
file, which is the one that grows during a run; the summed companion is written at the end, and
**Open in mainspring** is how to reach it.
