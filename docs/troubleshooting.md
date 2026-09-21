---
title: Troubleshooting and development
order: 50
summary: What each error means and the fix, plus how to run the tests and debug a real fetch or push.
---

> [!TLDR]
> Most failures are a missing config key or a token without the right scope; the sections below are grouped by service, then general, then development.


**`fatal: Unable to find remote helper for 'jira'`**
The symlink isn't on `PATH`:

```bash
python git_remote_tasks.py install --bin-dir ~/.local/bin
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

**`git-remote-tasks: no config for remote 'xxx'`**
`tasks-remote.<name>.*` keys are not set. Use
`python git_remote_tasks.py check <name>` to see what's missing.

**`warning[push-stub]: ...write path is not implemented yet`**
Obsolete — every driver now has a real push path. If you still see
this, you're on an old copy; re-install from this repo.

**Import produces a massive diff on first fetch**
Expected — the initial snapshot replaces nothing with every current
task. Subsequent incremental fetches show only real changes.

**Fetch hangs for several minutes then errors with `git-remote-X died of signal 9`**
You're on an old version. The empty-delta path used to forget to
terminate the fast-import stream, so `git fast-import` waited forever
and the helper blocked reading the never-closed stdin. Re-install from
this repo. To diagnose any future hang, set `GIT_REMOTE_TASKS_DEBUG=1`
to log every HTTP call's URL and round-trip time on stderr — if the
HTTP traffic finishes in a second but the fetch keeps running, the
stream is being mis-terminated.

**MS Todo prints the device code, you approve it, helper still errors with `AADSTS70016`**
Same — old version. The helper used to corrupt MSAL's `expires_at`
deadline so it polled exactly once before giving up. Re-install. If it
ever happens on the current version, set
`tasks-remote.<name>.deviceFlowTimeout` to a value (in seconds) larger
than however long you take to enter the code in the browser.

**Every MS Todo fetch re-downloads everything / spams `invalid key: ...deltaLink...`**
Old version. Delta-link config keys used to start with a digit, which
git config refuses. Every write failed silently, so the next fetch had
no cursor to resume from. Re-install from this repo and run
`git fetch mstodo` once to repopulate the (correctly-named) delta
links — subsequent fetches will be delta-only.

**MS Todo `git fetch` lingers a few seconds after browser approval**
MSAL's device-code poll loop sleeps up to the AAD-provided `interval`
(typically 5s) between polls. That sleep is inside the MSAL library
and we can't shorten it without patching MSAL. Once the first run
persists a refresh token, later fetches skip the device flow entirely
and start instantly.

**Only one remote's files show up in `tasks/`**
Each remote fetches to its own tracking ref (`refs/remotes/<name>/main`);
the working tree only contains what you have merged. To see a remote's
tasks without merging, read the ref directly:

```bash
git ls-tree refs/remotes/jira/main -- tasks/ | head
git show refs/remotes/jira/main:tasks/jira-PROJ-42.yaml
```

To materialize them all in one working tree, merge each remote once
with `--allow-unrelated-histories`:

```bash
git merge jira/main   --allow-unrelated-histories
git merge mstodo/main --allow-unrelated-histories
git merge notion/main --allow-unrelated-histories
```

Ids are prefixed per source (`jira-PROJ-42`, `mstodo-AQM…`,
`notion-<uuid>`, `vikunja-7`), so file names never collide.

**First fetch takes a long time on a large project**
The full snapshot paginates sequentially: 7000+ tasks at 100 per page
is ~70 round-trips. Only the first fetch is full; later fetches are
incremental (JQL `updated >= <ts>`, Vikunja `filter=updated > <ts>`,
MS Todo delta, Notion `last_edited_time`) and typically finish in a
second or two. If you need a faster first run, raise `pageSize`:

```bash
git config tasks-remote.jira-work.pageSize 100     # default; max 100
git config tasks-remote.vikunja.pageSize   250     # max for most instances
```

Jira Cloud's new `/search/jql` endpoint returns opaque
`nextPageToken`s, so pages cannot be fetched in parallel. If you
routinely need snapshots faster than this allows, narrow the JQL
(`jql = assignee = currentUser() AND updated >= -30d`).

**Round-trip YAML/Org changes whitespace**
The serializers are round-trip-stable by design. If you see drift,
either the file was hand-edited in a way the parser canonicalizes
(normal) or it's a bug — please file an issue with a minimal repro.

**Atlassian returns an OpenAPI schema preview instead of data**
Caused by a missing `Accept: application/json` header. The helper
sets it automatically; if you see schema previews while poking Jira
with curl, add `-H 'Accept: application/json'`.

**`error: id/path mismatch for tasks/<remote>-<id>.<ext>: content id is …`**
The file's `id:` field does not match the filename. The helper
treats the filename as the operator's intent — pushing a file
whose content id points at a different upstream issue would have
silently overwritten the wrong task. Either correct the `id:`
field or rename the file. (If you really do want to move the
content from issue A to issue B on the same service, rename the
file: `git mv tasks/jira-A.yaml tasks/jira-B.yaml` and edit the
content `id:` to match.)

**`warning[notion-missing-col:<name>]` / `warning[notion-tags-shape:<name>]`**
The remote's Notion database does not have a column with the name
the helper expected — usually because the column was renamed in
Notion but `fieldMap.<logical>` was never set in the repo's
`.git/config`. The helper drops the affected field on push and
keeps going. Either rename the column back, or:

```bash
git config tasks-remote.<name>.fieldMap.dueDate "Tarih"
git config tasks-remote.<name>.fieldMap.status "Workflow"
```

For tags, `notion-tags-shape:<col>` warns when the column is a
`select` (single value) rather than `multi_select` — the first
tag is written, the rest are dropped. Convert the column in
Notion's UI if you want every tag to land.

**`warning[unsafe-id]: skipping … with unsafe id …`**
A driver returned a task id that does not match
`[A-Za-z0-9][A-Za-z0-9._=-]{0,254}` — most likely a service-side
data-quality issue. The single task is skipped with the warning;
the rest of the fetch / push proceeds.

**`git push` reports `error <ref> upsert failed: HTTP 400 from …: <body>`**
Since the audit, HTTP error responses surface a redacted body
excerpt instead of the bare status code. Strings that look like
auth tokens (`Authorization: Bearer …`, `?token=…`,
`"access_token":"…"`-shaped JSON) are replaced with `<redacted>`,
but any free-text the service returns alongside is preserved.
Treat the captured excerpts as still-sensitive — the redaction is
best-effort, not guaranteed-complete.

**Incremental fetch sometimes misses a row that was edited mid-fetch**
By default the helper subtracts a 5-second overlap from the
persisted `since` token so events that landed during the fetch
are re-checked on the next run. Tune via:

```bash
git config tasks-remote.<name>.syncOverlapSeconds 30   # busy projects
git config tasks-remote.<name>.syncOverlapSeconds 0    # opt out
```

The cost is re-fetching a small recent window on every
incremental call; on Jira / Vikunja / Notion this is typically
faster than picking up a missed update via a forced full fetch.

**`git rm tasks/<id>.<ext>` followed by push fails with HTTP 404**
Should not happen any more — every driver treats 404 / 410 on
delete as soft success, since the local tree has already removed
the file. If you still see this, you're on an old copy; reinstall
from this repo.

**`git push` reports `fields updated, but no Jira transition to '…' is available for PROJ-1 …`**
Jira applied your field edits (title, description, priority,
labels, due date) but the workflow your project uses does not
offer a transition to the requested status from the issue's
current state. Either add the transition in Jira's workflow
editor, or change the local status to one the workflow allows
and push again.

## Development

```bash
# Default suite (stdlib only).
python -m unittest test_git_remote_tasks

# + hypothesis fuzz for the hand-written YAML parser.
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest test_git_remote_tasks test_yaml_parser_fuzz

# End-to-end against live services. Set GRT_LIVE_CONFIG to a
# todo-harvest-style config.yaml containing your credentials.
GRT_LIVE_CONFIG=/path/to/config.yaml python test_live_integration.py
```

The live test harness never deletes or modifies items that it did not
create, and caps created items at five per service per run.

### Debugging a real fetch / push

Set `GIT_REMOTE_TASKS_DEBUG=1` and re-run any git command. Per HTTP
call, the helper logs `http[METHOD] <elapsed>s <url>` to stderr
(query-string stripped to keep tokens out of logs). Useful when a
fetch feels slow — if HTTP is fast but the fetch keeps running, the
problem is in the fast-import stream, not the network.
