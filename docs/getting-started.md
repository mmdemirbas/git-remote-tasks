---
title: Getting started
order: 10
summary: Requirements, the one-symlink-per-scheme install, and a first fetch and push against Vikunja.
---

> [!TLDR]
> Python 3.10+ and git 2.20+. `python git_remote_tasks.py install --bin-dir ~/.local/bin` drops one symlink per scheme on the PATH; then `git remote add`, three `git config` lines, `git fetch`, edit, `git push`.

## Requirements

- Python 3.10 or newer (tested on 3.14).
- git 2.20 or newer.
- Standard library only. `msal` is the one *optional* runtime extra,
  needed for the Microsoft To Do OAuth device-code flow.

## Installation

```bash
# Clone or copy the script.
git clone https://github.com/mmdemirbas/git-remote-tasks.git ~/src/git-remote-tasks
cd ~/src/git-remote-tasks

# Install the per-scheme symlinks onto PATH.
python git_remote_tasks.py install --bin-dir ~/.local/bin

# Confirm.
python git_remote_tasks.py list-schemes
which git-remote-jira tasks-init
```

`install` creates five symlinks in `--bin-dir`: `git-remote-jira`,
`git-remote-vikunja`, `git-remote-mstodo`, `git-remote-notion`, and the
convenience `tasks-init`. All point at the same Python script, which
decides what to do by reading its own invocation name.

## Quick start

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
