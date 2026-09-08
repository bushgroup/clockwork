# MIPS wire format: pulse-sequence Tables, timing/trigger model, Twave, and ARB

Single source of truth for how Clockwork talks to MIPS boxes. Both
the `clockwork` package must conform to this file
rather than encoding protocol knowledge locally. Scope: serial framing,
the Table (pulse sequence) wire format and execution model,
trigger/clock semantics, the Twave command set, and the ARB module
(§6; on this instrument the ARB modules, not Twave, drive all TW
regions). Remaining module-specific command sets (DCbias, RF driver,
DIO beyond what tables need) get added as needed.

## Provenance

Derived by reading the MIPS firmware source, cross-checked against GAA
Custom Electronics vendor documents, inventoried in the lab's vendor-document manifest:

- **Firmware:** `github.com/GordonAnderson/MIPS`, commit `bd32aae`
  (2026-06-30), firmware version string `1.263, June 20, 2026`.
  Key files: `src/Table.cpp`, `include/Table.h`, `src/Serial.cpp`,
  `src/Twave.cpp`, `src/Compressor.cpp`, `src/DIO.cpp`,
  `src/ARB.cpp`, `src/ARBcompressor.cpp`, `include/ARB.h`,
  `MIPScommands.txt`.
- **ARB module firmware:** `github.com/GordonAnderson/ARB`, commit
  `1e8a149` (2024-12-07), version 1.24: the ARB module's own on-board
  firmware (the other end of the controller↔module link). Note the
  caveat in §6.4: this clone predates the alternate-waveform feature.
- **Vendor docs:** primarily *Pulse sequence generation*
  (`Pulse_Sequence_Generation.pdf`), which matches the firmware on every
  overlapping fact except two nuances flagged inline below; for §6,
  *ARB Operation Manual Addendum* (`ARB_Module.pdf`, v4.0, Sept 2021)
  and *TWAVE reverser* (`SLIM_Reverser.pdf`, v4.0).

**Repo question resolved:** both `GordonAnderson/MIPS-Arduino` and
`GordonAnderson/MIPS` exist. They are the same codebase; `MIPS` is the
current, canonical one, a PlatformIO restructure (`src/` + `include/`)
that is more recently maintained (v1.263 vs. MIPS-Arduino's last commit
2026-04-10) and matches the version the vendor Operations Manual is
written against (v1.262+). `MIPS-Arduino` is the older flat Arduino-IDE
sketch layout. Cite and read `MIPS`.

Hardware context: Arduino Due (Atmel SAM3X8E, 84 MHz Cortex-M3,
`VARIANT_MCK = 84 MHz`). The table engine uses one timer-counter channel
(`TMR_Table`, TC2 channel 2) with compare registers RA and RC.

---

## 1. Serial link and framing

### Transport

USB (native port), hardware serial, Ethernet, or Wi-Fi; the firmware
auto-detects the active port. All transports carry the same ASCII
command protocol.

### Command framing

- Commands are ASCII: `NAME` or `NAME,arg1,arg2,...` terminated by
  newline (`\n`).
- The tokenizer treats `\n ; : , ] [ /` as delimiters
  (`Serial.cpp: GetToken()`), so those characters cannot appear inside
  arguments.
- `STBLDAT` is special: its payload is terminated by a **semicolon**,
  not the newline, and the parser pulls tokens with a **3-second
  inter-token timeout** (`Table.cpp: NextToken()`). The whole table
  string must therefore stream in without multi-second stalls, or the
  load fails with a token-timeout error.

### The 4096-byte input ring buffer

Received characters go into a fixed 4096-byte ring buffer
(`Serial.h: RB_BUF_SIZE`) before the tokenizer sees them. Two properties
of it constrain how a host may send a long table:

- **Overflow is silent.** `RB_Put()` returns a full-buffer indication
  that `PutCh()` discards, so once the buffer is full every further
  character is dropped without any response. A table string truncated
  this way does not NAK cleanly; it fails somewhere downstream of the
  loss, or parses into a table that is not the one that was sent.
- **The buffer drains a token at a time but fills all at once.** Each
  pass of `NextToken()` consumes one token and then calls
  `ReadAllSerial()`, which moves *everything* the USB stack has waiting
  into the ring buffer. A host writing a long table in one call can
  therefore run the buffer up faster than the parser empties it.

So the two failure modes pull in opposite directions: sending too slowly
trips the 3-second token timeout, and sending too fast overruns 4096
bytes. Bench measurement settles where the boundary lies (lab record,
task 04). Written as fast as the host's USB stack accepts it, a table
string survives to 4090 bytes and fails at 4108, which is the 4096-byte
buffer rather than any limit on table size. Pausing 10 ms between
256-byte writes carried 8572 bytes and 17572 bytes, both verified byte
for byte through `TBLRPT`, whereas the same 8572-byte table written with
no pause failed. The USB CDC link therefore offers the box no flow
control: a host that writes a long table at full speed loses the tail of
it silently.

**An overrun does not merely fail the load, it stops the interface.**
Written with no pause, an 8573-byte table drew no ACK, no NAK and no
output of any kind, and the box then ignored `GVER` for 73 seconds on
the same connection (lab record, task 04). Waiting is not the recovery.
Resetting the box's USB port is: dropping DTR after it has been asserted
makes `USBportTest()` call `SerialPortReset()`, the device re-enumerates
in about 0.4 s, and the box answers again with `GERR` reporting 8, the
token timeout, as the record of the load that failed. Closing the host
port does exactly that, so `SerialTransport.reset_link()` is the way
back. Note that the same mechanism fires on any ordinary close, so a
host that opens and closes the port around each operation makes the box
re-enumerate every time; open once and keep it.

A sender must pace deliberately, and the pause can be small. Delivery
rates from 3.1 kB/s to 39.7 kB/s all loaded an 8572-byte table
correctly, so only writing with no pause at all fails, and a 10 ms pause
against a 3-second token timeout spends three parts in a thousand of the
margin. Note that both numbers depend on how fast the host's USB stack
delivers relative to how fast the parser tokenizes, so a box on
different firmware or a host with a different USB stack wants the
measurement repeated. `clockwork.mips.DEFAULT_CHUNK_BYTES` and
`DEFAULT_CHUNK_GAP_S` carry the values measured here.

### Responses

Byte-exact, from the `SendACK` / `SendACKonly` / `SendNAK` macros
(`include/Serial.h`) and the `CMDstr` / `CMDint` arms of `ExecuteCommand`
(`src/Serial.cpp`). Note that the set-style ACK's terminator is **LF then
CR**, while every value or status line the firmware prints goes through
Arduino `Print::println` and so ends **CR then LF**:

| Response | Bytes | Meaning |
|---|---|---|
| ACK | `06 0A 0D` (`\x06\n\r`) | Command accepted (set-style commands) |
| ACK + value | `06`, then the value, then `0D 0A` | Get-style commands: a bare ACK byte with no terminator of its own, then the value as a `println` line |
| NAK | `15 3F 0A 0D` (`\x15?\n\r`) | Command rejected; query `GERR` for the last error code |

- The `?` is part of the NAK sequence, not a separate error line. The
  firmware has no other `?`-prefixed error form on the table path (the
  lone `println("?")` in `src/ARB.cpp` is an unrelated ARB query reply).
- `MUTE,ON` suppresses all responses; `ECHO,TRUE` switches the ACK-only
  string from `\x06` to `,\x06` (echo mode). The sequencer should leave
  both at defaults and treat `0x06`/`0x15` as the accept/reject bytes.
- A host tokenizer that drops every `\r`, splits the rest on `\n`, treats
  `0x06` and `0x15` as standalone tokens and discards empty lines reduces
  all of the terminator conventions above (and the doubled ones in the
  next section) to a single stream of ACK / NAK / text tokens. That is
  the only framing a sequencer needs.
- **Command termination:** the input ring buffer maps `\r` to `\n`
  (`Serial.cpp: RB_Get()`), and a stray `\n` in the command state is
  skipped, so `\n`, `\r` and `\r\n` are all accepted terminators.
- **Two-pass dispatch:** a command and its arguments are consumed on one
  call of `ProcessCommand()` and the function itself runs on the next,
  when the terminator token arrives. This is why `STBLDAT`'s payload
  parser (§2) starts reading at the token *after* the first `;`: that
  semicolon is what triggered the dispatch.

### Error codes

`GERR` returns the last error as an integer and **never clears it**, so
it is only meaningful read immediately after a NAK. Codes are defined in
`include/Errors.h`: 1-29 shared with AMPS, 101-126 MIPS-specific. The
ones a sequencer will actually meet:

| Code | Meaning |
|---|---|
| 1 | Invalid command |
| 2 | Invalid argument |
| 3 | Already in LOC mode |
| 4 | Already in TBL mode |
| 5 | No tables loaded |
| 6 | Not in table mode |
| 7 | Table not ready |
| 8 | Timed out waiting for a token (the 3-second `STBLDAT` timeout) |
| 9 | Expected a `:` |
| 10 | Table too big |
| 19 | Expected a `,` |
| 20 | Table nesting too deep |
| 21 | `]` without a matching `[` |
| 27 | Not in LOC mode |

### Asynchronous table-status messages

While in table mode the box emits unsolicited lines (unless disabled
with `STBLREPLY,FALSE`). All but one go through `println` with a `\n`
already inside the string, so they arrive as the text, then `\n\r\n`:
a host that discards empty lines sees one line per event.

| Message | Bytes on the wire | When |
|---|---|---|
| `TBLRDY` | `TBLRDY\n\r\n` | Entered table mode, or re-armed, and waiting for a trigger |
| `TBLTRIG` | `TBLTRIG\n\r\n` | Trigger received, table running |
| `TBLCMPLT` | `TBLCMPLT\n\r\n` | Table finished |
| `ABORTED by user` | `ABORTED by user\n\r\n` | Front-panel button held down |
| `ABORTED` | `ABORTED\n` | `TBLABRT`, or input voltage below 10 V |
| `Table stoped by user` | `Table stoped by user\r\n` | `TBLSTOP` processed (sic, the misspelling is in the firmware) |

The two abort forms come from different points in `Table.cpp`'s service
loop and are not interchangeable: the button path prints `ABORTED by
user` and leaves table mode, while `TBLABRT` and the low-voltage check
set the status to `ABORTED` and write the bare word with a single `\n`.
A parser keyed on the exact string `ABORTED` misses the button abort, so
match on the prefix.

**Re-arming is automatic under an external trigger.** With `STBLTRG` set
to `EDGE`, `POS` or `NEG`, the loop emits `TBLCMPLT` and then `TBLRDY`
again without any host command, and stays in table mode for the next
edge. Only under `SW` does it fall out of the inner loop after each pass.
A sequencer that re-arms per frame must therefore expect `TBLRDY` it did
not ask for, and must not treat a second `TBLRDY` as a protocol error.

A host parser must tolerate all of these interleaved with command
responses. `GTBLSTA` polls the same state machine and returns one of
`IDLE`, `READY`, `TRIGGERED`, `ABORTED`.

---

## 2. Table (pulse sequence) wire syntax: `STBLDAT`

A table is an ASCII string of *events* referenced to a shared clock:

```
STBLDAT;<sequence>;
```

### Events

```
Count:Channel:Value[:Channel:Value...]
```

- `Count`: time of the event in **clock ticks** (int), counted from
  table release (trigger). Multiple `Channel:Value` pairs may share one
  `Count`.
- Events are comma-separated. Time values within a (sub)table must be
  **ascending**.
- A **negative** `Count` marks the time point as *dynamic*: the firmware
  uses `abs(Count) + TimeDelta`, where `TimeDelta` is accumulated by the
  `d` (increment) and `p` (set) channel commands. This is how swept
  delays are built, for example shifting a gate 1 tick per loop iteration.

### Loops

```
[name:cycles,event1,event2,...,length:]
```

- `name`: single character naming the (sub)table.
- `cycles`: repeat count; **0 = repeat forever** (until `TBLSTOP`,
  abort, or re-trigger config stops it).
- `length:`: a final bare `Count:` that sets the loop period in ticks
  (it is the sub-table's MaxCount, i.e. the RC compare value).
- Loops nest to a **maximum depth of 5** (`MaxNesting`).
- Multiple loops/sequences can be concatenated in series; a leading
  `offset:[...` delays the loop start. Example, two-event loop, 10
  cycles, 100-tick period, starting 25 ticks after trigger:
  `STBLDAT;25:[A:10,10:A:1,25:A:0:5:34.5,100:];`

### Channel codes

From `Table.cpp` (authoritative) and vendor Table 1:

| Channel token | Wire meaning | Value |
|---|---|---|
| `1`–`32` | DC bias output channel n | Volts (float, converted to DAC counts at parse time) |
| `33`–`36` | RF driver channels 1–4 (drive level) | Float, raw float bits stored, applied via `UpdateRFdrive()` |
| `A`–`P` | Digital outputs A–P | `0` or `1` |
| `t` | Trigger output pulse | Width in µs; `0` and `-1` set static levels (see polarity note below) |
| `b` | Fire pre-programmed burst generator | Ignored |
| `c` | Trigger compression table | `A` = ARB, `T` = Twave |
| `r` | ARB compress line | `0` or `1` |
| `s` | ARB sync line | `0` or `1` |
| `d` | `TimeDelta += value` (dynamic time points) | Ticks, signed |
| `p` | `TimeDelta = value` | Ticks, signed |
| `101`–`104` (aka `e`–`h`; see note below, emit numerals) | ARB module 1–4 AUX output voltage | Volts (±50) |
| `105`, `106` (aka `i`, `j`) | ARB module **1** offset **A**, offset **B** | Volts (±50) |
| `107`, `108` (aka `k`, `l`) | ARB module **2** offset **A**, offset **B** | Volts (±50) |
| `W` | No-op placeholder (parser skips; use to define a bare time point) | none |
| `a` | Arm "stop on RC and wait for next trigger" for this pass (with retrigger + `STBLEVY`) | none |
| `=` `>` `<` | Conditional test of loop counter (named-`P` loops only) | Compare value |
| `40` / `41` / `42` | (`P`-loop mode) trigger ADC record / trigger-out high / trigger-out low | Ignored |

Numeric channel tokens with flag bits `RAMP = 0x80` and `INITIAL = 0x40`
added select the two voltage-ramping subsystems (e.g. channel 1 → `129`
ramping, `65` initial). Details:

- **ISR-based ramping** (`STBLRMPENA,TRUE,<freq>`): max 3 concurrent
  ramps; `RAMP|INITIAL` (192+chan) sets the start voltage, `RAMP`
  (128+chan) sets volts-per-update delta (0 stops); a dedicated timer at
  `<freq>` Hz steps the DAC between time points.
- **Table-based ramping** (`STBLVDLT,TRUE`): max 8 channels; `INITIAL`
  (64+chan) sets the start value, `RAMP` (128+chan) adds a delta per
  execution, used inside loops, including conditional `P` loops.

Conditional (`P`-named) loops: a `=:n:`, `>:n:` or `<:n:` prefix gates
only the immediately following `Channel:Value` pair on the loop counter;
two comparisons in a row AND together (`>:25:<:30:7:43.2`). In `P`
mode digital outputs are not usable; channels 40/41/42 above become
available.

**Discrepancy note (`a` takes a value):** the channel table above, and
the vendor doc, give `a` no value. The v1.263 parser disagrees: `a` sits
in `ParseEntry()`'s character group but not in its integer sub-group, so
it runs `ExpectColon()` and then stores the *first character of the next
token* as the entry's value. Omitting the value makes the parser consume
whatever follows as `a`'s argument and desynchronise the rest of the
table. Emit `a:1`; the value itself is never read at run time.

**Polarity note (`t` = 0 / -1):** the vendor doc says value `0` sets the
trigger output *low* and `-1` sets it *high*; the firmware
(`DIO.cpp: ProcessTriggerOut()`) writes pin HIGH for `0` and LOW for
`-1`, i.e. the MIPS trigger-output BNC is inverted relative to the
processor pin. Trust the vendor doc for BNC-level behavior, but verify
on hardware before relying on the static levels.

**Discrepancy note (`i`–`l`):** the vendor doc's Table 1 describes
`i`–`l` as "offset values for ARB modules 1 through 4"; the firmware
actually maps them to offsets A/B of ARB modules 1 and 2 as shown
above. The firmware wins.

**Discrepancy note (`e`–`l` letter tokens don't parse):** the vendor
doc's Table 1 (and the firmware's own comment block at the top of
`Table.cpp`) present the ARB channels as letter tokens `e`–`l`, but the
v1.263 parser has no letter branch for them: anything that isn't one of
the explicitly handled characters falls through to
`sscanf(TK,"%d",&i)` (`Table.cpp: ParseEntry()`), and a literal `e`
converts nothing, so the channel byte is then written from an
*uninitialized* local (undefined behavior, silently corrupt table).
The working wire encoding is the **numeric channel number 101–108**,
which are simply the ASCII codes of `e`–`l`, so run-time dispatch
(`Chan` in 101–108) and debug dumps are letter-compatible even though
the parser is not. The compiler must emit numerals. See §6.5 for these
channels' execution semantics.

### Compiled (on-controller) binary layout

The ASCII string is parsed **on the box at `STBLDAT` time** into packed
structs in RAM (this is what `STBLVLT`/`STBLCNT` patch and `TBLRPT`
dumps). The host never sends binary, but the sequencer compiler should
understand this layout because it defines the real semantics and limits:

```c
#pragma pack(1)
typedef struct {          // one per (sub)table
    char TableName;       // 0xFF = unnamed, 0x00 = end-of-tables marker
    int  RepeatCount;     // loop cycles; 0 = forever
    int  MaxCount;        // period in ticks -> timer RC compare
    int  NumEntries;      // number of TableEntryHeaders that follow
} TableHeader;            // 13 bytes

typedef struct {          // one per time point
    int  Count;           // tick of this event -> timer RA compare (negative = dynamic)
    char NumChans;        // TableEntry count that follows
} TableEntryHeader;       // 5 bytes

typedef struct {          // one per Channel:Value pair
    char Chan;            // 0-31 DCB, 33-36 RF, 'A'-'P' DIO, function chars, ']' = loop end
    int  Value;           // encoding depends on Chan, see below
} TableEntry;             // 5 bytes
```

- Value encoding by channel class: DC bias values are converted from
  volts to a pre-formatted 4-byte DAC/SPI frame at parse time (so the
  ISR can stream it straight to the DAC); RF and ARB values store raw
  IEEE-754 float bits; DIO values store the ASCII character
  (`'0'`/`'1'`); `t`/`b`/`d`/`p`/conditionals store the int.
- **DC bias channels are stored zero-based.** A `1`-`32` token is written
  as `Chan = n - 1`, so DCB channel 1 appears in the dump as `Chan = 0`.
  Every other channel class stores its token as written: RF `33`-`36`,
  ARB `101`-`108`, and the character channels as their ASCII codes.
- Loop-closing `]` is stored as a TableEntry (`Chan = ']'`) and
  interpreted at run time against a 5-deep nesting stack.
- **A leading offset costs a whole table.** `offset:[...` with a non-zero
  offset emits an extra unnamed TableHeader (`TableName = 0xFF`,
  `RepeatCount = 1`, `MaxCount = offset`) carrying one TableEntryHeader
  with `NumChans = 0`, ahead of the named loop's own header. A host that
  predicts the compiled size, in order to ask `TBLRPT` for the right
  number of bytes, has to account for it.
- **The end-of-tables marker is one byte.** After the last table the
  firmware writes a TableHeader whose `TableName` is `0x00` and leaves
  the remaining 12 bytes of that header untouched, so only the marker
  byte itself is meaningful, and the bytes after it are whatever the
  previous table left in the buffer. A comparison against a predicted
  layout must stop at the marker.
- **DC bias values cannot be predicted off-box.** The parser converts
  volts through `DCbiasValue2Counts()` and the board's channel-to-DAC
  map, both of which depend on that module's stored calibration. A host
  can verify the structure of a DCB entry and its `Chan`, but its
  `Value` is only meaningful to the box that produced it.
- Storage: up to **5 independent table buffers** (`STBLNUM` selects,
  1–5), each grown by `realloc` in 1000-byte increments; table size is
  bounded only by Due RAM (96 KB total, shared), not by a fixed limit.
  A single `STBLDAT` may also contain several concatenated sub-tables
  played in sequence; with table-advance (`STBLADV,ON`) the active
  buffer number auto-advances after each trigger.

---

## 3. Execution and timing model

This section answers the two questions the 2026-07 planning left open: whether hard real-time is on-controller (Q1) and what an event time is referenced to (Q2).

### Hard real-time is on-controller (Q1: yes)

Sequence timing is generated by the SAM3X8E timer-counter hardware, not
by host software and not even primarily by firmware software:

1. `STBLDAT` parses the table into the binary layout above (box must be
   in LOC mode).
2. `SMOD,TBL` (or `ONCE`, or `SMOD,<n>` to run n times) enters table
   mode: the timer is configured (`SetupTimer()`), the first time
   point's tick is written to compare register **RA**, the (sub)table
   period to **RC**, the trigger source is armed, and `TBLRDY` is
   emitted.
3. On trigger, the counter runs. When the counter hits RA, the timer's
   TIOA output pin **toggles in hardware**, generating the LDAC latch
   that applies pre-loaded DAC values and DIO states. Output edge
   timing is set by silicon, not an ISR.
4. The RA-match interrupt then fires and *pre-loads* the next event:
   streams the next DAC frames over SPI, stages DIO image registers,
   writes the next RA/RC, all before the next compare. The RC match
   handles loop wrap / table advance / stop.
5. `TBLCMPLT` on completion; depending on mode/trigger the box re-arms
   (`TBLRDY` again) or drops back to LOC.

The host's only real-time obligation is *none*: it compiles, uploads,
configures, arms, and (optionally) sends the software trigger. During
table execution the firmware keeps servicing serial commands between
interrupts, so status polling remains possible.

Consequence of the ISR pre-load design: the **first** event of a table
at tick 0 is applied at trigger time; events must leave the ISR enough
time to stage the next event (see timing limits below).

### Clock semantics (Q2: clock-referenced, one release trigger)

**Time points are counts of the table clock, not counts of trigger
edges.** One trigger edge releases the table; from then on timing
free-runs on the selected clock until the table completes.

Clock sources (`STBLCLK`):

| Argument | Source | Tick rate |
|---|---|---|
| `42000000` (or `MCK2`) | Internal, MCK/2 | 42 MHz |
| `10500000` (or `MCK8`) | Internal, MCK/8 | 10.5 MHz |
| `2625000` (or `MCK32`) | Internal, MCK/32 | 2.625 MHz |
| `656250` (or `MCK128`) | Internal, MCK/128 | 656.25 kHz |
| `EXT` | **Q input BNC**, rising edge, hardware XC2 | External |
| `EXTN` | Q input, falling edge | External |
| `EXTS` | **S input BNC**, rising edge, counted in software ISR | External, low rate |

- With an external clock, tell the box the frequency via
  `SEXTFREQ,<hz>` if you want `TBLCHK` timing checks or `STBLTSKS`
  idle-task mode to work; it does not affect execution itself.
- `EXTS` emulates the counter in software (used when the hardware XC2
  route isn't available). It forces software LDAC and has far lower
  usable rates. Historical note: the firmware credits "Bush lab" with
  finding a nested-loop bug in this path (fixed July 2019,
  `Table.cpp: ISRclk()`); prefer `EXT`/`EXTN` over `EXTS`.

Trigger sources (`STBLTRG`): `SW` (command `TBLSTRT`), `POS`/`NEG`/
`EDGE`: rising/falling/any edge on the **R input BNC**, wired to the
timer's external-event trigger so release latency is hardware-level.

Re-trigger behavior:

- Default: with an external trigger source, after `TBLCMPLT` the table
  re-arms and every subsequent trigger edge replays it (status returns
  to `READY`, `TBLRDY` emitted). `SMOD,ONCE` runs once and exits to
  LOC; `SMOD,<n>` runs n times.
- `STBLRETRIG,FALSE` makes an armed table one-shot (trigger detaches
  after release).
- `STBLEVY,TRUE` + retrigger + an `a` channel event: the table stops at
  each (sub)table boundary (stop on RC) and **waits for the next
  trigger edge to continue**. This is the mechanism for advancing a
  sequence trigger-by-trigger rather than free-running.
- Exotic trigger paths exist and are table-adjacent but out of core
  scope: trigger-on-ADC-change with dynamic gate-time adjustment
  (`TRGTBLADC`, `STPADJ`, `SADJRNG`, `SADCMZCAL`, `SMZTARG`) and
  trigger-from-level-detector-TWI module (`TRIGCHG`).

### Multi-box coherence

Nothing in the protocol synchronizes boxes to each other except what
you wire: distribute a **common external clock** (Q inputs, `STBLCLK,
EXT`) and a **common trigger** (R inputs) and boxes stay phase-locked
indefinitely. Tick n means the same instant on every box, and drift is
eliminated by construction. If instead each box free-runs its internal
clock from its own 84 MHz crystal, alignment is only as good as the
crystals' relative accuracy (order 10⁻⁵), i.e. tens of µs drift per
second of table time. That is unacceptable for long experiments.
**Clockwork should assume common-clock + common-trigger wiring**
(consistent with the TOF-pusher-derived trigger design, lab record).

### Timing limits (compiler constraints)

From `TableCheck()` constants and the vendor guideline:

- Fixed ISR setup overhead ≈ 7 µs; each DC bias channel in an event
  ≈ 11 µs; DIO processing in an event ≈ 19 µs; function channels
  (`t`, `b`, ARB, RF, ...) budgeted ≈ 50 µs.
- Rule of thumb from the vendor: never closer than **10 µs between
  events**; start from 20 µs × (number of values changing) and tighten
  after debugging.
- The gap that matters is between *consecutive time points* (the ISR
  must finish pre-loading before the next RA match). The compiler must
  validate this; `TBLCHK` on the box re-checks (needs `SEXTFREQ` first
  when on external clock).
- All time points within a (sub)table ascending; dynamic (`d`/`p`)
  shifts must not reorder or collide events; firmware does not guard
  against it.
- Loop period (`length`/MaxCount) must be ≥ the last event tick plus the
  setup time of the first event of the next iteration.

---

## 4. Table-related host command reference

Grouped from `MIPScommands.txt` + dispatch table in `Serial.cpp`
(`S...`/`G...` = set/get pairs; get takes no or fewer args):

| Command | Args | Notes |
|---|---|---|
| `STBLDAT` | `;<sequence>;` | Load table(s); LOC mode (or TBL+READY) only; NAK + buffer flush on parse error, and the current buffer's table count is zeroed |
| `STBLCLK` | `EXT\|EXTN\|EXTS\|42000000\|10500000\|2625000\|656250` | Clock source (LOC mode only) |
| `STBLTRG` | `SW\|POS\|NEG\|EDGE` | Trigger source (LOC mode only) |
| `SMOD` | `LOC\|TBL\|ONCE\|<n>` | Mode: enter/leave table mode; `ONCE`/`<n>` auto-exit |
| `TBLSTRT` | none | Software trigger (TBL mode) |
| `TBLSTOP` | none | Graceful stop, stays in table mode |
| `TBLABRT` | none | Abort table mode |
| `GTBLSTA` | none | `IDLE\|READY\|TRIGGERED\|ABORTED` |
| `GTBLFRQ` | none | Current internal clock frequency (Hz) |
| `STBLNUM`/`GTBLNUM` | `1..5` | Active table buffer |
| `STBLADV`/`GTBLADV` | `ON\|OFF` | Auto-advance buffer after each trigger |
| `STBLVLT`/`GTBLVLT` | `count,chan[,volts]` | Patch/read a loaded DC-bias entry in place (no re-upload) |
| `STBLCNT` | `count,chan,newcount` | Patch a time point in place |
| `STBLDLY` | ms | Inter-table delay in the table-mode service loop (default 3) |
| `STBLDLT`/`GTBLDLT` | ticks | Read/write `TimeDelta` directly |
| `STBLRETRIG`/`GTBLRETRIG` | `TRUE\|FALSE` | External re-trigger enable |
| `STBLEVY`/`GTBLEVY` | `TRUE\|FALSE` | Stop after each segment awaiting trigger (with retrigger + `a`) |
| `STBLREPLY`/`GTBLREPLY` | `TRUE\|FALSE` | Async status messages on/off |
| `SOFTLDAC` | `TRUE\|FALSE` | Force software LDAC generation |
| `STBLRMPENA` | `TRUE\|FALSE,freq` | Enable ISR-based ramping at freq Hz |
| `STBLVDLT`/`GTBLVDLT` | `TRUE\|FALSE` | Enable table-based (conditional-loop) ramping |
| `TBLCHK` | none | On-box timing-violation check (prints human-readable report) |
| `TBLRPT` | count | Debug: dump `count + 1` table-buffer bytes as hex, with a five-line preamble and **no ACK** (see below) |
| `SEXTFREQ`/`GEXTFREQ` | Hz | Declare external clock frequency |
| `STBLTSKS`/`GTBLTSKS`, `TBLTSKENA` | `TRUE\|FALSE` | Run system tasks in table idle time (needs `SEXTFREQ` on ext clock; use with care) |
| `STBLUSBTST`/`GTBLUSBTST` | `TRUE\|FALSE` | USB link test during table loop |

Three commands in this table behave in ways the row cannot carry, and
all three matter to a sequencer:

**`SMOD` refuses the mode it is already in.** `SetTableMode()`
(`Table.cpp`) tests the current mode before doing anything: `SMOD,LOC`
NAKs with `ERR_LOCALREADY` (3) when the box is already local, and
`SMOD,TBL`/`SMOD,ONCE` NAK with `ERR_TBLALREADY` (4) when it is already
in table mode or `ERR_NOTBLLOADED` (5) when the active buffer holds no
tables. **None of them is idempotent.** This matters because `STBLDAT`
requires LOC mode, so a host naturally sends `SMOD,LOC` to *ensure* the
box is local before a load, and on an idle box that NAKs every time. A
sequencer must treat error 3 from `SMOD,LOC` as success, not as a
failure. Confirmed on the bench (lab record, task 04).

Note also that the mode change is not complete when the ACK arrives: the
`LOC` arm sends the ACK and then sets `LOCrequest`, which the table
service loop acts on afterwards.

**`STBLDAT` can take three seconds to say no.** On a parse error the
firmware flushes the rest of the command by calling `NextToken()` until
it times out, and only then sends the NAK
(`Table.cpp: ParseTableCommand()`). A NAK for a bad table therefore
arrives roughly 3 seconds after the last byte, not promptly, and a host
read timeout shorter than that will report a timeout where the box was
about to report a parse error. The same flush runs when `STBLDAT`
arrives in table mode while the status is not `READY`, so an ill-timed
load costs 3 seconds as well. Give the load a read timeout of at least
the token timeout plus the streaming time, and read `GERR` afterwards to
find out which it was.

**`TBLRPT` sends no ACK.** It is dispatched as a plain function
(`Serial.cpp` command table, `CMDfunction` with one argument), and
`ReportTable()` writes only its output, so a host waiting for `0x06`
before reading the dump waits forever. The output is a five-line
preamble followed by one byte per line:

```
TestNesting = <int>
TablesLoaded = <int>
Size of TableHeader = 13
Size of TableEntryHeader = 5
Size of TableEntry = 5
<hex>
<hex>
...
```

Each byte is `printf("%x\n")`: lowercase, no `0x`, **not** zero-padded,
so a zero byte is the single character `0`. Exactly `count + 1` byte
lines follow the preamble, counted from the start of the active buffer,
and the argument is a byte offset rather than an entry count. The three
`Size of` lines are `sizeof` on the box and are the cheapest available
confirmation that the packing assumed in §2 matches the firmware that is
actually running.

`ReportTable()` is called nowhere else; the call inside the successful
`STBLDAT` path is commented out, so a dump only ever happens because the
host asked for one.

**Not every command above exists on every firmware.** `GTBLSTA` is
absent from v1.163t (Nov 2019), where it NAKs as an invalid command
(1), and present in the v1.263 this document is written against. A
host that polls table status must either require a firmware new enough
to have it or fall back to the asynchronous status lines, which every
version emits. Observed on the bench box (lab record, task 04); the
version in which it appeared has not been bisected.

General commands the sequencer will also need: `GVER` (version),
`GERR` (last error code), `GNAME`/`SNAME` (box identity), `MUTE`,
`ECHO`, `SAVE` (persist config to SD), `TRIGOUT,HIGH|LOW|PULSE`
(manual trigger output), `GCMDS` (list all commands).

---

## 5. Twave command set

> **Scope note:** on this instrument all eight SLIMPHONY TW regions are
> driven by **ARB modules**, not Twave modules (confirmed 2026-07-02).
> §6, not this section, is normative for Layer 1 wave control. This
> section is retained because the compressor concepts and the `c`:`T`
> table channel are defined here, and in case Twave hardware appears in
> an inventory check.

Twave hardware: up to 2 Twave modules per box (rev 1–5 boards), each
with 4 analog channels (pulse voltage, resting voltage, guard 1,
guard 2), plus a clock ("velocity", Hz) and an 8-bit output-sequence
pattern that defines the traveling wave. `<mod>` below is 1 or 2.

### Core waveform commands

| Command | Args | Notes |
|---|---|---|
| `STWF`/`GTWF` | `<mod>,<hz>` | Wave clock frequency. Valid range is board-rev dependent (rev 4/5 UI allows 250 kHz–2 MHz; earlier revs 3 kHz–300 kHz); compressor-table `F` command enforces 1 kHz–300 kHz |
| `STWPV`/`GTWPV` | `<mod>,<volts>` | Pulse voltage |
| `STWG1V`/`GTWG1V` | `<mod>,<volts>` | Guard 1 voltage |
| `STWG2V`/`GTWG2V` | `<mod>,<volts>` | Guard 2 voltage |
| `STWSEQ`/`GTWSEQ` | `<mod>,<bits>` | Output sequence as a bit string, e.g. `11000000` |
| `STWDIR`/`GTWDIR` | `<mod>,FWD\|REV` | Wave direction |
| `STWCCLK` | `TRUE\|FALSE` | Common clock for both modules (forced TRUE in compressor mode) |
| `STWCMP` | `TRUE\|FALSE` | Enable compressor mode (config flag, persists via `SAVE`) |
| `STWINV` | `<mod>,TRUE\|FALSE` | Invert waveform |

Note: Twave rev ≥ 4 generates its clock/sequence in a CPLD driven over
SPI; rev 3 and below bit-bang. This is invisible on the wire; the
commands above are the interface either way.

### Compressor commands

The compressor alternates the second Twave module between "normal" and
"compress" behavior on a schedule, optionally driving a gate switch. This is
its own little sequencer, separate from the Table engine (it runs off
a dedicated timer, `TMR_TwaveCmp`, and can be triggered from a Table
via the `c` channel with value `T`, by external input, or by `TWCTRG`).

| Command | Args | Notes |
|---|---|---|
| `STWCTBL`/`GTWCTBL` | string ≤ 130 chars | Compression table program (mini-language below) |
| `STWCMODE`/`GTWCMODE` | `Normal\|Compress` | Force mode |
| `STWCORDER`/`GTWCORDER` | 0–255 | Compression order |
| `STWCTD` | ms (float) | Trigger delay |
| `STWCTC` | ms | Compress time per cycle |
| `STWCTN` | ms | Normal time per compress cycle |
| `STWCTNC` | ms | Non-compressed cycle time |
| `TWCTRG` | none | Software compressor trigger |
| `STWCSW`/`GTWCSW` | `Open\|Close` | Gate switch state |

### Compression table mini-language (`STWCTBL`)

An ASCII program, executed left to right when the compressor is
triggered (`Twave.cpp: GetNextOperationFromTable()`). An op is a single
character optionally followed by a number (repeat count or value;
floats allowed where noted, must start with a digit):

| Op | Meaning |
|---|---|
| `N`*n* | n non-compressed passes |
| `C`*n* | n compressed passes |
| `O`*n* | Set compression order (0–255) |
| `V`*v* / `v`*v* | Set Twave module 1 / module 2 pulse voltage (8–100 V, float) |
| `F`*hz* | Set module 1 frequency (1 kHz–300 kHz) |
| `c`*ms* / `n`*ms* / `t`*ms* | Set compress / normal / non-compress times (float ms) |
| `D`*ms* | Delay (float ms) |
| `s` / `r` | Stop / restart the Twave clock |
| `S`*0/1* | Gate switch close (0) / open (1) |
| `o`*ms* / `g`*ms* / `G`*ms* | Switch open duration / open-at time / close-at time (from table start) |
| `M`*0/1* | Normal-mode amplitude source (1 = use module 1 amplitude on module 2) |
| `K`*n* / `k`*n* | Compression ramp / ramp order step |
| `Q`*n* / `q`*n* | Set module 1 / module 2 sequence to n |
| `[`...`]`*n* | Loop, n iterations, nesting ≤ 5 |

Default table is `"C"` (one compressed pass).

### Sweep commands (older shared Twave/ARB sweep system)

`STWSSTRT`/`STWSSTP` (start/stop frequency), `STWSSTRTV`/`STWSSTPV`
(start/stop voltage), `STWSTM` (sweep time, s), all `<mod>,<value>`
with `G` variants; `STWSGO` / `STWSHLT` start/halt, `GTWSTA` status.
Newer ARB-side sweeps have their own commands in the ARB section of
`MIPScommands.txt` (out of scope here).

---

## 6. ARB modules and the alternate-waveform system

The ARB (arbitrary waveform generator) module is the waveform source
for every TW region on this instrument, so this section defines the
capability vocabulary Layer 1 compiles to: direction flips,
traveling ↔ stationary transitions, gating, amplitude/frequency steps.
Derived from `ARB.cpp`/`ARBcompressor.cpp`/`include/ARB.h` in the MIPS
firmware, the ARB module's own firmware (version 1.24, see Provenance
and the version caveat in §6.4), and `ARB_Module.pdf`.

### 6.1 Hardware and control model

- A box holds up to **6 ARB modules**. Each module generates **8
  output channels** (one TW electrode set) plus one **AUX** DC output.
  Outputs are 8-bit DACs scaled by a programmable range (0–100 V p-p)
  and offset (±50 V, applies to all 8 channels and AUX). Boards
  factory-configured with dual output amplifiers ("dual output boards")
  drive two identical 8-channel sets, A and B, with independently
  offsettable A/B outputs (`SARBOFFA`/`SARBOFFB`, ±10 V).
- Two operating modes per module (`SARBMODE,<mod>,TWAVE|ARB`):
  **TWAVE**, where the 8 channels replay one waveform cycle (PPP points,
  default 32) with a 45° phase step channel-to-channel, producing a
  continuous traveling wave; **ARB**, classic one-shot/looped
  arbitrary-buffer playback (up to 8000 samples). TW regions run in
  TWAVE mode; ARB-mode buffer commands (`SARBBUF`, `SARBNUM`,
  `SARBCHS`, `SARBCH`, `SACHRNG`, `SARBSINE`) are out of scope here.
- Waveform frequency ceiling = 1.28 MHz digitization rate ÷ PPP
  (`GARBPPP`/`SARBPPP`, 8–128; 32 default → 40 kHz max TW frequency).
  Changing PPP requires a MIPS reboot.
- **Controller ↔ module link:** the MIPS controller talks to each
  module's on-board Arduino over **TWI (I2C)** for all state (TWI
  addresses `SARBADD`; 0x40/0x42/0x44 for module pairs), plus **two
  box-wide hardware lines** for time-critical signaling (§6.3). The
  host never addresses a module directly: every `S…`/`G…` command below
  is a MIPS command that the controller re-encodes as TWI writes. The
  module's own serial command set (see the `ARB/` clone,
  `Serial.ino`) is reachable only through the `TWITALK,<board>,<addr>`
  passthrough. Treat it as factory/debug access, not wire protocol,
  so its TWI constants are deliberately not tabulated here.
- Module firmware version gates features (§6.4). `GARBVER,<mod>`
  returns it; a deploy-time check belongs in Layer 3 (§7).

### 6.2 Core TWAVE-mode host commands

All take `<mod>` (1–6) as first argument; `G` variants return the
current value. From the dispatch table in `Serial.cpp` and
`ARB.cpp`:

| Command | Args | Notes |
|---|---|---|
| `SARBMODE`/`GARBMODE` | `<mod>,TWAVE\|ARB` | Operating mode |
| `SWFREQ`/`GWFREQ` | `<mod>,<hz>` | Waveform frequency; max 1.28 MHz/PPP (TWAVE), 1 MHz (ARB). Actual may differ from requested; read it back |
| `SWFVRNG`/`GWFVRNG` | `<mod>,<volts>` | Output range, p-p volts for full-scale DAC (0–100) |
| `SWFVOFF`/`GWFVOFF` | `<mod>,<volts>` | Offset, ±50 V, applied to all 8 channels + AUX; **not** folded into reported values, host must track |
| `SWFVAUX`/`GWFVAUX` | `<mod>,<volts>` | AUX output, ±50 V (same target as table channels 101–104) |
| `SWFDIR`/`GWFDIR` | `<mod>,FWD\|REV` | TW direction = sign of the 45° channel-to-channel phase step. Serial-paced; for tick-accurate flips use the alternate waveform `REV` (§6.4) or a SLIM Reverser |
| `SWFTYP`/`GWFTYP` | `<mod>,SIN\|RAMP\|TRI\|PULSE\|ARB` | Waveform type; `ARB` = the user waveform below |
| `SWFARB`/`GWFARB` | `<mod>,<32 values>` | User waveform, 32 points, ±100 (% of p-p range) |
| `SWFENA` / `SWFDIS` | `<mod>` | Start / stop waveform generation (software trigger) |
| `SWFVRAMP`/`GWFVRAMP` | `<mod>,<v/s>` | Amplitude slew limit for range changes; 0 = step immediately |
| `SARBOFFA`/`B`, `GARBOFFA`/`B` | `<mod>,<volts>` | Dual-output-board A/B set offsets, ±10 V (same targets as table channels 105–108) |
| `SARBREVA` / `CLRARBRV` | `<mod>,<volts>` / `<mod>` | AUX voltage automatically applied while direction is reversed, or clear that behavior |
| `SARBCCLK` | `<mod>,TRUE\|FALSE` | Use the common (controller-generated) clock. **Required on every module that must stay phase-coherent with others** |
| `SARBEXT` | `<mod>,MIPS\|EXT` | Common-clock source: controller or external clock-in BNC (box must have the external ARB clock option) |
| `SARBPPP`/`GARBPPP` | `<mod>,<8–128>` | Points per waveform period; reboot after changing |
| `SARBCOFF` | `TRUE\|FALSE` | All modules share one offset |
| `SARBDBRD` | `<mod>,TRUE\|FALSE` | Declare dual-output board (factory setup) |
| `SARBADD`/`GARBADD` | `<mod>,<addr>` | Module TWI address (factory setup) |
| `GARBVER` | `<mod>` | Module firmware version (feature gate, §6.4) |
| `ARBSYNC` | none | Software sync: TWI-enables sync on all modules, pulses the sync line, disables again (`ARB.cpp: ARBmoduleSync()`) |

Sweep commands exist in two flavors: controller-based (`STWS*`
`STWSGO`/`STWSHLT`/`GTWSTA`, §5's table, modules 1–2 only, works with
common clock) and module-based (`SARBSGO`/`SARBSHLT`/`GARBSTA` +
`CLRSPTTBL`/`ADDSPPNT` piecewise-linear table, any module, faster
rates, requires the module's own clock, i.e. `SARBCCLK,…,FALSE`).
Out of Layer 1 v1 scope; noted for completeness.

Power-up state: modules on a common clock are **not** phase-aligned
until synced. Issue `ARBSYNC` (or pulse the sync line) after enabling
waveforms and before anything timing-sensitive.

### 6.3 The two broadcast lines: `r`/`s` table channels and fan-out limits

The time-critical signals do not travel over TWI. The controller has
exactly **two digital lines fanned out in parallel to every ARB module
in the box** (`include/ARB.h`: Due pin 9 "ARBsync", Due pin 48
"ARBmode"/compress; `ARB_Module.pdf` p.29 confirms "two hardware
lines"). Everything fast (phase sync, compress entry/exit, alternate-
waveform triggering) is a level or pulse on one of these two lines.

Table engine connection (`Table.cpp`):

- `s:<0|1>` sets the sync line, `r:<0|1>` sets the compress line. Both
  are direct `digitalWrite`s executed by `ProcessTableQueue()` inside
  the timer-compare ISR at LDAC time
  (`Table.cpp: ProcessCompress()/ProcessSync()`, `RAmatch_Handler()`),
  **tick-accurate** to ISR latency (µs-scale), unlike the TWI-staged
  ARB channels in §6.5.
- `c:A` triggers the ARB compression table (§6.6) at LDAC time.

Module-side interpretation (module firmware `ARB.ino`, v1.24 baseline):

- **Sync**, rising edge, if the module's sync enable is set: in TWAVE
  mode the module snaps its waveform phase back to the start of the
  cycle. Every listening module re-aligns to the same instant, which
  is how multi-module (multi-region) TW phase coherence is established;
  in ARB mode the same edge is the play trigger.

  *Config gotcha:* there is **no host serial command** for the
  persistent per-module sync enable. The controller only TWI-enables it
  on modules whose "Sync input" (a rear-panel DI + level) is configured
  via the front-panel UI (`ARB.cpp` polling loop; stored in module
  EEPROM via the UI Save). `ARBSYNC` enables it transiently for the
  pulse. So for a module to follow table `s` events, its UI Sync input
  must be configured, even though the table path drives the line
  directly and never uses that input. Verify per module (§7). The UI
  "Dir input" is analogous: a front-panel-only DI attachment that flips
  TW direction on an input change (TWI-paced, not tick-accurate).
- **Compress**, level-sensitive, if the module's compress-follow flag
  is set (`SARBCPEX,<mod>,TRUE`): line HIGH = compress mode (the
  module latches its compression order and switches waveform), LOW =
  normal. `SARBHISR,<mod>,TRUE` makes the module service this line in
  an ISR instead of polling once per waveform cycle.

**Per-module line-role mapping.** Each module is told which physical
line is its sync and which its compress: `SARBSYNLN,<mod>,1|2`
(default 1) and `SARBCMPLN,<mod>,1|2` (default 2). Both roles may even
share one line, since sync is a narrow pulse, compress a level, and the
module firmware distinguishes them (vendor manual, advanced-config
section). The alternate-waveform hardware trigger (§6.4) also rides
the compress line by default.

**Fan-out constraint (hard limit for Layer 1):** two physical lines
per box, each seen by every module. Grouping is by per-module opt-in
flags, not by addressing, so a box supports **at most two
independently signaled ARB groups** over the broadcast lines (e.g.
"sync these four modules" and "compress/switch those two"). Any finer
independent, tick-accurate control requires wiring a Table DIO output
(`A`–`P`) from the rear panel back into a digital input (Q–X)
configured as that module's trigger (§6.4), one loopback cable per
extra independent group.

**Discrepancy note (line naming is crossed between firmwares):** MIPS
`include/ARB.h` says its "ARBsync" pin lands on ARB CPU pin 22 and its
"ARBmode" (compress) pin on ARB "AD6"; the module firmware
(`Hardware.h`) defines pin 22 as its *CompressPin* and A0 as its
*ARBsync*. The role-remap commands above exist precisely to paper over
this kind of crossing, and the line *roles* are therefore
configuration, not fixed wiring. Treat "line 1"/"line 2" as physical
identities and verify each module's actual `SARBSYNLN`/`SARBCMPLN`
config on our boxes (§7) before relying on defaults.

### 6.4 Alternate-waveform system: tick-accurate wave-state switching

This is the mechanism Layer 1 wave-state transitions compile to: each
module can hold one pre-configured **alternate waveform** and switch
between primary and alternate on a command or a hardware signal.

**Version gate:** ARB module firmware ≥ 2.1 and MIPS ≥ 1.167; the
`CUR` type needs ARB ≥ 2.21 and MIPS ≥ 1.227. TWAVE mode only.

> **Provenance caveat:** the cloned module firmware
> (version 1.24, Dec 2022; see Provenance) **predates this feature**: the
> alternate-waveform TWI commands (0x3A–0x4B in the MIPS-side
> `include/ARB.h`) are absent from its `ARB.h`. Module-side behavior
> below is derived from the MIPS controller source plus
> `ARB_Module.pdf`, not from module source. This makes the per-module
> `GARBVER` check mandatory at deploy time, and any hairline behavior
> question (exact switch latency, edge cases) a hardware test, not a
> source read.

**Alternate waveform types** (`SALTWFM`; encoded 1–5 over TWI):

| Type | Meaning | Layer 1 use |
|---|---|---|
| `COMP` | Default. Freeze at the final value of one primary cycle (the compression waveform) | CRIMP-style compression |
| `REV` | Primary waveform with the phase step reversed | **Direction flip** |
| `ARB` | The user's 32-point waveform (`SWFARB`) | Custom wave state |
| `FIX` | Static per-electrode voltage profile from `SALTFVAL` | **Stationary hold / gating** |
| `CUR` | Same waveform as primary | Amplitude/frequency-only step (with the range/frequency overrides below) |

**Switch timing:** on trigger (either direction, alternate→primary
included, even for `FIX`), the module **finishes the current waveform
cycle first**, then switches. Switching latency is therefore up to one
waveform period (1/f, e.g. 100 µs at 10 kHz) after the trigger
arrives. Layer 1's timing model must carry this as the switching
granularity on top of table-tick accuracy.

**Trigger paths:**

1. **Software:** `SALTENA,<mod>,TRUE|FALSE` switches to/from the
   alternate waveform over serial+TWI, ms-scale, not tick-accurate.
2. **Hardware level/edge:** `SALTTRG,<mod>,Q..W|NA` attaches a
   MIPS-side pin-change ISR on the chosen rear-panel digital input that
   *repeats the input's state onto the compress broadcast line*
   (`ARB.cpp: ARBaltISR()`; onto the sync line instead if
   `SARBALTTS,TRUE`). It is one global signal: configure it once (last
   module argument wins); each module then opts in with
   `SALTHWD,<mod>,TRUE` and interprets it per `SALTTMODE,<mod>,…`:
   - `LEVEL`: alternate waveform while the line is high;
   - `POS`/`NEG`: on that edge, wait `SALTDLY,<mod>,<ms>` (float),
     apply the alternate waveform for `SALTPLY,<mod>,<ms>`, then
     revert. Delay/duration are timed **on the module**, so they are
     ms-precision, asynchronous to table ticks.
3. **From a Table** (the Layer 1 path): drive the compress line
   directly with `r:1`/`r:0` events. With modules configured
   `SALTHWD,TRUE` + `SALTTMODE,LEVEL`, the wave state follows the `r`
   channel tick-accurately (± one waveform period, above). For more
   than the two broadcast groups, wire a DIO output (`A`–`P`) back
   into the `SALTTRG` input instead: same semantics, one cable per
   independent group.

**Overrides while the alternate waveform is active:**

- `SALTRENA,<mod>,TRUE|FALSE` + `SALTRNG,<mod>,<0–100 V p-p>`:
  alternate output range, applied automatically on switch.
- `SALTFENA,<mod>,TRUE|FALSE` + `SALTFRQ,<mod>,<1000–160000 Hz>`:
  alternate frequency (firmware-validated bounds).

**Fixed profile:** `SALTFVAL,<mod>,<index 0–7>,<value>` sets electrode
`index` of the `FIX` profile to `value` percent (±100) of the *current*
p-p range. ±100 % maps to ± half the p-p range on the output.

**Command reference** (all have `G` counterparts returning the set
value): `SALTENA`, `SALTTRG`, `SALTHWD`, `SALTTMODE`, `SALTWFM`,
`SALTFVAL`, `SALTDLY`, `SALTPLY`, `SALTRENA`, `SALTRNG`, `SALTFENA`,
`SALTFRQ` (`Serial.cpp:666-731`). Exotic: `CARBADLY`/`CARBADUR` let an
optional Level Detection Module rewrite delay/duration per a lookup
table (bit mask selects modules); `SARBALTTS` (alt trigger uses sync
line); `SARBDISCI` (skip compress-line init). Everything is queryable,
so Layer 3 can verify module state at arm time instead of trusting it.

**Discrepancy note (`SALTTRG` input range):** firmware accepts inputs
`Q` through `X`; the command help text and manual say Q–W. Also, the
manual's example 2 says "you only need to issue the SALTHWD command one
time." Its own reference section (and the firmware) show the shared
one-time command is `SALTTRG`, while `SALTHWD` is the per-module
opt-in. Firmware wins on both.

**SLIM Reverser (adjacent hardware, not an ARB feature):** an external
16-pole bidirectional analog switch that passes a TW signal set through
straight or electrode-order-reversed, selected by an isolated 0–5 V
control input, i.e. a direction flip implemented downstream of the
waveform generator. Protocol impact is nil: the control input is just a
Table DIO output (`A`–`P`) or any other logic source. It is the
alternative to `REV` when a region must flip direction independently of
its ARB module's state.

### 6.5 Execution semantics of the ARB table channels (`101`–`108`, `r`, `s`, `c:A`)

The §2 table defines the syntax; this defines what actually happens
and when. Two distinct execution classes:

**TWI-staged, committed at LDAC (`101`–`108`):** when the ISR
pre-loads an event containing ARB aux/offset channels, the value is
sent immediately to the module as a *pending* update
(`TWI_ARB_UPDATE_AUX` / `TWI_ARB_UPDATE_BRD_BIAS`,
`ARB.cpp: UpdateAux()/UpdateOffsetA()/UpdateOffsetB()`), and a commit
is queued. At the event's LDAC the queued `ProcessARB()` sends
`TWI_ARB_LOAD_UPDATES` to each flagged module, which loads the staged
values into hardware. Consequences for the compiler:

- The staging TWI traffic happens in the **preceding inter-event gap**;
  the ~50 µs function-channel budget (§3 timing limits) is real here,
  so leave room before any event carrying these channels.
- The commit itself is soft: if the TWI bus is busy at LDAC the load is
  re-queued and can slip past the tick (`ProcessARB()` re-queues on a
  busy bus). Treat 101–108 as "near-tick-accurate, not guaranteed" and
  don't use them where µs alignment matters.
- Values are volts (±50 aux; A/B offsets are board-bias values), sent
  as floats; no parse-time range check, so the compiler must validate.

**Direct pin writes at LDAC (`r`, `s`) and compressor start (`c:A`):**
tick-accurate as described in §6.3. `c:A` calls
`ARBcompressorTriggerISR()` from the LDAC-time queue
(`Compressor.cpp: ProcessCompressionTrigger()`), starting the
compression-table state machine of §6.6 (`c:T` starts the Twave one,
§5).

### 6.6 ARB compressor (`c:A` target: future CRIMP support)

Deferred capability for Layer 1 v1 (current experiments don't use
compression), documented because `c:A` is a table channel and the
compression table is the module-orchestration mini-language.

Structure parallels the Twave compressor (§5) but is richer: a
MIPS-side state machine on a dedicated timer (`ARBcompressor.cpp`),
started by `c:A`, `TARBTRG`, or a UI-configured external input. It
walks the compression table, driving the **compress broadcast line**
(normal/compress states) and per-module TWI parameter writes. By
convention ARB module 2 is the compress region (`CompressBoard = 1`,
i.e. board index 1): at init the controller enables compress-line
following on that module only. Order semantics: compression order *n*
gates off *n−1* of every *n* waveform cycles on the compress module
(order 1 ≡ normal).

| Command | Args | Notes |
|---|---|---|
| `SARBCTBL`/`GARBCTBL` | string ≤ 130 chars | Compression table (mini-language below); volatile, not saved |
| `SARBCMODE`/`GARBCMODE` | `Normal\|Compress` | Force mode (drives the compress line directly) |
| `SARBCORDER`/`GARBCORDER` | 0–65535 | Compression order (>255 uses the extended TWI command) |
| `SARBCTD`/`GARBCTD` | ms (float) | Trigger delay |
| `SARBCTC`/`GARBCTC` | ms | Compress time per cycle |
| `SARBCTN`/`GARBCTN` | ms | Normal time per compress cycle |
| `SARBCTNC`/`GARBCTNC` | ms | Non-compressed cycle time |
| `TARBTRG` | none | Software compressor trigger |
| `SARBCSW`/`GARBCSW` | `Open\|Close` | Gate switch output state |
| `SARBCDIS`/`GARBCDIS` | `TRUE\|FALSE` | Disable the compression table engine |
| `SARBCMP` | `TRUE\|FALSE` | Compressor-mode-enabled config flag |

Compression-table ops (`ARBcompressor.cpp:
ARBgetNextOperationFromTable()`): the Twave set (§5), `N`/`C` passes,
`D` delay, `O` order, `V`/`v` module 1/2 voltage, `F` frequency,
`c`/`n`/`t` times, `s`/`r` clock stop/restart, `S` switch, `o`/`g`/`G`
gate times, `M` amplitude mode, `K`/`k` order ramp (Cramp) rate/step,
`[`…`]`*n* loops, plus ARB-only ops: `W`*n*/`w`*n* waveform type for
module 1/2 (1=SIN…5=ARB), `L`/`l` module 3/4 voltage, `B`/`b`/`E`/`e`
module 1–4 amplitude ramp rates (V/s), `m`*n*`N|C` per-module
normal/compress mode, `J`*n**order* per-module compression order,
`A`/`a`*n**volts* per-module aux voltage (positive/negative), and
`H`*<input>* halt-until-trigger (uppercase input = rising edge,
lowercase = falling). Numeric arguments may be floats where the
quantity is a time/voltage. Unknown characters are skipped without
error; the table is not syntax-checked on load.

---

## 7. Open items / to verify on hardware

Items needing a physical check (asked of the lab directly: scope/board
inspection, not scriptable):

- Trigger-output (`t` channel, values 0/-1) BNC polarity. Vendor doc
  and firmware pin writes disagree in sign; assume the vendor doc's
  BNC-level description, verify with a scope.
- Which Twave board revs we have (affects wave-frequency range limits;
  determined by reading the board, not the protocol). Likely moot if
  the inventory confirms no Twave modules (§5 scope note).
- Whether any DIO-output→digital-input loopback cables are installed
  (needed for more than two independent ARB trigger groups per box,
  §6.3/§6.4). Part of the pending lab wiring inspection.
- Scope-check the alternate-waveform switch latency (§6.4 says up to
  one waveform period, from the manual; the module firmware that
  implements it isn't public) if Layer 1 timing budgets come to depend
  on the exact value.

Items that are plain protocol-level pass/fail probes, deferred to the
`clockwork` hardware-in-the-loop test suite once it
exists, rather than a one-off manual check:

- Current firmware version per box (`GVER`; protocol above is v1.263).
  Capture as a fixture/setup step that logs each box's version.
- ARB module firmware version per module (`GARBVER,<mod>`; alternate
  waveform needs ≥ 2.1, `CUR` ≥ 2.21, §6.4). Same fixture.
- Each module's actual sync/compress line-role config
  (`SARBSYNLN`/`SARBCMPLN`; defaults 1/2, but see the §6.3 crossed-
  naming discrepancy) plus `SARBCPEX`/`SARBHISR`/`SALTHWD` states.
  Query and log at arm time rather than assuming defaults.
- Each module's UI "Sync input" config, required for the module to
  follow table `s` events (§6.3 config gotcha), but front-panel-only,
  so it must be inspected/set at the box and persisted with Save.
- Whether `101`–`108` (ARB aux/offset) events land on-tick under load.
  The TWI commit can slip (§6.5); a test should stress event spacing
  against these channels.
- Exact usable event-rate ceiling for our typical event shapes (few DCB
  channels + DIO per time point). Derive from the per-channel budgets
  above, then have a test sweep spacing and check `TBLCHK`/response
  behavior.
- **Answered for one box, 2026-09-07** (lab record, task 04): maximum
  practical `STBLDAT` length is set by the send rate, not by the table
  size. Unpaced, the ceiling is the 4096-byte ring buffer; with a pause
  between chunks, 17572 bytes loaded and verified. §1 carries the
  numbers. Measured on firmware 1.163t rather than the v1.263 this
  document describes, so repeat it on a SLIMPHONY box.
- **Answered, 2026-09-07** (lab record, task 04): the compiled layout a
  host predicts from the table string is the layout the box parsed. A
  bench box reported `sizeof` 13, 5 and 5 for the three structs and
  round-tripped a probe table through `TBLRPT` with no difference from
  the prediction, including the extra table a leading offset costs and
  the zero-based DC-bias channel numbering. DC-bias values stay
  unpredictable by construction (§2). Confirmed on firmware 1.163t, one
  box, one table shape.
- Round-trip latency of a get-style command (`GVER`) measured 16 ms on
  USB CDC, and the wall-clock cost of streaming a real trainee table is
  still unmeasured, per transport. Both are inputs
  to the sequencer's read timeouts, and the load timeout in particular
  has to exceed the streaming time plus the 3-second flush a parse error
  costs before its NAK (§4).
