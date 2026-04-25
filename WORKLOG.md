# Work log

## 2026-04-25 — Correctness audit: 18 findings closed across five commits

Self-initiated correctness audit. Read the source end-to-end, probed
suspect paths empirically, drafted 18 findings, then implemented and
tested every actionable one. Test count 352 → 401.

### What the audit found (and what shipped)

**P0 — silent data loss on round-trip (commit `caf4afe`).** Five
distinct failure modes, all reproducible from a fresh checkout, all
landing as the same commit:

- **R-01** `_handle_modify` trusted `task["id"]` over the filename. A
  stale or copy-pasted file with `id: jira-OTHER` would PATCH the
  wrong upstream issue. The filename is the operator's intent; the
  helper now rejects content/filename mismatches and canonicalizes
  the id to the filename when content omits it.
- **R-02** Org body line `* note` was parsed as a new headline and
  dropped because the guard fired on `lstrip().startswith("*")`. The
  serializer always indents body lines, so column-zero `*` is the
  real boundary. Tightened the check.
- **R-03** Org `:TAGS:` used `,` as a separator with no escaping; a
  tag containing a comma got split into multiple tags on round-trip.
  Added quoting via `"` with `\"` and `\\` escapes; legacy unquoted
  files still parse since the new reader treats bare commas as
  separators identically.
- **R-04** YAML `\r` in a description silently truncated the value
  because `splitlines()` treats CR as a line break and the emitter
  wrote raw CR. Force the double-quoted form when CR or TAB are
  present; decode `\r` / `\t` escapes on parse. Block scalars are
  still used for plain LF-only multiline. R-16 (TAB-escape) shipped
  with this since the implementations are intertwined.
- **R-05** Notion `_query_pages` looped forever when `has_more=True`
  came paired with `next_cursor=None` (observed during eventual-
  consistency windows). Defensive break on falsy cursor.

**P1 — push-side hardening (commits `2784710`, `5bf53dd`).**

- **R-06 / R-07** `_select_payload` defaulted to `select` shape when
  the column was missing from the database schema, producing payloads
  Notion 400s on. Now: skip with one-shot warning. Same treatment
  for tags (multi/single/missing each handled), due_date, and
  description shape mismatches.
- **R-08** `_write_incremental_import` emitted `D tasks/<id>.<ext>`
  from `deleted_ids` without validating the id, so a buggy or hostile
  upstream could push paths like `D tasks/../etc/passwd.yaml` into
  the stream. fast-import accepts those silently as a no-op miss;
  symmetric `is_safe_task_id` filter added.
- **R-10** Jira's transition-failure message now names the half-
  applied state ("fields updated, but no transition to … is available
  for PROJ-1 on its current workflow") so operators know the field
  PUT already landed.
- **R-11** All four drivers treat 404 / 410 on delete as soft success.
  `git rm` + push for an issue that the upstream service already
  removed used to fail; idempotent now.

**P1 — sync drift (commit `9ffac1d`).**

- **R-13** `_now_iso()` was the next-since token on Jira / Vikunja /
  Notion. Events that landed upstream during a paginated fetch had
  `updated_date` slightly before our wall-clock read, so the next
  call's `updated >= "<now>"` filter missed them. Added
  `_since_with_overlap()` (default 5s) so the persisted token is
  `now - overlap_seconds`. Configurable per remote via
  `tasks-remote.<name>.syncOverlapSeconds`; set to 0 to opt out.
  MS Todo unaffected (Graph delta links handle ordering server-side).

**P2 — polish (commit `9e46105`).**

- **R-14** `MSTodoDriver.fetch_all` now uses `/tasks/delta` so the
  first fetch persists the deltaLink. Previously the second fetch
  (first incremental call) re-downloaded everything to seed the
  link — operators paid for two full fetches in a row.
- **R-15** `_redact_http_error` stripped the response body wholesale,
  losing useful diagnostics (Jira `errorMessages`, Notion `message`,
  Vikunja `message`). Read up to 1KB, run targeted redactions for
  `Authorization: Bearer/Basic`, `?token=...` query strings, and
  JSON `"access_token":"..."`-shaped fields, then append. Whitespace
  collapsed so HTML 500s don't flood stderr.
- **R-17** `_iso_to_org_timestamp` used `dt.strftime("%a")` which is
  locale-dependent — two clones in different locales emitted
  different bytes for the same ISO timestamp. Hard-coded English
  short weekday table.
- **R-18** `cmd_init`'s git invocations had no timeout. A hung git
  blocked `tasks-init` indefinitely. Wrapped in a helper that honours
  `_GIT_SUBPROCESS_TIMEOUT`.

### What I deliberately did not fix

- **R-09** Vikunja `upsert` discarding the response body. The planned
  `mirror.last_observed` design (`DESIGN-sync.md`) needs the response
  shape and will land with that work.
- **R-12** Org headline `[#X]` priority cookie collision. Org tradition;
  documenting only.

### Second-pass observations

After all fixes landed, re-read the diff for new issues. Three notes,
none are bugs:

- The R-01 pre-check now fires before `_native_id`'s cross-source
  error for protocol-layer pushes; the new "id/path mismatch"
  message is structurally clearer than the old "refusing to push X
  to <scheme>" but loses the cross-service phrasing. Driver unit
  tests still cover the cross-source path directly.
- R-15 redaction is best-effort by design — `Token xyz`,
  `X-API-Key`-style headers, and YAML-shaped credentials in bodies
  are not caught. Treat error logs as still-sensitive.
- MS Todo deltaLink seeding is opportunistic — if the FIRST
  `_persist_delta_link` fails (config locked), the next process
  re-seeds. Equivalent to the old behaviour; no regression.

### Verification

- `python -m unittest -q test_git_remote_tasks` → 401 / 401.
- `.venv/bin/python -m unittest -v test_yaml_parser_fuzz` → 11 / 11.
  Fuzz strategies relaxed so `_SAFE_TEXT` now generates `\r`, `\n`,
  `\t` (previously blacklisted as out-of-subset). Hypothesis found
  no counter-examples on the new escape paths.
- README §7 (config table) and §12 (troubleshooting) updated for
  every operator-visible change.
- `DONE.md` carries the per-finding commit map.

---

## 2026-04-15 — MS Todo deltaLink corruption + task-id regex + fetch feedback

Three issues reported against the second round of live MS Todo fetches.

### Bug A — delta links were never persisted

Every MS Todo fetch spammed stderr with lines like

```
git-remote-tasks: git config: error: invalid key:
  tasks-remote.mstodo.sync.deltaLink.41514d6b41444177...
```

Root cause: git config variable names must start with a letter, but the
hex-encoded list id starts with a digit (`41...`) in essentially every
real-world case. Every write to the delta-link key failed silently.
With no persisted delta link, every subsequent fetch re-ran the full
`/tasks/delta?token=...` flow from scratch on every list — ~43s for
the user's account.

Fix: use `-` as the final joiner (so the variable name is
`deltaLink-<hex>`, starting with `d`), and also prefix the hex with
`l` for defence in depth. Keys look like
`tasks-remote.<name>.sync.deltaLink-l41514d6b...`. Live verified: the
writes succeed, the second fetch finishes in 6s and walks each list's
persisted delta cursor.

### Bug B — MS Todo task ids ending in `=` dropped on the floor

Every task with a base64-padded id surfaced as
`warning[unsafe-id]: skipping task ... must match
[A-Za-z0-9][A-Za-z0-9._-]* (≤255 chars).`. The regex didn't allow `=`.
`=` is safe in filenames and is neither a traversal nor a
shell-metachar; rejecting it just silently omits real tasks. Added `=`
to the allowed set.

### Feature — per-fetch stderr summary

Git prints `* [new branch] main -> <remote>/main` on success but
doesn't say how much work was done, so "incremental found nothing" is
indistinguishable from "something swallowed the output". Added a
one-line stderr report per `import` batch:

- Full: `<remote>: fetched <n> tasks (full snapshot)`
- Empty incremental: `<remote>: up to date since <ts>`
- Non-empty incremental: `<remote>: <n> changed, <m> deleted since <ts>`

### Known limitation (not fixed)

After the user approves the device code in the browser, MSAL's poll
loop still sleeps up to `interval` seconds (default 5s) before its
next poll, so `git fetch mstodo` can linger for a few seconds after
the browser step completes. That sleep is inside MSAL and we can't
shorten it without patching the library.

### Tests — 348 → 352, all green
- `test_base64_padding_equals_allowed` — the `=` fix.
- Updated `TestMSTodoIncrementalDelta` pair to pin the new key shape
  (`sync.deltaLink-l<hex>`, no `sync.deltaLink.<hex>`).
- `test_fetch_prints_summary_on_full_snapshot`
- `test_fetch_prints_summary_on_empty_incremental`
- `test_fetch_prints_summary_on_non_empty_incremental`

## 2026-04-15 — Fetch hang on empty deltas + MSAL device-code never authorizes

Two unrelated bugs surfaced from continued live use after the page-size
work. Both showed up as "the fetch is taking forever" but the causes
are completely different.

### Bug A — empty-delta import deadlock

User report: `git fetch jira` took several minutes even when
`sync.lastFetchAt` was a few seconds ago. Vikunja was fine.

Diagnosis: added `GIT_REMOTE_TASKS_DEBUG` timing on every HTTP call.
The trace showed exactly one Jira call (0.55s, returning zero issues)
and then nearly 4 minutes of wall time before the helper got killed.
A `sample(1)` of the helper showed it blocked in `_buffered_readline`
on stdin.

Root cause: when `fetch_changed` returned no changes and no deletes,
`_cmd_import_batch` returned silently without writing anything. But
git invoked us via `import <ref>`, which means it had already spawned
`git fast-import` on our stdout — and fast-import only exits when it
sees `done` or EOF. Without `done`, fast-import waited forever, git
kept the helper's stdin open waiting for fast-import to finish, the
helper blocked reading stdin, and the whole thing sat there until
something killed the process.

This bug was actually present in every prior version, masked by the
fact that early fetches always had at least one new task. It only
showed up once `lastFetchAt` was recent enough that the next fetch
returned an empty delta.

Vikunja was unaffected because in our test setup it always had at
least one updated task in the window.

Fix: write `done\n` before returning from the empty-delta branch.

### Bug B — MSAL device flow always reports 'authorization_pending'

User report: `git fetch mstodo` printed the device code, the user
approved it in the browser, and the helper still raised:

```
NotImplementedError: MSAL device flow failed: AADSTS70016: ...
the user must input their code.
```

Diagnosis: read the line that broke it.

```python
expires_in = int(flow.get("expires_in") or 600)
flow["expires_at"] = expires_in
```

`flow["expires_at"]` is an **absolute epoch timestamp** (seconds since
1970), not a duration. MSAL's polling loop terminates as soon as
`time.time() > flow["expires_at"]`. Setting it to `expires_in` (~900)
means "expired in 1970-01-01 00:15:00", so MSAL bailed on its very
first poll iteration, returning whatever the AAD response was at that
moment — invariably `authorization_pending` because the user can't
possibly have approved in the few milliseconds between
`initiate_device_flow` and the first poll.

Fix: don't override `expires_at` at all unless a per-remote
`deviceFlowTimeout` is configured to clamp BELOW the upstream
`expires_in`. In that case, set `expires_at = min(upstream_deadline,
now + cap)` — a real absolute deadline.

### Watermark drift fix (related)

While in the area, fixed a long-standing latent bug: in the
empty-delta path, `lastFetchAt` was never promoted because the
two-phase watermark requires the tip to move and an empty fetch never
produces a commit. So in a quiet period each subsequent fetch
re-queried a widening window. Now we promote `lastFetchAt` directly
when no fast-import stream was emitted (nothing can fail between our
exit and git's, so the two-phase guard isn't needed).

### Diagnostic: `GIT_REMOTE_TASKS_DEBUG`

Existing env var (previously only used by `_handle_modify`'s
traceback path) now also makes `_http_request` log
`http[METHOD] <elapsed>s <url-without-query>` to stderr per
request. Operators can debug "is it the network or is it me" without
strace.

### Tests
- `test_empty_delta_still_terminates_fast_import_stream` — pins the
  `done\n` requirement.
- `test_no_changes_still_advances_lastFetchAt` — pins watermark
  promotion in the empty-delta path.
- `test_msal_device_flow_preserves_absolute_expires_at` — pins that we
  no longer overwrite the deadline with a small duration.
- `test_msal_device_flow_respects_deviceFlowTimeout_cap` — covers the
  optional clamp.

348 tests, all green.

### Live verification
- `git fetch jira`: was several minutes → **1.16s** (one HTTP call,
  empty delta, terminates cleanly).
- `git fetch notion`: similar, **1.34s**.
- `git fetch vikunja`: **0.24s** (already fast).

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
