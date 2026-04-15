# PLAN.md

Every task from the original adversarial-review backlog has landed. The
current open list is short — and mostly bounded by the single-file
stdlib-only distribution constraint. Resolved items moved to
[`DONE.md`](./DONE.md).

This document survives in two halves:

1. **Invariants that shape future work** — decisions made along the way
   that should not be relitigated without a reason.
2. **Open items and explicit out-of-scope** — what's left, and what we
   deliberately will not do.

---

## 1. Invariants

| Invariant | Why |
|-----------|-----|
| Single-file runtime, stdlib-only | Trivial distribution: `scp git_remote_tasks.py` + `install`. `msal` is the sole optional runtime extra (guarded by `MSAL_AVAILABLE`). |
| Test-time dependencies allowed under `.venv` | Isolated in `requirements-dev.txt`; never imported by the shipped script. `hypothesis` is the first example. |
| Docs are a last-resort warning, never a fix | Anything we cannot fix today must *also* emit a runtime warning via `ProtocolHandler._warn_once`. |
| Push works for every remote, including Notion | The "pull-only" label was self-imposed; lifting it is done (FEAT-08). |
| Incremental fetch by default | Sync state in `.git/config` under `tasks-remote.<name>.sync.*`. `sync.mode=full` is the escape hatch. |
| Cross-source writes are refused loudly | Editing a `jira-X.yaml` file in a Vikunja remote raises `VikunjaPushError` (etc.) rather than silently duplicating. |
| YAML parser documents its supported subset | Fuzz tests plus adversarial corpus in `test_yaml_parser_fuzz.py` keep the subset honest. |

---

## 2. Open items

### 2.1 Incremental fetch for MS Todo and Notion

The base `Driver.fetch_changed()` still delegates to `fetch_all()` for
MS Todo and Notion; only Jira and Vikunja have driver-specific
incrementals. Complete coverage:

| ID       | Cat  | Title                                   | Detail                                                                                          |
|----------|------|-----------------------------------------|-------------------------------------------------------------------------------------------------|
| FEAT-06b | FEAT | MS Todo delta-query incremental fetch   | Use Graph `/me/todo/lists/{id}/tasks/delta` and persist `@odata.deltaLink` per list as `sync.deltaLink.<listId>`. Emits native `@removed` tombstones as `D` directives. |
| FEAT-06c | FEAT | Notion `last_edited_time` filter        | Call `/databases/{id}/query` with `{"filter": {"property": "last_edited_time", "last_edited_time": {"after": "<iso>"}}}`. Archived pages come back with `archived: true` — emit as `D`. |

### 2.2 Known weaker behaviour

| ID   | Cat | Title                                      | Workaround today                                                                                    |
|------|-----|--------------------------------------------|------------------------------------------------------------------------------------------------------|
| OPS-01 | DX | Jira / Vikunja upstream deletions          | Neither API has a native deletion feed. Operators flip `sync.mode full` periodically to reconcile. A `tasks-remote.<name>.reconcileInterval` helper is on the wish list. |
| OPS-02 | DX | Concurrent push conflict handling          | If two pushes race the same page, the second overwrites the first. todo-harvest has a sqlite sync-map; we defer until there's a real user-facing collision. |

### 2.3 Out of scope (will not do)

- Replacing the hand-written YAML parser with PyYAML. Single-file
  distribution is the motivating constraint.
- Supporting file formats other than YAML and Org.
- Webhook / push-based sync (the inverse direction). Services send
  events to a listener; out of scope for a command-line helper.
- A long-running daemon. `git fetch` / `git push` is the only
  invocation surface.
- Bundled UI, TUI, or GUI. `$EDITOR` on the task files is the model.

---

## 3. How to add the next service

The template is stable enough to write down:

1. Add a driver class under the "Driver base + service drivers"
   section of `git_remote_tasks.py`:
   - `SCHEME` class attribute.
   - `_auth_header()` / `_acquire_token()` as needed.
   - `normalize(raw) -> unified task dict`, threading status and
     priority through `self._apply_status_override` and
     `self._apply_priority_override`.
   - `fetch_all()`; override `fetch_changed(since)` to hit a narrower
     API (see Jira / Vikunja for the pattern).
   - `upsert(task)` and `delete(task_id)`, rejecting cross-source ids
     via `self._native_id()`.
2. Register it in `SCHEMES` at the bottom of the file.
3. Add required-keys to `REMOTE_REQUIRED_KEYS`; widen
   `_missing_required_keys` if the service has a one-of requirement
   (see mstodo accessToken vs clientId).
4. Tests: normalize fixtures, fetch pagination, fetch_changed with
   `since`, upsert (update + create), delete, cross-source refusal.
5. README §7 add a row; §8 add a setup block.

The ~250-line `MSTodoDriver` is the most complete worked example.
