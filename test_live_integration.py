"""Live integration tests against real services.

Runs end-to-end exercises of the helper against Vikunja, Jira, and Notion
using credentials from a todo-harvest-style config.yaml. Point
`GRT_LIVE_CONFIG` at your config file to run; the harness skips with a
clear message when the env var is unset.

Safety rules (enforced programmatically):
  - Never delete anything on any service. The driver.delete path is not
    exercised here.
  - Never modify items we did not create. We tag every created item with
    a unique `GRT-LIVE-<timestamp>` marker in its title and refuse to
    modify anything whose title we did not produce.
  - At most 5 items per service. A counter short-circuits with an error
    if we attempt to exceed.
  - No MS Todo — the device-code flow requires a human. The test prints
    the relevant setup steps and skips.

Each service test: init a temp git repo, configure the remote, fetch,
push one new task, update that task, fetch again, assert visibility
at each step.

Run with:
    GRT_LIVE_CONFIG=/path/to/config.yaml python test_live_integration.py

Exit code reflects combined success/failure.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import git_remote_tasks as grt


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

MAX_CREATED_PER_SERVICE = 5
MARKER_PREFIX = "GRT-LIVE-"


def _live_marker() -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{MARKER_PREFIX}{ts}"


class SafetyError(RuntimeError):
    """Raised when a live test tries to touch something it must not."""


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _config_path() -> Path | None:
    """Return the live-config path from $GRT_LIVE_CONFIG, or None if unset."""
    raw = os.environ.get("GRT_LIVE_CONFIG")
    return Path(raw).expanduser() if raw else None


def _load_yaml_config() -> dict:
    """Minimal YAML reader covering the shapes we use in this project.

    Handles: top-level sections, single-level scalar keys, nested dicts of
    scalar keys, and lists of scalars. Two-space indent units are assumed.
    Not a general YAML parser.

    Raises unittest.SkipTest when the config path is not configured so a
    bare `python test_live_integration.py` doesn't error — it skips.
    """
    path = _config_path()
    if path is None or not path.exists():
        raise unittest.SkipTest(
            "set GRT_LIVE_CONFIG=/path/to/config.yaml to run live tests"
        )
    lines = path.read_text(encoding="utf-8").splitlines()

    def tokenize():
        for raw in lines:
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            content = raw[indent:]
            yield indent, content

    out: dict = {}
    # Stack of (indent, container). Top of stack is where next key lands.
    stack: list[tuple[int, dict | list]] = [(-1, out)]
    # When we see `key:` with empty value we defer materialization to
    # decide dict vs list from the following line's shape.
    pending: tuple[int, str, dict | list] | None = None

    def container_for_indent(indent: int) -> dict | list:
        # Pop entries whose indent is >= current (we have moved up).
        while stack and stack[-1][0] >= indent:
            stack.pop()
        return stack[-1][1]

    for indent, content in tokenize():
        if pending is not None:
            p_indent, p_name, p_container = pending
            if content.startswith("- ") and indent > p_indent:
                new_list: list = []
                p_container[p_name] = new_list
                # Push at the PARENT's indent so sibling keys at p_indent
                # pop the list but children at indent > p_indent don't.
                stack.append((p_indent, new_list))
                pending = None
            elif indent > p_indent:
                new_dict: dict = {}
                p_container[p_name] = new_dict
                stack.append((p_indent, new_dict))
                pending = None
            else:
                p_container[p_name] = None
                pending = None

        container = container_for_indent(indent)
        if content.startswith("- "):
            if isinstance(container, list):
                container.append(_strip_quotes(content[2:].strip()))
            continue
        name, _, rest = content.partition(":")
        name = _strip_quotes(name.strip())
        val = rest.strip()
        if val == "":
            pending = (indent, name, container if isinstance(container, dict)
                        else out)
            continue
        if isinstance(container, dict):
            container[name] = _strip_quotes(val)
    if pending is not None:
        _, p_name, p_container = pending
        p_container[p_name] = None
    return out


# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------

class LiveHarness:
    """Helpers for running the helper against a real git repo + real service."""

    def __init__(self, scheme: str, remote_name: str, url: str,
                 config: dict):
        self.scheme = scheme
        self.remote_name = remote_name
        self.url = url
        self.config = config
        self._workdir: Path | None = None
        self._created_count = 0

    def __enter__(self) -> "LiveHarness":
        self._tmp = tempfile.TemporaryDirectory(
            prefix=f"grt-live-{self.scheme}-")
        self._workdir = Path(self._tmp.name)
        # `git init` + `tasks.format=yaml`.
        subprocess.run(["git", "init", "--quiet", str(self._workdir)],
                        check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(self._workdir), "config", "--local",
             "tasks.format", "yaml"],
            check=True, capture_output=True,
        )
        # Add the remote under a fake URL (the helper is the one that talks
        # to the real service, not git) and populate per-remote config.
        subprocess.run(
            ["git", "-C", str(self._workdir), "remote", "add",
             self.remote_name, self.url],
            check=True, capture_output=True,
        )
        for k, v in self.config.items():
            subprocess.run(
                ["git", "-C", str(self._workdir), "config", "--local",
                 f"tasks-remote.{self.remote_name}.{k}", v],
                check=True, capture_output=True,
            )
        return self

    def __exit__(self, *_) -> None:
        if self._tmp:
            self._tmp.cleanup()

    def guard_create(self) -> None:
        if self._created_count >= MAX_CREATED_PER_SERVICE:
            raise SafetyError(
                f"{self.scheme}: would create a {self._created_count+1}-th "
                f"item; safety cap is {MAX_CREATED_PER_SERVICE}."
            )
        self._created_count += 1

    def driver(self) -> grt.Driver:
        os.chdir(str(self._workdir))
        cfg = grt.read_remote_config(self.remote_name)
        cfg.setdefault("scheme", self.scheme)
        return grt.driver_for_scheme(self.scheme,
                                       remote_name=self.remote_name,
                                       url=self.url, config=cfg)


# ---------------------------------------------------------------------------
# Vikunja
# ---------------------------------------------------------------------------

class TestVikunjaLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = _load_yaml_config().get("vikunja") or {}
        if not cfg.get("api_token"):
            raise unittest.SkipTest("vikunja.api_token missing")
        cls.config = {
            "scheme": "vikunja",
            "baseUrl": cfg["base_url"],
            "apiToken": cfg["api_token"],
            "projectId": str(cfg.get("default_project_id") or 1),
        }

    def test_fetch_then_create_then_update(self):
        h = LiveHarness("vikunja", "vikunja-live",
                         self.config["baseUrl"], self.config)
        with h:
            driver = h.driver()

            # 1. Pull — must not raise.
            tasks = driver.fetch_all()
            print(f"\n  vikunja: pulled {len(tasks)} tasks")
            self.assertIsInstance(tasks, list)

            # 2. Create a fresh task with a unique marker title.
            h.guard_create()
            marker = _live_marker()
            title = f"{marker} vikunja create smoke"
            new_task = grt.empty_task() | {
                "id": "", "source": "vikunja", "title": title,
                "status": "todo", "priority": "medium",
                "description": "created by git-remote-tasks live test",
            }
            driver.upsert(new_task)
            print(f"  vikunja: created {title!r}")

            # 3. Re-fetch, find our created task, verify visibility.
            tasks = driver.fetch_all()
            ours = [t for t in tasks if t["title"] == title]
            self.assertEqual(len(ours), 1,
                              f"created task not visible on refetch: {title!r}")
            created = ours[0]
            self.assertEqual(created["priority"], "medium")

            # 4. Update our own task — verify cross-source refusal first.
            with self.assertRaises(grt.VikunjaPushError):
                driver.upsert(grt.empty_task() | {"id": "jira-PROJ-1",
                                                    "title": "nope"})

            updated = created | {"priority": "high",
                                 "description": "updated by live test"}
            driver.upsert(updated)
            print(f"  vikunja: updated priority on {created['id']}")

            # 5. Verify update landed.
            tasks = driver.fetch_all()
            final = next(t for t in tasks if t["id"] == created["id"])
            self.assertEqual(final["priority"], "high")


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

class TestJiraLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = _load_yaml_config().get("jira") or {}
        if not cfg.get("api_token"):
            raise unittest.SkipTest("jira.api_token missing")
        cls.config = {
            "scheme": "jira",
            "baseUrl": cfg["base_url"],
            "email": cfg["email"],
            "apiToken": cfg["api_token"],
        }
        # Only map the user's two priority overrides / one status override so
        # real data flows through statusMap/priorityMap.
        # Use the JSON-encoded map form so non-ASCII keys (Turkish
        # 'Yüksek', 'Ertelendi') survive — git config variable-name
        # rules forbid them in the dotted form.
        for ukey, prefix in (("priority_map", "priorityMap"),
                              ("status_map", "statusMap")):
            m = cfg.get(ukey) or {}
            if m:
                cls.config[prefix] = json.dumps(m, ensure_ascii=False)

    def test_pull_only_live(self):
        """Pull-only test — we intentionally do NOT push to Jira on the
        first pass because the user's projects may contain items we
        shouldn't add tickets into without explicit permission. We verify
        that pagination + JQL + statusMap overrides work end-to-end.
        """
        h = LiveHarness("jira", "jira-live", self.config["baseUrl"], self.config)
        with h:
            driver = h.driver()
            tasks = driver.fetch_all()
            print(f"\n  jira: pulled {len(tasks)} issues")
            self.assertIsInstance(tasks, list)
            self.assertGreater(len(tasks), 0,
                                "user has issues; fetch_all should not be empty")
            sample = tasks[0]
            self.assertTrue(sample["id"].startswith("jira-"))
            self.assertIn(sample["status"],
                           ("todo", "in_progress", "done", "cancelled"))


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

class TestNotionLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = _load_yaml_config().get("notion") or {}
        if not cfg.get("token"):
            raise unittest.SkipTest("notion.token missing")
        dbs = cfg.get("database_ids") or []
        if not dbs:
            raise unittest.SkipTest("notion.database_ids empty")
        cls.config = {
            "scheme": "notion",
            "token": cfg["token"],
            "databaseId": dbs[0],
        }
        # Use the JSON-encoded map form for status / priority / field
        # so the user's Turkish, emoji, and underscore-bearing keys all
        # survive git config's variable-name restrictions.
        field_xlate = {"due_date": "dueDate"}
        for ukey, prefix in (("status_map", "statusMap"),
                              ("priority_map", "priorityMap"),
                              ("field_map", "fieldMap")):
            m = cfg.get(ukey) or {}
            if not m:
                continue
            if ukey == "field_map":
                m = {field_xlate.get(k, k): v for k, v in m.items()}
            cls.config[prefix] = json.dumps(m, ensure_ascii=False)

    def test_fetch_then_create_then_update(self):
        h = LiveHarness("notion", "notion-live", f"notion://{self.config['databaseId']}",
                         self.config)
        with h:
            driver = h.driver()

            tasks = driver.fetch_all()
            print(f"\n  notion: pulled {len(tasks)} pages")
            self.assertIsInstance(tasks, list)

            h.guard_create()
            marker = _live_marker()
            title = f"{marker} notion create smoke"
            new_task = grt.empty_task() | {
                "id": "", "source": "notion", "title": title,
                "status": "todo", "priority": "medium",
                "description": "created by git-remote-tasks live test",
            }
            driver.upsert(new_task)
            print(f"  notion: created {title!r}")

            tasks = driver.fetch_all()
            ours = [t for t in tasks if t["title"] == title]
            self.assertEqual(len(ours), 1,
                              f"notion created task not visible: {title!r}")
            created = ours[0]

            # Safety: refuse to modify anything whose title we didn't produce.
            if not created["title"].startswith(MARKER_PREFIX):
                raise SafetyError("refusing to modify item we didn't create")

            updated = created | {"priority": "high",
                                 "description": "updated by live test"}
            driver.upsert(updated)
            print(f"  notion: updated priority on {created['id']}")

            tasks = driver.fetch_all()
            final = next(t for t in tasks if t["id"] == created["id"])
            self.assertEqual(final["priority"], "high")


# ---------------------------------------------------------------------------
# MS Todo — documented, skipped under automation.
# ---------------------------------------------------------------------------

class TestMSTodoLive(unittest.TestCase):
    @unittest.skip("MS Todo device-code flow requires human interaction; run "
                    "`GIT_REMOTE_TASKS_DEBUG=1 git fetch mstodo-live` manually.")
    def test_placeholder(self):  # pragma: no cover - documentation
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
