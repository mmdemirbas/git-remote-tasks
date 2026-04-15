# git-remote-tasks

Bidirectional sync between a git repository and external task management
services — Jira, Vikunja, Microsoft To Do, and Notion — by implementing each
service as a **git remote helper**.

```
$ git fetch jira-work
$ ls tasks/
jira-PROJ-123.yaml jira-PROJ-124.yaml jira-PROJ-127.yaml

$ $EDITOR tasks/jira-PROJ-123.yaml
$ git add tasks/ && git commit -m "tasks: bump priority"
$ git push jira-work main      # updates the issue on Jira
```

---

## 1. What this is

A git remote helper is any executable named `git-remote-<scheme>` on `PATH`.
When git sees a remote URL using that scheme, it spawns the helper and
communicates over stdin/stdout using a line-based protocol. The helper is
free to translate between whatever the "remote" actually is (an SVN repo,
an S3 bucket, a task tracker) and git's object model.

`git-remote-tasks` is one Python script that handles four schemes. Each task
on the remote service becomes a file in `tasks/`; fetching pulls the current
list into a commit; pushing re-materializes local edits as API calls.

## 2. Requirements

- Python 3.10 or newer (tested on 3.14).
- git 2.20 or newer.
- Standard library only. `msal` is optional and only needed for MS To Do
  OAuth device-code flow.

## 3. Installation

```bash
# 1. Clone this repo (or just copy git_remote_tasks.py somewhere).
git clone https://example.com/git-remote-tasks.git ~/src/git-remote-tasks
cd ~/src/git-remote-tasks

# 2. Install the per-scheme symlinks onto PATH.
python git_remote_tasks.py install --bin-dir ~/.local/bin

# 3. Confirm the helpers are visible.
python git_remote_tasks.py list-schemes
which git-remote-jira
```

`install` creates four symlinks on the target dir (`git-remote-jira`,
`git-remote-vikunja`, `git-remote-msftodo`, `git-remote-notion`) — all pointing
back to `git_remote_tasks.py`. The script decides which service to drive by
reading its own `argv[0]` basename.

## 4. Quick start

```bash
# One-time repo setup.
python git_remote_tasks.py init --format yaml ~/work/tasks
cd ~/work/tasks
# (Equivalent: if symlinks are installed, `tasks-init --format yaml ~/work/tasks`.)

# Add a Vikunja remote.
git remote add vikunja vikunja://localhost:3456
git config tasks-remote.vikunja.scheme    vikunja
git config tasks-remote.vikunja.baseUrl   http://localhost:3456
git config tasks-remote.vikunja.apiToken  $(pass show vikunja/api)

# Fetch + inspect.
git fetch vikunja
git log vikunja/main
git diff main vikunja/main

# Make changes and push.
$EDITOR tasks/vikunja-42.yaml
git add tasks/
git commit -m "tasks: raise priority on 42"
git push vikunja main
```

## 5. Format choice: YAML vs Org-mode

Pick one format per repo at `tasks-init` time. Files always live under
`tasks/` as `tasks/<source>-<native-id>.<ext>`.

| Aspect             | YAML                                  | Org-mode                                   |
|--------------------|---------------------------------------|--------------------------------------------|
| Extension          | `.yaml`                               | `.org`                                     |
| Diff-friendliness  | Excellent (one line per field)        | Good (headline + drawer + body)            |
| Native editors     | Any text editor; VS Code extensions   | Emacs org-mode, neovim orgmode, VS Code    |
| Status transitions | `status: in_progress`                 | `* IN-PROGRESS`, logbook preserved         |
| Priority           | `priority: high`                      | `[#B]` cookies                             |
| Multiline body     | Block scalar `|`                      | Paragraphs under the headline              |
| When to choose     | Teams used to YAML configs            | Personal workflows already in Emacs/vim   |

Both formats round-trip through the same internal schema, so switching is a
script away — but mixing within one repo is not supported.

## 6. Remote URL format

```
<scheme>://<host-or-id>
```

Examples:

| Scheme    | Example URL                          | Notes                                  |
|-----------|--------------------------------------|----------------------------------------|
| `jira`    | `jira://company.atlassian.net`       | Host portion is informational.         |
| `vikunja` | `vikunja://localhost:3456`           | Or remote `vikunja://vikunja.local`.   |
| `msftodo` | `msftodo://consumers`                | Tenant: `consumers`, `organizations`.  |
| `notion`  | `notion://db-<id>`                   | Suffix is purely cosmetic.             |

The URL is recorded by git and passed to the helper; all real credentials live
in `.git/config` (see next section).

## 7. Configuration reference

Everything is stored under `[tasks]` and `[tasks-remote "<name>"]` sections of
the repo's `.git/config`. Use `git config` to read/write — never edit the file
by hand.

Global:

| Key             | Type   | Description                               |
|-----------------|--------|-------------------------------------------|
| `tasks.format`  | string | Either `yaml` or `org`. Set once at init. |

Per-remote (`tasks-remote.<name>.*`):

| Key            | Scheme(s) | Description                                  |
|----------------|-----------|----------------------------------------------|
| `scheme`       | all       | `jira` / `vikunja` / `msftodo` / `notion`.   |
| `baseUrl`      | jira, vikunja | Service base URL.                        |
| `email`        | jira      | Your Atlassian account email.                |
| `apiToken`     | jira, vikunja | Service API token.                       |
| `tenantId`     | msftodo   | MSAL tenant (`consumers` for personal).      |
| `clientId`     | msftodo   | Registered Azure AD client ID.               |
| `accessToken`  | msftodo   | Optional pre-acquired bearer token.          |
| `databaseId`   | notion    | Target database ID.                          |
| `token`        | notion    | Integration token (bearer).                  |
| `databaseTitle`| notion    | Optional friendly category name.             |

Run `python git_remote_tasks.py check <remote>` to validate required keys
without touching the network. Secret-like keys are redacted in its output.

### 7.1 Incremental sync state

After a successful fetch, the helper writes a token to `.git/config` so
the next run can ask the service only for what changed:

| Key                                     | Who sets it | Meaning                                                      |
|-----------------------------------------|-------------|--------------------------------------------------------------|
| `tasks-remote.<name>.sync.mode`         | user        | `incremental` (default) or `full`. `full` forces a deleteall snapshot. |
| `tasks-remote.<name>.sync.lastFetchAt`  | helper      | ISO timestamp token passed back as `since` on the next fetch. |

- **Jira** uses `updated >= "<ts>"` in the JQL filter.
- **Vikunja** uses `filter=updated > '<ts>'`.
- **MS Todo** and **Notion** currently fall back to a full fetch on
  every run; per-service incremental APIs land with their write paths.

### 7.2 Custom status / priority / field mapping

Every non-trivial tracker lets the team rename workflows and columns.
Map them back to the unified vocabulary through per-remote config:

| Key pattern                                        | Applies to        | Example                                           |
|----------------------------------------------------|-------------------|---------------------------------------------------|
| `tasks-remote.<name>.statusMap.<upstream>`         | all writable      | `statusMap.Triage = in_progress`                  |
| `tasks-remote.<name>.priorityMap.<upstream>`       | all writable      | `priorityMap.P0 = critical`                       |
| `tasks-remote.<name>.fieldMap.status`              | Notion            | `fieldMap.status = Workflow`                      |
| `tasks-remote.<name>.fieldMap.priority`            | Notion            | `fieldMap.priority = Urgency`                     |
| `tasks-remote.<name>.fieldMap.tags`                | Notion            | `fieldMap.tags = Labels`                          |
| `tasks-remote.<name>.fieldMap.description`         | Notion            | `fieldMap.description = Notes`                    |
| `tasks-remote.<name>.fieldMap.due_date`            | Notion            | `fieldMap.due_date = Deadline`                    |

Overrides apply to *both* directions — the same column name is read
on pull and written on push, so renaming a field once in git config is
enough. Status / priority lookups are case-insensitive after checking
for an exact match, so `statusMap.Triage` wins over the default for
`"triage"` too. Upstream values the map does not cover fall back to
the driver's built-in defaults (e.g. `_JIRA_STATUS_MAP`).

Neither Jira nor Vikunja exposes a native deletion feed, so tasks
removed upstream are only garbage-collected on a full fetch. Set
`sync.mode=full` periodically (or before releases) to reconcile:

```bash
git config tasks-remote.jira-work.sync.mode full
git fetch jira-work
git config tasks-remote.jira-work.sync.mode incremental
```

## 8. Service setup

### Jira (Atlassian Cloud)

1. Log in at <https://id.atlassian.com/manage-profile/security/api-tokens>.
2. Create a token labelled something like `git-remote-tasks`.
3. Save it securely; you will only see it once.
4. Configure the remote:

   ```bash
   git remote add jira-work jira://company.atlassian.net
   git config tasks-remote.jira-work.scheme    jira
   git config tasks-remote.jira-work.baseUrl   https://company.atlassian.net
   git config tasks-remote.jira-work.email     me@company.com
   git config tasks-remote.jira-work.apiToken  "$TOKEN"
   ```

### Vikunja (self-hosted)

1. In the Vikunja UI: **Settings → API Tokens → New token** with `tasks.read`
   and `tasks.write` scopes.
2. Copy the token and register the remote:

   ```bash
   git remote add vikunja vikunja://localhost:3456
   git config tasks-remote.vikunja.scheme    vikunja
   git config tasks-remote.vikunja.baseUrl   http://localhost:3456
   git config tasks-remote.vikunja.apiToken  "$TOKEN"
   ```

### Microsoft To Do

1. Register an app in the Azure portal under **App registrations** with
   redirect URI `http://localhost:1234`.
2. Grant `Tasks.ReadWrite` delegated permission.
3. Copy the Application (client) ID and pick a tenant
   (`consumers` for personal Microsoft accounts).
4. Configure the remote:

   ```bash
   git remote add todo msftodo://consumers
   git config tasks-remote.todo.scheme    msftodo
   git config tasks-remote.todo.tenantId  consumers
   git config tasks-remote.todo.clientId  "$CLIENT_ID"
   ```

5. Install `msal` if you want device-code auth:

   ```bash
   pip install msal
   ```

### Notion

1. Go to <https://www.notion.so/my-integrations>, create a new internal
   integration, copy its Internal Integration Token.
2. Share the target database with the integration (via the database's
   share menu).
3. Find the database ID in the URL (the 32-hex segment).
4. Configure the remote:

   ```bash
   git remote add notion-inbox notion://inbox
   git config tasks-remote.notion-inbox.scheme       notion
   git config tasks-remote.notion-inbox.databaseId   "$DB_ID"
   git config tasks-remote.notion-inbox.token        "$INTEGRATION_TOKEN"
   ```

Notion push is supported: `git push` creates pages via
`POST /v1/pages`, updates existing pages via `PATCH /v1/pages/{id}`, and
soft-deletes by archiving (`PATCH archived: true`). The title column's
name is auto-discovered via the database schema; `Status`, `Priority`,
`Tags`, `Due`, and `Description` are the expected property names unless
overridden through `fieldMap.*` config keys (see §7.2 mapping).

## 9. How it works

```
        ┌───────────┐  stdin  ┌──────────────────────┐  HTTPS  ┌────────┐
  git → │ git-remote-<scheme> │ ──────────────────► │ service │
        │  (this script)       │ ◄──────────────────── API     │        │
        │                      │  REST responses     │        │
        └──────────────────────┘                     └────────┘
                ▲
                │  stdout (fast-import stream)
                ▼
           git repository
```

### Import (git fetch)

1. git spawns the helper and writes `capabilities` / `list` / `import <ref>`.
2. Helper calls `Driver.fetch_all()` (paginated HTTPS to the service).
3. Tasks are sorted by id for deterministic output, then serialized with
   the configured format.
4. Helper writes a fast-import stream to stdout:
   ```
   blob
   mark :1
   data <N>
   <serialized task>
   ...
   commit refs/heads/main
   mark :<N>
   committer git-remote-tasks <tasks@local> <ts> +0000
   data <M>
   tasks: import <remote> (<count> tasks) [<timestamp>]
   deleteall
   M 100644 :1 tasks/<id>.<ext>
   ...
   done
   ```
5. `deleteall` makes every import a full snapshot — tasks removed upstream
   disappear locally.

### Export (git push)

1. git writes a fast-export stream to the helper's stdin.
2. Helper reads `blob` / `commit` / `M` / `D` directives, ignoring anything
   outside `tasks/`.
3. For each `M`, the blob is deserialized back to a unified task dict and
   handed to `Driver.upsert()`.
4. For each `D`, `Driver.delete()` is called with the extracted task id.
5. Helper responds `ok <ref>` once the batch is complete.

## 10. Git workflow

| Git command                       | Effect                                         |
|-----------------------------------|------------------------------------------------|
| `git fetch <remote>`              | Pulls remote task snapshot into `<remote>/main`. |
| `git diff main <remote>/main`     | Shows what changed upstream since last sync.   |
| `git merge <remote>/main`         | Materializes remote tasks into working tree.   |
| `git log -- tasks/`               | Audit trail of every sync + manual edit.       |
| `git push <remote> main`          | Upserts edited tasks; deletes removed files.   |
| `git tag release/2025-W16`        | Snapshots the task state at a moment in time.  |

Because every sync is a real commit, `git bisect` works on the task history —
useful for answering "when did that ticket's priority change?".

## 11. Org-mode tips

- Open `tasks/foo.org` in Emacs with `org-mode` enabled for headline-aware
  diffs (`M-x diff` in an Org buffer).
- Neovim: `nvim-orgmode/orgmode` gives folding and TODO cycling.
- VS Code: `vscode-org-mode` renders drawers and tags cleanly; combine with
  the built-in git panel.
- Org diffs show up in `git diff` as plain text — the headline change is
  usually the first non-context line, which scans well.
- YAML diffs are one property per line; Org diffs group all metadata under
  a single drawer. On large batches, YAML tends to produce smaller diffs.

## 12. Troubleshooting

**`fatal: Unable to find remote helper for 'jira'`**
The symlink isn't on `PATH`. Run:
```bash
python git_remote_tasks.py install --bin-dir ~/.local/bin
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

**`git-remote-tasks: no config for remote 'xxx'`**
The `tasks-remote.<name>.*` config keys are not set. Use
`python git_remote_tasks.py check <name>` to see what's missing.

**`NotImplementedError: Notion is pull-only`**
You tried to `git push` to a Notion remote. Edit in Notion, fetch to sync.

**Import produces a massive diff on first fetch**
Expected — the initial import replaces nothing with every current task.
Subsequent fetches will only show real changes.

**Roundtrip YAML/Org changes whitespace**
The serializers are roundtrip-stable by design. If you see drift, either the
file was hand-edited in a way the parser canonicalizes (normal) or a bug —
please file an issue with a minimal repro.

**`git push` succeeds but nothing changes upstream**
The driver stubs currently raise `NotImplementedError` for the live API
write paths; see `git-remote-tasks: upsert not implemented: …` on stderr.
Wire up real endpoints or tail the log to confirm the helper is reached.
