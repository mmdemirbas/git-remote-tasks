# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the default stdlib suite.
python -m unittest -v test_git_remote_tasks

# Run a single test / class / method.
python -m unittest -v test_git_remote_tasks.TestYAMLSerializer
python -m unittest -v test_git_remote_tasks.TestYAMLSerializer.test_roundtrip_identical

# Add hypothesis fuzz tests under .venv.
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest test_git_remote_tasks test_yaml_parser_fuzz

# Live end-to-end tests against real services. Set GRT_LIVE_CONFIG to a
# todo-harvest-style config.yaml; tests skip cleanly when unset.
GRT_LIVE_CONFIG=/path/to/config.yaml python test_live_integration.py

# Install / uninstall the per-scheme symlinks.
python git_remote_tasks.py install --bin-dir ~/.local/bin
python git_remote_tasks.py uninstall --bin-dir ~/.local/bin

# Validate a configured remote without hitting the network.
python git_remote_tasks.py check <remote-name>

# Force a full resync (wipes sync.* state for one remote).
python git_remote_tasks.py reset <remote-name>
```

Tests use **unittest + unittest.mock only** — this is an explicit override of
the global "prefer pytest" rule. Do not convert tests to pytest.

## Architecture

`git_remote_tasks.py` is a single-file implementation of four git remote
helpers (`jira`, `vikunja`, `mstodo`, `notion`). The active scheme is
resolved from `argv[0]` basename when invoked by git, or from the URL
scheme when run directly. `install` materializes this as five symlinks
(four helpers + `tasks-init`) all pointing at the same script.

The code is built as strict layers; later layers depend only on earlier
ones, and this ordering is load-bearing for the test suite:

1. **Unified schema** (`empty_task`, `normalize_task`, `TASK_FIELDS`,
   `is_safe_task_id`). Every task is a dict with this exact shape.
   Task ids are validated against a conservative regex at every
   import/export boundary so a hand-edited `id: ../etc/passwd` can never
   escape `tasks/`.
2. **Serializers** (`YAMLSerializer`, `OrgSerializer`). Both round-trip
   to the same unified dict. The YAML parser/emitter is **hand-written**
   — there is no PyYAML dependency and none should be added. The shipped
   script uses only the standard library; `msal` is the sole optional
   runtime extra (guarded by `MSAL_AVAILABLE`). **Test-time deps are
   allowed** under `.venv`; they must not leak into `git_remote_tasks.py`.
3. **Git config reader** (`read_format`, `read_remote_config`,
   `write_config_value`, `unset_config_values`). Thin wrappers over
   `git config --local`. All credentials live in `.git/config`, never
   the URL. `read_remote_config` returns a `CaseInsensitiveConfig` to
   work around git's silent lowercasing of variable names.
4. **Drivers** (`JiraDriver`, `VikunjaDriver`, `MSTodoDriver`,
   `NotionDriver`) inherit from `Driver`. Each implements `fetch_all`,
   `fetch_changed`, `upsert`, `delete`. HTTP seams are `_http_get` /
   `_http_post` / `_http_put` / `_http_patch` / `_http_delete` — tests
   monkey-patch these. The base `_http_request` adds a 30s timeout and
   retries transient 408/425/429/5xx and `URLError` up to three times.
5. **ProtocolHandler** speaks git's remote-helper line protocol on
   stdin/stdout: `capabilities`, `list`, `import`, `export`. Imports use
   a two-phase sync watermark: `_record_pending_since` on write, then
   `_promote_pending_since` on the next run only if the tip actually
   advanced. Exports track per-ref failures and emit `error <ref>` when
   a push partially fails — `git push`'s exit code reflects reality.
6. **Management subcommands** (`install`, `uninstall`, `list-schemes`,
   `check`, `init`, `reset`, `version`) dispatched only when `argv[0]`
   is not a known helper name.

### Invariants to preserve

- **SerializerSymmetry.** A task serialized by one format and
  deserialized by the other (or the same) must equal
  `normalize_task(original)`. Any change to either serializer must
  keep this true for all `TASK_FIELDS`.
- **`logbook` is an extension key.** It is *not* in `TASK_FIELDS` but
  `normalize_task` preserves it, and both serializers round-trip it if
  present. This exists so org `:LOGBOOK:` drawers survive a YAML
  round-trip. Do not add it to `TASK_FIELDS`.
- **Timezone offsets round-trip.** ISO strings with an offset survive
  the YAML ↔ Org ↔ ISO conversion; they are *not* normalized to UTC.
- **All four remotes are expected to round-trip.** Every driver
  implements `upsert` + `delete`. Stubs that raise `NotImplementedError`
  are a bug; the `_warn_once("push-stub", ...)` branch should never
  fire in a released build.
- **Cross-source writes are refused loudly.** `_native_id` raises the
  driver-specific `*PushError` for any id whose prefix matches a
  different scheme. Each `upsert` calls `_native_id` BEFORE any
  network IO so a refused push is synchronous.
- **Path-traversal guards at every trust boundary.** `is_safe_task_id`
  + `_is_safe_tasks_path` must run before any `tasks/<id>.<ext>` write
  or before a `D tasks/<...>` directive is honored.
- **Secret redaction in `check`.** Variable names containing `token`,
  `password`, `secret`, `key`, `credential`, or `bearer` are redacted
  in stdout; an allow-list covers known-safe keys like `baseUrl`,
  `email`, `clientId`. Adding a new credential-like key means extending
  the filter.
- **Fast-import blob bytes** are written through `sys.stdout.buffer`
  when available, with a UTF-8 text fallback that counts bytes (not
  chars). The fallback is test-only — real git always exposes
  `.buffer`. Do not drop either path without updating the tests that
  rely on them.

### Incremental fetch

Every driver implements `fetch_changed(since)` returning
`(changed_tasks, deleted_ids, new_since_token)`. Strategies:

| Driver   | Mechanism                                                                              |
|----------|----------------------------------------------------------------------------------------|
| Jira     | JQL `(base) AND updated >= "<ts>" ORDER BY updated ASC`.                               |
| Vikunja  | `filter=updated > '<ts>'`.                                                             |
| MS Todo  | Graph `/me/todo/lists/{id}/tasks/delta`; `@odata.deltaLink` persisted per list.        |
| Notion   | `/v1/databases/{id}/query` with `last_edited_time on_or_after <ts>`; archived→D.       |

Jira and Vikunja have no native deletion feed — operators reconcile
with `sync.mode=full` or `python git_remote_tasks.py reset <remote>`.

### Test layout

- `test_git_remote_tasks.py` — default stdlib suite (324 tests).
- `test_yaml_parser_fuzz.py` — hypothesis property tests (opt-in via
  `.venv`; skipped cleanly when `hypothesis` is absent).
- `test_live_integration.py` — real-service harness. Reads credentials
  from the user's personal todo-harvest config. Safety guards: at most
  five items created per service, every item tagged with a
  `GRT-LIVE-<timestamp>` marker, refuses to modify anything we didn't
  create, never deletes.

## Reference documents

- `PLAN.md` — invariants and the short list of open items.
- `DONE.md` — index of resolved tasks with their commits.
- `WORKLOG.md` — narrative log of design decisions and turning points.
- `README.md` — user-facing docs; always the source of truth for CLI
  behaviour and config keys.
