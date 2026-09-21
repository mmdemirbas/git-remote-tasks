# git-remote-tasks

**Jira, Vikunja, Microsoft To Do and Notion as native git remotes. `git fetch` pulls every task
in as a file under `tasks/`; edit, commit, `git push`, and the tracker changes.**

[![License](https://img.shields.io/github/license/mmdemirbas/git-remote-tasks)](LICENSE)

[Website](https://mmdemirbas.github.io/git-remote-tasks/) ·
[Source](https://github.com/mmdemirbas/git-remote-tasks) ·
[Project page](https://mdemirbas.com/en/projects/git-remote-tasks/)

![A task tracker as a git remote: fetch imports tasks as files, push exports edits as API calls](docs/remote-helper.svg)

```text
$ git fetch jira-work
$ ls tasks/
jira-PROJ-123.yaml jira-PROJ-124.yaml jira-PROJ-127.yaml
$ $EDITOR tasks/jira-PROJ-123.yaml
$ git add tasks/ && git commit -m "tasks: bump priority"
$ git push jira-work main      # updates the issue on Jira
```

A git remote helper is any executable named `git-remote-<scheme>` on the PATH: git spawns it for
a URL with that scheme and speaks a line protocol over stdin and stdout. This one is a single
Python file, standard library only, that answers to four names through symlinks —
`git-remote-jira`, `git-remote-vikunja`, `git-remote-mstodo`, `git-remote-notion` — and
translates between each tracker's REST API and git's object model.

- **Every sync is a commit** — `refs/remotes/<remote>/main` is a real linear history, so
  `git diff`, `git log`, `git blame`, `git bisect` and tags work on tasks
- **YAML or Org-mode** per repository, chosen at init; both round-trip through one schema
- **Incremental** — a full snapshot on the first fetch, a changed-since delta afterwards; an
  interrupted import never loses data
- **Honest push** — one upsert per edited file, one delete per removed file; a cross-source id
  is refused rather than duplicated, and `git push`'s exit code reflects every failure

## Quick start

```bash
git clone https://github.com/mmdemirbas/git-remote-tasks.git ~/src/git-remote-tasks
python ~/src/git-remote-tasks/git_remote_tasks.py install --bin-dir ~/.local/bin

tasks-init --format yaml ~/work/tasks && cd ~/work/tasks
git remote add vikunja vikunja://localhost:3456
git config tasks-remote.vikunja.scheme    vikunja
git config tasks-remote.vikunja.baseUrl   http://localhost:3456
git config tasks-remote.vikunja.apiToken  $(pass show vikunja/api)

git fetch vikunja
git merge vikunja/main --allow-unrelated-histories   # first time only
$EDITOR tasks/vikunja-42.yaml
git add tasks/ && git commit -m "tasks: raise priority on 42"
git push vikunja main
```

Python 3.10+ and git 2.20+. `msal` is the one optional extra, for Microsoft To Do's device-code
login.

## How it works

1. **Fetch** — git sends `capabilities`, `list`, `import`. The helper decides between a full
   snapshot (`deleteall` and a rebuilt `tasks/` tree) and a delta (`M` for changed files, `D`
   for tombstones), writes a fast-import stream, and records the new `since` token only once
   the tip has advanced.
2. **Push** — git writes a fast-export stream. Each `M` under `tasks/` is deserialised and
   upserted; each `D` is deleted; anything outside `tasks/` is ignored. `ok` or `error` per
   ref, non-zero exit if anything failed.

Credentials live under `[tasks-remote "<name>"]` in `.git/config`, never in the URL.

## Format choice: YAML vs Org-mode

Pick one format per repo at init time. Files always live under `tasks/`
as `tasks/<source>-<native-id>.<ext>`.

| Aspect             | YAML                                  | Org-mode                                  |
|--------------------|---------------------------------------|-------------------------------------------|
| Extension          | `.yaml`                               | `.org`                                    |
| Diff-friendliness  | Excellent (one line per field)        | Good (headline + drawer + body)           |
| Native editors     | Any text editor; VS Code extensions   | Emacs org-mode, neovim orgmode, VS Code   |
| Status transitions | `status: in_progress`                 | `* IN-PROGRESS` with `DEADLINE:` agenda   |
| Priority           | `priority: high`                      | `[#B]` cookies                            |
| Multiline body     | Block scalar `|`                      | Paragraphs under the headline             |
| When to choose     | Teams used to YAML configs            | Personal workflows already in Emacs/vim   |

Both formats round-trip through the same internal schema, so switching
is a script away — but mixing formats within one repo is not supported.

## Remote URL format

```
<scheme>://<host-or-id>
```

| Scheme    | Example URL                   | Notes                                 |
|-----------|-------------------------------|---------------------------------------|
| `jira`    | `jira://company.atlassian.net`| Host portion is informational only.   |
| `vikunja` | `vikunja://localhost:3456`    | Or `vikunja://vikunja.local`.         |
| `mstodo`  | `mstodo://consumers`          | Tenant: `consumers`, `organizations`. |
| `notion`  | `notion://db-<id>`            | Suffix is cosmetic.                   |

The URL is recorded by git and passed to the helper; real credentials
live in `.git/config` (next section).

## Configuration reference

Everything lives under `[tasks]` and `[tasks-remote "<name>"]` in the
repo's `.git/config`. Use `git config` to read and write — never edit
the file by hand.

Global:

| Key            | Type   | Description                               |
|----------------|--------|-------------------------------------------|
| `tasks.format` | string | Either `yaml` or `org`. Set once at init. |

Per-remote (`tasks-remote.<name>.*`):

| Key             | Scheme(s)     | Description                                |
|-----------------|---------------|--------------------------------------------|
| `scheme`        | all           | `jira` / `vikunja` / `mstodo` / `notion`.  |
| `baseUrl`       | jira, vikunja | Service base URL.                          |
| `email`         | jira          | Your Atlassian account email.              |
| `apiToken`      | jira, vikunja | Service API token.                         |
| `projectKey`    | jira          | Required to CREATE new issues.             |
| `projectId`     | vikunja       | Required to CREATE new tasks.              |
| `jql`           | jira          | Override the default JQL filter.           |
| `tenantId`      | mstodo        | MSAL tenant (`consumers` for personal).    |
| `clientId`      | mstodo        | Registered Azure AD client ID.             |
| `accessToken`   | mstodo        | Pre-acquired bearer token (bypasses MSAL). |
| `defaultListId` | mstodo        | List id for CREATE + DELETE.               |
| `deviceFlowTimeout` | mstodo    | Max seconds to wait for device-code approval. |
| `databaseId`    | notion        | Target database ID.                        |
| `token`         | notion        | Integration token (bearer).                |
| `databaseTitle` | notion        | Optional friendly category name.           |
| `httpTimeout`   | all           | Per-request timeout in seconds (default 30). |
| `pageSize`      | jira, vikunja | Fetch page size (Jira 100/max 100, Vikunja 100/max 250). |
| `syncOverlapSeconds` | jira, vikunja, notion | Subtract this many seconds from the persisted `since` token so events that landed mid-fetch are not missed on the next run. Default 5. Set to 0 to opt out. |

Run `python git_remote_tasks.py check <remote>` to validate required
keys without touching the network. Secret-like keys are redacted in
its output.

### Incremental sync state

After every successful fetch, the helper writes per-remote state to
`.git/config` so the next run asks the service only for what changed:

| Key                                                   | Source  | Meaning                                                                |
|-------------------------------------------------------|---------|------------------------------------------------------------------------|
| `tasks-remote.<name>.sync.mode`                       | user    | `incremental` (default) or `full`. `full` forces a deleteall snapshot. |
| `tasks-remote.<name>.sync.lastFetchAt`                | helper  | ISO timestamp token, passed back as `since` on the next fetch.         |
| `tasks-remote.<name>.sync.pending.*`                  | helper  | Two-phase watermark; promoted on the next run only if the import landed. |
| `tasks-remote.<name>.sync.deltaLink.<hex(listId)>`    | helper  | Graph delta link for each MS Todo list.                                |

Per-service incremental strategy:

| Service  | API                                                                                  | Native deletion feed? |
|----------|--------------------------------------------------------------------------------------|------------------------|
| Jira     | JQL `updated >= "<ts>"`.                                                             | No — use `sync.mode=full` periodically. |
| Vikunja  | `filter=updated > '<ts>'`.                                                           | No — same.              |
| MS Todo  | Graph delta query at `/me/todo/lists/{id}/tasks/delta`; tombstones as `@removed`.    | Yes.                    |
| Notion   | `databases/{id}/query` with `last_edited_time on_or_after <ts>`; archived as deletes.| Yes, via `archived:true`. |

Force a full resync with:

```bash
python git_remote_tasks.py reset <remote>       # wipes sync.* keys
# or
git config tasks-remote.<name>.sync.mode full   # one-off
git fetch <remote>
git config --unset tasks-remote.<name>.sync.mode
```

### Custom status / priority / field mapping

Every non-trivial tracker lets the team rename workflows and columns.
Two encodings are supported — pick whichever fits the key characters:

**Dotted keys** (ASCII alphanumeric plus `-`):

| Key                                                | Example                           |
|----------------------------------------------------|-----------------------------------|
| `tasks-remote.<name>.statusMap.<upstream>`         | `statusMap.Triage = in_progress`  |
| `tasks-remote.<name>.priorityMap.<upstream>`       | `priorityMap.P0 = critical`       |
| `tasks-remote.<name>.fieldMap.<logical>`           | `fieldMap.dueDate = Deadline`     |

**JSON-encoded maps** (any key, including non-ASCII / underscores / dots):

```bash
git config tasks-remote.jira-work.statusMap '{"Ertelendi":"todo","İptal":"cancelled"}'
git config tasks-remote.notion-inbox.fieldMap '{"dueDate":"Tarih","priority":"Acil"}'
```

Logical field names for `fieldMap.*`: `status`, `priority`, `tags`,
`dueDate`, `description`, `done`. The unified `due_date` schema field
is addressed in camelCase to satisfy git's variable-name rules.

Overrides apply in **both** directions — the same column name is read
on pull and written on push. On Notion, a push also *inverts* your
`statusMap` / `priorityMap` so the database's real option names
(e.g. `"Not started"`, `"Today"`) get used instead of the helper's
generic defaults.

Status / priority lookups are case-insensitive after an exact match.
Upstream values not covered by the map fall back to each driver's
built-in dictionary.

## Service setup

### Jira (Atlassian Cloud)

1. Log in at https://id.atlassian.com/manage-profile/security/api-tokens.
2. Create a token labelled something like `git-remote-tasks`.
3. Save it securely — you'll only see it once.
4. Configure the remote:

   ```bash
   git remote add jira-work jira://company.atlassian.net
   git config tasks-remote.jira-work.scheme    jira
   git config tasks-remote.jira-work.baseUrl   https://company.atlassian.net
   git config tasks-remote.jira-work.email     me@company.com
   git config tasks-remote.jira-work.apiToken  "$TOKEN"
   # Optional: to create new issues from local commits.
   git config tasks-remote.jira-work.projectKey PROJ
   # Optional: override the default JQL (`created is not EMPTY ORDER BY updated DESC`).
   git config tasks-remote.jira-work.jql       "assignee = currentUser()"
   ```

   The driver targets Atlassian's new `/rest/api/3/search/jql`
   endpoint by default, falling back to the legacy `/search` on 404/410
   for self-hosted Jira Data Center. `Accept: application/json` is set
   automatically so Atlassian returns real data rather than the schema
   preview.

### Vikunja (self-hosted)

1. In the Vikunja UI: **Settings → API Tokens → New token** with
   `tasks.read` and `tasks.write` scopes.
2. Copy the token and register the remote:

   ```bash
   git remote add vikunja vikunja://localhost:3456
   git config tasks-remote.vikunja.scheme    vikunja
   git config tasks-remote.vikunja.baseUrl   http://localhost:3456
   git config tasks-remote.vikunja.apiToken  "$TOKEN"
   # Required for CREATE; points at the project new tasks land in.
   git config tasks-remote.vikunja.projectId 1
   ```

### Microsoft To Do

1. Register an app in the Azure portal under **App registrations** with
   redirect URI `http://localhost:1234`.
2. Grant `Tasks.ReadWrite` delegated permission.
3. Copy the Application (client) ID. Pick a tenant — `consumers` for
   personal Microsoft accounts.
4. Configure the remote:

   ```bash
   git remote add todo mstodo://consumers
   git config tasks-remote.todo.scheme        mstodo
   git config tasks-remote.todo.tenantId      consumers
   git config tasks-remote.todo.clientId      "$CLIENT_ID"
   # Required for CREATE + DELETE (the removed task file no longer
   # carries the list id).
   git config tasks-remote.todo.defaultListId "$LIST_ID"
   ```

5. Install `msal` for the device-code flow:

   ```bash
   pip install msal
   ```

   The first fetch prompts on stderr with the device-code URL. The
   refresh token is persisted under `tasks-remote.todo.refreshToken`
   so subsequent runs are silent.

### Notion

1. Go to https://www.notion.so/my-integrations, create a new internal
   integration, copy its Internal Integration Token.
2. Share the target database with the integration (database → share).
3. Find the database ID in the URL (the 32-hex segment).
4. Configure the remote:

   ```bash
   git remote add notion-inbox notion://inbox
   git config tasks-remote.notion-inbox.scheme     notion
   git config tasks-remote.notion-inbox.databaseId "$DB_ID"
   git config tasks-remote.notion-inbox.token      "$INTEGRATION_TOKEN"
   ```

   Notion push supports create (`POST /v1/pages`), update
   (`PATCH /v1/pages/{id}`), and archive-on-delete
   (`PATCH archived: true`). The title column's name is auto-discovered
   from the database schema. Each column's payload shape (`select` vs
   the newer `status` type, `multi_select`, `date`, `rich_text`) is
   adapted per-column so we don't 400 on database-specific config.

## Git workflow

| Git command                   | Effect                                           |
|-------------------------------|--------------------------------------------------|
| `git fetch <remote>`          | Pulls remote task snapshot into `<remote>/main`. |
| `git diff main <remote>/main` | Shows what changed upstream since last sync.     |
| `git merge <remote>/main`     | Materializes remote tasks into the working tree. |
| `git log -- tasks/`           | Audit trail of every sync + manual edit.         |
| `git push <remote> main`      | Upserts edited tasks; deletes removed files.     |
| `git tag release/2025-W16`    | Snapshots task state at a moment in time.        |

Each sync bases the new commit on the previous remote tip (via
`from <sha>` in the fast-import stream). `refs/remotes/<remote>/main`
accumulates a real linear history; `git bisect` walks every sync.
`git merge <remote>/main` works without `--allow-unrelated-histories`
except on the very first fetch, when the remote-tracking ref is still
a root commit.

## Org-mode tips

- Emacs with `org-mode` highlights `DEADLINE:` lines natively and
  surfaces them in the agenda view.
- Neovim: `nvim-orgmode/orgmode` gives folding and TODO cycling.
- VS Code: `vscode-org-mode` renders drawers and tags cleanly; combine
  with the built-in git panel.
- Org diffs show up in `git diff` as plain text — the headline change
  is usually the first non-context line.
- YAML diffs are one property per line; org diffs group metadata under
  one drawer. On large batches, YAML tends to produce smaller diffs.

## Troubleshooting

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
python -m unittest test_git_remote_tasks                     # default suite, stdlib only
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest test_git_remote_tasks test_yaml_parser_fuzz   # + hypothesis fuzz
GRT_LIVE_CONFIG=/path/to/config.yaml python test_live_integration.py       # against live services
```

`GIT_REMOTE_TASKS_DEBUG=1` logs every HTTP call with its elapsed time to stderr, query strings
stripped. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

[MIT](LICENSE)
