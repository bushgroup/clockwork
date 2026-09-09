# Glossary

To read the protocol documents, the package docstrings or a method file without stopping at an
unfamiliar name, start here. A term earns an entry when this repository's code or documents use
it. Terms belonging to the experiments the instrument runs, rather than to the software that
drives it, are defined in the lab record instead.

Three terms carry more than one sense and are given both in one entry: *table*, *compressor* and
*accumulation*.

## Instrument and lineage

- **Ion mobility.** Separation of gas-phase ions by how quickly they move through a buffer gas
  under an electric field, which distinguishes ions of equal mass and different shape. Every file
  clockwork writes is a set of such separations, each resolved into mass spectra.
- **SLIM.** Structures for lossless ion manipulations, i.e. ion mobility performed on printed
  circuit boards, where a traveling potential wave carries ions along the board instead of a
  static drift field.
- **SLIMPHONY.** The lab's SLIM instrument, eight traveling-wave regions ahead of a time-of-flight
  analyzer. It is the instrument clockwork controls.
- **Traveling wave, TW.** The potential pattern stepped from electrode to electrode along a SLIM
  board. Its direction, amplitude and frequency are what a wave state names.
- **TW region.** One stretch of SLIM electrodes whose wave direction, amplitude and frequency are
  set independently of every other stretch. All eight regions on this instrument are driven by ARB
  modules, so section 6 of [mips-wire-format.md](mips-wire-format.md) governs them.
- **Pusher pulse.** The extraction pulse of the time-of-flight analyzer, emitted continuously.
  Each pulse yields one mass spectrum, and the digitizer takes one record per pulse, so the pusher
  period sets the length of that record.
- **Technical replicate.** The same method acquired again into a new file. In clockwork it is one
  button.
- **FALKOR.** The acquisition and control software the lab runs today, which clockwork replaces
  for the experiments this lab runs. Its files reach the digitizer through an older U1084A card and
  hold, per scan, the sum over many pushes, which is what makes the file shape a compatibility
  question rather than a free choice.
- **mainspring.** The lab's UIMF reader and viewer,
  [bushgroup/mainspring](https://github.com/bushgroup/mainspring). It reads what clockwork writes.
- **CRIMP.** The ion-compression technique the ARB compressor, the `c:A` table channel and the
  `COMP` alternate waveform exist to support. Version 1 defers it; section 6.6 of
  [mips-wire-format.md](mips-wire-format.md) documents the hooks so that deferring it costs nothing
  later.
- **SLIM Reverser.** An external 16-pole bidirectional analog switch that passes a traveling-wave
  signal set through either straight or in reversed electrode order, selected by an isolated
  0 to 5 V control input. It reverses a region downstream of the waveform generator, so its
  protocol footprint is one digital output.

## MIPS boxes and pulse sequences

- **MIPS.** Modular intelligent power sources, GAA Custom Electronics' Arduino Due based controller
  boxes that generate the DC, RF and traveling-wave potentials an experiment needs. The firmware is
  public at `GordonAnderson/MIPS`.
- **Box.** One MIPS controller with its modules, reached over one USB serial port. A method names
  each box, the port it answers on, and the strings it receives.
- **Module.** One card in a box: ARB, DC bias, RF driver, Twave, DIO. The host never addresses a
  module directly; every module command is a box command the controller re-encodes.
- **String.** One ASCII MIPS command, or one whole `STBLDAT` table, exactly as it goes on the wire.
  `clockwork.method` stores strings per box and validates none of them, so the wire format in
  [mips-wire-format.md](mips-wire-format.md) is what a string must already satisfy.
- **Table.** Three senses. On a MIPS box it is the firmware's pulse sequence, a list of
  `Count:Channel:Value` events on a hardware timer, uploaded with `STBLDAT`, armed with `SMOD,TBL`,
  released by one trigger edge, and dumped back with `TBLRPT`. A *compression table* is the
  mini-language a compressor walks, unrelated to the sequencer. A *TOML table* is a section of a
  method file, and `[[boxes]]` is an array of them.
- **Event.** One time point in a table: a tick count, then one or more channel and value pairs
  applied at that count. Times within a sub-table must ascend.
- **Tick.** One period of the table clock selected by `STBLCLK`, from 42 MHz internal down to an
  external rate. Event times are counts of ticks measured from table release, never counts of
  trigger edges.
- **Sub-table, loop.** `[name:cycles,...,length:]`, nesting to a depth of five. The bare trailing
  `length:` sets the loop period, the value the firmware calls MaxCount.
- **Dynamic time point.** An event whose `Count` is negative, which the firmware reads as
  `abs(Count) + TimeDelta`. The `d` and `p` channels increment and set `TimeDelta`, which is how a
  swept delay is written.
- **Channel code.** The token naming what an event changes: `1` to `32` for DC bias volts, `33` to
  `36` for RF drive, `A` to `P` for digital outputs, `t` for the trigger output, `r` and `s` for
  the two ARB broadcast lines, `c` for a compressor trigger, and `101` to `108` for ARB auxiliary
  and offset voltages. Numeric codes carry flag bits that select the two voltage-ramping
  subsystems.
- **LOC mode, table mode.** LOC is ordinary local control, and a table can only be loaded in it.
  `SMOD,TBL` enters table mode, where a table is armed, released and reported on, and the box
  returns to LOC when the mode ends.
- **Release trigger.** The single edge that starts an armed table, from the R input or from
  `TBLSTRT`. After that edge the sequence free-runs on the table clock until it completes.
- **Re-arm.** Under an external trigger source a box replays its table on every subsequent edge,
  emitting `TBLRDY` without being asked. A host must expect that unsolicited line rather than treat
  it as a protocol error.
- **LDAC.** The hardware latch that applies an event's pre-loaded DAC values and digital states at
  its tick. The timer's compare output drives it in silicon, so output edge timing does not depend
  on interrupt latency.
- **ACK, NAK.** `0x06` for a command accepted, `0x15` for one rejected. `GERR` returns the code
  behind a rejection and never clears it, so it means something only when read immediately after a
  NAK.
- **Table-status lines.** The unsolicited `TBLRDY`, `TBLTRIG`, `TBLCMPLT` and `ABORTED` messages a
  box emits while in table mode, interleaved with ordinary command responses. `clockwork.mips`
  parses them into a `TableEvent` a caller can wait on.
- **Token timeout.** The three-second inter-token timeout of the `STBLDAT` payload parser, reported
  as error code 8. A table string that stalls mid-stream fails on it, which is why sends never run
  on the UI thread.
- **Input ring buffer.** The box's fixed 4096-byte serial buffer, which overflows silently and
  takes the interface down with it. A long table is therefore written in paced chunks, sized by
  `clockwork.mips.DEFAULT_CHUNK_BYTES` and `DEFAULT_CHUNK_GAP_S`.
- **Q, R and S inputs.** The box's external clock input (Q), external trigger input (R), and the
  low-rate clock input counted in software (S).
- **DIO A to P, Q to X.** The rear-panel digital outputs a table can drive and the digital inputs
  a module or trigger path can watch. A cable from an output back to an input is how an experiment
  buys a signaling group beyond the two the box broadcasts.

## Waveform hardware

- **ARB module.** The arbitrary-waveform module that drives a TW region, up to six per box. Each
  generates eight output channels plus one auxiliary DC output; in TWAVE mode the eight channels
  replay one waveform cycle with a 45° phase step channel to channel, which is the traveling wave.
- **TWAVE mode, ARB mode.** A module's two operating modes. TWAVE replays a cycle continuously and
  drives the TW regions; ARB plays a buffer of up to 8000 samples once or on a loop, and is out of
  scope for this instrument.
- **PPP.** Points per waveform period, 8 to 128 and 32 by default. It sets the frequency ceiling,
  1.28 MHz divided by PPP, and changing it requires a reboot of the box.
- **AUX output.** A module's own DC output, ±50 V, addressed either by command or by table channels
  `101` to `104`.
- **Offsets A and B.** The independently offsettable outputs of a dual-output board, a module
  factory-configured to drive two identical eight-channel sets. Table channels `105` to `108` reach
  them.
- **Broadcast lines, `s` and `r`.** The two digital lines a box fans out in parallel to every ARB
  module, `s` for sync and `r` for compress. Table events write them at LDAC time, so they are the
  tick-accurate path to a module, and a box has at most two independently signaled groups because
  it has exactly two lines.
- **Sync.** A rising edge on the sync line, which makes every listening module snap its waveform
  phase back to the start of a cycle. Multi-region phase coherence is established this way, and
  modules on a common clock are not aligned until it happens.
- **Alternate waveform.** A second wave state held ready in a module and switched into by a signal
  on the compress line: `COMP` to freeze at a cycle's final value, `REV` to reverse the phase step,
  `ARB` for a user waveform, `FIX` for a static profile, `CUR` for the primary waveform at a new
  range or frequency. A module finishes its current cycle before switching, so the switching
  granularity is one waveform period on top of tick accuracy.
- **Wave state.** The condition of a region, i.e. its direction, amplitude, frequency, and whether
  its wave travels or stands. Transitions between wave states compile to alternate-waveform
  switches.
- **TWI.** The I2C link between a controller and its modules, which carries all module state that
  is not time critical. Everything fast travels on the two broadcast lines instead.
- **Twave module.** The older traveling-wave module and its `STW...` command set, section 5 of
  [mips-wire-format.md](mips-wire-format.md). It is not the wave source on this instrument, and the
  section is kept because the compressor concepts and the `c:T` table channel are defined there.
- **Compressor.** Two of them, one on the Twave path and one on the ARB path, each a state machine
  on its own timer that walks a compression table. A table event starts one with `c:T` or `c:A`.
- **Compression table.** The compressor's program, an ASCII mini-language of at most 130
  characters, executed left to right. Unknown characters are skipped and nothing is syntax-checked
  at load time, so a malformed table runs quietly.
- **Compression order.** The parameter *n* that gates off *n* − 1 of every *n* waveform cycles on
  the compress module. Order 1 is normal operation.

## Digitizer and acquisition

- **SA220P.** The Keysight/Acqiris digitizer clockwork acquires with, which appears to software as
  a VISA `PXI...::INSTR` resource.
- **AqMD3.** The Acqiris driver the console is built on, and the name the console carries.
- **The console.** PNNL's AqMD3 Acquisition Console, a ZeroMQ server that streams the digitizer,
  appends scans to a UIMF file and publishes a live summary of each batch. Clockwork is one of its
  clients, and [console-protocol.md](console-protocol.md) is what such a client has to know.
- **Zero suppress, ZS1.** On-card data reduction in which only samples past a threshold are
  streamed, described by a separate marker stream. The console runs it with a hardcoded threshold
  and hysteresis and no pre- or post-gate samples.
- **Gate.** One run of retained samples in a zero-suppressed scan. The zeros between gates are
  stored as negative run-length entries rather than as samples.
- **Record.** One trigger's worth of samples, sized as the measured pusher period less the
  post-trigger delay and the trigger rearm dead time, rounded down to a multiple of 32 samples.
- **`tof width`.** The console command that measures the pusher period from 20 trigger timestamps
  and sizes the record from it. A period that drifts afterwards is not measured again.
- **Post-trigger delay, trigger rearm dead time.** Two `config.txt` values, 10 µs and 2.048 µs by
  default, subtracted from the pusher period when a record is sized.
- **Scan.** One row of `Frame_Scans`, which is one trigger: the gated samples, the TIC, BPI and
  NonZeroCount of that trigger, and its timestamp in samples of the digitizer clock.
- **Frame.** A numbered group of scans. `acquire frame` acquires `frame_length` scans into a named
  file, and every row it writes carries that frame number.
- **Accumulation.** Two senses, and the difference matters. Files written today by FALKOR hold, per
  scan, the sum over many pushes. Through the console `nbr_accumulations` is stored on the frame
  parameters and never applied, one row per trigger, so summing belongs to the client. What a
  method's `accumulations` count means on the wire is a question for the lab record on accumulation
  semantics rather than for these documents.
- **Batch.** The `NotifyOnScansCount` scans, 500 by default, that the console publishes and writes
  in one transaction.
- **TIC, BPI, NonZeroCount.** Per scan, the total ion current, the base peak intensity, and the
  number of samples the gates retained.
- **UIMF.** PNNL's SQLite-based ion mobility format: `Global_Params`, `Frame_Param_Keys`,
  `Frame_Params` and `Frame_Scans`. The console inserts `Frame_Scans` rows only, so the client
  creates the file with its full schema and owns every parameter value.
- **Control I/O.** The digitizer's configurable digital lines. The console points `enable io port`
  at line 2 and sets it to `In-TriggerEnable`, which is how an acquisition is gated by an external
  edge.
- **`start_trigger`, `offset_bins`.** Two fields of the `acquire frame` request. Scans before the
  `start_trigger` index are dropped and `ScanNum` is renumbered from it; `offset_bins` is added to
  the leading zero run of the first gate in every scan.
- **Snappy, protobuf.** The compression and the message encoding the console uses on both sockets:
  a Snappy-compressed protobuf message is the argument of `acquire frame` and the payload of every
  published batch.

## Clockwork's parts

- **The three layers and the seam.** `clockwork.mips`, `clockwork.acq` and `clockwork.method` never
  import Qt, and `clockwork.app` is the PySide6 window on top of them. The seam is what lets a
  script drive one box with no GUI stack installed, and `tests/test_architecture.py` and the
  self-check enforce it.
- **Method.** The saved experiment a trainee loads: the strings each box needs, the acquisition
  settings, and the map from box name to serial port, as one flat TOML document. Its shape is
  [method-file-format.md](method-file-format.md).
- **Provenance stamp.** The record that ties an acquisition to the method that produced it: the
  method's name, the SHA-256 hash of its canonical text, that text in full, and the clockwork and
  console versions. A method's hash changes if and only if the document changes, which makes it a
  stable key for grouping acquisitions.
- **`file_stem`.** The base name a method's UIMF file is written under. A stem containing a path
  separator is rejected at load time.
- **`schema_version`.** The integer pinning a method document to a known layout. Clockwork rejects
  a version it does not recognize rather than guess at the fields.
- **Public self-check.** `uv run tools/check_public.py`, which passes on a machine with no MIPS box,
  no digitizer and no acquisition console. Checks that need one of those report SKIPPED, so a
  failure is a real failure.
- **Lab repo.** The private sibling repository holding this project's development record.
  `clockwork.lab_dir()` resolves it when it is present, and every code path here works when it is
  not.
- **Layer 1 to Layer 4.** Vocabulary from a superseded four-layer plan: an experiment data model
  (1), a compiler from it to table strings (2), deployment to the boxes (3), and acquisition (4).
  Version 1 has no compiler and no data model, so a reference to Layer 1 in the protocol documents
  marks a capability deferred to version 2, and a reference to Layer 3 marks a check that belongs
  at deploy time.
