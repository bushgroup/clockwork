# packaging/

The build and install chain for `clockwork.exe`, adapted from mainspring's (lab record, task 32).
It was built and first measured around a placeholder window, and rebuilt around the real one on
2026-09-17: same excludes, still no hidden imports, 0.15 s more cold start. Task 52 adapts this
chain to carry the acquisition console alongside it, so that one installer puts both on a fresh
instrument PC (lab record, decision 9).

## Building

```
uv run tools/warm_numba_cache.py   # writes packaging/numba_cache_seed/
uv run tools/write_commit.py       # writes src/clockwork/_commit.py
uv run tools/stage_console.py      # writes packaging/console_payload/, if this machine has built one
uv run tools/make_icon.py          # writes src/clockwork/app/resources/clockwork.ico (only after packaging/icon/clockwork.svg changes)
powershell -ExecutionPolicy Bypass -File tools/build_exe.ps1
```

`build_exe.ps1` runs the first three steps itself, cuts `PATH` down to the Windows directories and
`uv`'s own before invoking PyInstaller (a wider `PATH` risks a foreign DLL substitution the way
mainspring's task 07 found once, and `clockwork.spec`'s provenance guard fails the build rather
than ship one), then copies `packaging/console_payload/` beside the built `clockwork.exe` (below),
runs `clockwork.exe --self-check` and times a cold and a warm launch. It needs Windows
PowerShell's script execution allowed for that one invocation (`-ExecutionPolicy Bypass`, or
`Set-ExecutionPolicy` once per machine) -- unset on a fresh clone.

## The acquisition console payload

`tools/stage_console.py` finds this machine's built console the same way a running `clockwork.exe`
finds it at first launch (`clockwork.acq.find_console`: `$CLOCKWORK_CONSOLE`, a `console/`
directory beside the installation, then a lab checkout), copies its executable, DLLs, `config.txt`
and `app.h` into `packaging/console_payload/`, and prints why it staged nothing when this machine
has not built a console yet -- which is not a build failure, since a bare `clockwork.exe` is still
a useful thing to build for testing away from the instrument. `build_exe.ps1` then copies that
directory to `dist/clockwork/console/`, a plain file copy and not a PyInstaller `datas` entry:
PyInstaller's own onedir layout nests bundled data under `dist/clockwork/_internal/`, and
`find_console` looks for `console/` beside the `.exe` itself. `clockwork.iss`'s existing `[Files]`
wildcard (`dist\clockwork\*`, recursive) picks the directory up without a change of its own, so the
installer puts the console beside `clockwork.exe` and the first launch's console path defaults to
it.

The console's own CMake build writes `app.h` beside its executable naming the version and commit
it was built from -- a public fork of PNNL's console
([`bushgroup/AqMD3-Acquisition-Console`](https://github.com/bushgroup/AqMD3-Acquisition-Console),
branch `clockwork`), so the commit is a derived fact and not vendor material. `tools/stage_console.py`
pins the commit it expects in `EXPECTED_CONSOLE_COMMIT`; `tools/check_public.py` compares that pin
against the staged `app.h`, SKIPPED when nothing is staged, and fails if the lab's console checkout
has moved on without the pin being bumped to match.

Compile the installer separately, with Inno Setup 6's `ISCC.exe`:

```
iscc packaging\clockwork.iss
```

The installer lands in `dist\installer\`. Installed via `winget install --id JRSoftware.InnoSetup`
on MASSTRO (2026-09-16), which put `ISCC.exe` under the current user's
`AppData\Local\Programs\Inno Setup 6\` rather than on `PATH` -- call it by that full path, or add
it to `PATH` yourself, until something does that for every clone.

## What differs from mainspring's chain

- **No file association.** mainspring is the only UIMF viewer (`CLAUDE.md`'s decisions of
  record), so `clockwork.iss` carries no `[Registry]` section and no `associate` task.
- **`console=True`**, not mainspring's windowed build: a windowed build redirects stdout/stderr to
  nowhere, which would swallow `--self-check`'s report. The real window kept it on for that
  reason; turning it off costs nothing the day nothing needs the console.
- **`pyserial`'s non-Windows `list_ports` backends are excluded** (`list_ports_linux`,
  `list_ports_osx`) -- dead code on the only OS this ships for.
- **The icon is placeholder art** (`packaging/icon/clockwork.svg`, one plain clock face, one
  source drawing unlike mainspring's two-tier scheme): nothing borrowed from mainspring's spiral,
  free for task 08's successors to replace outright.

## The four new dependencies, and what building for them took

mainspring never packaged `pyzmq` (`clockwork.acq`'s ZeroMQ client, a bundled `libzmq`),
`protobuf` (the console's wire schema; `wire.py` builds `FileDescriptorProto` at runtime rather
than importing `protoc` output), `python-snappy` (the console's compression) or `pyserial`
(`clockwork.mips`'s COM port link). **None of the four needed a hidden-import fix**:
`pyinstaller-hooks-contrib`'s `hook-zmq.py` and PyInstaller's own `hook-sqlite3.py` (protobuf's
upb extension and snappy's native library both follow as ordinary binary dependencies once
something imports them) covered pyzmq and protobuf, and `--self-check` -- which exercises
`clockwork.mips` (pyserial), `clockwork.acq` (pyzmq, protobuf, snappy) and the fold (numba,
llvmlite) end to end -- passed against the frozen build on the first `pyinstaller` run that got
past the provenance guard. Two warnings appeared and neither is a real problem:

- `WARNING: Hidden import "scipy.special._cdflib" not found!` -- a stock PyInstaller hook naming
  a private scipy module that this scipy version does not have; nothing here imports it either.
- `WARNING: Library not found: could not resolve 'tbb12.dll'` -- numba's optional Intel TBB
  threading backend; not installed, and numba falls back to its default backend, which
  `--self-check`'s fold already exercises successfully.

The numba/llvmlite seed problem mainspring already solved end to end is copied wholesale rather
than rediscovered: `tools/warm_numba_cache.py` and `clockwork.app._seed_numba_cache`, both against
`mainspring.uimf.decode` (the same kernels mainspring's own build warms, since `clockwork` and
`mainspring` share that module).

## `pyqtgraph`

Comes in transitively through mainspring's own viewer dependency; nothing in clockwork imports it
(`clockwork.app` draws nothing; mainspring is the only viewer, Matt, 2026-09-10). Dropped as a
direct dependency (`pyproject.toml`); confirmed excluded from the build with 0 files under that
name, so the exclusion list in `clockwork.spec` is doing real work, not standing in for one.

## Open

- **Where the installer is validated** (task 52 step 5): still open in the lab record. The
  installer itself compiles clean on MASSTRO -- what remains is running it on a Windows install
  that never had the dev toolchain on it, since developing on the deployment machine is what hides
  a missing dependency until someone else runs the build.
- **The console subsystem is on** (`console=True`) so `--self-check` has somewhere to print;
  `tools/build_exe.ps1`'s launch check waits past the console's own default-titled window before
  reading the title, which a windowed (`console=False`) build never needed to. The real window
  left it on, so a trainee launching the installed build sees a console window beside the Qt one;
  whether that is worth keeping for `--self-check`'s sake is the open half.

