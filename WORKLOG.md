# Work log

## 2026-04-15 — Initial implementation from SPEC.md

### Deliverables produced
- `git_remote_tasks.py` — single-file remote helper (1398 lines).
- `test_git_remote_tasks.py` — unittest suite (1422 lines, 179 tests).
- `tasks-init` — bash initializer, verified end-to-end against a temp repo.
- `README.md` — all 12 sections mandated by spec.
- `requirements.txt` — documents the optional `msal` extra (stdlib otherwise).
- `.gitignore` — added `.coverage`, `__pycache__/`, `*.pyc`, `.venv/`.

### Build order followed
Implemented strictly in the order SPEC.md prescribed so each layer was
testable before the next depended on it:

1. Unified schema (`empty_task`, `normalize_task`, `TASK_FIELDS`).
2. YAML serializer — hand-written emitter plus line-based state-machine
   parser (no PyYAML). Round-trip verified.
3. Org-mode serializer — headline, `:PROPERTIES:`, `:LOGBOOK:`, body.
4. Symmetry check — both formats deserialize to the identical unified dict.
5. Config reader via `git config --local` subprocess wrapper.
6. Driver base class + four drivers: Jira, Vikunja, MS Todo, Notion, each
   with full field-normalization logic and mockable `_http_get` /
   `_http_post` seams.
7. `ProtocolHandler` — capabilities, list, import (with `deleteall`
   snapshots and sorted blobs for deterministic hashes), export
   (M/D dispatch, blob-mark tracking).
8. Management subcommands: `install`, `uninstall`, `list-schemes`, `check`.

### Design decisions
- **Logbook preservation.** Spec requires org `:LOGBOOK:` entries to
  survive a round trip even though the unified schema does not list a
  `logbook` field. Chosen approach: both serializers treat `logbook` as
  an *optional extension key* — absent on pure unified inputs so the
  SerializerSymmetry invariant still holds, but preserved when an org
  file supplies it.
- **Date semantics.** Org timestamps use UTC conversion for
  round-trip stability. An ISO string with timezone offset is normalized
  to UTC before formatting, then re-emitted as `…Z`. A plain date
  (`2025-04-20`) round-trips without gaining a synthetic time component.
- **HTTP seams.** Each driver exposes `_http_get` / `_http_post` methods
  that the tests monkey-patch. The underlying `_http_request` uses
  `urllib.request` so the code path is exercised without shipping a
  hard dependency on `requests`.
- **Secret handling in `check`.** Keys containing `token` or `password`
  (case-insensitive) are redacted in the stdout summary; only the
  required-key presence check leaks structural info.
- **Notion pull-only.** `upsert` and `delete` raise
  `NotImplementedError("Notion is pull-only")` exactly as the spec
  requires, rather than silently no-op'ing.

### Test posture
- `unittest` + `unittest.mock` only, per spec (explicit override of the
  global rule that would prefer pytest).
- 179 tests, 0 failures.
- Coverage: **97%** on `git_remote_tasks.py`.
  The remaining ~3% is in defensive fall-throughs: the raw-bytes stdin
  branch used only when `sys.stdin` lacks a `.buffer`, the `urlopen`
  network path (marked `pragma: no cover`), and a handful of
  single-character early-return guards.

### Verification commands
```bash
python -m unittest -v test_git_remote_tasks
python -m coverage run -m unittest test_git_remote_tasks \
  && python -m coverage report -m --include=git_remote_tasks.py

# End-to-end smoke for tasks-init:
mkdir /tmp/tkt && /path/to/tasks-init --format yaml --dir /tmp/tkt
(cd /tmp/tkt && git log --oneline && git config --local --get tasks.format)
```

### Known limits / deferred work
- Live API write paths (Jira/Vikunja/MSTodo `upsert` & `delete`) remain
  stubs that raise `NotImplementedError` with a TODO message. The
  read/fetch paths are wired and pagination-complete; writes were out of
  scope for this experimental cut.
- MS Todo device-code OAuth flow is only reachable when `msal` is
  installed; absent that, `fetch_all` raises unless an `accessToken` is
  preconfigured.
- Fast-import blob data is written through `sys.stdout.buffer` when
  available, falling back to a UTF-8 text write otherwise. The fallback
  is exercised only in tests using `io.StringIO`; real git invocations
  always expose `buffer`.
