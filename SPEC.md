Implement a Python-based git remote helper called `git-remote-tasks` that enables
bidirectional sync between a standard git repository and external task management
services (Jira, Vikunja, MS Todo, Notion) by translating between git's fast-import/
fast-export wire protocol and each service's REST API.

This is a focused, self-contained implementation. Read every section carefully before
writing a single line of code.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## DELIVERABLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. `git_remote_tasks.py`   — single-file main implementation (~800-1200 lines)
2. `test_git_remote_tasks.py` — comprehensive test suite, 100% coverage target
3. `tasks-init`            — bash helper script for repo initialization
4. `requirements.txt`      — only if external deps are strictly necessary
5. `README.md`             — complete usage and internals documentation

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ARCHITECTURE OVERVIEW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Git remote helpers are executables that git spawns when it encounters a remote URL
with an unrecognized scheme. Git communicates via stdin/stdout using a line-based
text protocol. The helper translates between git's object model and the remote API.

`git-remote-tasks` is a SINGLE executable handling ALL service types. The scheme
in the remote URL determines which service driver is invoked. The script is symlinked
under multiple names (git-remote-jira, git-remote-vikunja, etc.) but it is one file.

```
git remote add jira-work    jira://company.atlassian.net
git remote add vikunja      vikunja://localhost:3456
git remote add msftodo      msftodo://consumers
git remote add notion-inbox notion://abc123

git fetch --all              # each remote helper is invoked transparently
git push vikunja main        # translates git objects → Vikunja API calls
```

The script detects its active scheme from argv[0] basename:
`git-remote-jira`    → scheme = "jira"
`git-remote-vikunja` → scheme = "vikunja"
etc.

If invoked directly as `git_remote_tasks.py`, it reads the scheme from the URL
argument (argv[2]) as fallback.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## FILE FORMAT SUPPORT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Two formats are supported. The format is chosen ONCE at repo init time and stored
in `.git/config` under `[tasks]`. It applies uniformly to all remotes in the repo.
Mixing formats within one repo is not supported.

### Format A: YAML

File extension: `.yaml`
One file per task. Field names are exactly the unified schema fields (see below).
Dates always quoted strings. Tags as YAML sequences.

Example: `tasks/jira-PROJ-123.yaml`
```yaml
id: jira-PROJ-123
source: jira
title: Fix authentication timeout on mobile login
description: |
  Users on mobile get logged out after 5 minutes.
  Affects iOS only. Reproduced on v2.3.1 and v2.4.0.
status: in_progress
priority: high
created_date: "2024-11-03T09:15:00Z"
due_date: "2025-04-20"
updated_date: "2025-04-12T14:30:00Z"
tags:
  - mobile
  - ios
  - auth
category:
  id: PROJ
  name: Backend
  type: project
url: https://company.atlassian.net/browse/PROJ-123
```

### Format B: Org-mode

File extension: `.org`
One file per task. Status and title on the headline. Metadata in :PROPERTIES: drawer.
State transitions recorded in :LOGBOOK: drawer. Description as body text.

Status keyword mapping (bidirectional):
todo        → TODO
in_progress → IN-PROGRESS
done        → DONE
cancelled   → CANCELLED

Priority mapping (bidirectional):
critical → [#A]
high     → [#B]
medium   → [#C]
low      → [#D]
none     → (omitted)

Example: `tasks/jira-PROJ-123.org`
```org
* IN-PROGRESS [#B] Fix authentication timeout on mobile login
  :PROPERTIES:
  :ID:       jira-PROJ-123
  :SOURCE:   jira
  :CREATED:  [2024-11-03 Sun 09:15]
  :UPDATED:  [2025-04-12 Sat 14:30]
  :DEADLINE: <2025-04-20 Sun>
  :CATEGORY: Backend
  :CAT_ID:   PROJ
  :CAT_TYPE: project
  :URL:      https://company.atlassian.net/browse/PROJ-123
  :TAGS:     mobile,ios,auth
  :END:
  :LOGBOOK:
  - State "IN-PROGRESS" from "TODO" [2025-04-10 Thu 11:00]
  :END:

  Users on mobile get logged out after 5 minutes.
  Affects iOS only. Reproduced on v2.3.1 and v2.4.0.
```

### Format implementation rules

- Implement a `Serializer` base class with `serialize(task: dict) -> str` and
  `deserialize(content: str) -> dict` methods.
- Implement `YAMLSerializer` and `OrgSerializer` as subclasses.
- YAML serializer: implement using ONLY Python stdlib (no PyYAML). Use a minimal
  hand-written YAML emitter sufficient for the known schema. For parsing, use a
  line-by-line state machine parser. The schema is fixed and known — do not
  implement a general-purpose YAML parser.
- Org serializer: implement using only Python stdlib. Parse headline, properties
  drawer, logbook drawer, and body text.
- Both serializers must be pure functions: serialize(deserialize(x)) == x for
  all valid inputs (roundtrip stable).
- Format is detected from file extension when deserializing existing files.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## UNIFIED TASK SCHEMA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

All tasks are represented internally as plain Python dicts with these fields:

```python
{
    "id":           str,           # "<source>-<original_id>", e.g. "jira-PROJ-123"
    "source":       str,           # "jira" | "vikunja" | "msftodo" | "notion"
    "title":        str,
    "description":  str | None,
    "status":       str,           # "todo" | "in_progress" | "done" | "cancelled"
    "priority":     str,           # "critical" | "high" | "medium" | "low" | "none"
    "created_date": str | None,    # ISO8601 UTC string, always quoted
    "due_date":     str | None,    # ISO8601 date string, always quoted
    "updated_date": str | None,    # ISO8601 UTC string, always quoted
    "tags":         list[str],     # may be empty list
    "category": {
        "id":   str | None,
        "name": str | None,
        "type": str,               # "list"|"epic"|"project"|"database"|"label"|"other"
    },
    "url":          str | None,
}
```

All fields always present in the dict. Missing optional fields are None or [].
Normalizer functions must never raise on missing/null source fields — use None.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## GIT REMOTE HELPER PROTOCOL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Implement a `ProtocolHandler` class that runs the stdin/stdout conversation loop.
Git always initiates. The helper always responds. stdout.flush() after every write
is mandatory — git blocks waiting for each response.

### Capabilities (always first exchange)

```
← capabilities
→ import
→ export
→ refspec refs/heads/*:refs/remotes/<remote-name>/*
→ *push
→ *fetch
→              ← blank line ends capability list
```

### List refs

```
← list
→ ? refs/heads/main     ← "?" means sha is unknown, git will resolve via import
→                        ← blank line ends list

← list for-push
→ (same format)
→
```

### Import (fetch: remote → local)

```
← import refs/heads/main
← import refs/heads/other   ← git may batch multiple import lines
←                            ← blank line ends import batch
→ (fast-import stream written to stdout)
→ done
```

### Export (push: local → remote)

```
← export
← (fast-export stream written to stdin by git)
← done
→ ok refs/heads/main        ← one result line per ref
→                            ← blank line ends results
```

### Fast-import stream format

The helper writes this to stdout during import. Git materializes real objects from it.

```
blob
mark :1
data <byte-length>
<raw file content — exactly byte-length bytes, no trailing newline added by format>

blob
mark :2
data <byte-length>
<raw file content>

commit refs/remotes/<remote-name>/main
mark :<N>
committer git-remote-tasks <tasks@local> <unix-timestamp> +0000
data <message-byte-length>
<commit message>
deleteall
M 100644 :1 tasks/<task-id>.<ext>
M 100644 :2 tasks/<task-id-2>.<ext>

done
```

Rules:
- `deleteall` before listing files means each import is a full snapshot. This is
  correct for task sync — tasks deleted on the remote should disappear locally.
- Commit message format: `tasks: import <remote-name> (<N> tasks) [<ISO timestamp>]`
- Use deterministic blob ordering (sorted by task id) for stable hashes.
- committer timestamp: use the most recent `updated_date` among all fetched tasks,
  or current time if no tasks have dates.

### Fast-export stream format (consumed during push)

Git writes this to the helper's stdin. The helper reads it, extracts changed files,
deserializes tasks, calls the remote API.

Parse these directives (ignore others):
```
commit <ref>
  → marks start of a commit, read associated data

data <N>
  → read exactly N bytes as raw data (commit message or blob content)

M <mode> <sha-or-mark> <path>
  → file added/modified — if path starts with "tasks/", deserialize and upsert

D <path>
  → file deleted — if path starts with "tasks/", delete task on remote

done
  → end of stream
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## SERVICE DRIVERS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Implement a `Driver` base class with:

```python
class Driver:
    def __init__(self, remote_name: str, url: str, config: dict): ...
    def fetch_all(self) -> list[dict]:  # returns unified task dicts
        raise NotImplementedError
    def upsert(self, task: dict) -> None:  # create or update
        raise NotImplementedError
    def delete(self, task_id: str) -> None:
        raise NotImplementedError
```

Implement stub drivers for all four services. Each stub must:
- Be structurally complete with correct method signatures and docstrings
- Include the full field mapping logic (source fields → unified schema fields)
- Include HTTP call sites (the `urllib.request` calls) with correct endpoints,
  auth headers, and pagination logic — BUT wrapped so they can be easily mocked
  in tests via dependency injection or a `_http_get` / `_http_post` override point
- Raise `NotImplementedError` with a clear TODO message for actual API secrets
  (since this is an experiment — we are not connecting to real services yet)

### Vikunja driver
- Base URL from config or URL argument
- Auth: `Authorization: Token <api_token>` header
- Fetch: `GET /api/v1/tasks/all` (paginated, page/per_page params)
- Upsert: `POST /api/v1/projects/{project_id}/tasks` (create) or
  `POST /api/v1/tasks/{id}` (update)
- Field mapping: title→title, description→description, priority (1-5)→unified,
  done(bool)→status, due_date→due_date, labels→tags, project→category

### Jira driver
- Base URL from config
- Auth: `Authorization: Basic base64(email:token)` header
- Fetch: `GET /rest/api/3/search?jql=ORDER BY updated DESC` (paginated, startAt/maxResults)
- Upsert: `POST /rest/api/3/issue` (create) or `PUT /rest/api/3/issue/{key}` (update)
- Field mapping: summary→title, description(Atlassian Document Format)→plain text,
  status.name→unified status, priority.name→unified priority, labels→tags,
  issuetype/epic→category

### MSTodo driver
- Auth: OAuth2 device code flow via MSAL (if msal is available) or stub
- Fetch: `GET https://graph.microsoft.com/v1.0/me/todo/lists` then
  `GET .../tasks` per list
- Upsert: PATCH for existing, POST for new
- Field mapping: title→title, body.content→description, status→unified,
  importance→priority, dueDateTime→due_date, list name→category

### Notion driver
- Import ONLY. `upsert()` and `delete()` raise `NotImplementedError("Notion is
  pull-only")`.
- Fetch: `POST https://api.notion.com/v1/databases/{id}/query` (paginated)
- Field mapping: title property→title, select properties→status/priority,
  multi_select→tags, database title→category

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## CONFIGURATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Configuration lives in the git repo's `.git/config` using standard git config
sections. Read via `git config --local` subprocess calls. Never use a separate
config file — git config IS the config.

```ini
[tasks]
    format = yaml          # or "org" — set once at init, never changes

[tasks-remote "jira-work"]
    scheme = jira
    baseUrl = https://company.atlassian.net
    email = me@company.com
    apiToken = ...

[tasks-remote "vikunja"]
    scheme = vikunja
    baseUrl = http://localhost:3456
    apiToken = ...

[tasks-remote "msftodo"]
    scheme = msftodo
    tenantId = consumers
    clientId = ...

[tasks-remote "notion-inbox"]
    scheme = notion
    databaseId = abc123
    token = ...
```

Config reader: implement `read_config(remote_name) -> dict` using
`subprocess.run(["git", "config", "--local", "--get", key])`. Never parse
`.git/config` directly.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## MANAGEMENT SUBCOMMANDS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When invoked directly (not as a git remote helper), the script exposes management
subcommands. Detect this by checking whether argv[1] is a known subcommand vs a
remote name.

```
python git_remote_tasks.py install [--bin-dir ~/.local/bin]
    Creates symlinks: git-remote-jira, git-remote-vikunja,
                      git-remote-msftodo, git-remote-notion
    Checks that bin-dir is on PATH, warns if not.

python git_remote_tasks.py uninstall [--bin-dir ~/.local/bin]
    Removes all symlinks created by install.

python git_remote_tasks.py list-schemes
    Prints all supported schemes and their driver class names.

python git_remote_tasks.py check <remote-name>
    Reads config for remote-name, validates required fields present,
    prints a summary. Does NOT make any API calls.
```

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## `tasks-init` BASH SCRIPT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

```bash
#!/usr/bin/env bash
# tasks-init — initialize a git repo for task sync

# Usage:
#   tasks-init [--format yaml|org] [--dir path]
#   tasks-init --help

# What it does:
# 1. Creates directory (or uses current), runs git init
# 2. Prompts for format if --format not given (yaml/org)
# 3. Writes [tasks] format = <choice> to .git/config via git config
# 4. Creates initial .gitignore (ignoring nothing tasks-specific)
# 5. Creates tasks/ directory with a .gitkeep
# 6. Makes an empty initial commit: "tasks: init (<format> format)"
# 7. Prints next steps: how to add remotes, how to fetch
```

Implement it completely — no TODOs, no placeholders.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## TESTING REQUIREMENTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

File: `test_git_remote_tasks.py`. Use only `unittest` and `unittest.mock`.
No pytest, no external test deps. Run with: `python -m unittest -v test_git_remote_tasks`

Target: 100% line coverage. Run coverage with:
`python -m coverage run -m unittest test_git_remote_tasks && python -m coverage report`

### Test classes to implement

**TestYAMLSerializer**
- roundtrip: serialize → deserialize → serialize produces identical output
- all fields present including nested category
- null/None fields serialized and parsed correctly
- empty tags list
- multiline description (block scalar)
- unicode in title and description
- dates always quoted strings, never parsed as date objects
- missing optional fields in input → None in output (not KeyError)

**TestOrgSerializer**
- roundtrip stability
- all status keywords map correctly both directions
- all priority levels map correctly both directions
- absent priority → "none" in unified schema
- multiline description preserved
- tags round-trip through comma-separated :TAGS: property
- unicode in all fields
- logbook entries preserved through roundtrip (stored, not discarded)
- empty/missing :PROPERTIES: drawer handled gracefully

**TestSerializerSymmetry**
- Given the same unified task dict, both serializers produce different text
  but both deserialize back to the identical dict (format-independent schema)

**TestProtocolHandler — capabilities**
- on "capabilities\n" input → correct capability lines + blank terminator
- output is flushed after each line (mock stdout, verify flush calls)

**TestProtocolHandler — list**
- "list\n" returns expected refs + blank line
- "list for-push\n" returns same format

**TestProtocolHandler — import**
- single ref import: driver.fetch_all() called, fast-import stream written
- stream contains correct blob count matching task count
- stream contains commit with "deleteall" directive
- stream ends with "done\n"
- tasks sorted by id deterministically
- blob data byte-length matches actual content length exactly
- empty task list: commit with deleteall and no M lines, still valid stream

**TestProtocolHandler — export**
- M directive for tasks/ file: driver.upsert() called with deserialized task
- D directive for tasks/ file: driver.delete() called with correct task id
- M directive for non-tasks/ file: ignored silently
- "done" ends processing
- multiple changes in one export batch all processed

**TestProtocolHandler — unknown command**
- unknown command line: logs to stderr, does not crash, continues loop

**TestDriverNormalization — Jira**
- status "In Progress" → "in_progress"
- status "Done" → "done"
- status "To Do" → "todo"
- status "Won't Do" / "Cancelled" → "cancelled"
- unknown status → "todo" (safe default)
- priority "Highest"/"Critical" → "critical"
- priority "High" → "high"
- priority mapping covers all Jira levels
- missing priority field → "none"
- missing description → None (not crash)
- ADF description (dict with "content" key) → extracted plain text
- labels list → tags list
- epic link present → category type "epic"
- no epic → category type "project" with project key

**TestDriverNormalization — Vikunja**
- priority int 1 → "critical", 2 → "high", 3 → "medium", 4 → "low", 5 → "none"
- done=true → "done", done=false → "todo"
- start_date / due_date / end_date mapping
- labels array → tags list
- project id/title → category

**TestDriverNormalization — MSTodo**
- importance "high" → "high", "normal" → "medium", "low" → "low"
- status "completed" → "done", "notStarted" → "todo", "inProgress" → "in_progress"
- list name → category name, type "list"
- reminderDateTime present → handled without crash

**TestDriverNormalization — Notion**
- title property (array of rich_text) → plain string extracted
- select property → mapped field
- multi_select property → tags list
- date property → ISO string
- checkbox property → bool → status mapping
- null property value → None without crash
- upsert() raises NotImplementedError
- delete() raises NotImplementedError

**TestManagementCommands**
- install: creates symlinks at expected paths (mock os.symlink)
- install: warns if bin-dir not on PATH
- uninstall: removes symlinks (mock os.unlink)
- uninstall: handles missing symlink gracefully (no crash)
- list-schemes: prints all four scheme names
- check: reads config keys, prints summary, makes no HTTP calls
- check: missing required config key → clear error message

**TestConfigReader**
- reads value via git config subprocess
- missing key → returns None (not exception)
- subprocess failure → returns None with stderr log

**TestFormatDetection**
- file ending in .yaml → YAMLSerializer
- file ending in .org → OrgSerializer
- unknown extension → ValueError with message

**TestInstallIntegration** (subprocess-free)
- scheme_for_name("git-remote-jira") → "jira"
- scheme_for_name("git-remote-vikunja") → "vikunja"
- scheme_for_name("git_remote_tasks.py") → reads from URL arg
- driver_for_scheme("jira") → JiraDriver instance
- driver_for_scheme("unknown") → ValueError

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## README.md REQUIRED SECTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. **What this is** — one paragraph, git remote helpers concept explained plainly
2. **Requirements** — Python version, any deps, git version
3. **Installation** — copy script, run install subcommand, verify with `git remote-tasks list-schemes`
4. **Quick start** — tasks-init, add a remote, git fetch, inspect with git log/diff
5. **Format choice** — YAML vs Org-mode comparison table, when to choose each
6. **Remote URL format** — `<scheme>://<host-or-id>` examples per service
7. **Configuration reference** — all git config keys per service with descriptions
8. **Service setup** — step-by-step credential instructions per service (same
   quality as in the todo-harvest README we defined earlier in this conversation)
9. **How it works** — git remote helper protocol explained, fast-import/export
   explained, with ASCII diagram of the data flow
10. **Git workflow** — git fetch, git diff, git merge, git push, git log, git tag
    all explained in the context of task sync
11. **Org-mode tips** — how to edit .org files, recommended Emacs/Neovim/VSCode
    plugins, how git diff looks for both formats
12. **Troubleshooting** — common errors with clear fixes

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## IMPLEMENTATION CONSTRAINTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- Python 3.10+ only. Use match/case where it improves clarity.
- stdlib only unless unavoidable. Allowed stdlib: sys, os, subprocess, json,
  urllib.request, urllib.parse, base64, hashlib, datetime, pathlib, io,
  unittest, unittest.mock, textwrap, dataclasses, abc, argparse.
- If msal is needed for MS Todo OAuth, list it in requirements.txt and guard the
  import: `try: import msal; MSAL_AVAILABLE = True / except ImportError: MSAL_AVAILABLE = False`
- No PyYAML, no org-mode libraries, no requests, no httpx. Implement serializers
  from scratch using stdlib only.
- All writes to stdout: immediately flush. Use a wrapper.
- All debug/log output: stderr only. Never mix into stdout.
- The protocol loop must never crash on malformed input — log to stderr and
  continue. Only exit on EOF (git closed stdin).
- Symlinks created by install must be absolute paths.
- Script must be executable (`chmod +x` in install subcommand).
- tasks/ directory in git tree uses forward slashes on all platforms.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## BUILD ORDER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Follow this order to avoid forward-dependency issues:

1. Unified schema definition (constants, empty task factory function)
2. YAMLSerializer — implement + test immediately, achieve roundtrip
3. OrgSerializer — implement + test immediately, achieve roundtrip
4. TestSerializerSymmetry — verify both serializers produce same schema
5. Config reader (git config subprocess wrapper)
6. Driver base class + all four driver stubs with normalization logic
7. Driver normalization tests — all mapping edge cases
8. ProtocolHandler — capabilities, list, import, export
9. Protocol tests — full coverage of all commands
10. Management subcommands (install/uninstall/list-schemes/check)
11. Management tests
12. tasks-init bash script
13. README.md
14. Final: run `python -m coverage run -m unittest test_git_remote_tasks`
    and fix any gaps until coverage report shows 100% on git_remote_tasks.py
