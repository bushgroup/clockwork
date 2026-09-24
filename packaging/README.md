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

## Releasing

**What a 1.x version promises** (from 1.0.0, 2026-09-22): the three things a user or a downstream
reader relies on stay compatible across every 1.x release --

- **the method file**: a method a 1.x release saved still loads in every later 1.x. The loader
  refuses any `schema_version` but `clockwork.method.SCHEMA_VERSION` (2 at 1.0.0), so moving that
  number inside 1.x means the loader accepting 2 as well; a new key is fine, a renamed or removed
  one is not;
- **the files a run leaves**: the `YYMMDD_INITIALS_NNN` stem and the names of the files beside it
  (the raw and summed UIMF, the transcript, the send log);
- **the UIMF parameters clockwork writes**, through `mainspring.uimf`.

A change that breaks one of those is a 2.0.0. A change that adds without breaking is a minor
release, a fix is a patch. Everything else -- the window's layout, module internals, the self-check's
wording -- carries no promise.

**Cutting a release**, the way 1.0.0 was cut:

1. One commit that changes the version and nothing else: `pyproject.toml`, `clockwork.__version__`
   and `packaging/clockwork.iss` together (`check_public.py` fails unless they agree), then
   `uv lock`. `uv run tools/check_public.py` and `uv run pytest` pass before it is committed; push it.
2. Build from a clean tree, so `src/clockwork/_commit.py` names that commit with no `-dirty`:
   `tools/build_exe.ps1`, then `ISCC.exe`, giving `dist\installer\clockwork-<version>-setup.exe`.
3. Install that file on a machine other than the one that built it, and run its self-check.
   The build is windowed, so PowerShell waits for it only through `Start-Process`:

   ```
   $p = Start-Process -FilePath "$env:LOCALAPPDATA\Programs\clockwork\clockwork.exe" -ArgumentList '--self-check' -Wait -PassThru -NoNewWindow; "exit code: $($p.ExitCode)"
   ```

4. Only then, an annotated tag on the bump commit, pushed: `git tag -a v<version> <commit> -m
   "clockwork <version>"`, `git push origin v<version>`. The tag always names the commit the
   installer was built from. Then attach the installer to a GitHub release on that tag.
5. Straight after, a second version-only commit moves `main` to the next development version
   (`1.1.0.dev0` after `1.0.0`), in the same three places plus `uv lock`. After that, no build from
   `main` can carry the number of the release before it.

**Versions between releases** follow PEP 440, in its normalized spelling in all three declarations
(`1.1.0rc1`, not `1.1.0-rc.1`), since `check_public.py` compares them as strings:

- **Development builds** (`X.Y.Z.devN`) get no tag and no release. `_commit.py` names the commit
  one came from.
- **A build handed to the bench for trial** is a release candidate, `X.Y.ZrcN`. It is cut by the
  same steps as a stable release, and its GitHub release is marked *pre-release*, so "Latest" keeps
  naming the last stable one.
- **A pushed tag is never moved or deleted.** A mistake in a release is fixed by the next patch
  or candidate number.

## What differs from mainspring's chain

- **No file association.** mainspring is the only UIMF viewer (`CLAUDE.md`'s decisions of
  record), so `clockwork.iss` carries no `[Registry]` section and no `associate` task.
- **Windowed (`console=False`), mainspring's own choice, since task 60.** A trainee launching the
  installed build sees the Qt window and nothing else -- no second, closeable console window whose
  close box kills the run. `--self-check` still reports: `clockwork.app.main` attaches to the
  console it was started from (`AttachConsole`) when there is one, and falls back to a per-user log
  file (`%LOCALAPPDATA%\clockwork\self-check.log`) when there is not.
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

- ~~**Where the installer is validated** (task 52 step 5).~~ **Closed 2026-09-19** on a clean
  Windows 11 LTSC install that never had the dev toolchain on it: per-user install, `--self-check`
  with `PATH` cut to the Windows directories, a full `--fake` run, clean uninstall, nothing missing
  (lab record, tasks 32 and 52). A release's step 3 above repeats the self-check part of it.

