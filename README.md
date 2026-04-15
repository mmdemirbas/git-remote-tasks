# git-remote-tasks

Bidirectional sync between a git repository and external task management
services — **Jira, Vikunja, Microsoft To Do, and Notion** — by implementing
each service as a **git remote helper**. Every task on the service becomes a
file under `tasks/`; fetching pulls the current state into a commit;
pushing re-materializes local edits as REST calls.

```
$ git fetch jira-work
$ ls tasks/
jira-PROJ-123.yaml jira-PROJ-124.yaml jira-PROJ-127.yaml

$ $EDITOR tasks/jira-PROJ-123.yaml
$ git add tasks/ && git commit -m "tasks: bump priority"
$ git push jira-work main      # updates the issue on Jira
```

Single-file Python script, standard-library only. Install is one symlink
per scheme. Test-time deps (`hypothesis`) are isolated in `.venv`.

---

## 1. What this is

A git remote helper is any executable named `git-remote-<scheme>` on
`PATH`. When git sees a remote URL with that scheme, it spawns the helper
and speaks a line-based protocol over stdin/stdout. The helper translates
between whatever the "remote" actually is (an SVN repo, an S3 bucket, a
task tracker) and git's object model.

`git-remote-tasks` is one Python script that handles four schemes: `jira`,
`vikunja`, `mstodo`, `notion`. The active scheme is picked from the
script's `argv[0]` basename, so a single file becomes four remote helpers
via symlinks.

## 2. Requirements

- Python 3.10 or newer (tested on 3.14).
- git 2.20 or newer.
- Standard library only. `msal` is the one *optional* runtime extra,
  needed for the Microsoft To Do OAuth device-code flow.

## 3. Installation

```bash
# 1. Clone or copy the script.
git clone https://github.com/mmdemirbas/git-remote-tasks.git ~/src/git-remote-tasks
cd ~/src/git-remote-tasks

# 2. Install the per-scheme symlinks onto PATH.
python git_remote_tasks.py install --bin-dir ~/.local/bin

# 3. Confirm.
python git_remote_tasks.py list-schemes
which git-remote-jira tasks-init
```

`install` creates five symlinks in `--bin-dir`: `git-remote-jira`,
`git-remote-vikunja`, `git-remote-mstodo`, `git-remote-notion`, and the
convenience `tasks-init`. All point at the same Python script, which
decides what to do by reading its own invocation name.

## 4. Quick start

```bash
# One-time repo setup.
python git_remote_tasks.py init --format yaml ~/work/tasks
cd ~/work/tasks
# (If symlinks are installed: `tasks-init --format yaml ~/work/tasks`.)

# Add a Vikunja remote.
git remote add vikunja vikunja://localhost:3456
git config tasks-remote.vikunja.scheme    vikunja
git config tasks-remote.vikunja.baseUrl   http://localhost:3456
git config tasks-remote.vikunja.apiToken  $(pass show vikunja/api)

# Fetch + inspect.
git fetch vikunja
git log vikunja/main
git diff main vikunja/main

# First-time only, merge the root commit:
git merge vikunja/main --allow-unrelated-histories

# Make changes and push.
$EDITOR tasks/vikunja-42.yaml
git add tasks/
git commit -m "tasks: raise priority on 42"
git push vikunja main
```

## 5. Format choice: YAML vs Org-mode

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

## 6. Remote URL format

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

## 7. Configuration reference

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

Run `python git_remote_tasks.py check <remote>` to validate required
keys without touching the network. Secret-like keys are redacted in
its output.

### 7.1 Incremental sync state

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

### 7.2 Custom status / priority / field mapping

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

## 8. Service setup

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

## 9. How it works

```
        ┌──────────────────────┐   stdin    ┌──────────┐   HTTPS    ┌────────┐
  git → │ git-remote-<scheme>  │ ─────────► │  helper  │ ─────────► │ service│
        │  (symlink to .py)    │ ◄───────── │  script  │ ◄───────── │  API   │
        └──────────────────────┘            └──────────┘            └────────┘
                │ stdout (fast-import stream)
                ▼
         git repository
```

### Import (`git fetch`)

1. git spawns the helper with the remote URL and sends `capabilities`,
   `list`, and `import <ref>` on stdin.
2. Helper checks `sync.mode` + `sync.lastFetchAt` + the current
   `<remote>/main` tip to decide between a **full snapshot** and an
   **incremental delta**.
3. Full snapshot: `Driver.fetch_all()` paginates the whole task set
   and the helper writes a `deleteall` fast-import stream that rebuilds
   the `tasks/` tree from scratch.
4. Incremental: `Driver.fetch_changed(since)` returns `(changed,
   deleted, new_since)`. The helper emits per-file `M` directives for
   changed blobs and `D` for tombstones — no `deleteall`, so the
   `git diff` shows only what actually changed upstream.
5. The new `since` token is written back under
   `sync.pending.since` together with the parent sha; it only promotes
   to `sync.lastFetchAt` on the next run if the tip actually advanced.
   An interrupted fast-import never loses data.

### Export (`git push`)

1. git writes a fast-export stream to the helper's stdin.
2. Helper reads `blob` / `commit` / `M` / `D` directives, ignoring
   anything outside `tasks/`.
3. For each `M`, the blob is deserialized back to the unified task
   dict and handed to `Driver.upsert()`.
4. For each `D`, `Driver.delete()` is called.
5. Cross-source ids (`jira-X` in a Vikunja remote, etc.) are refused
   with a driver-specific `PushError` — no silent duplication.
6. Helper responds `ok <ref>` on success, `error <ref> <msg>` on any
   failure, and exits non-zero whenever at least one failure was
   recorded. `git push`'s own exit code reflects reality.

## 10. Git workflow

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

## 11. Org-mode tips

- Emacs with `org-mode` highlights `DEADLINE:` lines natively and
  surfaces them in the agenda view.
- Neovim: `nvim-orgmode/orgmode` gives folding and TODO cycling.
- VS Code: `vscode-org-mode` renders drawers and tags cleanly; combine
  with the built-in git panel.
- Org diffs show up in `git diff` as plain text — the headline change
  is usually the first non-context line.
- YAML diffs are one property per line; org diffs group metadata under
  one drawer. On large batches, YAML tends to produce smaller diffs.

## 12. Troubleshooting

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

## 13. Development

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

## 14. Licence

MIT — see `LICENSE`.
