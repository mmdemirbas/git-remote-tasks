# REVIEW: correctness and usability audit (2026-04-25)

Targeted deep review focused on round-trip stability, push-side safety,
incremental-fetch invariants, and edge cases in the protocol layer.
All findings are reproducible from a fresh checkout against the
current `main` (commit `759b521`). Tests pass: 352 of 352.

> **STATUS:** All 18 findings landed across five commits ending at
> `9e46105`. Test suite is now 401 / 401. See "Resolution" at the
> bottom for the commit map and a second-pass review.

Findings are grouped by severity. Each entry has:
- a one-line repro that reads as a unit test,
- the proposed fix,
- the proposed test name(s).

---

## P0 — silent data loss on round-trip

### R-01. `_handle_modify` trusts task["id"] over the filename
**Severity:** correctness, possible accidental cross-issue overwrite.

A user editing `tasks/jira-A.yaml` to set `id: jira-B` and pushing causes
the helper to PATCH issue **B**, not A. The filename is the operator's
intent; the YAML/Org `id` field is service-side data the operator is
likely to leave alone, but nothing enforces consistency. A stale or
copy-pasted file becomes a silent overwrite of an unrelated issue.

```python
# Repro
ph._handle_modify(
    "M 100644 :2 tasks/jira-FILEID.yaml",
    {":2": ser.serialize({"id": "jira-OTHER", "title": "evil",
                            "source": "jira"}).encode()},
    "refs/heads/main",
)
# driver.upsert is called with id == "jira-OTHER" — no error.
```

**Fix.** In `_handle_modify`, after `task = serializer.deserialize(...)`:
```python
if task.get("id") and task["id"] != task_id_from_path:
    self._record_export_error(
        current_ref,
        f"id/path mismatch: file is {task_id_from_path!r} "
        f"but content id is {task['id']!r}",
    )
    return
# Canonicalize so the driver always sees the filename id.
task["id"] = task_id_from_path
```

**Test.** `test_handle_modify_rejects_id_filename_mismatch`.

---

### R-02. Org body line starting with `*` is dropped
**Severity:** correctness, silent data loss in Org descriptions.

`OrgSerializer.deserialize` breaks the body loop on any line that, after
`lstrip()`, starts with `*` — including bullet lines that are part of
the description body. Round-trip:

```
description: "* not a headline"  →  description: None
```

The loop check only looks at `lstrip()`, but body lines are always
indented (the emitter writes `f"  {line}"`); a real second headline is
column-zero. Tightening the check to "column-zero `*`" preserves bodies
that contain bullets.

**Fix.** Replace
```python
if lines[i].lstrip().startswith("*") and lines[i].lstrip() != "*":
    break
```
with
```python
if lines[i].startswith("*") and not lines[i].startswith("* "[:0]):
    # Column-zero '*' starts a new headline; '  *' is body content.
    break
```
or the simpler:
```python
if lines[i][:1] == "*":
    break
```

**Test.** `test_org_body_with_leading_star_preserved`.

---

### R-03. Org tags containing `,` are split on round-trip
**Severity:** correctness, silent data loss in tags.

The Org serializer writes tags as `:TAGS: a,b,c`. A tag with an embedded
comma (legal in Jira labels, Vikunja labels, MS Todo categories, Notion
multi-select) gets sliced into multiple tags on parse. Round-trip:

```
tags: ["needs review", "a,b", "c"]
  →  :TAGS: needs review,a,b,c
  →  tags: ["needs review", "a", "b", "c"]
```

**Fix options.**
1. Quote tags containing `,` and respect the quoting on parse.
2. Use `;` as a separator; document that tags may not contain `;`.
3. Switch to Org's native `:tag1:tag2:` headline syntax; richer but
   bigger change. Keep the `:TAGS:` drawer for backwards compatibility
   and fall back to it when the headline form is absent.

Option 1 is the smallest change and survives any tag content. Sketch:
```python
def _quote_tag(t):
    if "," in t or t != t.strip() or '"' in t:
        return '"' + t.replace('"', '\\"') + '"'
    return t
# emit
",".join(_quote_tag(t) for t in tags)
# parse
def _split_tags(s):
    out, cur, in_q, esc = [], [], False, False
    for ch in s:
        if esc:
            cur.append(ch); esc = False; continue
        if ch == "\\" and in_q:
            esc = True; continue
        if ch == '"':
            in_q = not in_q; continue
        if ch == "," and not in_q:
            out.append("".join(cur).strip()); cur = []; continue
        cur.append(ch)
    if cur: out.append("".join(cur).strip())
    return [t for t in out if t]
```

**Test.** `test_org_tag_with_comma_roundtrips`.

---

### R-04. YAML `\r` in a value silently truncates the description
**Severity:** correctness, silent data loss when source contains CR.

`YAMLSerializer.serialize` does not escape `\r`. The parser uses
`content.splitlines()`, which treats both `\n` and `\r` as line
breaks. A description containing `\r` writes the raw CR into the YAML
file; on round-trip the parser splits the value at the CR and only the
first segment survives.

```python
task["description"] = "line1\rline2"
ser.deserialize(ser.serialize(task))["description"]  # → "line1"
```

Notion's rich-text export and MS Todo's HTML body sometimes carry CR
characters. The serializer's docstring says "CRLF line endings" are
unsupported on **input**; the bug is on **output**.

**Fix.** Either:
1. Escape `\r` in double-quoted strings:
   ```python
   escaped = (s.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\r", "\\r")
                .replace("\n", "\\n"))
   ```
   Add `\\r` to `_parse_yaml_inline_scalar`.
2. For block scalars, normalize `\r\n` → `\n` and bare `\r` → `\n` on
   serialize; document the lossy conversion.

Option 1 is round-trip clean. Option 2 is a behaviour change but easier
to read.

**Test.** `test_yaml_carriage_return_in_description_roundtrip`.

---

### R-05. Notion `_query_pages` loops forever on `has_more=True, next_cursor=None`
**Severity:** correctness, hang on malformed/transitional Notion responses.

```python
if not data.get("has_more"):
    break
cursor = data.get("next_cursor")
```

If Notion ever returns `has_more=True` with `next_cursor=None` (rare,
but observed during eventual-consistency windows), the next iteration
sends `start_cursor=None` — which Notion treats as the first page — and
we loop on the same page indefinitely. `git fetch` hangs until the
operator notices.

**Fix.** Defensive break on falsy cursor:
```python
if not data.get("has_more"):
    break
cursor = data.get("next_cursor")
if not cursor:
    break
```

**Test.** `test_notion_pagination_breaks_when_cursor_missing`.

---

## P1 — push-side correctness

### R-06. Notion `_build_properties` writes columns that aren't in the database
**Severity:** correctness, push fails with 400 instead of skipping cleanly.

`_select_payload` defaults to `select` shape when `schema.get(col)` is
absent:
```python
ptype = schema.get(col, "select")
```

If a Notion database has no `Status` column, the helper still emits
`{"Status": {"select": {"name": "To Do"}}}` and Notion responds 400
("property does not exist"). This mirrors the same hazard for
`Priority`, `Tags`, `Description`, `Due`. Tags/Due/Description already
short-circuit on `schema.get(name) == "<expected>"`; status and
priority don't.

**Fix.** Make `_select_payload` return `None` when the column is
missing:
```python
def _select_payload(col, value):
    ptype = schema.get(col)
    if ptype == "status":
        return {"status": {"name": value}}
    if ptype == "select":
        return {"select": {"name": value}}
    return None
```

**Test.** `test_notion_skips_missing_status_column`.

---

### R-07. Notion silently drops tags when the column type is `select`
**Severity:** UX, silent data loss on misconfigured database.

```python
if task.get("tags") and schema.get(names["tags"]) == "multi_select":
    props[names["tags"]] = ...
```

If the operator's Tags column is a single-select instead of
multi-select, the tag list is dropped with no warning. Either:
- log a one-shot warning via `_warn_once("notion-tags-shape", ...)` at
  push time, or
- write the first tag as a single value when the column is `select`.

**Fix.** Add the warn-and-best-effort path:
```python
tags = task.get("tags") or []
if tags:
    tcol = names["tags"]
    ptype = schema.get(tcol)
    if ptype == "multi_select":
        props[tcol] = {"multi_select": [{"name": t} for t in tags]}
    elif ptype == "select":
        # Write only the first; warn so the operator sees the loss.
        self._warn_tags_shape(tcol)
        props[tcol] = {"select": {"name": tags[0]}}
    else:
        # No column or unknown shape: warn and drop.
        self._warn_tags_shape(tcol)
```

`_warn_tags_shape` would call `protocol._warn_once` if the protocol
handle is reachable, otherwise stderr direct. (Today the driver has no
back-reference to the protocol; either thread one through, or use a
module-level `sys.stderr` warn.)

**Test.** `test_notion_warns_when_tags_column_is_select`.

---

### R-08. `deleted_ids` not safety-checked before emitting `D <path>`
**Severity:** defense-in-depth.

`_emit_blobs` filters out unsafe ids before emitting `M`. The matching
filter on `D` lines is missing — `_write_incremental_import` writes
`f"D tasks/{tid}.{ext}\n"` directly from `deleted_ids`. Today
`git fast-import` rejects `..` segments only on `M`; on `D` it accepts
the path silently and treats it as a no-op miss. Not exploitable in the
current code, but the symmetric check belongs.

**Fix.** Filter `deleted_ids` through `is_safe_task_id` in
`_write_incremental_import`:
```python
for tid in deleted_ids:
    if not is_safe_task_id(tid):
        self._warn_once("unsafe-id",
                         f"skipping delete of unsafe id {tid!r}")
        continue
    self._write(f"D tasks/{tid}.{ext}\n")
```

**Test.** `test_write_incremental_import_skips_unsafe_deleted_ids`.

---

### R-09. Vikunja `upsert` write-then-fetch reads no response
**Severity:** UX, drives a class of "did it really work?" bugs.

`upsert` POSTs to `/tasks/{id}` and discards the response body. If the
service returned a different `updated` timestamp than what the local
file says, the local file is now stale; the next fetch will pull the
service's value back. This is acceptable for now but should be noted
in `DESIGN-sync.md` because the planned `mirror.last_observed` design
in §6.2 needs the server response.

**Fix.** Out of scope for this review; track in PLAN.md as part of the
mirrors design.

---

### R-10. Jira `_transition` partial failure leaves split state
**Severity:** UX.

In `JiraDriver.upsert`, the order is:
```python
self._http_put(.../issue/<key>, body={"fields": fields})
self._transition(base, native, task.get("status"), headers)
```

If the PUT lands but the transition raises (no transition path
available, or transient 5xx after retries), the issue's title /
description / etc. are updated but its status isn't, and `git push`
reports `error <ref>`. The next push will retry both. This is
arguably the right behaviour, but the user-visible failure should be
clearer: the message currently says
`"no Jira transition to 'In Progress' available for PROJ-1"` with no
hint that the *other* fields were already saved.

**Fix.** Tag the message:
```python
raise JiraPushError(
    f"fields updated, but no Jira transition to "
    f"{target_name!r} is available for {key} (current workflow)"
)
```

**Test.** unit test for the message format already exists; widen to
cover the "fields-updated-but-status-not" wording.

---

## P2 — usability and polish

### R-11. `_handle_delete` errors on a 404 from the remote
**Severity:** UX.

`git rm tasks/jira-X.yaml && git push` calls `driver.delete("jira-X")`.
If the issue was already deleted on Jira (or never existed), the API
returns 404 → `HTTPError` → recorded as a push failure. The local tree
already removed the file, so this is a no-op locally; failing is
hostile to the operator.

**Fix.** In every driver's `delete`, treat 404 as success:
```python
try:
    self._http_delete(...)
except urllib.error.HTTPError as exc:
    if exc.code == 404:
        return
    raise
```

**Test.** `test_jira_delete_404_is_soft_success` (and same for the
other three drivers).

---

### R-12. Org headline `[#A] looks-like-priority` round-trips lossy
**Severity:** UX, low-frequency edge case.

A title literally containing `[#A]` (e.g., a copy-pasted Jira summary
that includes a priority cookie) is parsed as priority + title:
```
* TODO [#A] looks-like-priority
  → status=todo, priority=critical, title="looks-like-priority"
```

This is a lossy round-trip but Org-mode's tradition is exactly that —
the cookie shape is reserved. Document the tradition, or escape the
opening `[` on serialize when the title genuinely starts with `[#X]`.

**Fix.** Document only; code change adds churn for a rare case.

---

### R-13. Notion `since` watermark drift
**Severity:** correctness in low-frequency case.

`_now_iso()` returns the local time at the moment the fetch finishes,
not the server-side last_edited_time of the latest result. If a Notion
update lands between our query response and our local clock read, the
update's `last_edited_time` may be < `_now_iso()`, and the next fetch's
`on_or_after` filter will skip it. Same hazard for Jira (`updated >=`)
and Vikunja (`updated > '<ts>'`).

**Fix.** Use the larger of `(server_max_last_edited_time,
_now_iso() - small_overlap)` as the next-since token. The overlap
window (~5s) makes us re-fetch a handful of recent items; on a busy
project this is cheap insurance vs. silent drift.

```python
def _next_since(self, results: list[dict], field: str) -> str:
    server_max = ""
    for r in results:
        v = r.get(field) or ""
        if v > server_max:
            server_max = v
    now = self._now_iso()
    overlap = self._overlap_seconds(default=5.0)
    return _iso_minus_seconds(now, overlap) if not server_max else server_max
```

**Test.** `test_jira_since_uses_overlap_window`.

---

### R-14. MS Todo first fetch wastes a delta seed
**Severity:** UX, performance.

`MSTodoDriver.fetch_all()` calls `_fetch_with_optional_delta(use_delta=False)`,
hitting `/me/todo/lists/{id}/tasks`. The next fetch then calls
`fetch_changed`, which is the FIRST delta call and re-downloads
everything to seed the link. So the operator pays for two full fetches
in a row.

**Fix.** Make `fetch_all` use the delta endpoint too — Microsoft's docs
recommend this. The result is still "all tasks" because the initial
delta call returns the full set; the difference is that the deltaLink
is persisted so the next fetch is a real diff.

Alternatively, write the delta link in `fetch_all`'s code path so the
first fetch seeds it.

**Test.** `test_mstodo_first_fetch_seeds_delta_link`.

---

### R-15. `_redact_http_error` strips the response body wholesale
**Severity:** UX.

`HTTPError` from a 4xx is repackaged as `f"HTTP {code} from {safe_url}"`
with no body. The body often contains useful diagnostics (Notion's
`message`, Jira's `errorMessages`, Vikunja's `message`). A blanket
strip protects tokens at the cost of debugging.

**Fix.** Read up to 1KB of the body, run a simple redaction
(`Authorization: Bearer ...`, `Authorization: Basic ...`, query string
`token=...`), and append. Limit to 1KB so a giant HTML error page
doesn't flood stderr.

**Test.** `test_http_error_includes_redacted_body`.

---

### R-16. YAML emitter doesn't escape `\t` in double-quoted strings
**Severity:** polish.

A title with a tab is emitted as a literal tab inside `"..."`. YAML
permits this, but most consumers prefer `\t`. Round-trips fine today;
matters for human readability of the file.

**Fix.** Add `.replace("\t", "\\t")` to the escape chain. Add `\\t` to
`_parse_yaml_inline_scalar`.

**Test.** `test_yaml_tab_in_title_escaped`.

---

### R-17. `_iso_to_org_timestamp` weekday is locale-dependent
**Severity:** polish, cross-machine determinism.

`dt.strftime("%a")` returns the locale's abbreviated weekday name. On
a machine with a non-English locale (e.g. `de_DE`), the same task
serializes to `[2024-01-15 Mo 10:30 +0100]`. Round-trip is OK because
the parser accepts any `[A-Za-z]+`, but two clones in different locales
emit different bytes — and that's a phantom diff in `git status` after
a no-op fetch.

**Fix.** Compute the English short weekday from `dt.weekday()`:
```python
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
weekday_short = _WEEKDAYS[dt.weekday()]
body = dt.strftime("%Y-%m-%d") + f" {weekday_short} " + dt.strftime("%H:%M")
```

**Test.** `test_iso_to_org_timestamp_locale_independent`.

---

### R-18. `cmd_init` subprocess calls miss timeouts
**Severity:** robustness, very low likelihood.

`subprocess.run(["git", "init", ...])`, `git add`, `git diff
--cached`, `git commit` are all called without timeouts. If git ever
hangs (corrupt repo, fs lock), `init` blocks. `_run_git_config` and
`_previous_tip` already use `_GIT_SUBPROCESS_TIMEOUT`; symmetry says
`cmd_init` should too.

**Fix.** Add `timeout=_GIT_SUBPROCESS_TIMEOUT` to each `subprocess.run`
call in `cmd_init`. Wrap in `try/except subprocess.TimeoutExpired`.

**Test.** Skip — exercising a hung git is brittle in CI.

---

## Tests to add (independent of fixes)

These cover behaviours that are correct today but not asserted; a
regression here would be silent.

| Test                                                    | Asserts                                                                |
|---------------------------------------------------------|------------------------------------------------------------------------|
| `test_path_safety_rejects_double_dot_segments_in_id`    | `is_safe_task_id("a..b")` policy is intentional (today: True). |
| `test_promote_pending_since_first_fetch_promotes`       | First fetch's pending state lands when current_tip becomes non-empty.  |
| `test_subconfig_dotted_overrides_json_blob`             | Documented merge-precedence stays true.                                |
| `test_yaml_block_scalar_with_blank_lines_in_middle`     | Block scalar preserves interior blank lines.                           |
| `test_org_drawer_unclosed_at_eof`                       | Missing `:END:` doesn't infinite-loop.                                 |
| `test_handle_modify_no_blob_uses_path_in_error`         | The error message names the path, not the mark.                        |
| `test_export_with_only_blob_no_commit`                  | Empty push → `ok refs/heads/main`, no error.                           |
| `test_jira_paginate_falls_back_to_legacy_on_410`        | The 410 branch in `_paginate` runs and switches to `/search`.          |

---

## Suggested order of fixes

1. **R-01, R-02, R-03, R-04, R-05** — silent data loss; ship together
   as one commit per fix.
2. **R-06, R-07, R-08** — push-side hardening; group as one commit.
3. **R-11** — soft-success on 404 deletes; one commit.
4. **R-13** — overlap window; touches all three time-based drivers,
   one commit.
5. **R-14, R-15, R-16, R-17, R-18** — polish; one commit each.

Each fix is locally scoped (no cross-module ripples) and covered by
the suggested test name.

---

## What this review did not cover

- The `DESIGN-sync.md` mirrors design (logical_id + `mirrors/` layer).
  It's draft and out of scope for current correctness work.
- Live integration tests (`test_live_integration.py`) — not run in
  this audit.
- Performance: only flagged R-14 because it's user-visible.

---

## Resolution

| ID    | Commit   | Notes                                                                                                  |
|-------|----------|--------------------------------------------------------------------------------------------------------|
| R-01  | caf4afe  | `_handle_modify` rejects id/path mismatch, canonicalizes empty content_id to filename.                 |
| R-02  | caf4afe  | Org body parser uses column-zero `*` only as a headline boundary.                                      |
| R-03  | caf4afe  | Org `:TAGS:` uses CSV with optional double-quoting; backwards compatible reader.                       |
| R-04  | caf4afe  | YAML emitter forces double-quoted form on `\r` / `\t`; parser decodes the new escapes.                 |
| R-05  | caf4afe  | Notion `_query_pages` defensive break on falsy `next_cursor`.                                          |
| R-06  | 2784710  | `_select_payload` skips fields whose column is missing from the database schema.                       |
| R-07  | 2784710  | Tags shape: multi-select writes all, single-select writes first with warn, missing/other warn-and-drop. |
| R-08  | 2784710  | `_write_incremental_import` filters `deleted_ids` through `is_safe_task_id`.                           |
| R-09  | (deferred) | Out of scope; tracked under the `mirrors/` design.                                                   |
| R-10  | 5bf53dd  | `_transition` failure now names the half-applied state ("fields updated, but no transition…").        |
| R-11  | 5bf53dd  | All four drivers treat 404 / 410 on delete as soft success.                                            |
| R-12  | (documented only) | The `[#X]` cookie collision is org-mode tradition; no code change.                            |
| R-13  | 9ffac1d  | New `syncOverlapSeconds` (default 5s) prevents mid-fetch drift across Jira/Vikunja/Notion.             |
| R-14  | 9e46105  | `MSTodoDriver.fetch_all` uses the delta endpoint to seed the link on the first run.                    |
| R-15  | 9e46105  | `_redact_http_error` keeps a 1KB body excerpt with auth-token redactions.                              |
| R-16  | caf4afe  | Shipped with R-04 (YAML escapes `\t` alongside `\r`).                                                  |
| R-17  | 9e46105  | `_iso_to_org_timestamp` uses a hard-coded English short-weekday table.                                 |
| R-18  | 9e46105  | `cmd_init` git invocations honour `_GIT_SUBPROCESS_TIMEOUT`.                                           |

Test count: 352 (start) → 401 (end), 49 new tests covering every fix.

---

## Second-pass review (post-fix)

Re-examined the diff for new issues introduced by the fixes. None
landed; one observation worth noting:

### S-01. R-01's pre-check shadows `_native_id`'s cross-source error
**Severity:** UX nuance, not a bug.

A foreign-source push (e.g. file `tasks/notion-X.yaml` with content
`id: jira-Y`) is now refused with `id/path mismatch for ...` instead of
the older `refusing to push 'jira-Y' to 'notion' — task id belongs to a
different service.` The new message is structurally correct (the file
*is* mismatched) and the cross-source-via-`_native_id` path is still
reachable when callers invoke `driver.upsert(task)` directly (driver
unit tests still cover it). For a protocol-layer push the new message
is fine — fix the file or rename it.

### S-02. R-15 redaction is best-effort
**Severity:** known limitation.

The redactions match `Authorization: Bearer/Basic`, query strings
`?token=...`, and JSON `"access_token":"..."`-shaped fields. They do
not catch every token format (`Authorization: Token xyz`, X-API-Key
headers, YAML-style `password: secret` in a body). Acceptable: the
goal is "don't ship operator tokens to logs", not perfect redaction;
operators should still treat error logs as sensitive.

### S-03. MS Todo first-fetch deltaLink seeding is opportunistic
**Severity:** correctness, but no regression.

If the FIRST `fetch_all` succeeds but `_persist_delta_link` fails to
write to git config (e.g. config locked by another process), the
deltaLink is held only in `self.config` for this run. Next process
sees no link and re-seeds — equivalent to the old behaviour. The
warn-on-write-fail path inside `write_config_value` already logs to
stderr; nothing silently breaks.

### Tests added beyond the original list

The implementation surfaced test cases the original review didn't
prescribe; all are now covered:

- YAML round-trip with mixed `\r\n\t`, leading/trailing CR.
- Org tag round-trip with quotes, leading commas, multi-comma sequences.
- R-04 block-scalar still preferred for plain LF (no regression).
- R-06 single-warning-per-code dedupe under repeated push.
- R-11 per-driver 404 AND 410 paths plus 500-still-raises.
- R-13 overlap precision at 0 / negative / non-numeric values.
- R-14 deltaLink persistence on the first fetch_all.
- R-15 token-in-body redaction across three real-world payload shapes.
- R-17 locale-independent weekday in a non-English locale environment.
- R-18 cmd_init returns non-zero on git-init timeout.
