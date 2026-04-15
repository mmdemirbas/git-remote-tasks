# Work log

## 2026-04-15 — Fetch throughput: bump default page size and expose knob

Context: first live `git fetch jira` against a 7808-issue project ran
~2.5 minutes. Fast-import itself is fine; the time is spent in
sequential HTTP round-trips (156 pages × RTT).

### Change
- New `Driver._page_size()` reads `tasks-remote.<name>.pageSize` and
  clamps to each driver's `PAGE_SIZE_MAX`.
- Jira: default 50 → 100 (the cap on Jira Cloud's `/search/jql`). Halves
  round-trips on any project over 50 issues.
- Vikunja: default 50 → 100 (`PAGE_SIZE_MAX` 250). Same motivation.
- Notion already uses the API's default 100; unchanged.
- MS Todo delta-query semantics don't expose a page-size knob; unchanged.

### Non-goals
- Parallel pagination: rejected for Jira's new endpoint because
  `nextPageToken` is opaque. The legacy `startAt` path could parallelize
  after one `total` read, but the complexity isn't worth it when the
  incremental path covers every subsequent fetch.
- Local CPU: fast-import on 8k small blobs takes under a second.

### Tests
- Unit tests for `_page_size`: default, override, empty string, non-numeric
  fallback, clamp to 1, clamp to max, per-driver max.
- Integration-style tests on Jira + Vikunja that assert the emitted URL
  contains the expected `maxResults=` / `per_page=` value both for the
  default and a config override.

344 tests, all green.

## 2026-04-15 — First live run against all four services

Exercised every driver end-to-end against real remotes (Vikunja on
localhost, Jira Cloud, Notion, MS Todo). Four regressions surfaced, one
per bug class. All of them were invisible to the stdlib suite because
the suite never wired a fast-export stream into the handler through a
real `sys.stdin`.

### Regressions fixed

- **`*fetch` / `*push` advertised as capabilities.** The helper only
  implements `import` and `export`. Git preferred the native
  `fetch` / `push` commands and then failed with
  `unknown command: 'fetch <sha> <ref>'`. Removed both lines from
  `_cmd_capabilities`.
- **`commit refs/heads/main` clobbered the user's own branch.** The
  refspec capability named `refs/remotes/<name>/*` as the destination
  but `_emit_commit_header` still wrote to `refs/heads/main`. Fast-import
  refused to fast-forward:
  `Not updating refs/heads/main (new tip X does not contain Y)`.
  Switched to a private namespace, matching the `git-remote-testgit`
  convention:
  - capability: `refspec refs/heads/*:refs/tasks/<name>/heads/*`
  - header: `commit refs/tasks/<name>/heads/main`
  Git reads from the private ref and applies the user's fetch refspec
  to update `refs/remotes/<name>/main`.
- **Export parser desync on real stdin.** `sys.stdin` is a
  `TextIOWrapper` whose `readline()` buffers extra bytes in an internal
  decode buffer. A subsequent `sys.stdin.buffer.read(n)` skips those
  buffered bytes — the blob body loses its prefix, the parser reads a
  URL value as the next `data N` header, and fast-export dies with
  `invalid literal for int() with base 10: 'http://...'`. Introduced
  `_BinaryStdinReader`: reads everything through the binary layer,
  decodes a line at a time, exposes `.buffer` for exact-byte reads. No
  second layer of buffering.
- **`tasks/.gitkeep` rejected as a suspicious path.** The path-safety
  check rightly rejects leading-dot names (`tasks/.git*`), but
  `.gitkeep` is a universal repo-hygiene file and shouldn't fail a push.
  Added `_is_task_file_path` as a cheap pre-filter: if the path doesn't
  end in `.yaml`/`.yml`/`.org`, silently skip it. Applies to both `M`
  and `D` directives. `tasks/README.md`, `tasks/notes.txt`, etc. are
  now tolerated too.

### Tests added
- `test_import_writes_to_private_namespace` — pins the new refspec
  destination.
- `test_binary_stdin_reads_blob_bytes_accurately` — end-to-end with a
  real `BytesIO` source mimicking git's binary stdin.
- `test_binary_stdin_reader_readline_decodes` — unit coverage for the
  shim.
- `test_gitkeep_under_tasks_silently_ignored`,
  `test_readme_under_tasks_silently_ignored`,
  `test_delete_of_gitkeep_silently_ignored`,
  `test_unknown_extension_in_export_silently_ignored` — four pins for
  the non-task companion policy.
- `TestTaskFilePath` class — positive / negative cases for the new
  helper.

324 → 332 tests. All green.

### Verified live
- Vikunja: full snapshot fetched, single-task priority edit pushed via
  `POST /api/v1/tasks/{id}`; confirmed via direct API read that the
  server-side `priority` and `updated` fields moved.
- Jira Cloud, Notion: full snapshot fetched.
- MS Todo: first run prompts for device-code auth; subsequent runs are
  silent once the refresh token is persisted.

### Notes
- The capabilities fix is backwards-incompatible with any previously
  fetched state: the first fetch after upgrade recreates
  `refs/tasks/<name>/heads/main` (private) alongside the existing
  `refs/remotes/<name>/main`. No user-visible change.

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

## 2026-04-15 — Adversarial review and PLAN.md

Reviewed the full delivery (code + tests + README + WORKLOG) against the
"what would break this in real use" question, then combined the review
findings with three product decisions the user raised:

1. documentation is a last-resort warning, not a fix;
2. push should work for every non-pull-only remote, not just Notion's
   explicit refusal;
3. custom field / status / priority mapping must be a first-class
   concept, as it is in the ancestor project `todo-harvest`
   (`todo-harvest/src/normalizer.py` applies `status_map`,
   `priority_map`, `field_map` per source).

### Bugs uncovered

- **Jira epic name drops to `None`** for the realistic dict-shaped
  `customfield_10014` payload — a Python operator-precedence bug in the
  inline ternary. Only the string branch is covered by tests.
- **`git push` reports `ok <ref>` even when every upsert raised
  `NotImplementedError`.** Users see exit 0 and trust that writes
  landed. This is the single most misleading behaviour in the cut.
- **`ProtocolHandler._read_exactly`** calls `stdin.read(n)` on the
  text-stream fallback path; `n` is a byte count, so multi-byte UTF-8
  bodies are silently truncated. Not hit in production (git provides
  `stdin.buffer`), but advertised as "defensive" — it is actively wrong.
- **Every fetch is a root commit.** No `from :mark` is emitted, so
  `refs/remotes/<remote>/main` never forms a linear history and
  `git merge <remote>/main` fails without
  `--allow-unrelated-histories`. README Quick Start teaches the broken
  form; only `tasks-init`'s stdout hints at the flag.
- **Timezones silently normalize to UTC** in `_iso_to_org_timestamp`.
  Documented in this worklog, not in the README; for an agenda user this
  shifts the displayed due-hour.
- **`cmd_uninstall` unlinks any file** named `git-remote-<scheme>` —
  including unrelated helpers installed into the same bin dir.
- **Secret redaction in `check`** matches only `token`/`password`
  substrings, leaking `apiKey`, `clientSecret`, `access*`, etc.
- Plus: Jira task URL falls back to the `jira://` scheme, `mstodo`
  `check` passes without a token, org `:DEADLINE:` is stored as a
  property (not agenda-visible), YAML nested hyphen keys vanish.

### Design decisions reached

- **`tasks-init` stays `git init`-shaped.** `git clone` would force us
  to refuse non-empty directories and would assume a source URL we do
  not have at init time. Planned change: replace `--dir` with a
  positional optional path, matching `git init [path]`.
- **Custom mapping** will be expressed as per-remote git-config keys
  (`tasks-remote.<name>.statusMap.*`, `priorityMap.*`, `fieldMap.*`,
  `jqlFilter`, `projectId`), applied in both directions. Unknown values
  fall back to the unified default *and* warn on stderr, never silently.
- **Runtime warnings are mandatory** for any surviving limitation.
  Documentation is additive, never a substitute.

### Deliverable

- `PLAN.md` captures every finding with IDs (BUG-/FEAT-/SEC-/DX-/TEST-/
  DOC-), priorities (P0/P1/P2), and an execution order. Eighteen
  concrete tasks plus four P2 docs notes that will be deleted as the
  backing fixes land.

Next action: tackle P0 correctness (BUG-01, BUG-04, BUG-05, BUG-07,
SEC-01, SEC-02) and the runtime-honesty pair (BUG-02, FEAT-04) before
the write paths (FEAT-01 → FEAT-02 → FEAT-03).

## 2026-04-15 — P0 correctness sweep shipped

Six fixes landed across six commits. Tests green (191 → now 193 after
timezone round-trip additions).

- `58e9311` BUG-01 Jira epic dict-shape category. Operator precedence
  bug replaced with explicit if/elif; tests added for dict epics with
  either `name` or `summary`.
- `24cfca9` BUG-04 `_read_exactly` byte/char mismatch. Fallback now
  counts UTF-8 bytes. Multi-byte blob round-trip test added.
- `5b0a712` BUG-05 Linear fetch history. `git rev-parse --verify` the
  previous remote tip; emit `from <sha>` when present so `git merge`
  and `git bisect` behave. First fetch still omits `from`.
- `f7a4a1c` BUG-07 Timezone offset preserved in org timestamps.
  Agenda users see 12:30 +03:00 instead of 09:30 Z. Round-trip tests.
- `438ed21` SEC-01 / SEC-02. Widened `cmd_check` redaction with an
  allow-list; hardened `cmd_uninstall` to only unlink symlinks that
  actually target our script.

## 2026-04-15 — Redirected by user on three new fronts

Mid-execution the user raised three requirements that reshaped the
plan:

1. **Incremental fetch.** External services are not git remotes; they
   have no native object graph. Pulling the full task set every
   `git fetch` is infeasible at real scale. Researched each service:
   - Jira: JQL `updated >= "<iso>" ORDER BY updated ASC`.
   - Vikunja: `filter=updated > '<iso>'&sort_by=updated`.
   - MS Todo: Graph delta query with persisted `@odata.deltaLink` —
     the only one with native deletion tombstones.
   - Notion: `filter.last_edited_time.after = <iso>`; archived pages
     come back with `archived: true`.
   Captured as **FEAT-06** (sync-state per remote in `.git/config`)
   and **FEAT-07** (emit per-file `M` / `D` on incremental runs
   instead of `deleteall`).
2. **Notion push.** The "pull-only" label was self-imposed. Notion
   supports `POST /v1/pages`, `PATCH /v1/pages/{id}`, and
   `PATCH archived: true`. Captured as **FEAT-08**; the invariant is
   removed from `CLAUDE.md`.
3. **YAML parser safety.** The single-file, stdlib-only constraint
   means we own the parser. Test-time dependencies are OK (confirmed
   explicitly); `hypothesis` fuzz tests plus an adversarial corpus go
   into **TEST-04**, with a documented supported-subset list so the
   bug surface is bounded.

PLAN.md and CLAUDE.md updated to match. Execution order revised:
runtime honesty (BUG-02, FEAT-04) → parser audit (TEST-04) →
tasks-init ergonomics → incremental fetch (FEAT-06 → FEAT-07) → write
paths (FEAT-01 → FEAT-02 → FEAT-08 → FEAT-03) → org agenda (FEAT-05).

### SPEC.md retirement

SPEC.md was the original delivery specification. Now that the
implementation is in git history and PLAN.md tracks forward work,
SPEC.md is redundant. Git preserves it at commit `8600dde` if we ever
need to look back. Removed in the same commit batch as the plan
update.

## 2026-04-15 — Remaining features landed; PLAN reduced to invariants

Through commits `893baad` … `0b2a91f` every backlog item from PLAN.md
shipped:

- `893baad` DX-01 / DX-04 tasks-init switched to positional path.
- `aaf4b93` / `7102031` / `8d60c55` / `ab94397` write paths for
  Vikunja, Jira, MS Todo, Notion. Each refuses cross-source ids with
  a driver-specific error class so silent duplication is impossible.
- `8d60c55` MSAL device-code flow with refresh-token persistence.
- `bbd1f2c` incremental fetch infrastructure + Jira / Vikunja
  driver overrides. MS Todo and Notion keep the base fallback for now
  (tracked as FEAT-06b / FEAT-06c in PLAN §2.1).
- `a95c30a` the bash `tasks-init` script is gone; its behaviour moves
  into `python git_remote_tasks.py init [path]` with `tasks-init` as
  a symlink alongside `git-remote-*`. Single-file distribution is
  restored.
- `949cdf4` FEAT-03 `statusMap.*` / `priorityMap.*` / `fieldMap.*`
  wired into every driver, bidirectionally.
- `0b2a91f` the cleanup batch: FEAT-05 org DEADLINE on agenda line,
  BUG-06 hyphens in YAML nested keys, BUG-08 no more `jira://` links,
  BUG-10 mstodo one-of required keys, BUG-11 org drawer terminates
  on stray headline, DX-02 debug traceback on
  `GIT_REMOTE_TASKS_DEBUG=1`, DX-03 install prints source path.

Tests: 247 in the default suite, 258 with hypothesis in `.venv`.
DONE.md now catalogues every resolved ID with its commit. PLAN.md is
reduced to invariants, two open driver-specific incrementals
(FEAT-06b / FEAT-06c), explicit out-of-scope, and a how-to-add-a-
service runbook.

Next: a second adversarial review of the current codebase — not the
same spots as the first, because those are fixed. Fresh axes:
concurrency / race conditions on sync state, error resilience under
network failure, UX when credentials expire mid-run, config drift
when a remote is renamed, partial-failure semantics during batch
pushes.

## 2026-04-15 — Round-two review closures and public-release prep

### Incremental fetch for every service

FEAT-06b (MS Todo Graph delta) and FEAT-06c (Notion
last_edited_time) shipped in `456a4ee`. Every driver now implements
a real `fetch_changed(since)` — the base-class fallback to
`fetch_all()` is still there for custom drivers, but all four
shipped drivers override it:

- Jira: JQL `(base) AND updated >= "<ts>" ORDER BY updated ASC`.
- Vikunja: `filter=updated > '<ts>'`.
- MS Todo: `/me/todo/lists/{id}/tasks/delta` with `@odata.deltaLink`
  persisted per list under `sync.deltaLink.<hex(listId)>`. `@removed`
  tombstones surface as `mstodo-<id>` deleted_ids — the only driver
  with native upstream-deletion detection.
- Notion: `/v1/databases/{id}/query` with the `last_edited_time`
  filter and `archived:true` promoted into deleted_ids on
  incremental runs (full snapshots filter archived pages out entirely).

### Rename

The `msftodo` scheme is now `mstodo` everywhere. Pure rename — no
behaviour change. Commit `02a642b`.

### Negative and edge-case tests

17 new tests in `87aee37` covering: case-insensitive pop/setdefault,
subprocess timeouts, unset-config partial failure, redacted-URL
preservation, strip-order-by edge cases, Vikunja non-numeric id
refused, MSAL device-flow failure paths, refresh persistence
warning, Notion inverse map with empty and colliding entries,
contiguous mark numbering after unsafe-id skips, cmd_init invalid
format, leading-dot path rejection (feature added: `_is_safe_tasks_path`
now refuses `tasks/.hidden` to avoid git-internal-name shadowing).

Default suite 324 tests / 335 with hypothesis — all green.

### Documentation refresh

README rewritten end-to-end to drop stale claims ("MS Todo and Notion
fall back to a full fetch", "Notion is pull-only" troubleshooting
entry), and extended with:

- A combined §7.1 table covering every sync-state key the helper
  writes, including the per-list delta links.
- A §7.2 rewrite with both dotted-key and JSON-encoded forms for
  status/priority/field maps, and an explicit callout that Notion
  push inverts the user's statusMap so the database's real options
  are used.
- Per-service setup bumped to current keys (`projectKey`,
  `projectId`, `defaultListId`, `jql`).
- New §13 Development section with venv + fuzz + live test commands.
- New §14 Licence pointer.

CLAUDE.md rewritten to match: removed "write paths are stubs" (none
are stubs now), added the invariants introduced in later batches
(path traversal, timezone offset round-trip, two-phase sync
watermark, cross-source refusal synchronous).
