# PLAN.md

Backlog derived from the 2026-04-15 adversarial review plus three product
decisions raised by the user. Scope: everything between the current cut
(commit `e0d414b`) and a release candidate we would trust in daily use.

Rules that shaped prioritization:

- **Code fixes beat documentation.** Docs are a last-resort warning for
  things we cannot fix yet. When a fix is unavoidable but not yet done,
  the runtime must surface the limitation (stderr warning, non-zero
  exit) *and* the README must say it — both, not either.
- **Push must work for every remote, including Notion.** Notion's API
  supports `POST /v1/pages`, `PATCH /v1/pages/{id}`, and
  `PATCH archived: true` — the "pull-only" label was self-imposed,
  not an API limit.
- **Custom field / status / priority mapping is a first-class feature.**
  Upstream services let users rename workflows; the helper must not
  guess.
- **Single-file, stdlib-only for the shipped script.** Zero *runtime*
  dependencies keep distribution trivial (symlink four names to one
  file). **Test-time dependencies are allowed** and installed into a
  `.venv` — so TEST-04 is free to use `hypothesis` for property-based
  fuzz testing and `respx` / `responses` for HTTP capture, as long as
  none of that leaks into `git_remote_tasks.py`. Cost of the single-file
  constraint: we own the YAML parser — it must be audited and
  fuzz-tested.
- **Do not fetch the whole world every run.** Services are not git
  remotes; they have no native object graph. Use their
  last-edited / delta APIs to pull only the changed slice, and emit
  per-file `M` / `D` directives instead of `deleteall`.

---

## 1. Design decisions (answers to open questions)

### 1.1 `tasks-init` directory semantics — pick `git init`, not `git clone`

**Decision.** Keep `tasks-init` as a `git init`-style command: operate in
the current directory by default; accept a positional path that is
created if missing (same contract as `git init <path>`). Drop the
`--dir` flag in favour of the positional argument.

**Why `git init` and not `git clone`:**

| Concern                           | `git init`-style                             | `git clone`-style                        |
|-----------------------------------|----------------------------------------------|------------------------------------------|
| Semantic fit                      | No source URL exists at init time — remotes are configured *after* init. Matches init. | Clone requires a source URL; we have none. |
| Mixing with an existing repo      | Can add tasks tracking to any existing repo (code + tasks in one tree). | Forbids that — clone refuses non-empty targets. |
| Unix least surprise               | `<tool>-init [path]` universally reads as "initialize here or there". | Users would not expect a tool called `init` to clone. |
| Error surface                     | No "directory already exists" error to handle. | Would need to refuse non-empty dirs, prompt, or force. |
| Matches current code              | Near-identical — only `--dir` → positional.  | Would require a full rewrite of `tasks-init`. |

Action captured as **DX-04** below.

### 1.2 Bidirectional sync for every remote, including Notion

Today Jira / Vikunja / MS Todo drivers have read paths (with pagination)
and stub writes that raise `NotImplementedError`. Notion drivers raise
`NotImplementedError("Notion is pull-only")` by design. The
`ProtocolHandler` swallows those exceptions and still reports
`ok <ref>` to git. Result: `git push` exits 0 and the user believes
their edits propagated.

Three-phase fix:

1. **Runtime honesty (BUG-02).** While writes are stubs, the helper must
   refuse to lie: propagate `NotImplementedError` into a non-zero exit
   and an `error <ref>` line per the git-remote-helper protocol. Add a
   banner to stderr on every such push.
2. **Implement the writes for Jira / Vikunja / MS Todo (FEAT-02).** Full
   upsert + delete + ID-back mapping. Contract borrowed from
   `todo-harvest/src/sources/*.py`: each driver exposes `push(tasks)`
   and surfaces per-task `PushResult` (created / updated / skipped /
   failed).
3. **Implement Notion push (FEAT-08).** Notion's API supports
   `POST /v1/pages`, `PATCH /v1/pages/{id}`, and `PATCH` with
   `archived: true`. The "pull-only" label was self-imposed. Lift it.

### 1.3 Custom field and status mapping

Inspired by `todo-harvest/config.example.yaml`. Per-remote config keys
accepted under `tasks-remote.<name>.*`:

| Key prefix        | Example                                   | Applies to            |
|-------------------|-------------------------------------------|-----------------------|
| `statusMap.*`     | `statusMap.Triage = todo`                 | All writable remotes  |
| `priorityMap.*`   | `priorityMap.P0 = critical`               | All writable remotes  |
| `fieldMap.*`      | `fieldMap.description = Notes`            | Notion (column names) |
| `jqlFilter`       | `jqlFilter = "assignee = currentUser()"`  | Jira only             |
| `projectId`       | `projectId = 42`                          | Vikunja write target  |

Mappings apply in both directions: pull inverts the map; push applies
it. Unknown values on pull emit a stderr warning and fall back to the
unified default so data is never silently dropped.

Captured as **FEAT-03**.

### 1.4 When we cannot fix, we warn — twice

Every limitation that survives into a release must produce (a) a runtime
warning on the relevant command and (b) a README caveat. No silent
success, ever. Captured as the cross-cutting rule in **FEAT-04**.

### 1.5 Incremental fetch — do not re-pull the world every run

External services are not git remotes: they have no commit graph and no
native "give me what changed since SHA". Fetching the full set every
run costs 10s–100s of HTTP calls and makes scheduled sync infeasible on
large trackers. For each service we pick the narrowest incremental API
available and persist minimal state in `.git/config` under
`tasks-remote.<name>.sync.*`.

| Service    | Incremental API                                                                      | Deletion signal                                             | State to persist                                  |
|------------|---------------------------------------------------------------------------------------|--------------------------------------------------------------|----------------------------------------------------|
| Jira       | JQL `updated >= "<iso>" ORDER BY updated ASC` on `/rest/api/3/search`.                | None native. Reconcile with periodic `fields=key` full sweep | `sync.lastFetchAt`                                 |
| Vikunja    | `/api/v1/tasks/all?filter=updated > '<iso>'&sort_by=updated`.                          | Same: periodic key-only sweep.                               | `sync.lastFetchAt`                                 |
| MS Todo    | Graph delta query `/me/todo/lists/{id}/tasks/delta` → persist `@odata.deltaLink`.      | Tombstones with `@removed` annotation — native and exact.   | `sync.deltaLink` (one per list)                    |
| Notion     | `databases/{id}/query` with `filter.last_edited_time.after = <iso>`, sorted.           | Archived pages returned with `archived: true`.              | `sync.lastEditedAfter`                             |

Coupled with **FEAT-07**, which drops the `deleteall` directive when
incremental state exists and instead emits per-file `M` for changed
tasks and `D` for known-deleted ones. The full-snapshot path remains
available via `--full-fetch` or `git config tasks-remote.<name>.sync.mode full`
for first fetches and operator-forced rebuilds.

### 1.6 YAML parser — audited and fuzzed, not replaced

The spec forbids PyYAML so we ship with a hand-written subset parser.
That choice is only safe if the subset is small, documented, and
property-tested. **TEST-04** adds: (a) a fuzz loop that round-trips
random-but-valid task dicts through the YAML serializer and asserts
`deserialize(serialize(t)) == normalize_task(t)`; (b) a corpus of
hand-crafted adversarial inputs covering quote escaping, block scalars,
reserved words, deeply indented blocks, CRLF line endings, BOMs,
trailing whitespace, and comment placement; (c) an explicit listing of
features *not* supported (anchors, aliases, flow collections beyond
`[]`, tagged types, multi-document streams) so users can't file a bug
we'll never fix.

---

## 2. Backlog

### Legend

**Category**

| Code  | Meaning                                           |
|-------|---------------------------------------------------|
| BUG   | Correctness defect in existing code.              |
| FEAT  | New behaviour or missing capability.              |
| SEC   | Security / credential handling.                   |
| DX    | Developer / operator experience, not correctness. |
| TEST  | Test suite gap or misleading metric.              |
| DOC   | Docs-only change, last resort.                    |

**Priority**

| P  | Meaning                                                             |
|----|---------------------------------------------------------------------|
| P0 | Silent data loss, wrong results, or security exposure. Fix now.     |
| P1 | Feature the product advertises but does not deliver. Fix next.      |
| P2 | Quality, polish, or edge cases. Fix when adjacent code is touched.  |

### 2.1 P0 — correctness and honesty

| ID      | Cat | Title                                              | Detail                                                                                                                                   | Fix sketch                                                                                                  |
|---------|-----|----------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| BUG-01  | BUG | Jira epic `category.name` is `None` for dict epics | `name` expression in `JiraDriver.normalize` binds `or` tighter than the outer ternary, so any dict-shaped `customfield_10014` drops the name. | Extract an explicit `if isinstance(epic, dict): name = epic.get("name")` block. Add tests for dict epics.   |
| BUG-02  | BUG | `git push` reports success for stub drivers        | `_handle_modify` / `_handle_delete` swallow `NotImplementedError`; `_cmd_export` writes `ok <ref>` unconditionally.                      | Track per-ref failure; emit `error <ref> <msg>` when any task failed; return non-zero from `main`.          |
| BUG-04  | BUG | `_read_exactly` reads characters as bytes          | Text-stream fallback calls `stdin.read(n)` where `n` is a byte count — multi-byte UTF-8 bodies are truncated.                            | Drop the fallback and require `stdin.buffer`; or encode only after reading the full byte count correctly.   |
| BUG-05  | BUG | Fetch produces unrelated histories; merge fails    | No `from :mark` → every fetch is a root commit on `<remote>/main`. `git merge <remote>/main` refuses without `--allow-unrelated-histories`. | Track last-imported mark per ref and emit `from :N`; or set `fetch-helper` mode to linear history.          |
| BUG-07  | BUG | Non-UTC timestamps silently normalize to UTC       | `_iso_to_org_timestamp` converts `+03:00` → `Z` with no user-visible trace. Hours drift for agenda users.                                | Emit org timestamp in the original local offset (store offset in a sibling property `:TZ:` when present).   |
| SEC-01  | SEC | Secret redaction in `check` misses most keys       | Only substrings `token` / `password` are redacted. `apiKey`, `secret`, `clientSecret`, `credential`, `access*` leak.                     | Widen substring list; allow-list known-safe keys (`scheme`, `baseUrl`, `tenantId`, `clientId`, `email`).    |
| SEC-02  | SEC | `cmd_uninstall` removes non-symlink files          | `os.unlink(link)` wipes any file at `bin_dir/git-remote-<scheme>`, including unrelated helpers from other tools.                         | Unlink only when `link.is_symlink()` *and* `os.readlink(link)` resolves to our `_script_path()`.            |

### 2.2 P1 — product-shaped gaps

| ID      | Cat  | Title                                                      | Detail                                                                                                                                  | Fix sketch                                                                                                   |
|---------|------|------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------|
| FEAT-01 | FEAT | MS Todo MSAL device-code flow                              | `MSTodoDriver._auth_header` just reads `accessToken`; `msal` is never imported. README §8 promises a flow that does not exist.          | Add `_acquire_token()` using MSAL device code when `accessToken` absent; cache refresh token in git config.  |
| FEAT-02 | FEAT | Real write paths for Jira, Vikunja, MS Todo                | `upsert` / `delete` are stubs. Tied to BUG-02.                                                                                          | Port push logic from `todo-harvest/src/sources/*.py`. Include ID-back mapping via a local `.git/tasks.db`.   |
| FEAT-03 | FEAT | Custom status / priority / field mapping per remote        | Per §1.3. Covers `statusMap`, `priorityMap`, `fieldMap`, `jqlFilter`, `projectId`.                                                       | Read keys in `read_remote_config`; apply in both driver directions; warn on unmapped values.                 |
| FEAT-04 | FEAT | Runtime warnings for all documented limitations            | Per §1.4. Every `NotImplementedError` branch and every silent normalization (BUG-07, BUG-09) must print a clear stderr warning.         | Central `warn(code, msg)` helper; emit once per run; enumerate codes in README §12.                          |
| FEAT-05 | FEAT | Org `:DEADLINE:` emitted as an agenda line, not a property | Emacs/agenda integration requires `DEADLINE: <…>` on the line below the headline, not inside the `:PROPERTIES:` drawer.                 | Move deadline out of drawer; keep the property as a read-only fallback for files edited by earlier versions. |
| FEAT-06 | FEAT | Incremental fetch per service                              | Per §1.5. Today every `git fetch` pulls the entire task set — O(N) HTTP calls. Unworkable on real trackers.                             | Persist sync state per remote; call service-specific "changed since" APIs; full fetch only when state absent or `sync.mode=full`. |
| FEAT-07 | FEAT | Per-file M/D directives on incremental import             | Today the fast-import stream always starts with `deleteall` and rewrites every blob. With incremental fetches, diffs must be minimal. | Track the previous snapshot tree via `git ls-tree refs/remotes/<remote>/main`; emit `M` for changed blobs and `D` for removed ids. |
| FEAT-08 | FEAT | Notion push (create / update / archive)                    | API supports `POST /v1/pages`, `PATCH /v1/pages/{id}`, and `PATCH archived: true`. Current "pull-only" is a self-imposed limit.         | Implement `NotionDriver.upsert` and `delete`; map unified fields back to Notion property shapes using the `fieldMap` from FEAT-03. |
| BUG-08  | BUG  | Jira task `url` falls back to `jira://` scheme             | `base = config.baseUrl or self.url` — if `baseUrl` is unset, the remote URL leaks into `url`. Tasks render as `jira://…/browse/PROJ-1`. | Drop the `or self.url` fallback; error in `check` when `baseUrl` is unset.                                   |
| BUG-10  | BUG  | `REMOTE_REQUIRED_KEYS` incomplete for `msftodo`            | `check` passes with only `tenantId`/`clientId`; `fetch_all` then raises because there is no token.                                      | Require one-of (`accessToken`, `msal`-installed + device-flow configured).                                   |
| BUG-11  | BUG  | `_parse_properties` ignores stray headlines                | If `:END:` is missing, the loop scans past a second `*` headline, mixing documents.                                                     | Break on any line starting with `*` before hitting `:END:`; log a warning.                                   |
| DX-04   | DX   | `tasks-init` switches to positional path                   | Per §1.1.                                                                                                                               | Replace `--dir PATH` with optional positional `[PATH]`; keep `--format` flag.                                |

### 2.3 P2 — quality and polish

| ID      | Cat  | Title                                                  | Detail                                                                                                                              | Fix sketch                                                                                 |
|---------|------|--------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| BUG-06  | BUG  | YAML nested keys with hyphens silently dropped         | `_read_nested` regex is `[A-Za-z_][A-Za-z0-9_]*`. A hand-edited `due-date:` vanishes without error.                                 | Accept hyphens in the inner regex; still reject on deserialize if outside the schema, with a warning. |
| DX-01   | DX   | `tasks-init --help` line range is hardcoded            | `sed -n '1,16p' "$0"` drifts when the header grows.                                                                                 | Replace with a heredoc or grep for a sentinel line.                                         |
| DX-02   | DX   | `_handle_modify` generic `except Exception`            | Pragma-no-cover branch hides driver bugs behind a single-line log.                                                                  | Log the traceback at DEBUG; re-raise under `GIT_REMOTE_TASKS_DEBUG=1`.                      |
| DX-03   | DX   | Absolute-path symlinks break on relocation             | `install` symlinks to `os.path.abspath(__file__)`. Moving the repo silently breaks the installed helpers.                           | Detect when `bin_dir` and script are on the same volume; prefer relative symlinks.          |
| TEST-01 | TEST | Coverage % is misleading                               | 97% is inflated by `assertRaises(NotImplementedError)` tests for the unimplemented write paths.                                     | Split coverage reporting: read paths vs write paths; publish both.                          |
| TEST-02 | TEST | No test exercises dict-shaped Jira epics               | Enabled BUG-01 to ship.                                                                                                             | Add a fixture with `customfield_10014={"key": "E", "name": "Platform"}`.                    |
| TEST-03 | TEST | `_read_exactly` fallback path has no multibyte test    | Enabled BUG-04 to hide.                                                                                                             | Add a test that drives the fallback with a non-ASCII blob and asserts bytes, not chars.     |
| TEST-04 | TEST | YAML parser audit + fuzz coverage                      | Hand-written parser ships on every install. No fuzz tests; adversarial corpus is thin. Bugs here corrupt every task silently.       | Per §1.6. Document the supported subset; fuzz round-trip on random task dicts; adversarial corpus.  |
| DOC-01  | DOC  | README Quick Start teaches an incorrect merge          | `git merge vikunja/main` is shown without `--allow-unrelated-histories`. Only surfaces in `tasks-init`'s stdout.                    | Move the flag / explanation into README §4, §10. Drop when BUG-05 lands.                    |
| DOC-02  | DOC  | README §10 "bisect works" claim needs a qualifier      | True only after a user merges every fetch locally. The remote-tracking ref itself is a moving root commit.                          | Add a sentence clarifying what bisects.                                                     |
| DOC-03  | DOC  | README §11 missing org-agenda caveat                   | Current `:DEADLINE:` placement is not agenda-visible. Remove when FEAT-05 lands.                                                    | Temporary warning; delete on FEAT-05 completion.                                            |

---

## 3. Execution order

Recommended sequence — each block should ship as its own commit:

1. **P0 correctness sweep.** BUG-01 → BUG-04 → BUG-05 → BUG-07 → SEC-01 → SEC-02. Add the tests in TEST-02 / TEST-03 alongside the bugs they uncover. *(Done.)*
2. **Runtime honesty pass.** BUG-02, FEAT-04. This unblocks all "the push looked fine" user reports before writes exist.
3. **YAML parser audit.** TEST-04. Must land before we add any new serializer feature — otherwise we're building on an untrusted base.
4. **`tasks-init` ergonomics.** DX-04, DX-01.
5. **Incremental fetch.** FEAT-06 → FEAT-07. Performance is a correctness concern at scale: a tool that times out on a 5000-issue Jira project is not usable.
6. **Write paths.** FEAT-01 (MSAL) → FEAT-02 (Jira/Vikunja/MS Todo push) → FEAT-08 (Notion push) → FEAT-03 (mapping). Port the contract from `todo-harvest`.
7. **Org agenda parity.** FEAT-05, DOC-03 cleanup.
8. **Remaining polish.** BUG-06, BUG-08, BUG-10, BUG-11, DX-02, DX-03, TEST-01, DOC-01, DOC-02.

## 4. Out of scope (for now)

- Replacing the hand-written YAML parser with PyYAML. Single-file
  stdlib-only distribution is a hard constraint; we audit and fuzz
  instead (TEST-04).
- Supporting anything other than YAML and Org.
- Conflict resolution across concurrent pushes (todo-harvest has a
  sqlite sync-map — we defer until bidirectional sync is stable).
- Webhook / push-based sync (inverse direction). Services send events
  to a listener; out of scope for a command-line helper.
