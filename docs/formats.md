---
title: Formats, URLs and configuration
order: 20
summary: YAML or Org-mode per repository, the remote URL shape, every tasks-remote.* key, incremental sync state, and custom status/priority/field maps.
---

> [!TLDR]
> One format per repo, chosen at init: `tasks/<source>-<id>.yaml` or `.org`. Credentials live under `[tasks-remote "<name>"]` in `.git/config`, never in the URL.

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
