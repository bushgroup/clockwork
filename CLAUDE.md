# clockwork — public-role code repo

Package `clockwork`: control software for the Bush lab's SLIMPHONY-style ion mobility mass
spectrometer. It sends pulse-sequence strings to MIPS controller boxes over USB serial, drives the
Keysight/Acqiris SA220P digitizer through PNNL's AqMD3 acquisition console over ZeroMQ, and
produces UIMF files that `mainspring` reads. It replaces FALKOR. This is the **public-role half of
a two-repo arrangement**: it carries `src/clockwork/`, `tools/`, `tests/`, `docs/`, `packaging/`
and the public self-check — nothing else. The development record lives in the private sibling
**`bushgroup/clockwork-lab`** (tasks, notes, explorations, vendor manuals, wiring maps, and the
working CLAUDE.md with the full project state and never-do list).

**Sessions are based here.** A gitignored `CLAUDE.local.md` imports the lab repo's CLAUDE.md; if
you are reading this file *without* that import, you have a public clone — the package and
`tools/check_public.py` are fully usable, the lab material is not yours to see.

## Rules that live with the code

- **Run anything with `uv run <path/to/script.py>`** — no flags, no venv activation. Never rely on
  the `python` on PATH.
- **`uv run tools/check_public.py`** after touching `src/clockwork/`. Hardware-free: a clone with
  no MIPS box, no digitizer, no acquisition console and no lab repo must pass it. Checks that need
  one of those report SKIPPED, never FAIL.
- **Three layers under one seam.** `clockwork.mips` (serial sender and response parser),
  `clockwork.acq` (ZeroMQ client to the acquisition console, UIMF file and parameter creation) and
  `clockwork.method` (the saved per-box strings plus acquisition settings) **never import Qt**;
  `clockwork.app` is PySide6 on top of them. A script that drives one box must not pull a GUI in.
- **Never talk to hardware from the UI thread.** An `STBLDAT` table string must stream to the box
  without a stall of more than a few seconds or the box abandons it; sends and acquisitions run on
  worker threads and report back.
- **Protocol facts live in `docs/`, code cites them.** `docs/mips-wire-format.md` is the MIPS
  wire protocol; `docs/console-protocol.md` is the acquisition console's ZeroMQ command set. Code
  never redefines either locally; a discrepancy is fixed in the document first.
- **Derived facts only, never vendor documents.** GAA's manuals are proprietary and stay in the
  lab repo; the MIPS and ARB firmware is public on GitHub and may be quoted with a commit hash.
- **No acquisition data here** — `.gitignore` excludes `.uimf` and `.githooks/pre-commit` refuses
  the path. No trainee method files or wiring photographs either; those are lab material.
- **Windows first.** The instrument PCs run Windows 11 LTSC; the installer is the primary
  deliverable. Nothing should *break* elsewhere, but nothing else is tested.
- **Lab-side paths resolve through `clockwork.lab_dir()`**: `$CLOCKWORK_LAB`, then this root, then
  the sibling `../clockwork-lab`. Nothing in this repo, code or docs, refers to lab material except
  in opaque form ("lab record, task NN"); never by a path that only resolves lab-side.
- **Public commit messages are self-contained statements of the change.** Task IDs may appear as
  opaque references at most. Trailer is `Assisted-by: <model name>`, no email — never
  `Co-Authored-By:`. `.githooks/commit-msg` rewrites, `.githooks/pre-commit` rejects staged files
  over 5 MiB and any `.uimf` path; both need `git config core.hooksPath .githooks` once per clone.
- **`.gitattributes` pins `* text=auto eol=lf`.**
- **Outward-facing prose (README, `docs/`, user guide) follows the `manuscript-voice` skill** from
  the lab repo. Repo-internal prose (this file, docstrings, commit messages) does not.
- **BSD 3-Clause, `LICENSE`, copyright University of Washington.** Written from the first commit
  (2026-09-06) as if public. The version is declared in `pyproject.toml` and
  `clockwork.__version__` (and in the Inno Setup script once one exists); `check_public.py` fails
  unless they agree.

## Maintaining this file

This file stays lean: rules for working *in this repo*, nothing about the science or the project's
state. Those belong in the lab repo's CLAUDE.md and notes. Keep it under 60 lines.
