#!/usr/bin/env python3
"""git-remote-tasks — bidirectional sync between git and task services.

Single-file git remote helper that translates between git's fast-import/
fast-export wire protocol and REST APIs of Jira, Vikunja, MS Todo, and Notion.

Invoked by git when a remote URL uses a scheme this helper registers. The
active scheme is detected from argv[0] basename (e.g. git-remote-jira →
"jira"); when run directly as git_remote_tasks.py, the scheme is taken
from the URL argument.

Supported file formats: YAML and Org-mode. Format is chosen once per repo
in .git/config under [tasks] format=... and applies to all remotes.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

try:  # MS Todo uses MSAL if available; otherwise device flow stubs out.
    import msal  # type: ignore
    MSAL_AVAILABLE = True
except ImportError:  # pragma: no cover - environment dependent
    MSAL_AVAILABLE = False


# ============================================================================
# Unified schema
# ============================================================================

STATUSES = ("todo", "in_progress", "done", "cancelled")
PRIORITIES = ("critical", "high", "medium", "low", "none")
CATEGORY_TYPES = ("list", "epic", "project", "database", "label", "other")

TASK_FIELDS = (
    "id", "source", "title", "description", "status", "priority",
    "created_date", "due_date", "updated_date", "tags", "category", "url",
)


def empty_task() -> dict:
    """Build a task dict with all schema fields at their neutral defaults."""
    return {
        "id": "",
        "source": "",
        "title": "",
        "description": None,
        "status": "todo",
        "priority": "none",
        "created_date": None,
        "due_date": None,
        "updated_date": None,
        "tags": [],
        "category": {"id": None, "name": None, "type": "other"},
        "url": None,
    }


def normalize_task(task: dict) -> dict:
    """Fill in any missing schema fields with neutral defaults. Non-mutating."""
    out = empty_task()
    for k, v in task.items():
        if k == "category" and isinstance(v, dict):
            cat = out["category"]
            for ck in ("id", "name", "type"):
                if ck in v:
                    cat[ck] = v[ck]
            if cat["type"] is None:
                cat["type"] = "other"
        elif k in out or k == "logbook":
            out[k] = v
    return out


# ============================================================================
# Serializers
# ============================================================================

class Serializer(ABC):
    EXTENSION: str = ""

    @abstractmethod
    def serialize(self, task: dict) -> str: ...

    @abstractmethod
    def deserialize(self, content: str) -> dict: ...


# ---------- YAML ------------------------------------------------------------

_YAML_RESERVED = {"null", "true", "false", "yes", "no", "~", "on", "off"}


def _yaml_needs_quoting(s: str) -> bool:
    if s == "":
        return True
    if s.lower() in _YAML_RESERVED:
        return True
    if s != s.strip():
        return True
    if s[0] in "!&*?|>'\"%@`#":
        return True
    if s.startswith("- "):
        return True
    if ": " in s or " #" in s:
        return True
    if s[0] == "-" and len(s) > 1 and s[1].isspace():
        return True
    # Digits as key values handled by always-quote option; allow here.
    return False


def _yaml_quote(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_scalar(value, always_quote: bool = False) -> str:
    if value is None:
        return "null"
    s = str(value)
    if always_quote:
        return _yaml_quote(s)
    if "\n" in s:
        # Caller handles block scalars directly; this is the fallback
        # double-quoted form where \n is a YAML escape for newline.
        escaped = (s.replace("\\", "\\\\")
                    .replace('"', '\\"')
                    .replace("\n", "\\n"))
        return f'"{escaped}"'
    if _yaml_needs_quoting(s):
        return _yaml_quote(s)
    return s


def _yaml_emit_string(lines: list[str], key: str, value, always_quote: bool = False,
                     indent: int = 0) -> None:
    pad = " " * indent
    if value is None:
        lines.append(f"{pad}{key}: null")
        return
    s = str(value)
    if "\n" in s and not always_quote:
        lines.append(f"{pad}{key}: |")
        for ln in s.split("\n"):
            lines.append(f"{pad}  {ln}" if ln else f"{pad}  ")
        return
    lines.append(f"{pad}{key}: {_yaml_scalar(s, always_quote=always_quote)}")


def _parse_yaml_inline_scalar(raw: str):
    raw = raw.strip()
    if raw == "" or raw == "null" or raw == "~":
        return None
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        body = raw[1:-1]
        return body.replace("\\\\", "\x00").replace('\\"', '"').replace("\\n", "\n").replace("\x00", "\\")
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


class YAMLSerializer(Serializer):
    EXTENSION = "yaml"

    def serialize(self, task: dict) -> str:
        t = normalize_task(task)
        lines: list[str] = []
        _yaml_emit_string(lines, "id", t["id"])
        _yaml_emit_string(lines, "source", t["source"])
        _yaml_emit_string(lines, "title", t["title"])
        _yaml_emit_string(lines, "description", t["description"])
        _yaml_emit_string(lines, "status", t["status"])
        _yaml_emit_string(lines, "priority", t["priority"])
        _yaml_emit_string(lines, "created_date", t["created_date"], always_quote=True)
        _yaml_emit_string(lines, "due_date", t["due_date"], always_quote=True)
        _yaml_emit_string(lines, "updated_date", t["updated_date"], always_quote=True)
        tags = t["tags"] or []
        if not tags:
            lines.append("tags: []")
        else:
            lines.append("tags:")
            for tag in tags:
                lines.append(f"  - {_yaml_scalar(tag)}")
        lines.append("category:")
        cat = t["category"]
        _yaml_emit_string(lines, "id", cat.get("id"), indent=2)
        _yaml_emit_string(lines, "name", cat.get("name"), indent=2)
        _yaml_emit_string(lines, "type", cat.get("type") or "other", indent=2)
        _yaml_emit_string(lines, "url", t["url"])
        logbook = t.get("logbook")
        if logbook:
            lines.append("logbook:")
            for entry in logbook:
                lines.append(f"  - {_yaml_scalar(entry)}")
        return "\n".join(lines) + "\n"

    def deserialize(self, content: str) -> dict:
        parsed = self._parse(content)
        t = empty_task()
        for k in ("id", "source", "title", "description", "status", "priority",
                 "created_date", "due_date", "updated_date", "url"):
            if k in parsed:
                t[k] = parsed[k]
        if "tags" in parsed:
            t["tags"] = parsed["tags"] or []
        if "category" in parsed and isinstance(parsed["category"], dict):
            cat = parsed["category"]
            t["category"] = {
                "id": cat.get("id"),
                "name": cat.get("name"),
                "type": cat.get("type") or "other",
            }
        if "logbook" in parsed and parsed["logbook"]:
            t["logbook"] = parsed["logbook"]
        # Ensure title/id/source/status/priority are strings when non-None.
        for k in ("id", "source", "title", "status", "priority"):
            if t[k] is None:
                t[k] = "" if k in ("id", "source", "title") else ("todo" if k == "status" else "none")
        return t

    def _parse(self, content: str) -> dict:
        lines = content.splitlines()
        out: dict = {}
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip() or line.lstrip().startswith("#"):
                i += 1
                continue
            if line.startswith((" ", "\t")):
                i += 1
                continue
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
            if not m:
                i += 1
                continue
            key, rest = m.group(1), m.group(2).rstrip()
            if rest == "|" or rest == "|-" or rest == "|+":
                block, i = self._read_block_scalar(lines, i + 1)
                out[key] = block
                continue
            if rest == "[]":
                out[key] = []
                i += 1
                continue
            if rest == "":
                # Nested mapping or sequence follows.
                value, i = self._read_nested(lines, i + 1)
                out[key] = value
                continue
            out[key] = _parse_yaml_inline_scalar(rest)
            i += 1
        return out

    @staticmethod
    def _read_block_scalar(lines: list[str], start: int) -> tuple[str, int]:
        block: list[str] = []
        indent = None
        i = start
        while i < len(lines):
            ln = lines[i]
            if ln == "":
                block.append("")
                i += 1
                continue
            if not ln.startswith((" ", "\t")):
                break
            stripped = ln.lstrip(" ")
            cur_indent = len(ln) - len(stripped)
            if indent is None:
                indent = cur_indent
            block.append(ln[indent:] if len(ln) >= indent else stripped)
            i += 1
        while block and block[-1] == "":
            block.pop()
        return "\n".join(block), i

    @staticmethod
    def _read_nested(lines: list[str], start: int):
        i = start
        # Skip blank lines.
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        if i >= len(lines) or not lines[i].startswith((" ", "\t")):
            return None, i
        first = lines[i]
        stripped = first.lstrip(" ")
        if stripped.startswith("- "):
            items = []
            while i < len(lines):
                ln = lines[i]
                if ln.strip() == "":
                    i += 1
                    continue
                if not ln.startswith((" ", "\t")):
                    break
                s = ln.lstrip(" ")
                if not s.startswith("- "):
                    break
                items.append(_parse_yaml_inline_scalar(s[2:]))
                i += 1
            return items, i
        # Nested mapping.
        sub: dict = {}
        base_indent = len(first) - len(first.lstrip(" "))
        while i < len(lines):
            ln = lines[i]
            if ln.strip() == "":
                i += 1
                continue
            if not ln.startswith((" ", "\t")):
                break
            cur_indent = len(ln) - len(ln.lstrip(" "))
            if cur_indent < base_indent:
                break
            m = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", ln)
            if m:
                sub[m.group(1)] = _parse_yaml_inline_scalar(m.group(2))
            i += 1
        return sub, i


# ---------- Org-mode --------------------------------------------------------

STATUS_TO_ORG = {
    "todo": "TODO",
    "in_progress": "IN-PROGRESS",
    "done": "DONE",
    "cancelled": "CANCELLED",
}
ORG_TO_STATUS = {v: k for k, v in STATUS_TO_ORG.items()}
ORG_STATUS_KEYWORDS = set(ORG_TO_STATUS.keys())

PRIORITY_TO_ORG = {"critical": "A", "high": "B", "medium": "C", "low": "D"}
ORG_TO_PRIORITY = {v: k for k, v in PRIORITY_TO_ORG.items()}


def _iso_to_org_timestamp(iso: str, active: bool = False) -> str:
    """Convert an ISO8601 string to an org timestamp; returns [...] or <...>."""
    s = iso.replace("Z", "+00:00")
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            body = dt.strftime("%Y-%m-%d %a %H:%M")
        else:
            dt = datetime.fromisoformat(s)
            body = dt.strftime("%Y-%m-%d %a")
    except ValueError:
        body = iso
    return f"<{body}>" if active else f"[{body}]"


def _org_timestamp_to_iso(token: str) -> str:
    """Parse [YYYY-MM-DD Day HH:MM] / <YYYY-MM-DD Day> → ISO8601."""
    token = token.strip()
    if token and token[0] in "[<" and token[-1] in "]>":
        inner = token[1:-1]
    else:
        inner = token
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:\s+[A-Za-z]+)?(?:\s+(\d{1,2}:\d{2}))?\s*$", inner)
    if not m:
        return inner
    date_part = m.group(1)
    time_part = m.group(2)
    if time_part:
        hh, mm = time_part.split(":")
        return f"{date_part}T{int(hh):02d}:{mm}:00Z"
    return date_part


class OrgSerializer(Serializer):
    EXTENSION = "org"

    def serialize(self, task: dict) -> str:
        t = normalize_task(task)
        status_kw = STATUS_TO_ORG.get(t["status"] or "todo", "TODO")
        pri = PRIORITY_TO_ORG.get(t["priority"] or "none")
        title = t["title"] or ""
        headline_parts = ["*", status_kw]
        if pri:
            headline_parts.append(f"[#{pri}]")
        headline_parts.append(title)
        out = [" ".join(headline_parts)]
        props: list[tuple[str, str]] = []
        if t["id"]:
            props.append(("ID", t["id"]))
        if t["source"]:
            props.append(("SOURCE", t["source"]))
        if t["created_date"]:
            props.append(("CREATED", _iso_to_org_timestamp(t["created_date"])))
        if t["updated_date"]:
            props.append(("UPDATED", _iso_to_org_timestamp(t["updated_date"])))
        if t["due_date"]:
            props.append(("DEADLINE", _iso_to_org_timestamp(t["due_date"], active=True)))
        cat = t["category"] or {}
        if cat.get("name"):
            props.append(("CATEGORY", cat["name"]))
        if cat.get("id"):
            props.append(("CAT_ID", cat["id"]))
        if cat.get("type"):
            props.append(("CAT_TYPE", cat["type"]))
        if t["url"]:
            props.append(("URL", t["url"]))
        if t["tags"]:
            props.append(("TAGS", ",".join(t["tags"])))
        out.append("  :PROPERTIES:")
        for k, v in props:
            # Align property keys for readability (Emacs convention).
            out.append(f"  :{k}: {v}")
        out.append("  :END:")
        logbook = t.get("logbook") or []
        if logbook:
            out.append("  :LOGBOOK:")
            for entry in logbook:
                out.append(f"  {entry}")
            out.append("  :END:")
        if t["description"]:
            out.append("")
            for line in t["description"].split("\n"):
                out.append(f"  {line}" if line else "")
        return "\n".join(out) + "\n"

    def deserialize(self, content: str) -> dict:
        lines = content.splitlines()
        t = empty_task()
        i = 0
        # Locate first headline.
        while i < len(lines) and not lines[i].lstrip().startswith("*"):
            i += 1
        if i >= len(lines):
            return t
        self._parse_headline(lines[i], t)
        i += 1
        # Parse drawers and body.
        body_lines: list[str] = []
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped == ":PROPERTIES:":
                i = self._parse_properties(lines, i + 1, t)
            elif stripped == ":LOGBOOK:":
                i = self._parse_logbook(lines, i + 1, t)
            else:
                body_lines.append(lines[i])
                i += 1
        body = self._dedent_body(body_lines).strip("\n")
        t["description"] = body if body else None
        return t

    @staticmethod
    def _parse_headline(line: str, t: dict) -> None:
        stripped = line.lstrip()
        if not stripped.startswith("*"):
            return
        rest = stripped.lstrip("*").strip()
        parts = rest.split(" ", 1)
        if parts and parts[0] in ORG_STATUS_KEYWORDS:
            t["status"] = ORG_TO_STATUS[parts[0]]
            rest = parts[1] if len(parts) > 1 else ""
        m = re.match(r"^\[#([A-D])\]\s*(.*)$", rest)
        if m:
            t["priority"] = ORG_TO_PRIORITY[m.group(1)]
            rest = m.group(2)
        t["title"] = rest.strip()

    @staticmethod
    def _parse_properties(lines: list[str], start: int, t: dict) -> int:
        i = start
        cat = dict(t["category"])
        while i < len(lines) and lines[i].strip() != ":END:":
            m = re.match(r"^\s*:([A-Z_]+):\s*(.*)$", lines[i])
            if m:
                key, value = m.group(1), m.group(2).strip()
                if key == "ID":
                    t["id"] = value
                elif key == "SOURCE":
                    t["source"] = value
                elif key == "CREATED":
                    t["created_date"] = _org_timestamp_to_iso(value)
                elif key == "UPDATED":
                    t["updated_date"] = _org_timestamp_to_iso(value)
                elif key == "DEADLINE":
                    t["due_date"] = _org_timestamp_to_iso(value)
                elif key == "CATEGORY":
                    cat["name"] = value
                elif key == "CAT_ID":
                    cat["id"] = value
                elif key == "CAT_TYPE":
                    cat["type"] = value or "other"
                elif key == "URL":
                    t["url"] = value
                elif key == "TAGS":
                    t["tags"] = [x.strip() for x in value.split(",") if x.strip()]
            i += 1
        t["category"] = cat
        return i + 1 if i < len(lines) else i

    @staticmethod
    def _parse_logbook(lines: list[str], start: int, t: dict) -> int:
        i = start
        entries = []
        while i < len(lines) and lines[i].strip() != ":END:":
            if lines[i].strip():
                entries.append(lines[i].strip())
            i += 1
        if entries:
            t["logbook"] = entries
        return i + 1 if i < len(lines) else i

    @staticmethod
    def _dedent_body(body_lines: list[str]) -> str:
        # Find common leading-space count across non-empty lines; strip it.
        non_empty = [ln for ln in body_lines if ln.strip()]
        if not non_empty:
            return ""
        indent = min(len(ln) - len(ln.lstrip(" ")) for ln in non_empty)
        dedented = [ln[indent:] if len(ln) >= indent else ln for ln in body_lines]
        return "\n".join(dedented)


def serializer_for_format(fmt: str) -> Serializer:
    if fmt == "yaml":
        return YAMLSerializer()
    if fmt == "org":
        return OrgSerializer()
    raise ValueError(f"unknown tasks format: {fmt!r} (expected 'yaml' or 'org')")


def serializer_for_extension(path: str) -> Serializer:
    p = str(path).lower()
    if p.endswith(".yaml") or p.endswith(".yml"):
        return YAMLSerializer()
    if p.endswith(".org"):
        return OrgSerializer()
    raise ValueError(f"unknown task file extension: {path!r}")


# ============================================================================
# Config reader
# ============================================================================

def _run_git_config(args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "config", "--local", *args],
            capture_output=True, text=True, check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        print(f"git-remote-tasks: git config failed: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        if proc.stderr.strip():
            print(f"git-remote-tasks: git config: {proc.stderr.strip()}", file=sys.stderr)
        return None
    return proc.stdout.rstrip("\n")


def read_config_value(key: str) -> str | None:
    """Return the git-config value for key, or None if unset / on error."""
    return _run_git_config(["--get", key])


def read_format() -> str:
    """Return the configured tasks format (yaml/org); defaults to yaml."""
    val = read_config_value("tasks.format")
    return val if val in ("yaml", "org") else "yaml"


REMOTE_REQUIRED_KEYS = {
    "jira":     ("baseUrl", "email", "apiToken"),
    "vikunja":  ("baseUrl", "apiToken"),
    "msftodo":  ("tenantId", "clientId"),
    "notion":   ("databaseId", "token"),
}


def read_remote_config(remote_name: str) -> dict:
    """Read all `tasks-remote.<name>.*` keys into a flat dict."""
    prefix = f"tasks-remote.{remote_name}."
    out = _run_git_config(["--get-regexp", re.escape(prefix) + ".*"])
    config: dict[str, str] = {}
    if not out:
        return config
    for line in out.splitlines():
        if " " not in line:
            continue
        key, _, value = line.partition(" ")
        if key.startswith(prefix):
            config[key[len(prefix):]] = value
    return config


# ============================================================================
# Driver base + service drivers
# ============================================================================

class Driver(ABC):
    """Base class for service drivers. Subclasses override HTTP entry points."""

    SCHEME = ""

    def __init__(self, remote_name: str, url: str, config: dict):
        self.remote_name = remote_name
        self.url = url
        self.config = config or {}

    # ---- HTTP seams (override or mock in tests) ----
    def _http_get(self, url: str, headers: dict | None = None) -> dict:
        return self._http_request("GET", url, headers=headers, body=None)

    def _http_post(self, url: str, body: dict | None = None,
                   headers: dict | None = None) -> dict:
        return self._http_request("POST", url, headers=headers, body=body)

    def _http_request(self, method: str, url: str, headers: dict | None = None,
                      body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        if body is not None and "Content-Type" not in (headers or {}):
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as resp:  # pragma: no cover - network
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}

    # ---- Public API ----
    @abstractmethod
    def fetch_all(self) -> list[dict]:
        """Fetch all tasks from the remote service as unified task dicts."""
        raise NotImplementedError

    def upsert(self, task: dict) -> None:
        """Create or update the task on the remote service."""
        raise NotImplementedError(
            f"{self.__class__.__name__}.upsert not connected to live API yet"
        )

    def delete(self, task_id: str) -> None:
        """Delete the task (given a <source>-<native-id> key) on the remote."""
        raise NotImplementedError(
            f"{self.__class__.__name__}.delete not connected to live API yet"
        )


# ---------- Jira ------------------------------------------------------------

_JIRA_STATUS_MAP = {
    "to do": "todo", "todo": "todo", "open": "todo", "backlog": "todo",
    "in progress": "in_progress", "in review": "in_progress", "in-progress": "in_progress",
    "done": "done", "closed": "done", "resolved": "done", "completed": "done",
    "cancelled": "cancelled", "canceled": "cancelled", "won't do": "cancelled",
    "wont do": "cancelled", "won't fix": "cancelled",
}
_JIRA_PRIORITY_MAP = {
    "highest": "critical", "critical": "critical", "blocker": "critical",
    "high": "high", "major": "high",
    "medium": "medium", "normal": "medium",
    "low": "low", "minor": "low",
    "lowest": "low", "trivial": "low",
}


def _jira_extract_adf_text(adf) -> str | None:
    """Flatten Atlassian Document Format to plain text."""
    if adf is None:
        return None
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return None
    buf: list[str] = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
            return
        if not isinstance(node, dict):
            return
        ntype = node.get("type")
        if ntype == "text":
            buf.append(node.get("text", ""))
        if "content" in node:
            walk(node["content"])
        if ntype in ("paragraph", "heading", "bulletList", "orderedList", "listItem"):
            buf.append("\n")

    walk(adf)
    text = "".join(buf).strip()
    return text or None


class JiraDriver(Driver):
    SCHEME = "jira"

    def _auth_header(self) -> dict:
        email = self.config.get("email", "")
        token = self.config.get("apiToken", "")
        raw = f"{email}:{token}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def normalize(self, issue: dict) -> dict:
        t = empty_task()
        fields = issue.get("fields") or {}
        key = issue.get("key") or ""
        t["id"] = f"jira-{key}" if key else ""
        t["source"] = "jira"
        t["title"] = fields.get("summary") or ""
        desc = fields.get("description")
        t["description"] = _jira_extract_adf_text(desc) if isinstance(desc, dict) else desc
        status_name = ((fields.get("status") or {}).get("name") or "").strip().lower()
        t["status"] = _JIRA_STATUS_MAP.get(status_name, "todo")
        pri = (fields.get("priority") or {}).get("name") if fields.get("priority") else None
        if pri:
            t["priority"] = _JIRA_PRIORITY_MAP.get(pri.strip().lower(), "medium")
        else:
            t["priority"] = "none"
        t["created_date"] = fields.get("created")
        t["updated_date"] = fields.get("updated")
        t["due_date"] = fields.get("duedate")
        t["tags"] = list(fields.get("labels") or [])
        epic = fields.get("customfield_10014") or fields.get("epic")
        project = fields.get("project") or {}
        if epic:
            if isinstance(epic, dict):
                epic_id = epic.get("key") or epic.get("id")
                epic_name = epic.get("name") or epic.get("summary")
            elif isinstance(epic, str):
                epic_id = epic
                epic_name = epic
            else:
                epic_id = None
                epic_name = None
            t["category"] = {"id": epic_id, "name": epic_name, "type": "epic"}
        else:
            t["category"] = {
                "id": project.get("key"),
                "name": project.get("name"),
                "type": "project",
            }
        base = self.config.get("baseUrl") or self.url or ""
        if key and base:
            t["url"] = f"{base.rstrip('/')}/browse/{key}"
        return t

    def fetch_all(self) -> list[dict]:
        base = self.config.get("baseUrl") or self.url
        if not base:
            raise NotImplementedError("Jira baseUrl is not configured")
        headers = self._auth_header()
        start_at = 0
        max_results = 50
        issues: list[dict] = []
        while True:
            url = (
                f"{base.rstrip('/')}/rest/api/3/search"
                f"?jql={urllib.parse.quote('ORDER BY updated DESC')}"
                f"&startAt={start_at}&maxResults={max_results}"
            )
            data = self._http_get(url, headers=headers)
            page = data.get("issues") or []
            issues.extend(page)
            total = data.get("total", 0)
            start_at += len(page)
            if not page or start_at >= total:
                break
        return [self.normalize(x) for x in issues]


# ---------- Vikunja ---------------------------------------------------------

_VIKUNJA_PRIORITY_MAP = {1: "critical", 2: "high", 3: "medium", 4: "low", 5: "none"}


class VikunjaDriver(Driver):
    SCHEME = "vikunja"

    def _auth_header(self) -> dict:
        token = self.config.get("apiToken", "")
        return {"Authorization": f"Token {token}"}

    def normalize(self, task: dict) -> dict:
        t = empty_task()
        tid = task.get("id")
        t["id"] = f"vikunja-{tid}" if tid is not None else ""
        t["source"] = "vikunja"
        t["title"] = task.get("title") or ""
        t["description"] = task.get("description") or None
        t["status"] = "done" if task.get("done") else "todo"
        pri = task.get("priority")
        t["priority"] = _VIKUNJA_PRIORITY_MAP.get(pri, "none") if pri is not None else "none"
        t["created_date"] = task.get("created")
        t["updated_date"] = task.get("updated")
        t["due_date"] = task.get("due_date") or task.get("end_date") or task.get("start_date")
        t["tags"] = [lbl.get("title", "") for lbl in (task.get("labels") or []) if lbl.get("title")]
        project = task.get("project") or {}
        if task.get("project_id") or project:
            t["category"] = {
                "id": str(task.get("project_id") or project.get("id") or ""),
                "name": project.get("title"),
                "type": "project",
            }
        base = self.config.get("baseUrl") or self.url or ""
        if tid and base:
            t["url"] = f"{base.rstrip('/')}/tasks/{tid}"
        return t

    def fetch_all(self) -> list[dict]:
        base = self.config.get("baseUrl") or self.url
        if not base:
            raise NotImplementedError("Vikunja baseUrl is not configured")
        headers = self._auth_header()
        page = 1
        per_page = 50
        tasks: list[dict] = []
        while True:
            url = f"{base.rstrip('/')}/api/v1/tasks/all?page={page}&per_page={per_page}"
            data = self._http_get(url, headers=headers)
            if not isinstance(data, list):
                data = data.get("tasks", []) if isinstance(data, dict) else []
            tasks.extend(data)
            if len(data) < per_page:
                break
            page += 1
        return [self.normalize(x) for x in tasks]


# ---------- MS Todo ---------------------------------------------------------

_MSTODO_STATUS_MAP = {
    "notStarted": "todo", "inProgress": "in_progress",
    "completed": "done", "deferred": "todo", "waitingOnOthers": "todo",
}
_MSTODO_PRIORITY_MAP = {"high": "high", "normal": "medium", "low": "low"}


class MSTodoDriver(Driver):
    SCHEME = "msftodo"

    def _auth_header(self) -> dict:
        # Real implementation would acquire a bearer via MSAL device code flow.
        token = self.config.get("accessToken", "")
        return {"Authorization": f"Bearer {token}"}

    def normalize(self, task: dict, list_name: str | None = None) -> dict:
        t = empty_task()
        tid = task.get("id")
        t["id"] = f"msftodo-{tid}" if tid else ""
        t["source"] = "msftodo"
        t["title"] = task.get("title") or ""
        body = task.get("body") or {}
        t["description"] = body.get("content") if isinstance(body, dict) else None
        t["status"] = _MSTODO_STATUS_MAP.get(task.get("status", ""), "todo")
        t["priority"] = _MSTODO_PRIORITY_MAP.get(task.get("importance", ""), "none")
        t["created_date"] = task.get("createdDateTime")
        t["updated_date"] = task.get("lastModifiedDateTime")
        due = task.get("dueDateTime")
        if isinstance(due, dict):
            t["due_date"] = due.get("dateTime")
        elif isinstance(due, str):
            t["due_date"] = due
        if task.get("reminderDateTime"):  # tolerated, not mapped to schema.
            pass
        t["tags"] = list(task.get("categories") or [])
        if list_name:
            t["category"] = {
                "id": task.get("parentListId"),
                "name": list_name,
                "type": "list",
            }
        t["url"] = task.get("linkedResources", [{}])[0].get("webUrl") if task.get("linkedResources") else None
        return t

    def fetch_all(self) -> list[dict]:
        if not MSAL_AVAILABLE and not self.config.get("accessToken"):
            raise NotImplementedError(
                "MS Todo requires MSAL device-code auth or a preconfigured accessToken"
            )
        headers = self._auth_header()
        lists_url = "https://graph.microsoft.com/v1.0/me/todo/lists"
        lists = self._http_get(lists_url, headers=headers).get("value", [])
        out: list[dict] = []
        for lst in lists:
            lid = lst.get("id")
            lname = lst.get("displayName")
            tasks = self._http_get(
                f"https://graph.microsoft.com/v1.0/me/todo/lists/{lid}/tasks",
                headers=headers,
            ).get("value", [])
            out.extend(self.normalize(t, list_name=lname) for t in tasks)
        return out


# ---------- Notion ----------------------------------------------------------

class NotionDriver(Driver):
    SCHEME = "notion"

    def _auth_header(self) -> dict:
        return {
            "Authorization": f"Bearer {self.config.get('token', '')}",
            "Notion-Version": "2022-06-28",
        }

    @staticmethod
    def _text_from_rich(rt_list) -> str:
        if not rt_list:
            return ""
        return "".join((item.get("plain_text") or "") for item in rt_list if isinstance(item, dict))

    def normalize(self, page: dict, db_title: str | None = None) -> dict:
        t = empty_task()
        pid = page.get("id") or ""
        t["id"] = f"notion-{pid}" if pid else ""
        t["source"] = "notion"
        props = page.get("properties") or {}
        title_prop = None
        for _, p in props.items():
            if isinstance(p, dict) and p.get("type") == "title":
                title_prop = p
                break
        if title_prop:
            t["title"] = self._text_from_rich(title_prop.get("title") or [])
        # Status / priority via select; tags via multi_select; dates via date.
        for name, p in props.items():
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            lname = name.lower()
            if ptype == "select":
                val = p.get("select") or {}
                name_val = val.get("name") if val else None
                if lname in ("status", "state") and name_val:
                    mapped = _JIRA_STATUS_MAP.get(name_val.lower())
                    t["status"] = mapped or "todo"
                elif lname == "priority" and name_val:
                    t["priority"] = _JIRA_PRIORITY_MAP.get(name_val.lower(), "none")
            elif ptype == "multi_select" and lname in ("tags", "labels"):
                t["tags"] = [m.get("name", "") for m in (p.get("multi_select") or [])]
            elif ptype == "date" and lname in ("due", "due date", "duedate", "deadline"):
                d = p.get("date") or {}
                t["due_date"] = d.get("start")
            elif ptype == "checkbox" and lname in ("done", "completed"):
                t["status"] = "done" if p.get("checkbox") else t["status"]
            elif ptype == "rich_text" and lname in ("description", "notes"):
                t["description"] = self._text_from_rich(p.get("rich_text") or []) or None
        t["created_date"] = page.get("created_time")
        t["updated_date"] = page.get("last_edited_time")
        t["url"] = page.get("url")
        if db_title:
            t["category"] = {
                "id": self.config.get("databaseId"),
                "name": db_title,
                "type": "database",
            }
        return t

    def fetch_all(self) -> list[dict]:
        database_id = self.config.get("databaseId")
        if not database_id:
            raise NotImplementedError("Notion databaseId is not configured")
        headers = self._auth_header()
        url = f"https://api.notion.com/v1/databases/{database_id}/query"
        results: list[dict] = []
        cursor = None
        db_title = self.config.get("databaseTitle")
        while True:
            body: dict = {}
            if cursor:
                body["start_cursor"] = cursor
            data = self._http_post(url, body=body, headers=headers)
            for page in data.get("results", []):
                results.append(self.normalize(page, db_title=db_title))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results

    def upsert(self, task: dict) -> None:
        raise NotImplementedError("Notion is pull-only")

    def delete(self, task_id: str) -> None:
        raise NotImplementedError("Notion is pull-only")


SCHEMES: dict[str, type[Driver]] = {
    "jira": JiraDriver,
    "vikunja": VikunjaDriver,
    "msftodo": MSTodoDriver,
    "notion": NotionDriver,
}


def driver_for_scheme(scheme: str, remote_name: str = "", url: str = "",
                      config: dict | None = None) -> Driver:
    if scheme not in SCHEMES:
        raise ValueError(f"unknown tasks scheme: {scheme!r}")
    return SCHEMES[scheme](remote_name, url, config or {})


def scheme_for_name(argv0: str) -> str | None:
    """Derive the scheme from the invoked program name (e.g. git-remote-jira)."""
    base = os.path.basename(argv0)
    if base.startswith("git-remote-"):
        candidate = base[len("git-remote-"):]
        return candidate if candidate in SCHEMES else None
    return None


# ============================================================================
# Protocol handler
# ============================================================================

class ProtocolHandler:
    """Drives the git remote helper stdin/stdout conversation."""

    def __init__(self, remote_name: str, url: str, driver: Driver,
                 serializer: Serializer,
                 stdin=None, stdout=None, stderr=None):
        self.remote_name = remote_name
        self.url = url
        self.driver = driver
        self.serializer = serializer
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr

    def _write(self, line: str) -> None:
        self.stdout.write(line)
        self.stdout.flush()

    def _log(self, msg: str) -> None:
        self.stderr.write(f"git-remote-tasks: {msg}\n")
        self.stderr.flush()

    def run(self) -> None:
        while True:
            line = self.stdin.readline()
            if not line:
                return
            line = line.rstrip("\n")
            if line == "":
                continue
            if line == "capabilities":
                self._cmd_capabilities()
            elif line == "list" or line == "list for-push":
                self._cmd_list()
            elif line.startswith("import"):
                self._cmd_import_batch(line)
            elif line == "export":
                self._cmd_export()
            else:
                self._log(f"unknown command: {line!r}")

    # ---- capabilities ----
    def _cmd_capabilities(self) -> None:
        self._write("import\n")
        self._write("export\n")
        self._write(f"refspec refs/heads/*:refs/remotes/{self.remote_name}/*\n")
        self._write("*push\n")
        self._write("*fetch\n")
        self._write("\n")

    # ---- list ----
    def _cmd_list(self) -> None:
        self._write("? refs/heads/main\n")
        self._write("\n")

    # ---- import ----
    def _cmd_import_batch(self, first_line: str) -> None:
        # Consume the rest of the batch (until blank line).
        while True:
            nxt = self.stdin.readline()
            if not nxt or nxt.strip() == "":
                break
        tasks = self.driver.fetch_all()
        tasks_sorted = sorted(tasks, key=lambda t: t.get("id") or "")
        self._write_fast_import(tasks_sorted)

    def _write_fast_import(self, tasks: list[dict]) -> None:
        ext = self.serializer.EXTENSION
        blobs: list[tuple[int, bytes, str]] = []
        for idx, task in enumerate(tasks, start=1):
            body = self.serializer.serialize(task).encode("utf-8")
            path = f"tasks/{task['id']}.{ext}"
            blobs.append((idx, body, path))
        for mark, body, _path in blobs:
            self._write("blob\n")
            self._write(f"mark :{mark}\n")
            self._write(f"data {len(body)}\n")
            # Raw bytes - write through underlying buffer when available.
            buf = getattr(self.stdout, "buffer", None)
            if buf is not None:
                self.stdout.flush()
                buf.write(body)
                buf.flush()
                self._write("\n")
            else:
                self._write(body.decode("utf-8"))
                self._write("\n")
        commit_mark = len(blobs) + 1
        latest = self._latest_updated(tasks)
        ts = int(latest.timestamp())
        message = (
            f"tasks: import {self.remote_name} ({len(tasks)} tasks) "
            f"[{latest.strftime('%Y-%m-%dT%H:%M:%SZ')}]"
        )
        mbytes = message.encode("utf-8")
        self._write(f"commit refs/heads/main\n")
        self._write(f"mark :{commit_mark}\n")
        self._write(f"committer git-remote-tasks <tasks@local> {ts} +0000\n")
        self._write(f"data {len(mbytes)}\n")
        self._write(message + "\n")
        self._write("deleteall\n")
        for mark, _body, path in blobs:
            self._write(f"M 100644 :{mark} {path}\n")
        self._write("\n")
        self._write("done\n")

    @staticmethod
    def _latest_updated(tasks: list[dict]) -> datetime:
        best: datetime | None = None
        for t in tasks:
            d = t.get("updated_date") or t.get("created_date")
            if not d:
                continue
            try:
                dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                dt = dt.astimezone(timezone.utc)
            except ValueError:
                continue
            if best is None or dt > best:
                best = dt
        return best or datetime.now(timezone.utc)

    # ---- export ----
    def _cmd_export(self) -> None:
        results: list[str] = []
        marks: dict[str, bytes] = {}
        current_ref: str | None = None
        while True:
            line = self.stdin.readline()
            if not line:
                break
            stripped = line.rstrip("\n")
            if stripped == "done":
                break
            if stripped.startswith("blob"):
                mark_line = self.stdin.readline().rstrip("\n")
                mark = mark_line.split(" ", 1)[1] if " " in mark_line else mark_line
                data_line = self.stdin.readline().rstrip("\n")
                nbytes = int(data_line.split(" ", 1)[1]) if " " in data_line else 0
                body = self._read_exactly(nbytes)
                marks[mark] = body
                continue
            if stripped.startswith("commit "):
                current_ref = stripped.split(" ", 1)[1]
                continue
            if stripped.startswith("data "):
                nbytes = int(stripped.split(" ", 1)[1])
                self._read_exactly(nbytes)
                continue
            if stripped.startswith(("author ", "committer ", "from ", "merge ", "original-oid ")):
                continue
            if stripped.startswith("M "):
                self._handle_modify(stripped, marks)
                continue
            if stripped.startswith("D "):
                self._handle_delete(stripped)
                continue
            if stripped.startswith(("reset ", "tag ", "feature ", "option ", "progress ")):
                continue
            # ignore other lines silently
        if current_ref:
            results.append(current_ref)
        if not results:
            results = [f"refs/heads/main"]
        for ref in results:
            self._write(f"ok {ref}\n")
        self._write("\n")

    def _read_exactly(self, n: int) -> bytes:
        """Read exactly n bytes from stdin, honoring byte counts even for text streams.

        Real git invocations expose stdin.buffer (binary). Tests often pass an
        io.StringIO. For the text fallback we read characters until the UTF-8
        encoding of the accumulated text hits n bytes — never more — so multi-byte
        bodies round-trip correctly.
        """
        buf = getattr(self.stdin, "buffer", None)
        if buf is not None:
            data = buf.read(n)
            return data if isinstance(data, bytes) else bytes(data, "utf-8")
        if not hasattr(self.stdin, "read"):
            return b""
        collected = bytearray()
        while len(collected) < n:
            ch = self.stdin.read(1)
            if not ch:
                break
            chunk = ch.encode("utf-8")
            if len(collected) + len(chunk) > n:
                # Should not happen if caller respects byte counts, but bail out
                # rather than silently mis-align.
                break
            collected.extend(chunk)
        return bytes(collected)

    def _handle_modify(self, line: str, marks: dict[str, bytes]) -> None:
        # M <mode> <sha-or-mark> <path>
        parts = line.split(" ", 3)
        if len(parts) < 4:
            return
        _, _mode, ref, path = parts
        if not path.startswith("tasks/"):
            return
        try:
            serializer = serializer_for_extension(path)
        except ValueError as exc:
            self._log(str(exc))
            return
        content = marks.get(ref, b"")
        if not content:
            self._log(f"no blob content for {path}")
            return
        try:
            task = serializer.deserialize(content.decode("utf-8"))
            self.driver.upsert(task)
        except NotImplementedError as exc:
            self._log(f"upsert not implemented: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"upsert failed: {exc}")

    def _handle_delete(self, line: str) -> None:
        # D <path>
        parts = line.split(" ", 1)
        if len(parts) < 2:
            return
        path = parts[1]
        if not path.startswith("tasks/"):
            return
        name = Path(path).name
        task_id, _, _ = name.rpartition(".")
        try:
            self.driver.delete(task_id)
        except NotImplementedError as exc:
            self._log(f"delete not implemented: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"delete failed: {exc}")


# ============================================================================
# Management subcommands
# ============================================================================

KNOWN_SUBCOMMANDS = {"install", "uninstall", "list-schemes", "check"}


def _script_path() -> Path:
    return Path(os.path.abspath(__file__))


def cmd_install(args) -> int:
    bin_dir = Path(os.path.expanduser(args.bin_dir)).resolve()
    bin_dir.mkdir(parents=True, exist_ok=True)
    src = _script_path()
    try:
        os.chmod(src, os.stat(src).st_mode | 0o111)
    except OSError as exc:
        print(f"warning: could not chmod {src}: {exc}", file=sys.stderr)
    for scheme in SCHEMES:
        link = bin_dir / f"git-remote-{scheme}"
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(str(src), str(link))
        print(f"installed {link}")
    path_env = os.environ.get("PATH", "")
    on_path = any(Path(os.path.expanduser(p)).resolve() == bin_dir
                  for p in path_env.split(os.pathsep) if p)
    if not on_path:
        print(f"warning: {bin_dir} is not on PATH", file=sys.stderr)
    return 0


def cmd_uninstall(args) -> int:
    bin_dir = Path(os.path.expanduser(args.bin_dir)).resolve()
    for scheme in SCHEMES:
        link = bin_dir / f"git-remote-{scheme}"
        try:
            os.unlink(link)
            print(f"removed {link}")
        except FileNotFoundError:
            print(f"skip {link} (not present)")
        except OSError as exc:
            print(f"warning: could not remove {link}: {exc}", file=sys.stderr)
    return 0


def cmd_list_schemes(args) -> int:
    for scheme, cls in SCHEMES.items():
        print(f"{scheme:10s} {cls.__name__}")
    return 0


def cmd_check(args) -> int:
    config = read_remote_config(args.remote_name)
    if not config:
        print(f"error: no config for remote {args.remote_name!r}", file=sys.stderr)
        return 2
    scheme = config.get("scheme")
    if scheme not in SCHEMES:
        print(f"error: remote {args.remote_name!r} has invalid scheme {scheme!r}",
              file=sys.stderr)
        return 2
    required = REMOTE_REQUIRED_KEYS.get(scheme, ())
    missing = [k for k in required if not config.get(k)]
    print(f"remote: {args.remote_name}")
    print(f"scheme: {scheme}")
    for k, v in sorted(config.items()):
        shown = "<redacted>" if "token" in k.lower() or "password" in k.lower() else v
        print(f"  {k} = {shown}")
    if missing:
        print(f"error: missing required keys: {', '.join(missing)}", file=sys.stderr)
        return 2
    print("ok: all required keys present")
    return 0


def build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="git-remote-tasks",
        description="Management commands for git-remote-tasks.",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_install = sub.add_parser("install", help="create git-remote-<scheme> symlinks")
    p_install.add_argument("--bin-dir", default="~/.local/bin")

    p_uninstall = sub.add_parser("uninstall", help="remove git-remote-<scheme> symlinks")
    p_uninstall.add_argument("--bin-dir", default="~/.local/bin")

    sub.add_parser("list-schemes", help="list known remote schemes")

    p_check = sub.add_parser("check", help="validate config for a remote")
    p_check.add_argument("remote_name")

    return parser


# ============================================================================
# Entry point
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    prog = argv[0] if argv else ""

    # Case 1: invoked as git-remote-<scheme>
    scheme = scheme_for_name(prog)
    if scheme is not None:
        if len(argv) < 3:
            print("usage: git-remote-<scheme> <remote-name> <url>", file=sys.stderr)
            return 2
        return _run_helper(scheme, argv[1], argv[2])

    # Case 2: management subcommand
    if len(argv) >= 2 and argv[1] in KNOWN_SUBCOMMANDS:
        parser = build_argparser()
        args = parser.parse_args(argv[1:])
        if args.cmd == "install":
            return cmd_install(args)
        if args.cmd == "uninstall":
            return cmd_uninstall(args)
        if args.cmd == "list-schemes":
            return cmd_list_schemes(args)
        if args.cmd == "check":
            return cmd_check(args)
        return 2

    # Case 3: direct invocation as helper (scheme from URL)
    if len(argv) >= 3:
        remote_name, url = argv[1], argv[2]
        sch = urllib.parse.urlparse(url).scheme
        if sch in SCHEMES:
            return _run_helper(sch, remote_name, url)

    build_argparser().print_help()
    return 0


def _run_helper(scheme: str, remote_name: str, url: str) -> int:
    config = read_remote_config(remote_name)
    config.setdefault("scheme", scheme)
    driver = driver_for_scheme(scheme, remote_name=remote_name, url=url, config=config)
    serializer = serializer_for_format(read_format())
    handler = ProtocolHandler(remote_name, url, driver, serializer)
    handler.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
