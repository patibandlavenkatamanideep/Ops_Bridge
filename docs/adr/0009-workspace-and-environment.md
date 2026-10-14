# ADR 0009: Workspace location and regenerable environments

Status: accepted (2026-10-14)

## Context (observed, not assumed)
The first working copy lived in `~/Desktop/Projects/Ops_Bridge`. That folder is synced by
iCloud "Desktop & Documents" and the disk was ~96 % full (8 GB free). Observed effects:
- uv's editable-install `.pth` files received the macOS `hidden` flag; CPython 3.12.12
  skips hidden `.pth` files, so `import opsbridge` failed until the venv was moved to an
  iCloud-excluded `.venv.nosync` directory.
- Within minutes, iCloud evicted 226 project files and 147 of 152 `.git` files to
  "dataless" stubs (including `.env`). Reading them blocked indefinitely (`head` on a
  1 KB file did not return within 20 s; `brctl download` did not restore them), which
  hung `python -m opsbridge.cli --help` and earlier made test runs take up to 132 s.

## Decision
- The working copy moved to `~/Projects/Ops_Bridge` (outside iCloud) as a fresh clone of
  the pushed branch; uncommitted Phase 1 files were rewritten from the session and `.env`
  was rebuilt from the running containers' configuration without printing values.
- Regenerable environments keep `.nosync` names (`backend/.venv.nosync`,
  `.pycache.nosync`) so a checkout in a synced folder degrades less badly.
- `scripts/doctor.py` reports a checkout inside an iCloud-synced folder as a problem.
- npm does not accept a symlinked `node_modules`; in synced folders the macOS attribute
  `com.apple.fileprovider.ignore#P` can exclude it, but the supported answer is to keep
  the checkout outside synced folders.

## Consequences
- The old Desktop copy is left untouched (its evicted files may still sync back later);
  it is not used by OpsBridge.
- Development `.env` secrets from the first location were uploaded to iCloud before
  eviction. They are development-only values for local containers; rotate them with
  `make reset-demo CONFIRM=delete-local-demo-data` followed by `make setup` if that matters.
