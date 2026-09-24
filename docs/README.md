# Documentation

- [**MIPS wire format**](mips-wire-format.md). How a MIPS controller box takes a pulse-sequence
  table over its serial link, how it times and releases it, what it reports back, and how the ARB
  modules that drive the traveling-wave regions are switched from a table. Derived from the
  public firmware and cross-checked against the manufacturer's documents.
- [**Acquisition console protocol**](console-protocol.md). The ZeroMQ command set, message
  formats and data stream of PNNL's AqMD3 Acquisition Console for the SA220P digitizer, and the
  division of UIMF writing between the console and its client. Derived from the console's source.
- [**Daemon protocol**](daemon-protocol.md). `clockwork serve`, the process that owns the boxes
  and the acquisition console with no window, and the loopback ZeroMQ protocol its clients use:
  the requests and replies, the event stream, the instrument lock, shutdown, and what happens when
  either side dies.
- [**MCP server**](mcp-server.md). `clockwork mcp`, the tools an agent such as a Claude Code
  session uses to turn a person's request into an experiment: templates and methods, the boxes,
  acquisition and reading the files back, the interlock that decides what may be sent, and the
  audit log of every call.
- [**Standing limits**](instrument-limits.md). The `limits.toml` beside the instrument file that
  bounds what an agent may send: the templates it may run and how far each knob may turn, the
  boxes it may address, the budget per daemon session, the cold-start check against what the
  boxes hold, and the run record every request leaves beside its files.
- [**Method file and provenance**](method-file-format.md). The flat TOML document a trainee loads
  to run an experiment: each box's strings in three phases, the ordered sequence that starts and
  restarts them, and the stamp that traces every acquisition back to the method that produced it.
- [**Method template**](template-file-format.md). A method with holes in its strings and the
  knobs that fill them: the range each knob may take, the arithmetic that carries one knob across
  boxes and clock domains, and the rendering that produces an ordinary method before anything is
  sent.
- [**Instrument file**](instrument-file-format.md). The flat TOML document beside the method that
  records what a file states about the machine rather than the experiment: the m/z calibration and
  its two forms, and the full scale and channel offset the digitizer acquired through.
- [**Glossary**](glossary.md). What the terms in these documents, in the package and in a method
  file mean, from SLIM and the pusher pulse to `STBLDAT`, zero suppress and the provenance stamp.
- [**User guide**](user-guide.md). The installed window, from the two vendor installs it needs
  before it to a first acquisition: the panes and their tags, Send setup, Load and arm, Acquire,
  replicates, the state panel, and the files a run leaves on disk.

The top-level [`README.md`](../README.md) covers running from source.
