# DONE.md

Tasks resolved out of `PLAN.md`. Each row names the task ID, the commit
(or commits) that landed it, and a one-line result. Use `git show <sha>`
for the full context.

## P0 — correctness and honesty

| ID     | Commit  | Result                                                                                             |
|--------|---------|----------------------------------------------------------------------------------------------------|
| BUG-01 | 58e9311 | Jira epic category `name` no longer drops to `None` for dict-shaped `customfield_10014`.           |
| BUG-02 | e6860e4 | `git push` emits `error <ref>` and exits non-zero when any `upsert` / `delete` fails.              |
| BUG-04 | 24cfca9 | `_read_exactly` fallback counts UTF-8 bytes, not characters, so multi-byte bodies round-trip.      |
| BUG-05 | 5b0a712 | Fetches emit `from <previous-tip>` so `<remote>/main` forms a linear history.                      |
| BUG-07 | f7a4a1c | Org timestamp emission preserves the original timezone offset; parser reads it back.               |
| SEC-01 | 438ed21 | `cmd_check` redacts any substring matching token/password/secret/key/credential/bearer.            |
| SEC-02 | 438ed21 | `cmd_uninstall` only removes symlinks pointing at this script.                                     |

## P1 — product-shaped gaps

| ID      | Commit  | Result                                                                                               |
|---------|---------|------------------------------------------------------------------------------------------------------|
| FEAT-01 | 8d60c55 | `MSTodoDriver._acquire_token` — MSAL device-code flow with refresh-token persistence in `.git/config`. |
| FEAT-02a | aaf4b93 | VikunjaDriver push: POST update, PUT create, DELETE remove. Needs `projectId` for creates.          |
| FEAT-02b | 7102031 | JiraDriver push: PUT edit, POST create, workflow transitions. Needs `projectKey` for creates.        |
| FEAT-02c | 8d60c55 | MSTodoDriver push: Graph PATCH / POST / DELETE against `/me/todo/lists/{id}/tasks`.                  |
| FEAT-03 | 949cdf4 | `statusMap.*` / `priorityMap.*` / `fieldMap.*` per-remote config applied in both directions.         |
| FEAT-04 | e6860e4 | `ProtocolHandler._warn_once(code, msg)` — deduped stderr warnings; first code is `push-stub`.        |
| FEAT-05 | 0b2a91f | Org `DEADLINE:` rendered as an agenda line below the headline; parser reads new + legacy shapes.     |
| FEAT-06 | bbd1f2c | Incremental fetch: Jira JQL `updated >=`, Vikunja `filter=updated > '…'`; sync state in `.git/config`. |
| FEAT-07 | bbd1f2c | Per-file `M` / `D` directives when incremental state exists; full snapshot only on first fetch.      |
| FEAT-08 | ab94397 | Notion push: POST create, PATCH update, archive-on-delete. Title column auto-discovered.             |
| FEAT-06b | 456a4ee | MS Todo incremental via Graph delta query. Delta link persisted per list; @removed tombstones become D. |
| FEAT-06c | 456a4ee | Notion incremental via last_edited_time filter. Archived pages become D.                           |
| BUG-08  | 0b2a91f | Jira `task.url` falls back only to a real http(s) base; never emits `jira://...` links.              |
| BUG-10  | 0b2a91f | `cmd_check` reports `accessToken or clientId` as a one-of requirement for mstodo.                   |
| BUG-11  | 0b2a91f | Org `_parse_properties` terminates cleanly on a stray `* ` headline.                                 |
| DX-04   | 893baad | `tasks-init` accepts a positional path (`git init [path]` shape); `--dir` kept as deprecated alias.  |

## P2 — quality and polish

| ID      | Commit  | Result                                                                                               |
|---------|---------|------------------------------------------------------------------------------------------------------|
| BUG-06  | 0b2a91f | YAML nested keys accept hyphens (`due-date:`) so hand-edits aren't silently dropped.                 |
| DX-01   | 893baad | `tasks-init --help` uses a heredoc; no longer drifts with header length.                             |
| DX-02   | 0b2a91f | Generic push-failure branches print a traceback when `GIT_REMOTE_TASKS_DEBUG=1`.                     |
| DX-03   | 0b2a91f | `cmd_install` prints the resolved source path so operators know where the symlinks point.            |
| TEST-02 | 58e9311 | Jira dict-epic test added alongside the fix.                                                         |
| TEST-03 | 24cfca9 | `_read_exactly` multibyte fallback test added.                                                       |
| TEST-04 | 7963c57 | YAML parser subset documented on `YAMLSerializer`; hypothesis fuzz + adversarial corpus under `.venv`. |
| DOC-01  | 0b2a91f | README §4 Quick Start mentions `--allow-unrelated-histories` on the first merge.                     |
| DOC-02  | 0b2a91f | README §10 bisect claim rewritten — remote-tracking ref now has a linear history.                    |
| DOC-03  | 0b2a91f | Org-agenda caveat no longer needed: FEAT-05 emits `DEADLINE:` as an agenda line.                     |

## Infrastructure and workflow

| Change     | Commit  | Result                                                                                    |
|------------|---------|-------------------------------------------------------------------------------------------|
| SPEC.md    | 311a337 | Retired; preserved at commit 8600dde. PLAN.md tracks forward work.                        |
| `tasks-init` | a95c30a | Absorbed into `git_remote_tasks.py init` subcommand + symlink — single-file distribution. |
| `.venv` + `requirements-dev.txt` | 7963c57 | Test-time deps (hypothesis) isolated from the shipped single-file script. |
| TEST-01    | —       | Mooted: write paths now have real tests; a separate read/write split no longer informative. |

## Correctness audit — 2026-04-25

Full narrative in `WORKLOG.md` (entry dated 2026-04-25). Test count
352 → 401. R-09 and R-12 are listed in `PLAN.md` §2.1 as deferred /
documented-only.

| ID    | Commit  | Result                                                                                              |
|-------|---------|------------------------------------------------------------------------------------------------------|
| R-01  | caf4afe | `_handle_modify` rejects content-id / filename mismatch; canonicalizes empty content_id to filename. |
| R-02  | caf4afe | Org body `*` line at column zero only triggers headline boundary; indented body bullets preserved.  |
| R-03  | caf4afe | Org `:TAGS:` quotes commas/quotes on emit; legacy unquoted files still parse.                        |
| R-04  | caf4afe | YAML `\r` / `\t` round-trip via double-quoted form with new escape handling.                         |
| R-05  | caf4afe | Notion `_query_pages` defensive break on falsy `next_cursor`.                                        |
| R-06  | 2784710 | Notion push skips fields whose column is missing from the database schema.                           |
| R-07  | 2784710 | Tags column shape adapts: multi/single/missing each handled with one-shot warnings.                  |
| R-08  | 2784710 | `_write_incremental_import` filters `deleted_ids` through `is_safe_task_id`.                         |
| R-10  | 5bf53dd | Jira `_transition` failure message names the half-applied state.                                     |
| R-11  | 5bf53dd | All four drivers treat 404 / 410 on delete as soft success (idempotent).                             |
| R-13  | 9ffac1d | `syncOverlapSeconds` (default 5s) prevents incremental drift on Jira/Vikunja/Notion.                 |
| R-14  | 9e46105 | `MSTodoDriver.fetch_all` uses delta endpoint to seed deltaLink on first run.                         |
| R-15  | 9e46105 | `_redact_http_error` keeps a 1KB body excerpt with auth-token redaction.                             |
| R-16  | caf4afe | YAML `\t` escaped (shipped with R-04).                                                               |
| R-17  | 9e46105 | Org weekday emission is locale-independent (hard-coded English short names).                         |
| R-18  | 9e46105 | `cmd_init` git invocations carry `_GIT_SUBPROCESS_TIMEOUT`.                                          |
