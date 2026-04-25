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

Layout (top-to-bottom):

    1. Unified schema          — empty_task / normalize_task / is_safe_task_id
    2. Serializers             — YAMLSerializer, OrgSerializer
    3. Config reader           — CaseInsensitiveConfig, read/write/unset helpers
    4. Driver base + drivers   — Driver, Jira/Vikunja/MSTodo/Notion
    5. Protocol handler        — ProtocolHandler (import/export over stdio)
    6. Management subcommands  — install / uninstall / check / init / reset / version
    7. Entry point             — main(), scheme dispatch, symlink naming

Each driver owns four API-facing methods: fetch_all, fetch_changed,
upsert, delete. Cross-source ids are refused in the base class'
_native_id so writes are synchronous failures.
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
import time
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


# Intentional public API of this module. Anything not listed is an
# implementation detail and may move without notice.
__all__ = [
    "__version__",
    # Unified schema
    "TASK_FIELDS", "STATUSES", "PRIORITIES", "CATEGORY_TYPES",
    "empty_task", "normalize_task", "is_safe_task_id",
    # Serializers
    "Serializer", "YAMLSerializer", "OrgSerializer",
    "serializer_for_format", "serializer_for_extension",
    # Config
    "CaseInsensitiveConfig",
    "read_format", "read_remote_config",
    "write_config_value", "unset_config_values",
    # Drivers
    "Driver", "JiraDriver", "VikunjaDriver", "MSTodoDriver", "NotionDriver",
    "SCHEMES", "REMOTE_REQUIRED_KEYS", "driver_for_scheme",
    "JiraPushError", "JiraConfigError",
    "VikunjaPushError", "VikunjaConfigError",
    "MSTodoPushError", "NotionPushError",
    # Protocol + entry point
    "ProtocolHandler", "main",
]


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


_SAFE_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._=-]{0,254}")


def _is_safe_tasks_path(path: str) -> bool:
    """Return True when `path` is exactly `tasks/<id>.<ext>` with no traversal.

    Rejects `tasks/../etc/passwd`, `tasks//foo.yaml`, leading-dot names
    that could shadow git internals (`tasks/.git`), and anything pathlib
    would normalize outside `tasks/`. Used at every import/export
    boundary alongside `is_safe_task_id`.
    """
    if not path.startswith("tasks/"):
        return False
    rest = path[len("tasks/"):]
    if not rest or "/" in rest or "\\" in rest:
        return False
    if rest.startswith("."):
        return False
    if ".." in rest.split("."):
        return False
    return True


_TASK_FILE_EXTS = (".yaml", ".yml", ".org")


def _is_task_file_path(path: str) -> bool:
    """Return True only for `tasks/<anything>.<yaml|yml|org>`.

    Task repos often carry companion files under `tasks/` — `.gitkeep` to
    keep the directory in git, `README.md` for contributors. Neither is a
    task. During an export we silently skip any path that does not have a
    serializable extension so those companions do not trigger the
    safety-path rejection.
    """
    if not path.startswith("tasks/"):
        return False
    lower = path.lower()
    return any(lower.endswith(ext) for ext in _TASK_FILE_EXTS)


def is_safe_task_id(task_id: str) -> bool:
    """Return True when task_id can safely become a filesystem basename.

    The repo's task files live at `tasks/<id>.<ext>`; any id with `/`, `..`,
    leading `.`, or control characters could escape the directory or shadow a
    git-internal name. Uses `fullmatch` so embedded control characters
    (including a trailing `\\n`) do not slip past the anchor.
    """
    if not isinstance(task_id, str) or not task_id:
        return False
    if task_id in (".", "..") or "/" in task_id or "\\" in task_id:
        return False
    return bool(_SAFE_TASK_ID_RE.fullmatch(task_id))


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
    # Always escape control whitespace too — a stray `\r` in a value would
    # otherwise terminate the line on parse via `splitlines()`. (R-04, R-16)
    escaped = (s.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\r", "\\r")
                .replace("\t", "\\t")
                .replace("\n", "\\n"))
    return f'"{escaped}"'


def _yaml_scalar(value, always_quote: bool = False) -> str:
    if value is None:
        return "null"
    s = str(value)
    if always_quote:
        return _yaml_quote(s)
    if "\n" in s or "\r" in s or "\t" in s:
        # Block scalars can't represent CR/TAB; force the double-quoted form
        # so the round-trip is byte-stable. (R-04, R-16)
        return _yaml_quote(s)
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
    if not always_quote and "\n" in s and "\r" not in s and "\t" not in s:
        # Block scalar `|` is fine for plain LF-only multiline content; CR
        # or TAB would be eaten by `splitlines()` / leading-space dedent on
        # parse, so those cases fall through to the double-quoted scalar
        # below. (R-04, R-16)
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
        # Decode escapes via a sentinel for `\\` so later passes don't treat
        # the introduced backslash as the start of another escape sequence.
        return (body.replace("\\\\", "\x00")
                    .replace('\\"', '"')
                    .replace("\\r", "\r")
                    .replace("\\t", "\t")
                    .replace("\\n", "\n")
                    .replace("\x00", "\\"))
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


class YAMLSerializer(Serializer):
    """Hand-written YAML emitter + parser, supporting a documented subset only.

    Supported on output (what this class emits):
      - Top-level scalar keys: `id`, `source`, `title`, `description`,
        `status`, `priority`, `created_date`, `due_date`, `updated_date`,
        `url`, plus the nested `category` mapping and the `tags` / `logbook`
        sequences.
      - Quoted double-quoted strings with `\\\\`, `\\"`, and `\\n` escapes.
      - Block scalars (`|`) for multi-line fields such as `description`.
      - Always-quoted date strings, `null` for None, `[]` for empty lists.

    Supported on input (what the parser tolerates from a hand edit):
      - All of the above, plus single-quoted strings with the `''`
        escape and unquoted scalars when `_yaml_needs_quoting` says it
        is safe.
      - Line comments (`#`), blank lines.

    Explicitly NOT supported — files using these features will be
    misparsed or silently truncated:
      - Anchors / aliases (`&anchor` / `*alias`).
      - Flow collections beyond the bare `[]` empty list.
      - Tagged types (`!!str`, `!!int`, explicit YAML tags).
      - Multi-document streams (`---` / `...`).
      - Nested mappings deeper than one level below the top key.
      - Keys containing characters outside `[A-Za-z_][A-Za-z0-9_]*`.
      - CRLF line endings (strip before passing in).
      - Explicit block-scalar chomping indicators (`|-`, `|+`). The
        emitter only writes `|` (clip-with-strip) — trailing `\\n` in
        a value is normalized away on round-trip. Upstream services
        do not emit trailing newlines in description-like fields so
        this never surfaces in practice.

    The round-trip invariant, exercised by the hypothesis fuzz suite in
    `test_yaml_parser_fuzz.py`, is:

        normalize_task(deserialize(serialize(t))) == normalize_task(t)
    """

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
            # BUG-06: permit hyphens in nested keys (e.g. `due-date:`) so
            # hand-edits don't vanish. Unknown keys are still dropped at
            # the deserialize-to-schema step.
            m = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_\-]*):\s*(.*)$", ln)
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
    """Convert an ISO8601 string to an org timestamp; returns [...] or <...>.

    Preserves the original timezone offset in the timestamp body so an agenda
    user sees the local hour the upstream service reported. A trailing
    '+HHMM' / '-HHMM' / 'Z' is appended when the source string carried an
    offset; naive ISO strings emit a body without an offset (assumed UTC
    for parseback).
    """
    s = iso.replace("Z", "+00:00")
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                body = dt.strftime("%Y-%m-%d %a %H:%M")
            else:
                offset = dt.utcoffset()
                total = int(offset.total_seconds()) if offset is not None else 0
                sign = "+" if total >= 0 else "-"
                total = abs(total)
                hh, mm = divmod(total // 60, 60)
                offset_str = "Z" if total == 0 else f"{sign}{hh:02d}{mm:02d}"
                body = dt.strftime("%Y-%m-%d %a %H:%M") + " " + offset_str
        else:
            dt = datetime.fromisoformat(s)
            body = dt.strftime("%Y-%m-%d %a")
    except ValueError:
        body = iso
    return f"<{body}>" if active else f"[{body}]"


def _org_emit_tag_csv(tags: list[str]) -> str:
    """Encode a tag list for the `:TAGS:` Org property.

    Tags that contain `,` or `"` (or leading/trailing whitespace) are
    double-quoted with `"` escaped as `\\"`. Tags without those
    characters emit bare so files written by older versions still
    round-trip. (R-03)
    """
    parts: list[str] = []
    for raw in tags:
        s = raw if isinstance(raw, str) else str(raw)
        if "," in s or '"' in s or s != s.strip():
            parts.append('"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"')
        else:
            parts.append(s)
    return ",".join(parts)


def _org_parse_tag_csv(value: str) -> list[str]:
    """Inverse of `_org_emit_tag_csv`. Bare commas split; quoted commas don't.

    Backwards compatible with the pre-R-03 emitter, which never quoted —
    files written by older versions parse identically since every tag was
    bare.
    """
    out: list[str] = []
    cur: list[str] = []
    in_q = False
    esc = False
    for ch in value:
        if esc:
            cur.append(ch)
            esc = False
            continue
        if in_q and ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_q = not in_q
            continue
        if ch == "," and not in_q:
            tag = "".join(cur).strip()
            if tag:
                out.append(tag)
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


_ORG_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})"            # date
    r"(?:\s+[A-Za-z]+)?"                # optional weekday
    r"(?:\s+(\d{1,2}:\d{2}))?"          # optional time
    r"(?:\s+([+-]\d{2}:?\d{2}|Z))?"     # optional offset
    r"\s*$"
)


def _org_timestamp_to_iso(token: str) -> str:
    """Parse an org timestamp (with optional offset) → ISO8601."""
    token = token.strip()
    if token and token[0] in "[<" and token[-1] in "]>":
        inner = token[1:-1]
    else:
        inner = token
    m = _ORG_TIMESTAMP_RE.match(inner)
    if not m:
        return inner
    date_part = m.group(1)
    time_part = m.group(2)
    offset_part = m.group(3)
    if not time_part:
        return date_part
    hh, mm = time_part.split(":")
    if offset_part in (None, "Z"):
        return f"{date_part}T{int(hh):02d}:{mm}:00Z"
    off = offset_part.replace(":", "")
    return f"{date_part}T{int(hh):02d}:{mm}:00{off[:3]}:{off[3:]}"


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
        # FEAT-05: DEADLINE belongs on the line immediately below the
        # headline so Emacs / nvim-orgmode pick it up in the agenda. The
        # :PROPERTIES: drawer follows.
        if t["due_date"]:
            out.append("  DEADLINE: "
                       + _iso_to_org_timestamp(t["due_date"], active=True))
        props: list[tuple[str, str]] = []
        if t["id"]:
            props.append(("ID", t["id"]))
        if t["source"]:
            props.append(("SOURCE", t["source"]))
        if t["created_date"]:
            props.append(("CREATED", _iso_to_org_timestamp(t["created_date"])))
        if t["updated_date"]:
            props.append(("UPDATED", _iso_to_org_timestamp(t["updated_date"])))
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
            props.append(("TAGS", _org_emit_tag_csv(t["tags"])))
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
        # Parse drawers, agenda timestamps, and body.
        body_lines: list[str] = []
        while i < len(lines):
            # BUG-11: a second '* ' headline terminates this task's drawers.
            # Only column-zero `*` is a real headline — body lines emitted by
            # the serializer are always indented (`f"  {line}"`), so a body
            # line such as `  * bullet` is part of the description, not a new
            # task. (R-02)
            if lines[i].startswith("* "):
                break
            if lines[i] == "*":
                break
            stripped = lines[i].strip()
            if stripped == ":PROPERTIES:":
                i = self._parse_properties(lines, i + 1, t)
                continue
            if stripped == ":LOGBOOK:":
                i = self._parse_logbook(lines, i + 1, t)
                continue
            # FEAT-05: DEADLINE and SCHEDULED agenda lines appearing between
            # the headline and the drawer.
            m_dl = re.match(r"^\s*DEADLINE:\s*(\S.*)$", lines[i])
            if m_dl:
                t["due_date"] = _org_timestamp_to_iso(m_dl.group(1).strip())
                i += 1
                continue
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
            # BUG-11: a stray '* ' headline means this drawer was never
            # closed. Abandon the drawer rather than swallowing the next
            # task's content. Only column-zero `*` counts so an indented
            # `* bullet` body line doesn't false-trip. (R-02)
            if lines[i].startswith("* "):
                t["category"] = cat
                return i
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
                    t["tags"] = _org_parse_tag_csv(value)
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

_GIT_SUBPROCESS_TIMEOUT = 10.0


def _run_git_config(args: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "config", "--local", *args],
            capture_output=True, text=True, check=False,
            timeout=_GIT_SUBPROCESS_TIMEOUT,
        )
    except (FileNotFoundError, OSError) as exc:
        print(f"git-remote-tasks: git config failed: {exc}", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("git-remote-tasks: git config timed out", file=sys.stderr)
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
    "mstodo":  ("tenantId", "clientId"),
    "notion":   ("databaseId", "token"),
}


class CaseInsensitiveConfig(dict):
    """Dict subclass with case-insensitive key lookup.

    Necessary because git lowercases the *variable* segment of every
    config key on read (e.g. `tasks-remote.foo.baseUrl` round-trips as
    `baseurl`), but our driver code historically used camelCase
    lookups. This wrapper accepts either form.

    Sub-keys with dots (e.g. `sync.lastFetchAt`) get the same treatment
    on each segment so writes done by us match reads from git.
    """

    @staticmethod
    def _norm(key: str) -> str:
        return key.lower()

    def __init__(self, base: dict | None = None):
        super().__init__()
        self._real_keys: dict[str, str] = {}
        for k, v in (base or {}).items():
            self[k] = v

    def __setitem__(self, key, value):
        norm = self._norm(key)
        super().__setitem__(norm, value)
        self._real_keys.setdefault(norm, key)

    def __getitem__(self, key):
        return super().__getitem__(self._norm(key))

    def __contains__(self, key):
        return super().__contains__(self._norm(key))

    def get(self, key, default=None):
        return super().get(self._norm(key), default)

    def pop(self, key, *args):
        return super().pop(self._norm(key), *args)

    def setdefault(self, key, default=None):
        norm = self._norm(key)
        if not super().__contains__(norm):
            super().__setitem__(norm, default)
            self._real_keys.setdefault(norm, key)
        return super().__getitem__(norm)


def read_remote_config(remote_name: str) -> CaseInsensitiveConfig:
    """Read all `tasks-remote.<name>.*` keys into a case-insensitive dict.

    Git lowercases the variable segment of every key on storage, so our
    driver must look up `baseUrl`, `baseurl`, `BASEURL` etc. as the same
    thing — `CaseInsensitiveConfig` provides that guarantee.
    """
    prefix = f"tasks-remote.{remote_name}."
    out = _run_git_config(["--get-regexp", re.escape(prefix) + ".*"])
    config: CaseInsensitiveConfig = CaseInsensitiveConfig()
    if not out:
        return config
    for line in out.splitlines():
        if " " not in line:
            continue
        key, _, value = line.partition(" ")
        if key.startswith(prefix):
            config[key[len(prefix):]] = value
    return config


def write_config_value(key: str, value: str) -> bool:
    """Write a single key/value to the local git config. Returns True on success.

    Rejects values containing embedded newlines because `git config
    --get-regexp` returns one key=value record per line and a value with an
    LF would smear across records on the next read (S2-03).
    """
    if "\n" in value or "\r" in value:
        print("git-remote-tasks: refusing to write config value containing newline",
              file=sys.stderr)
        return False
    try:
        proc = subprocess.run(
            ["git", "config", "--local", key, value],
            capture_output=True, text=True, check=False,
            timeout=_GIT_SUBPROCESS_TIMEOUT,
        )
    except (FileNotFoundError, OSError) as exc:
        print(f"git-remote-tasks: git config write failed: {exc}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("git-remote-tasks: git config write timed out", file=sys.stderr)
        return False
    if proc.returncode != 0:
        if proc.stderr.strip():
            print(f"git-remote-tasks: git config: {proc.stderr.strip()}",
                  file=sys.stderr)
        return False
    return True


def unset_config_values(pattern: str) -> bool:
    """Remove all local config keys matching `<pattern>` (regex).

    Used by `reset` to wipe `tasks-remote.<name>.sync.*` state. `git config
    --unset-all` with `--get-regexp` is a single subprocess per matching key;
    we list-and-unset so unknown keys don't error.
    """
    out = _run_git_config(["--get-regexp", pattern])
    if not out:
        return True
    ok = True
    for line in out.splitlines():
        key = line.split(" ", 1)[0]
        proc = subprocess.run(
            ["git", "config", "--local", "--unset-all", key],
            capture_output=True, text=True, check=False,
            timeout=_GIT_SUBPROCESS_TIMEOUT,
        )
        if proc.returncode != 0:
            ok = False
    return ok


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
        # Per-run dedupe so a driver doesn't spam the same warning for every
        # task in a batch. Reset on every Driver instantiation, which matches
        # the helper's process lifetime.
        self._warned_codes: set[str] = set()
        # Test seam — tests substitute an io.StringIO so warnings can be
        # asserted on, and so the unittest output stays clean.
        self._warn_stream = sys.stderr

    def _warn_once(self, code: str, msg: str) -> None:
        """Emit a stderr warning at most once per run per code.

        Mirrors `ProtocolHandler._warn_once`; lives on the driver because
        push-time decisions (e.g. "tags column is select, not multi-select")
        happen too far from the protocol handler to easily route through it.
        """
        if code in self._warned_codes:
            return
        self._warned_codes.add(code)
        self._warn_stream.write(
            f"git-remote-tasks: warning[{code}]: {msg}\n"
        )
        self._warn_stream.flush()

    # ---- HTTP seams (override or mock in tests) ----
    def _http_get(self, url: str, headers: dict | None = None) -> dict:
        return self._http_request("GET", url, headers=headers, body=None)

    def _http_post(self, url: str, body: dict | None = None,
                   headers: dict | None = None) -> dict:
        return self._http_request("POST", url, headers=headers, body=body)

    def _http_put(self, url: str, body: dict | None = None,
                  headers: dict | None = None) -> dict:
        return self._http_request("PUT", url, headers=headers, body=body)

    def _http_patch(self, url: str, body: dict | None = None,
                    headers: dict | None = None) -> dict:
        return self._http_request("PATCH", url, headers=headers, body=body)

    def _http_delete(self, url: str, headers: dict | None = None) -> dict:
        return self._http_request("DELETE", url, headers=headers, body=None)

    HTTP_TIMEOUT_DEFAULT = 30.0
    HTTP_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
    HTTP_MAX_RETRIES = 3

    def _http_request(self, method: str, url: str, headers: dict | None = None,
                      body: dict | None = None) -> dict:
        """Single-shot HTTP with timeout and bounded retry on transient errors.

        Timeout defaults to 30s, overridable per remote via
        `tasks-remote.<name>.httpTimeout`. Retries the request up to
        `HTTP_MAX_RETRIES` times on a 408/425/429/5xx response or a
        `URLError` (network hiccup) with exponential backoff starting at
        0.5s, capped at 10s. Error message redacts auth headers.
        """
        data = json.dumps(body).encode("utf-8") if body is not None else None
        merged_headers = dict(headers or {})
        if body is not None and "Content-Type" not in merged_headers:
            merged_headers["Content-Type"] = "application/json"
        timeout = self._http_timeout()

        attempt = 0
        backoff = 0.5
        last_exc: Exception | None = None
        debug = bool(os.environ.get("GIT_REMOTE_TASKS_DEBUG"))
        while attempt <= self.HTTP_MAX_RETRIES:
            attempt += 1
            req = urllib.request.Request(url, data=data, method=method)
            for k, v in merged_headers.items():
                req.add_header(k, v)
            t0 = time.time() if debug else 0.0
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8")
                if debug:
                    # Scrub query string so tokens embedded in URLs (rare)
                    # don't leak into operator logs; keep path + host.
                    safe = url.split("?", 1)[0]
                    sys.stderr.write(
                        f"git-remote-tasks: http[{method}] "
                        f"{time.time() - t0:.2f}s {safe}\n"
                    )
                    sys.stderr.flush()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code in self.HTTP_RETRY_STATUSES \
                        and attempt <= self.HTTP_MAX_RETRIES:
                    self._sleep_backoff(backoff)
                    backoff = min(backoff * 2, 10.0)
                    last_exc = exc
                    continue
                raise self._redact_http_error(exc, url) from exc
            except urllib.error.URLError as exc:
                # Typically a transient network issue; retry.
                if attempt <= self.HTTP_MAX_RETRIES:
                    self._sleep_backoff(backoff)
                    backoff = min(backoff * 2, 10.0)
                    last_exc = exc
                    continue
                raise
        if last_exc is not None:  # pragma: no cover - fall-through on exhaustion
            raise last_exc
        return {}

    def _http_timeout(self) -> float:
        raw = self.config.get("httpTimeout")
        if not raw:
            return self.HTTP_TIMEOUT_DEFAULT
        try:
            return float(raw)
        except (TypeError, ValueError):
            return self.HTTP_TIMEOUT_DEFAULT

    # Per-driver paginated-fetch batch size. Overridable per remote via
    # `tasks-remote.<name>.pageSize`; each driver sets its own default
    # close to what the service's API allows (Jira Cloud: 100, Vikunja: 100).
    PAGE_SIZE_DEFAULT = 50
    PAGE_SIZE_MAX = 1000

    def _page_size(self) -> int:
        raw = self.config.get("pageSize")
        if raw in (None, ""):
            return self.PAGE_SIZE_DEFAULT
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return self.PAGE_SIZE_DEFAULT
        # Clamp: services reject absurd page sizes with a 400 anyway, but
        # clamping keeps the error local and actionable.
        if val < 1:
            return 1
        if val > self.PAGE_SIZE_MAX:
            return self.PAGE_SIZE_MAX
        return val

    @staticmethod
    def _sleep_backoff(seconds: float) -> None:  # pragma: no cover - timing
        import time
        time.sleep(seconds)

    @staticmethod
    def _redact_http_error(exc: urllib.error.HTTPError,
                            url: str) -> urllib.error.HTTPError:
        """Return an HTTPError whose string form omits auth and bodies.

        The default HTTPError repr includes `hdrs` and `msg`; some services
        echo parts of the request (including bearer tokens) in their 4xx
        payloads. Keep only the status code and a trimmed URL.
        """
        safe_url = url.split("?", 1)[0]
        return urllib.error.HTTPError(
            safe_url, exc.code,
            f"HTTP {exc.code} from {safe_url}",
            hdrs=None, fp=None,
        )

    # ---- Public API ----
    @abstractmethod
    def fetch_all(self) -> list[dict]:
        """Fetch all tasks from the remote service as unified task dicts."""
        raise NotImplementedError

    def fetch_changed(
        self, since: str | None
    ) -> tuple[list[dict], list[str], str | None]:
        """Return (changed_tasks, deleted_ids, new_since_token).

        `since` is an opaque per-driver state token previously returned by this
        method — typically an ISO timestamp, but for MS Todo it will be a
        Graph delta link. Pass `None` to request a full snapshot.

        Returns a tuple:
          - changed_tasks: tasks to M into the tree.
          - deleted_ids: task ids to D from the tree (only services with
            native deletion signals populate this; the rest return []).
          - new_since_token: the token to persist for the next run.

        Base implementation delegates to fetch_all() — every subclass is
        free to override to hit a narrower API.
        """
        tasks = self.fetch_all()
        return tasks, [], self._now_iso()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

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

    # ---- Mapping helpers (FEAT-03) ----
    def _subconfig(self, prefix: str) -> dict:
        """Return the flat map under `<prefix>` from per-remote config.

        Two forms are supported:

        1. Dotted keys: `<prefix>.<name>=<value>` per `git config`. Works
           for ASCII alphanumeric `<name>` only because git rejects
           anything else as a variable-name segment.
        2. A single JSON-encoded value: `<prefix>=<json>` where the value
           is a JSON object. Use this for arbitrary keys (Turkish names,
           emoji, anything with `_` or `.`):

               git config tasks-remote.<name>.statusMap '{"Yüksek":"high"}'

        When both are present, the dotted form wins on collision so
        narrower per-key overrides keep working alongside a bulk JSON
        block. Comparison of the prefix is case-insensitive (matches
        CaseInsensitiveConfig).
        """
        out: dict = {}
        json_blob = self.config.get(prefix)
        if json_blob:
            try:
                parsed = json.loads(json_blob)
                if isinstance(parsed, dict):
                    out.update({str(k): str(v) for k, v in parsed.items()})
            except (TypeError, ValueError):
                pass  # malformed JSON falls through to dotted-key form
        pref = f"{prefix}.".lower()
        for k, v in self.config.items():
            if k.lower().startswith(pref):
                out[k[len(pref):]] = v
        return out

    def _apply_status_override(self, upstream_value: str | None,
                                default: str) -> str:
        """Return the unified status for an upstream status string.

        Lookup order: exact match in the user-configured `statusMap`,
        then case-insensitive match, then the default the caller passed
        (driver's built-in map result). Unknown upstream values that the
        default could not translate produce a one-time warning via the
        driver's `_note_unmapped` sink so data loss is never silent.
        """
        if upstream_value is None:
            return default
        m = self._subconfig("statusMap")
        if upstream_value in m:
            return m[upstream_value]
        lower = {k.lower(): v for k, v in m.items()}
        return lower.get(upstream_value.strip().lower(), default)

    def _apply_priority_override(self, upstream_value: str | None,
                                  default: str) -> str:
        if upstream_value is None:
            return default
        m = self._subconfig("priorityMap")
        if upstream_value in m:
            return m[upstream_value]
        lower = {k.lower(): v for k, v in m.items()}
        return lower.get(upstream_value.strip().lower(), default)

    def _field_name(self, logical: str, default: str) -> str:
        """Resolve a service-side field name via `fieldMap.<logical>`.

        Honors both the dotted-key form (`fieldMap.dueDate = Tarih`) and
        the JSON-encoded form (`fieldMap = '{"dueDate":"Tarih"}'`) by
        going through `_subconfig`.
        """
        m = self._subconfig("fieldMap")
        # Case-insensitive on the logical key for parity with
        # CaseInsensitiveConfig elsewhere.
        for k, v in m.items():
            if k.lower() == logical.lower():
                return v
        return default

    # ---- Push-side shared helpers ----
    _CROSS_SOURCE_PREFIXES = ("jira-", "vikunja-", "mstodo-", "notion-")

    def _native_id(self, task_id: str) -> str | None:
        """Strip the `<scheme>-` prefix from a unified id.

        Returns:
          - The native id (everything after the prefix) for an id that
            belongs to this driver's scheme.
          - None for an id that has no recognised scheme prefix
            (interpreted as a brand-new task → create).

        Raises a driver-specific error class — caller-supplied via
        `_cross_source_error` — when the id explicitly belongs to a
        DIFFERENT scheme. That's a hard refusal: we never silently
        duplicate someone else's task into our service.
        """
        prefix = f"{self.SCHEME}-"
        if task_id.startswith(prefix):
            rest = task_id[len(prefix):]
            return rest or None
        for foreign in self._CROSS_SOURCE_PREFIXES:
            if foreign != prefix and task_id.startswith(foreign):
                raise self._cross_source_error()(
                    f"refusing to push {task_id!r} to {self.SCHEME!r} — "
                    f"task id belongs to a different service."
                )
        return None

    def _cross_source_error(self):
        """Driver-specific exception class for cross-source push refusal."""
        return RuntimeError


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


class JiraConfigError(RuntimeError):
    """Missing or invalid Jira driver configuration."""


class JiraPushError(RuntimeError):
    """Raised when a push is rejected before or by Jira."""


class JiraDriver(Driver):
    # Jira Cloud caps /search/jql at 100 per request.
    PAGE_SIZE_DEFAULT = 100
    PAGE_SIZE_MAX = 100
    SCHEME = "jira"

    def _cross_source_error(self):
        return JiraPushError

    def _auth_header(self) -> dict:
        email = self.config.get("email", "")
        token = self.config.get("apiToken", "")
        raw = f"{email}:{token}".encode("utf-8")
        return {
            "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "Accept": "application/json",
        }

    def normalize(self, issue: dict) -> dict:
        t = empty_task()
        fields = issue.get("fields") or {}
        key = issue.get("key") or ""
        t["id"] = f"jira-{key}" if key else ""
        t["source"] = "jira"
        t["title"] = fields.get("summary") or ""
        desc = fields.get("description")
        t["description"] = _jira_extract_adf_text(desc) if isinstance(desc, dict) else desc
        status_name_raw = ((fields.get("status") or {}).get("name") or "").strip()
        built_in = _JIRA_STATUS_MAP.get(status_name_raw.lower(), "todo")
        t["status"] = self._apply_status_override(status_name_raw, built_in)
        pri = (fields.get("priority") or {}).get("name") if fields.get("priority") else None
        if pri:
            built_in_pri = _JIRA_PRIORITY_MAP.get(pri.strip().lower(), "medium")
            t["priority"] = self._apply_priority_override(pri, built_in_pri)
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
        # BUG-08: only use a real HTTP(S) base; self.url is the git-remote
        # URL like `jira://company.atlassian.net` and would produce an
        # unclickable `jira://...` link.
        base = self.config.get("baseUrl") or ""
        if key and base.startswith(("http://", "https://")):
            t["url"] = f"{base.rstrip('/')}/browse/{key}"
        return t

    # Default JQL — satisfies the new /search/jql endpoint which requires
    # at least one condition. Operators who want a narrower view set
    # `tasks-remote.<name>.jql` (or `jqlFilter`) in git config.
    _DEFAULT_JQL = "created is not EMPTY ORDER BY updated DESC"

    def _user_jql(self) -> str:
        return (self.config.get("jql")
                or self.config.get("jqlFilter")
                or self._DEFAULT_JQL)

    @staticmethod
    def _strip_order_by(jql: str) -> str:
        idx = jql.upper().rfind("ORDER BY")
        return jql[:idx].rstrip() if idx >= 0 else jql

    def fetch_all(self) -> list[dict]:
        return [self.normalize(x) for x in self._paginate(jql=self._user_jql())]

    def fetch_changed(
        self, since: str | None
    ) -> tuple[list[dict], list[str], str | None]:
        """Incremental fetch via JQL `updated >= "<iso>"`.

        Jira has no native deletion feed, so deleted_ids is always [].
        Operators who need to GC removed issues should run a periodic
        full fetch (`git config tasks-remote.<name>.sync.mode full`).
        """
        base = self._user_jql()
        if since is None:
            issues = self._paginate(jql=base)
        else:
            jira_ts = since.replace("T", " ").replace("Z", "").split(".")[0]
            base_no_order = self._strip_order_by(base)
            jql = (f'({base_no_order}) AND updated >= "{jira_ts}" '
                    f'ORDER BY updated ASC')
            issues = self._paginate(jql=jql)
        tasks = [self.normalize(x) for x in issues]
        return tasks, [], self._now_iso()

    def _paginate(self, jql: str) -> list[dict]:
        """Paginate over Jira issues.

        K2-01: Jira Cloud deprecated `/rest/api/3/search` in May 2024 in
        favour of `/rest/api/3/search/jql`. The new endpoint returns
        `nextPageToken` / `isLast` instead of `startAt` / `total`. We try
        the new endpoint first; on 410 / 404 we fall back to the legacy
        shape so self-hosted Jira Data Center instances (which still run
        the old route) keep working.
        """
        base = self.config.get("baseUrl") or self.url
        if not base:
            raise NotImplementedError("Jira baseUrl is not configured")
        base = base.rstrip("/")
        headers = self._auth_header()
        issues: list[dict] = []

        if self.config.get("searchEndpoint", "jql") == "legacy":
            return self._paginate_legacy(base, headers, jql)

        # The new /search/jql endpoint returns only `id` unless `fields` is
        # specified. Request every field we map in normalize() plus the
        # epic custom field, explicitly rather than relying on `*all`.
        fields = (
            "summary,description,status,priority,created,updated,duedate,"
            "labels,project,customfield_10014"
        )
        page_size = self._page_size()
        next_token: str | None = None
        while True:
            qs = (f"jql={urllib.parse.quote(jql)}"
                  f"&fields={urllib.parse.quote(fields)}"
                  f"&maxResults={page_size}")
            if next_token:
                qs += f"&nextPageToken={urllib.parse.quote(next_token)}"
            url = f"{base}/rest/api/3/search/jql?{qs}"
            try:
                data = self._http_get(url, headers=headers)
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    return self._paginate_legacy(base, headers, jql)
                raise
            page = data.get("issues") or []
            issues.extend(page)
            next_token = data.get("nextPageToken")
            if data.get("isLast") or not next_token or not page:
                break
        return issues

    def _paginate_legacy(self, base: str, headers: dict, jql: str
                          ) -> list[dict]:
        start_at = 0
        max_results = self._page_size()
        issues: list[dict] = []
        while True:
            url = (
                f"{base}/rest/api/3/search"
                f"?jql={urllib.parse.quote(jql)}"
                f"&startAt={start_at}&maxResults={max_results}"
            )
            data = self._http_get(url, headers=headers)
            page = data.get("issues") or []
            issues.extend(page)
            total = data.get("total", 0)
            start_at += len(page)
            if not page or start_at >= total:
                break
        return issues

    # ---- Push ----
    _STATUS_TO_JIRA_TRANSITION = {
        "todo": "To Do",
        "in_progress": "In Progress",
        "done": "Done",
        "cancelled": "Cancelled",
    }
    _PRIORITY_TO_JIRA = {
        "critical": "Highest", "high": "High", "medium": "Medium",
        "low": "Low", "none": None,
    }

    def _base_url(self) -> str:
        base = self.config.get("baseUrl") or self.url
        if not base:
            raise JiraConfigError("Jira baseUrl is not configured")
        return base.rstrip("/")

    def _serialize_for_push(self, task: dict) -> dict:
        """Map unified task → Jira /issue fields payload (edit-shape).

        Description uses ADF document-of-a-single-paragraph for multi-line
        support; single-line descriptions still ride the same wrapper.
        """
        fields: dict = {}
        if task.get("title"):
            fields["summary"] = task["title"]
        desc = task.get("description")
        if desc:
            # K2-03: emit one ADF paragraph per source line, including empty
            # ones so blank separators survive the round-trip.
            fields["description"] = {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph",
                     "content": [{"type": "text", "text": line}] if line
                                 else []}
                    for line in desc.split("\n")
                ],
            }
        pri = self._PRIORITY_TO_JIRA.get(task.get("priority") or "none")
        if pri:
            fields["priority"] = {"name": pri}
        if task.get("due_date"):
            fields["duedate"] = (task["due_date"].split("T")[0]
                                  if "T" in task["due_date"]
                                  else task["due_date"])
        if task.get("tags") is not None:
            fields["labels"] = list(task["tags"])
        return fields

    def _project_key(self) -> str | None:
        """Return the Jira project key used for create; prefer explicit config."""
        return self.config.get("projectKey")

    def upsert(self, task: dict) -> None:
        base = self._base_url()
        headers = self._auth_header()
        headers.setdefault("Accept", "application/json")
        native = self._native_id(task.get("id") or "")
        fields = self._serialize_for_push(task)
        if native:
            # Update existing issue.
            self._http_put(f"{base}/rest/api/3/issue/{native}",
                            body={"fields": fields}, headers=headers)
            # Status is a transition, not a field edit.
            self._transition(base, native, task.get("status"), headers)
            return
        # Create.
        project = self._project_key()
        if not project:
            raise JiraPushError(
                "projectKey is required to create new Jira issues; "
                "set tasks-remote.<name>.projectKey"
            )
        fields["project"] = {"key": project}
        fields.setdefault("issuetype", {"name": "Task"})
        self._http_post(f"{base}/rest/api/3/issue",
                         body={"fields": fields}, headers=headers)

    def _transition(self, base: str, key: str, status: str | None,
                    headers: dict) -> None:
        if not status:
            return
        target_name = self._STATUS_TO_JIRA_TRANSITION.get(status)
        if not target_name:
            return
        # Look up available transitions; Jira requires an id, not a name.
        trans = self._http_get(
            f"{base}/rest/api/3/issue/{key}/transitions",
            headers=headers,
        ).get("transitions") or []
        for t in trans:
            tname = ((t.get("to") or {}).get("name")
                      or t.get("name") or "").strip()
            if tname.lower() == target_name.lower():
                self._http_post(
                    f"{base}/rest/api/3/issue/{key}/transitions",
                    body={"transition": {"id": t.get("id")}},
                    headers=headers,
                )
                return
        # Transition not offered for this issue's workflow — not an error,
        # but worth noting in the helper log so the operator can diagnose.
        # R-10: name the half-applied state explicitly so the failure is
        # actionable (the field PUT already landed; only the workflow move
        # is missing).
        raise JiraPushError(
            f"fields updated, but no Jira transition to {target_name!r} "
            f"is available for {key} on its current workflow"
        )

    def delete(self, task_id: str) -> None:
        base = self._base_url()
        native = self._native_id(task_id)
        if not native:
            raise JiraPushError(
                f"cannot delete {task_id!r}: not a Jira-sourced id"
            )
        try:
            self._http_delete(f"{base}/rest/api/3/issue/{native}",
                               headers=self._auth_header())
        except urllib.error.HTTPError as exc:
            # R-11: idempotent delete. The local tree has already removed
            # the file; failing here is hostile when the issue was deleted
            # upstream by someone else (or never existed). 404 / 410 are
            # both "gone" outcomes — accept them as success.
            if exc.code in (404, 410):
                return
            raise


# ---------- Vikunja ---------------------------------------------------------

# K2-02: Vikunja's native priority encoding matches the enum in
# https://vikunja.io/docs/api-reference/#tag/task/paths/~1tasks~1all/get
#   0 = none, 1 = low, 2 = medium, 3 = high, 4 = urgent, 5 = do now
# We fold the two highest buckets into "critical" so the unified schema
# keeps its five-level shape. The inverse map used by push picks 4 for
# critical (the common "urgent" case); operators who want "do now" can
# override via priorityMap.
_VIKUNJA_PRIORITY_MAP = {0: "none", 1: "low", 2: "medium", 3: "high",
                         4: "critical", 5: "critical"}
_VIKUNJA_PRIORITY_MAP_INV = {"none": 0, "low": 1, "medium": 2,
                              "high": 3, "critical": 4}


class VikunjaConfigError(RuntimeError):
    """Missing or invalid Vikunja driver configuration."""


class VikunjaPushError(RuntimeError):
    """Raised when a push is rejected before we even talk to Vikunja."""


class VikunjaDriver(Driver):
    # Vikunja accepts up to 250 per page on typical deployments; 100 is a
    # safe default that still halves round-trips vs the previous 50.
    PAGE_SIZE_DEFAULT = 100
    PAGE_SIZE_MAX = 250
    SCHEME = "vikunja"

    def _cross_source_error(self):
        return VikunjaPushError

    def _auth_header(self) -> dict:
        token = self.config.get("apiToken", "")
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def normalize(self, task: dict) -> dict:
        t = empty_task()
        tid = task.get("id")
        t["id"] = f"vikunja-{tid}" if tid is not None else ""
        t["source"] = "vikunja"
        t["title"] = task.get("title") or ""
        t["description"] = task.get("description") or None
        # Vikunja's `done` boolean is our status. The statusMap override
        # still applies to 'done' / 'todo' literals in case the user wants
        # a different unified value.
        default_status = "done" if task.get("done") else "todo"
        t["status"] = self._apply_status_override(default_status, default_status)
        pri = task.get("priority")
        if pri is not None:
            built_in_pri = _VIKUNJA_PRIORITY_MAP.get(pri, "none")
            t["priority"] = self._apply_priority_override(str(pri), built_in_pri)
        else:
            t["priority"] = "none"
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
        return [self.normalize(x) for x in self._paginate(filter_expr=None)]

    def fetch_changed(
        self, since: str | None
    ) -> tuple[list[dict], list[str], str | None]:
        """Incremental fetch via Vikunja's `filter=updated > '<iso>'`.

        Like Jira, Vikunja has no native deletion feed — operators who
        care about upstream deletions should alternate with a full fetch.
        """
        filter_expr = None
        if since is not None:
            # Vikunja's filter syntax accepts ISO strings quoted as string
            # literals; it tolerates the trailing Z.
            filter_expr = f"updated > '{since}'"
        rows = self._paginate(filter_expr=filter_expr)
        tasks = [self.normalize(x) for x in rows]
        return tasks, [], self._now_iso()

    def _paginate(self, filter_expr: str | None) -> list[dict]:
        """Vikunja's tokens are scope-limited. `/api/v1/tasks/all` needs the
        `tasks.read_all` scope which not every token has; `/api/v1/tasks`
        returns the caller-visible task list without that requirement.
        We prefer the broader path when the token supports it.
        """
        base = self.config.get("baseUrl") or self.url
        if not base:
            raise NotImplementedError("Vikunja baseUrl is not configured")
        headers = self._auth_header()
        page = 1
        per_page = self._page_size()
        tasks: list[dict] = []
        path = "/api/v1/tasks"
        while True:
            query = f"page={page}&per_page={per_page}"
            if filter_expr is not None:
                query += "&filter=" + urllib.parse.quote(filter_expr)
            url = f"{base.rstrip('/')}{path}?{query}"
            try:
                data = self._http_get(url, headers=headers)
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 403, 404) and path != "/api/v1/tasks/all":
                    # Token may only permit /tasks/all — try that once.
                    path = "/api/v1/tasks/all"
                    tasks.clear()
                    page = 1
                    continue
                raise
            if not isinstance(data, list):
                data = data.get("tasks", []) if isinstance(data, dict) else []
            tasks.extend(data)
            if len(data) < per_page:
                break
            page += 1
        return tasks

    # ---- Push ----
    def _base_url(self) -> str:
        base = self.config.get("baseUrl") or self.url
        if not base:
            raise VikunjaConfigError("Vikunja baseUrl is not configured")
        return base.rstrip("/")

    def _serialize_for_push(self, task: dict) -> dict:
        payload: dict = {
            "title": task.get("title") or "",
            "description": task.get("description") or "",
            "done": task.get("status") == "done",
        }
        pri = _VIKUNJA_PRIORITY_MAP_INV.get(task.get("priority") or "none")
        if pri is not None:
            payload["priority"] = pri
        due = task.get("due_date")
        if due:
            payload["due_date"] = due if "T" in due else f"{due}T00:00:00Z"
        return payload

    def upsert(self, task: dict) -> None:
        base = self._base_url()
        headers = self._auth_header()
        native = self._native_id(task.get("id") or "")
        payload = self._serialize_for_push(task)
        if native and native.isdigit():
            self._http_post(f"{base}/api/v1/tasks/{native}",
                             body=payload, headers=headers)
            return
        if native and not native.isdigit():
            raise VikunjaPushError(
                f"task id {task.get('id')!r} is not a numeric Vikunja id"
            )
        # Create path — needs a default project.
        project_id = self.config.get("projectId")
        if not project_id:
            raise VikunjaPushError(
                "projectId is required to create new Vikunja tasks; "
                "set tasks-remote.<name>.projectId"
            )
        self._http_put(f"{base}/api/v1/projects/{project_id}/tasks",
                        body=payload, headers=headers)

    def delete(self, task_id: str) -> None:
        base = self._base_url()
        native = self._native_id(task_id)
        if not native or not native.isdigit():
            raise VikunjaPushError(
                f"cannot delete {task_id!r}: not a Vikunja-sourced id"
            )
        try:
            self._http_delete(f"{base}/api/v1/tasks/{native}",
                               headers=self._auth_header())
        except urllib.error.HTTPError as exc:
            # R-11: idempotent delete — see JiraDriver.delete.
            if exc.code in (404, 410):
                return
            raise


# ---------- MS Todo ---------------------------------------------------------

_MSTODO_STATUS_MAP = {
    "notStarted": "todo", "inProgress": "in_progress",
    "completed": "done", "deferred": "todo", "waitingOnOthers": "todo",
}
_MSTODO_PRIORITY_MAP = {"high": "high", "normal": "medium", "low": "low"}


class MSTodoPushError(RuntimeError):
    """Raised when a push is rejected before or by MS Todo."""


class MSTodoDriver(Driver):
    SCHEME = "mstodo"

    def _cross_source_error(self):
        return MSTodoPushError

    _GRAPH_BASE = "https://graph.microsoft.com/v1.0"
    _MSAL_SCOPES = ("Tasks.ReadWrite",)

    def _auth_header(self) -> dict:
        token = self._acquire_token()
        return {"Authorization": f"Bearer {token}"}

    def _acquire_token(self) -> str:
        """Return a live Graph bearer.

        Priority:
          1. `accessToken` in config — pre-provisioned bearer, unchanged.
          2. `refreshToken` in config + MSAL installed — silent refresh.
          3. MSAL installed + `clientId` / `tenantId` — device code flow,
             prompting the user on stderr. Persists the refresh token on
             success so subsequent runs path 2.

        Without any of these, raise NotImplementedError with a clear hint.
        """
        token = self.config.get("accessToken")
        if token:
            return token
        if not MSAL_AVAILABLE:
            raise NotImplementedError(
                "MS Todo auth requires either accessToken in git config or the "
                "optional msal package (pip install msal) plus clientId/tenantId."
            )
        client_id = self.config.get("clientId")
        tenant_id = self.config.get("tenantId") or "consumers"
        if not client_id:
            raise NotImplementedError(
                "MS Todo auth needs tasks-remote.<name>.clientId "
                "(Azure AD app registration)."
            )
        app = msal.PublicClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        scopes = list(self._MSAL_SCOPES)
        refresh = self.config.get("refreshToken")
        if refresh:
            result = app.acquire_token_by_refresh_token(refresh, scopes=scopes)
            if result and "access_token" in result:
                self._store_refresh(result.get("refresh_token") or refresh)
                return result["access_token"]
        # Device-code fall-through. Prompt on stderr so the user sees it
        # whether this runs under `git fetch` (stderr is TTY) or directly.
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise NotImplementedError(
                f"MSAL device flow init failed: {flow!r}"
            )
        print(flow.get("message") or
              f"Visit {flow.get('verification_uri')} and enter {flow['user_code']}",
              file=sys.stderr, flush=True)
        # E2-03: cap the device-code wait so `git fetch` never hangs
        # indefinitely when the user walks away. `flow["expires_at"]` is an
        # ABSOLUTE epoch timestamp (seconds since 1970) — MSAL stops
        # polling once `time.time() > expires_at`. The previous version
        # assigned `expires_in` (a small duration) here, which evaluated
        # to "expired in January 1970" and made MSAL bail on its first
        # poll with an 'authorization_pending' error even when the user
        # had already approved. `initiate_device_flow` already sets
        # `expires_at` to a correct absolute deadline (now + upstream
        # `expires_in`, typically 900s); we only clamp below that when a
        # shorter per-remote ceiling is configured.
        cap_raw = self.config.get("deviceFlowTimeout")
        if cap_raw:
            try:
                cap = float(cap_raw)
            except (TypeError, ValueError):
                cap = 0.0
            if cap > 0:
                flow["expires_at"] = min(
                    float(flow.get("expires_at") or (time.time() + cap)),
                    time.time() + cap,
                )
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise NotImplementedError(
                f"MSAL device flow failed: {result.get('error_description') or result}"
            )
        if result.get("refresh_token"):
            self._store_refresh(result["refresh_token"])
        return result["access_token"]

    def _store_refresh(self, refresh_token: str) -> None:
        # E2-04: surface write failures so operators don't silently re-do
        # the device flow every run without understanding why.
        key = f"tasks-remote.{self.remote_name}.refreshToken"
        if not write_config_value(key, refresh_token):
            print(
                "git-remote-tasks: warning[msal-persist]: could not save "
                "refresh token; next run will prompt the device flow again.",
                file=sys.stderr, flush=True,
            )

    def normalize(self, task: dict, list_name: str | None = None) -> dict:
        t = empty_task()
        tid = task.get("id")
        t["id"] = f"mstodo-{tid}" if tid else ""
        t["source"] = "mstodo"
        t["title"] = task.get("title") or ""
        body = task.get("body") or {}
        t["description"] = body.get("content") if isinstance(body, dict) else None
        status_raw = task.get("status", "")
        built_in_status = _MSTODO_STATUS_MAP.get(status_raw, "todo")
        t["status"] = self._apply_status_override(status_raw, built_in_status)
        importance_raw = task.get("importance", "")
        built_in_pri = _MSTODO_PRIORITY_MAP.get(importance_raw, "none")
        t["priority"] = self._apply_priority_override(importance_raw, built_in_pri)
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
        changed, _, _ = self._fetch_with_optional_delta(use_delta=False)
        return changed

    def fetch_changed(
        self, since: str | None
    ) -> tuple[list[dict], list[str], str | None]:
        """Incremental fetch via Graph delta query.

        FEAT-06b. Graph's `/me/todo/lists/{id}/tasks/delta` returns a
        `@odata.deltaLink` URL that, when followed on the next run,
        returns only tasks that changed since the previous link was
        issued. Removed tasks come back as objects with `@removed`
        annotation — we emit those as `D` directives.

        State is persisted as one git config key per list:

            tasks-remote.<name>.sync.deltaLink.<listId> = <opaque-url>

        On `since=None` (first fetch) we also use delta so that the
        next call has a token; this matches Microsoft's recommendation
        to seed with a delta call rather than a plain GET.
        """
        return self._fetch_with_optional_delta(use_delta=True)

    def _fetch_with_optional_delta(
        self, use_delta: bool,
    ) -> tuple[list[dict], list[str], str | None]:
        if not MSAL_AVAILABLE and not self.config.get("accessToken"):
            raise NotImplementedError(
                "MS Todo requires MSAL device-code auth or a preconfigured accessToken"
            )
        headers = self._auth_header()
        lists_url = f"{self._GRAPH_BASE}/me/todo/lists"
        lists = self._http_get(lists_url, headers=headers).get("value", [])
        changed: list[dict] = []
        deleted_ids: list[str] = []
        for lst in lists:
            lid = lst.get("id") or ""
            lname = lst.get("displayName")
            if not lid:
                continue
            page_url, link_key = self._delta_starting_url(lid, use_delta)
            new_link = self._consume_delta_pages(
                page_url, headers, lname, lid, changed, deleted_ids,
                use_delta=use_delta,
            )
            if use_delta and new_link:
                self._persist_delta_link(link_key, new_link)
        return changed, deleted_ids, self._now_iso()

    def _delta_starting_url(self, list_id: str,
                             use_delta: bool) -> tuple[str, str]:
        """Return (url, config-key-for-deltalink) for `list_id`.

        Persisted delta links are stored case-insensitively under
        `sync.deltaLink.<listId-sanitized>`. The list id is base64-ish
        and may contain `=` / `_` which git config rejects in variable
        names, so we hex-encode for the key form.
        """
        # Git config variable names must start with a letter. Hex-encoded
        # list ids usually start with a digit, which made every write
        # `invalid key: ...` and effectively disabled delta persistence.
        # Use `-` as the final separator (keeps the variable name as
        # `deltaLink-<hex>`, starting with `d`) and a per-run letter
        # prefix on the hex itself for defence in depth.
        list_safe = "l" + list_id.encode("utf-8").hex()
        key = f"sync.deltaLink-{list_safe}"
        stored = self.config.get(key) if use_delta else None
        if stored:
            return stored, key
        if use_delta:
            return (
                f"{self._GRAPH_BASE}/me/todo/lists/"
                f"{urllib.parse.quote(list_id, safe='')}/tasks/delta",
                key,
            )
        return (
            f"{self._GRAPH_BASE}/me/todo/lists/"
            f"{urllib.parse.quote(list_id, safe='')}/tasks",
            key,
        )

    def _consume_delta_pages(self, url: str, headers: dict,
                              list_name: str | None, list_id: str,
                              changed: list[dict], deleted_ids: list[str],
                              use_delta: bool) -> str | None:
        """Walk `@odata.nextLink`s and collect changed + removed tasks.

        Returns the final `@odata.deltaLink` (only present on the last
        page of a delta sequence) so the caller can persist it for the
        next run. Plain non-delta fetches return None.
        """
        last_delta_link: str | None = None
        while url:
            data = self._http_get(url, headers=headers)
            for raw in data.get("value", []):
                if raw.get("@removed"):
                    rid = raw.get("id")
                    if rid:
                        deleted_ids.append(f"mstodo-{rid}")
                    continue
                changed.append(self.normalize(raw, list_name=list_name))
            url = data.get("@odata.nextLink") or ""
            link = data.get("@odata.deltaLink")
            if link:
                last_delta_link = link
        return last_delta_link

    def _persist_delta_link(self, key: str, value: str) -> None:
        config_key = f"tasks-remote.{self.remote_name}.{key}"
        if not write_config_value(config_key, value):
            return
        # Reflect the live update so a subsequent call in the same run
        # picks the persisted link.
        self.config[key] = value

    # ---- Push ----
    _STATUS_TO_MSTODO = {
        "todo": "notStarted", "in_progress": "inProgress",
        "done": "completed", "cancelled": "notStarted",  # no cancelled state
    }
    _PRIORITY_TO_MSTODO = {
        "critical": "high", "high": "high", "medium": "normal",
        "low": "low", "none": "normal",
    }

    def _list_id_for(self, task: dict) -> str | None:
        """Resolve the parent list id for an upsert.

        Prefer the task's category.id (populated on pull). Fall back to the
        per-remote defaultListId. Returning None means caller must refuse.
        """
        cat = task.get("category") or {}
        return cat.get("id") or self.config.get("defaultListId")

    def _serialize_for_push(self, task: dict) -> dict:
        payload: dict = {
            "title": task.get("title") or "",
            "status": self._STATUS_TO_MSTODO.get(
                task.get("status") or "todo", "notStarted"),
            "importance": self._PRIORITY_TO_MSTODO.get(
                task.get("priority") or "none", "normal"),
        }
        desc = task.get("description")
        if desc:
            payload["body"] = {"content": desc, "contentType": "text"}
        if task.get("due_date"):
            payload["dueDateTime"] = {
                "dateTime": (task["due_date"] if "T" in task["due_date"]
                             else f"{task['due_date']}T00:00:00"),
                "timeZone": "UTC",
            }
        if task.get("tags"):
            payload["categories"] = list(task["tags"])
        return payload

    def upsert(self, task: dict) -> None:
        # Resolve the id FIRST so a cross-source push fails synchronously
        # (no MSAL prompt, no network).
        native = self._native_id(task.get("id") or "")
        list_id = self._list_id_for(task)
        if not list_id:
            raise MSTodoPushError(
                "cannot resolve MS Todo list id; pull first so category.id "
                "is populated or set tasks-remote.<name>.defaultListId"
            )
        headers = self._auth_header()
        payload = self._serialize_for_push(task)
        lid = urllib.parse.quote(list_id, safe="")
        if native:
            tid = urllib.parse.quote(native, safe="")
            self._http_patch(
                f"{self._GRAPH_BASE}/me/todo/lists/{lid}/tasks/{tid}",
                body=payload, headers=headers,
            )
            return
        self._http_post(
            f"{self._GRAPH_BASE}/me/todo/lists/{lid}/tasks",
            body=payload, headers=headers,
        )

    def delete(self, task_id: str) -> None:
        native = self._native_id(task_id)
        if not native:
            raise MSTodoPushError(
                f"cannot delete {task_id!r}: not a MS Todo-sourced id"
            )
        list_id = self.config.get("defaultListId")
        if not list_id:
            raise MSTodoPushError(
                "MS Todo delete needs tasks-remote.<name>.defaultListId "
                "because the removed task file no longer carries the list id"
            )
        headers = self._auth_header()
        lid = urllib.parse.quote(list_id, safe="")
        tid = urllib.parse.quote(native, safe="")
        try:
            self._http_delete(
                f"{self._GRAPH_BASE}/me/todo/lists/{lid}/tasks/{tid}",
                headers=headers,
            )
        except urllib.error.HTTPError as exc:
            # R-11: idempotent delete — see JiraDriver.delete.
            if exc.code in (404, 410):
                return
            raise


# ---------- Notion ----------------------------------------------------------

# Native-looking option names we expect to see in Notion databases. Exact
# matches are case-insensitive; anything else the user can remap through
# statusMap / priorityMap.
_NOTION_STATUS_MAP = {
    "todo": "todo", "to do": "todo", "to-do": "todo",
    "not started": "todo", "backlog": "todo",
    "in progress": "in_progress", "in-progress": "in_progress",
    "today": "in_progress", "doing": "in_progress",
    "done": "done", "completed": "done", "shipped": "done",
    "cancelled": "cancelled", "canceled": "cancelled",
}
_NOTION_PRIORITY_MAP = {
    "critical": "critical", "urgent": "critical", "p0": "critical",
    "high": "high", "p1": "high",
    "medium": "medium", "normal": "medium", "p2": "medium",
    "low": "low", "p3": "low",
    "lowest": "low",
}


class NotionPushError(RuntimeError):
    """Raised when a push is rejected before or by Notion."""


class NotionDriver(Driver):
    SCHEME = "notion"

    def _cross_source_error(self):
        return NotionPushError

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

    # Logical → git-config-safe variable name. Git config does not
    # accept underscores in the variable segment, so multi-word logical
    # fields are addressed in camelCase. A user therefore writes
    # `fieldMap.dueDate = Tarih` (not `fieldMap.due_date`).
    _FIELD_LOGICAL_TO_CONFIG = {
        "status":      "status",
        "priority":    "priority",
        "tags":        "tags",
        "due_date":    "dueDate",
        "description": "description",
        "done":        "done",
    }

    def _prop_names(self) -> dict:
        """Return the service-side property names for each logical field.

        Each value can be overridden via `fieldMap.<config>` in the remote
        config — e.g. `fieldMap.dueDate = Tarih`. The lookup is
        case-insensitive (CaseInsensitiveConfig) so casing of the config
        key doesn't matter.
        """
        out = {}
        defaults = {"status": "Status", "priority": "Priority",
                    "tags": "Tags", "due_date": "Due",
                    "description": "Description", "done": "Done"}
        for logical, default in defaults.items():
            cfg_key = self._FIELD_LOGICAL_TO_CONFIG.get(logical, logical)
            out[logical] = self._field_name(cfg_key, default)
        return out

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
        wanted = {k: v.lower() for k, v in self._prop_names().items()}
        status_map = _NOTION_STATUS_MAP
        priority_map = _NOTION_PRIORITY_MAP
        for name, p in props.items():
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            lname = name.lower()
            if ptype in ("select", "status"):
                key = "select" if ptype == "select" else "status"
                val = p.get(key) or {}
                name_val = val.get("name") if val else None
                if lname == wanted["status"] and name_val:
                    mapped = status_map.get(name_val.lower()) or "todo"
                    t["status"] = self._apply_status_override(name_val, mapped)
                elif lname == wanted["priority"] and name_val:
                    mapped_pri = priority_map.get(name_val.lower(), "none")
                    t["priority"] = self._apply_priority_override(name_val, mapped_pri)
            elif ptype == "multi_select" and lname == wanted["tags"]:
                t["tags"] = [m.get("name", "") for m in (p.get("multi_select") or [])]
            elif ptype == "date" and lname == wanted["due_date"]:
                d = p.get("date") or {}
                t["due_date"] = d.get("start")
            elif ptype == "checkbox" and lname == wanted["done"]:
                t["status"] = "done" if p.get("checkbox") else t["status"]
            elif ptype == "rich_text" and lname == wanted["description"]:
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
        changed, _, _ = self._query_pages(since=None,
                                            include_archived=False)
        return changed

    def fetch_changed(
        self, since: str | None,
    ) -> tuple[list[dict], list[str], str | None]:
        """Incremental fetch via Notion `last_edited_time` filter.

        FEAT-06c. The query body is:

            {"filter":
              {"timestamp": "last_edited_time",
               "last_edited_time": {"on_or_after": "<iso>"}},
             "sorts": [{"timestamp": "last_edited_time",
                         "direction": "ascending"}]}

        Notion returns ARCHIVED pages too when `archived` filter is
        omitted; we promote those into `deleted_ids` so a `git rm`
        equivalent flows back into the local tree.

        Setting `since=None` falls back to a full fetch and returns the
        current timestamp as the next-since token.
        """
        return self._query_pages(since=since, include_archived=True)

    def _query_pages(self, since: str | None, include_archived: bool,
                      ) -> tuple[list[dict], list[str], str | None]:
        database_id = self.config.get("databaseId")
        if not database_id:
            raise NotImplementedError("Notion databaseId is not configured")
        headers = self._auth_header()
        url = f"{self._NOTION_BASE}/databases/{database_id}/query"
        changed: list[dict] = []
        deleted_ids: list[str] = []
        cursor: str | None = None
        db_title = self.config.get("databaseTitle")
        while True:
            body: dict = {}
            if cursor:
                body["start_cursor"] = cursor
            if since:
                body["filter"] = {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"on_or_after": since},
                }
                body["sorts"] = [{
                    "timestamp": "last_edited_time",
                    "direction": "ascending",
                }]
            data = self._http_post(url, body=body, headers=headers)
            for page in data.get("results", []):
                pid = page.get("id") or ""
                if page.get("archived"):
                    # Archived pages are hidden in the Notion UI; never
                    # surface them as live tasks. Only the incremental
                    # path promotes them into deleted_ids so a `git rm`
                    # equivalent flows back into the local tree.
                    if include_archived and pid:
                        deleted_ids.append(f"notion-{pid}")
                    continue
                changed.append(self.normalize(page, db_title=db_title))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            # R-05: defensive break. Notion's docs say `has_more=True` always
            # comes with a `next_cursor`, but during eventual-consistency
            # windows we have observed `next_cursor=None` paired with
            # `has_more=True`. Without this guard the loop re-issues the same
            # page indefinitely (start_cursor=None == "first page") and
            # `git fetch` hangs.
            if not cursor:
                break
        return changed, deleted_ids, self._now_iso()

    # ---- Push ----
    _NOTION_BASE = "https://api.notion.com/v1"
    _STATUS_TO_NOTION = {
        "todo": "To Do", "in_progress": "In Progress",
        "done": "Done", "cancelled": "Cancelled",
    }
    _PRIORITY_TO_NOTION = {
        "critical": "Critical", "high": "High", "medium": "Medium",
        "low": "Low", "none": None,
    }

    def _discover_schema(self, database_id: str, headers: dict) -> dict:
        """Return `{column_name: column_type}` for the target database.

        Notion has both `select` and the newer `status` column type, with
        different push payload shapes. We need to know per-column to avoid
        400s. Cached on the instance for the duration of one run.
        """
        cached = getattr(self, "_schema_cache", None)
        if cached is not None:
            return cached
        data = self._http_get(
            f"{self._NOTION_BASE}/databases/{database_id}",
            headers=headers,
        )
        schema: dict[str, str] = {}
        for name, prop in (data.get("properties") or {}).items():
            if isinstance(prop, dict):
                schema[name] = prop.get("type") or ""
        self._schema_cache = schema
        return schema

    def _discover_title_prop(self, database_id: str, headers: dict) -> str:
        schema = self._discover_schema(database_id, headers)
        for name, ptype in schema.items():
            if ptype == "title":
                return name
        raise NotionPushError(
            f"Notion database {database_id} has no title property"
        )

    def _invert_map(self, prefix: str) -> dict[str, str]:
        """Return `{unified: upstream}` from a user-configured map.

        Used on push so a database whose Status column reads
        ('Not started', 'Today', 'Done') round-trips through statusMap
        entries the operator already configured for pull, instead of us
        guessing a universal vocabulary that doesn't exist on the column.
        """
        out: dict[str, str] = {}
        for upstream, unified in self._subconfig(prefix).items():
            out.setdefault(unified, upstream)
        return out

    def _build_properties(self, task: dict, title_prop: str,
                           schema: dict) -> dict:
        """Render a unified task into Notion's properties shape.

        Service-side column names come from `_prop_names()` so push and
        pull use the same `fieldMap.*` overrides — no risk of writing to
        "Status" while reading from "State". Each column's payload shape
        is chosen from the discovered schema (status vs select vs
        multi_select etc.) so we don't 400 on database-specific
        configuration.
        """
        names = self._prop_names()
        props: dict = {
            title_prop: {
                "title": [{"type": "text",
                            "text": {"content": task.get("title") or ""}}]
            }
        }

        def _select_payload(col: str, value: str) -> dict | None:
            # R-06: when the database has no column with this name we used
            # to default `ptype` to "select" and emit a select payload that
            # Notion 400s on. Skip the field instead, with a one-shot
            # warning so the operator notices the silent drop.
            ptype = schema.get(col)
            if ptype is None:
                self._warn_once(
                    f"notion-missing-col:{col}",
                    f"database has no column {col!r}; field skipped on push.",
                )
                return None
            if ptype == "status":
                return {"status": {"name": value}}
            if ptype == "select":
                return {"select": {"name": value}}
            self._warn_once(
                f"notion-bad-col-shape:{col}",
                f"column {col!r} has type {ptype!r}; expected select/status. "
                f"Field skipped on push.",
            )
            return None

        # Prefer the inverse of the user's statusMap / priorityMap when
        # present — Notion column options are database-specific, so the
        # configured pull mapping is the authoritative source for push.
        status_inv = self._invert_map("statusMap")
        priority_inv = self._invert_map("priorityMap")
        status_unified = task.get("status") or "todo"
        status = status_inv.get(status_unified) \
                  or self._STATUS_TO_NOTION.get(status_unified)
        if status:
            payload = _select_payload(names["status"], status)
            if payload:
                props[names["status"]] = payload
        pri_unified = task.get("priority") or "none"
        pri = priority_inv.get(pri_unified) \
              or self._PRIORITY_TO_NOTION.get(pri_unified)
        if pri:
            payload = _select_payload(names["priority"], pri)
            if payload:
                props[names["priority"]] = payload
        # R-07: tags column may be `multi_select` (the common case) or a
        # `select` (single value) on databases that haven't been migrated.
        # Multi-select pushes the full list; single-select pushes only the
        # first tag and warns; missing/other types warn and drop. No silent
        # data loss either way.
        tags = task.get("tags") or []
        if tags:
            tcol = names["tags"]
            tcol_type = schema.get(tcol)
            if tcol_type == "multi_select":
                props[tcol] = {
                    "multi_select": [{"name": t} for t in tags]
                }
            elif tcol_type == "select":
                self._warn_once(
                    f"notion-tags-shape:{tcol}",
                    f"column {tcol!r} is select (single value); pushing only "
                    f"the first tag {tags[0]!r}.",
                )
                props[tcol] = {"select": {"name": tags[0]}}
            else:
                self._warn_once(
                    f"notion-tags-shape:{tcol}",
                    f"column {tcol!r} type is {tcol_type!r}; tags dropped on push.",
                )
        if task.get("due_date"):
            dcol = names["due_date"]
            if schema.get(dcol) == "date":
                props[dcol] = {"date": {"start": task["due_date"]}}
            else:
                self._warn_once(
                    f"notion-due-shape:{dcol}",
                    f"column {dcol!r} type is {schema.get(dcol)!r}; "
                    f"due date dropped on push.",
                )
        desc = task.get("description")
        if desc:
            ccol = names["description"]
            if schema.get(ccol) == "rich_text":
                props[ccol] = {
                    "rich_text": [{"type": "text",
                                    "text": {"content": desc}}]
                }
            else:
                self._warn_once(
                    f"notion-desc-shape:{ccol}",
                    f"column {ccol!r} type is {schema.get(ccol)!r}; "
                    f"description dropped on push.",
                )
        return props

    def upsert(self, task: dict) -> None:
        database_id = self.config.get("databaseId")
        if not database_id:
            raise NotionPushError("Notion databaseId is not configured")
        # Validate the id (raises on cross-source) BEFORE doing any
        # network IO so a refused push is a synchronous failure.
        native = self._native_id(task.get("id") or "")
        headers = self._auth_header()
        headers["Content-Type"] = "application/json"
        schema = self._discover_schema(database_id, headers)
        title_prop = self._discover_title_prop(database_id, headers)
        props = self._build_properties(task, title_prop, schema)
        if native:
            # Update existing page. Notion PATCH on /pages/{id} expects
            # { "properties": {...} } and optionally { "archived": false }.
            pid = urllib.parse.quote(native, safe="")
            self._http_patch(
                f"{self._NOTION_BASE}/pages/{pid}",
                body={"properties": props, "archived": False},
                headers=headers,
            )
            return
        # Create new page in the target database.
        self._http_post(
            f"{self._NOTION_BASE}/pages",
            body={
                "parent": {"database_id": database_id},
                "properties": props,
            },
            headers=headers,
        )

    def delete(self, task_id: str) -> None:
        """Notion has no hard-delete in the public API — archive instead.

        Archived pages are filtered out of normal queries, so this matches
        the behaviour git users expect from `git rm` + push.
        """
        native = self._native_id(task_id)
        if not native:
            raise NotionPushError(
                f"cannot delete {task_id!r}: not a Notion-sourced id"
            )
        headers = self._auth_header()
        headers["Content-Type"] = "application/json"
        pid = urllib.parse.quote(native, safe="")
        try:
            self._http_patch(
                f"{self._NOTION_BASE}/pages/{pid}",
                body={"archived": True},
                headers=headers,
            )
        except urllib.error.HTTPError as exc:
            # R-11: idempotent delete — see JiraDriver.delete. Notion also
            # uses 400 with a "validation_error" body for already-archived
            # pages on some endpoints; we keep the soft-success limited to
            # 404 / 410 so genuine schema-mismatch errors still surface.
            if exc.code in (404, 410):
                return
            raise


SCHEMES: dict[str, type[Driver]] = {
    "jira": JiraDriver,
    "vikunja": VikunjaDriver,
    "mstodo": MSTodoDriver,
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

class _BinaryStdinReader:
    """Text-like shim over a binary stream that does not buffer reads.

    `sys.stdin` is a `TextIOWrapper`, which fills an internal decode buffer
    on `readline()` — bytes that then become invisible to a subsequent
    `sys.stdin.buffer.read(n)`. Fast-export streams interleave text
    directives (`blob`, `data N`) with raw binary blob bodies, so the
    decode buffer eats into blob bytes and the parser desyncs.

    This shim reads everything through the binary layer: `readline()`
    decodes a single line, `read(n)` returns exactly n bytes via the
    `.buffer` property. No second layer of buffering.
    """

    def __init__(self, binary):
        self._bin = binary

    def readline(self) -> str:
        return self._bin.readline().decode("utf-8", errors="replace")

    def read(self, n: int) -> str:
        # Kept for parity with TextIO; the export path reads via .buffer.
        return self._bin.read(n).decode("utf-8", errors="replace")

    @property
    def buffer(self):
        return self._bin


class ProtocolHandler:
    """Drives the git remote helper stdin/stdout conversation."""

    def __init__(self, remote_name: str, url: str, driver: Driver,
                 serializer: Serializer,
                 stdin=None, stdout=None, stderr=None):
        self.remote_name = remote_name
        self.url = url
        self.driver = driver
        self.serializer = serializer
        # When no stdin is passed, wrap sys.stdin.buffer in a byte-accurate
        # reader. Mixing TextIOWrapper.readline() with stdin.buffer.read(n)
        # desyncs the stream because TextIOWrapper keeps an internal decode
        # buffer that read() on the raw buffer skips past — see the
        # fast-export blob-length regression test.
        if stdin is None:
            self.stdin = _BinaryStdinReader(sys.stdin.buffer)
        else:
            self.stdin = stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        # Per-ref failure messages collected during an export. Populated by
        # _handle_modify / _handle_delete; consumed by _cmd_export so we emit
        # 'error <ref>' instead of lying with 'ok <ref>' when any task failed.
        self.export_errors: dict[str, str] = {}
        self.had_errors = False
        self._warned_codes: set[str] = set()

    def _write(self, line: str) -> None:
        self.stdout.write(line)
        self.stdout.flush()

    def _log(self, msg: str) -> None:
        self.stderr.write(f"git-remote-tasks: {msg}\n")
        self.stderr.flush()

    def _warn_once(self, code: str, msg: str) -> None:
        """Emit a stderr warning at most once per run per code.

        Limitations we cannot fix yet must be visible to the user on every
        run that touches them; the `code` keyed dedupe stops a single fetch
        from spamming the same warning N times.
        """
        if code in self._warned_codes:
            return
        self._warned_codes.add(code)
        self._log(f"warning[{code}]: {msg}")

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
        # Private namespace: git redirects the helper's writes here, then
        # the user's remote fetch refspec maps them to refs/remotes/<name>/*.
        # Writing directly to refs/heads/* would clobber the user's branches.
        self._write(
            f"refspec refs/heads/*:refs/tasks/{self.remote_name}/heads/*\n"
        )
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

        self._promote_pending_since()
        mode = (self.driver.config.get("sync.mode") or "incremental").lower()
        since = self.driver.config.get("sync.lastFetchAt")
        parent = self._previous_tip()

        # Incremental mode requires both: a persisted `since` token AND an
        # existing remote tip to base the next commit on. Without a parent
        # commit, a diff-only import produces an empty tree. Fall back to a
        # full snapshot in that case so the first fetch still works.
        if mode == "full" or since is None or parent is None:
            tasks = self.driver.fetch_all()
            tasks_sorted = sorted(tasks, key=lambda t: t.get("id") or "")
            self._write_fast_import(tasks_sorted, parent=parent)
            self._record_pending_since(self.driver._now_iso(), parent)
            self._log_fetch_summary(mode="full", changed=len(tasks_sorted),
                                     deleted=0, since=None)
            return

        changed, deleted_ids, new_since = self.driver.fetch_changed(since)
        if not changed and not deleted_ids:
            # P2-02: nothing new — skip the empty commit entirely so
            # `git log <remote>/main` stays clean of no-op rows.
            #
            # CRITICAL: even with no commits, we MUST terminate the
            # fast-import stream with `done\n`. `git fetch` spawned
            # `git fast-import` to consume our stdout when it sent
            # `import <ref>`, and fast-import only exits when it sees
            # `done` or EOF. Returning silently here makes fast-import
            # block forever, which makes git keep our stdin open, which
            # makes our run() loop block on readline → multi-minute hang
            # that looks indistinguishable from "fetch is slow".
            self._write("done\n")
            # The two-phase watermark is pointless when no stream was
            # emitted — there is nothing that could fail between our
            # exit and git's. Promote `new_since` directly, otherwise a
            # quiet period keeps re-querying the same widening window on
            # every fetch (the tip never moves, so pending never
            # promotes). Also clears any stale pending entry.
            promote = new_since or since
            if promote:
                self._commit_pending_since(promote)
            self._log_fetch_summary(mode="incremental", changed=0,
                                     deleted=0, since=since)
            return
        changed_sorted = sorted(changed, key=lambda t: t.get("id") or "")
        self._write_incremental_import(changed_sorted, deleted_ids,
                                        parent=parent)
        if new_since:
            self._record_pending_since(new_since, parent)
        self._log_fetch_summary(mode="incremental",
                                 changed=len(changed_sorted),
                                 deleted=len(deleted_ids), since=since)

    def _log_fetch_summary(self, mode: str, changed: int, deleted: int,
                            since: str | None) -> None:
        """One-line stderr report per fetch.

        git shows `* [new branch] main -> <remote>/main` after a successful
        fetch, but nothing about how much work actually happened. This
        fills that gap so the user can tell "incremental found nothing" from
        "something went wrong and git is lying". Deliberately terse: one
        line, no colour, no heuristics.
        """
        if mode == "full":
            msg = f"{self.remote_name}: fetched {changed} tasks (full snapshot)"
        elif changed == 0 and deleted == 0:
            tail = f" since {since}" if since else ""
            msg = f"{self.remote_name}: up to date{tail}"
        else:
            tail = f" since {since}" if since else ""
            msg = (f"{self.remote_name}: {changed} changed, "
                   f"{deleted} deleted{tail}")
        self._log(msg)

    def _record_pending_since(self, value: str, parent: str | None) -> None:
        """Phase 1 of the two-phase sync watermark.

        Writes the new token to `sync.pending.since` together with the
        parent sha we based the import on. The next run's
        `_promote_pending_since` promotes this to `sync.lastFetchAt`
        only if git actually accepted the fast-import stream (the tip
        moved). If import failed, the pending record is dropped on the
        next run and the watermark never advances — so tasks are never
        lost to a crash between our exit and fast-import's.
        """
        key_since = f"tasks-remote.{self.remote_name}.sync.pending.since"
        key_parent = f"tasks-remote.{self.remote_name}.sync.pending.parent"
        ok = True
        if not write_config_value(key_since, value):
            ok = False
        if not write_config_value(key_parent, parent or ""):
            ok = False
        if not ok:
            self._warn_once(
                "sync-persist",
                f"failed to persist pending sync state for {self.remote_name}; "
                f"next fetch will do a full snapshot.",
            )

    def _promote_pending_since(self) -> None:
        """Phase 2: check pending watermark and either promote or discard.

        Called at the start of every import batch, before we consult
        `sync.lastFetchAt`. Promotes the pending token when the current
        remote tip differs from the parent we recorded — meaning git
        accepted our last fast-import stream.
        """
        pending_since = self.driver.config.get("sync.pending.since")
        if not pending_since:
            return
        pending_parent = self.driver.config.get("sync.pending.parent") or ""
        current_tip = self._previous_tip() or ""
        # parent=="" means there was no previous tip (first fetch); in that
        # case any current_tip counts as success.
        if current_tip and current_tip != pending_parent:
            self._commit_pending_since(pending_since)
        else:
            self._discard_pending_since()

    def _commit_pending_since(self, value: str) -> None:
        key = f"tasks-remote.{self.remote_name}.sync.lastFetchAt"
        write_config_value(key, value)
        self._discard_pending_since()
        # Also mirror into the driver's live config so this run's
        # incremental logic sees the promoted value.
        self.driver.config["sync.lastFetchAt"] = value

    def _discard_pending_since(self) -> None:
        unset_config_values(
            f"^tasks-remote\\.{re.escape(self.remote_name)}\\.sync\\.pending\\."
        )
        self.driver.config.pop("sync.pending.since", None)
        self.driver.config.pop("sync.pending.parent", None)

    def _write_fast_import(self, tasks: list[dict],
                           parent: str | None = None) -> None:
        """Full-snapshot import: deleteall + every blob."""
        if parent is None:
            parent = self._previous_tip()
        ext = self.serializer.EXTENSION
        blobs = self._emit_blobs(tasks, ext)
        commit_mark = len(blobs) + 1
        self._emit_commit_header(commit_mark, tasks, parent)
        self._write("deleteall\n")
        for mark, _body, path in blobs:
            self._write(f"M 100644 :{mark} {path}\n")
        self._write("\n")
        self._write("done\n")

    def _write_incremental_import(self, changed: list[dict],
                                   deleted_ids: list[str],
                                   parent: str) -> None:
        """Diff-only import: M for changed tasks, D for deleted, no deleteall.

        Requires a parent commit to base the tree on — the caller guarantees
        this. With no parent, a diff-only import yields an empty tree.
        """
        ext = self.serializer.EXTENSION
        blobs = self._emit_blobs(changed, ext)
        commit_mark = len(blobs) + 1
        self._emit_commit_header(commit_mark, changed, parent,
                                  summary="update")
        for mark, _body, path in blobs:
            self._write(f"M 100644 :{mark} {path}\n")
        for tid in deleted_ids:
            # R-08: symmetric defense-in-depth with `_emit_blobs`. A driver
            # bug or hostile upstream id could otherwise produce
            # `D tasks/../etc/passwd.yaml`. fast-import accepts D for
            # missing paths as a no-op — refusing here keeps the audit
            # trail honest.
            if not is_safe_task_id(tid):
                self._warn_once(
                    "unsafe-id",
                    f"skipping delete of task with unsafe id {tid!r}: "
                    f"must match [A-Za-z0-9][A-Za-z0-9._=-]* (≤255 chars).",
                )
                continue
            self._write(f"D tasks/{tid}.{ext}\n")
        self._write("\n")
        self._write("done\n")

    def _emit_blobs(self, tasks: list[dict], ext: str
                    ) -> list[tuple[int, bytes, str]]:
        blobs: list[tuple[int, bytes, str]] = []
        idx = 0
        for task in tasks:
            tid = task.get("id") or ""
            if not is_safe_task_id(tid):
                # Refuse to emit an entry that could escape tasks/ or shadow
                # a git-internal name. The whole task is dropped with a
                # warning; a single bad row must not abort the import.
                self._warn_once(
                    "unsafe-id",
                    f"skipping task with unsafe id {tid!r}: "
                    f"must match [A-Za-z0-9][A-Za-z0-9._=-]* (≤255 chars).",
                )
                continue
            idx += 1
            body = self.serializer.serialize(task).encode("utf-8")
            path = f"tasks/{tid}.{ext}"
            blobs.append((idx, body, path))
        for mark, body, _path in blobs:
            self._write("blob\n")
            self._write(f"mark :{mark}\n")
            self._write(f"data {len(body)}\n")
            buf = getattr(self.stdout, "buffer", None)
            if buf is not None:
                self.stdout.flush()
                buf.write(body)
                buf.flush()
                self._write("\n")
            else:
                self._write(body.decode("utf-8"))
                self._write("\n")
        return blobs

    def _emit_commit_header(self, commit_mark: int, tasks: list[dict],
                             parent: str | None,
                             summary: str = "import") -> None:
        latest = self._latest_updated(tasks)
        ts = int(latest.timestamp())
        message = (
            f"tasks: {summary} {self.remote_name} ({len(tasks)} tasks) "
            f"[{latest.strftime('%Y-%m-%dT%H:%M:%SZ')}]"
        )
        mbytes = message.encode("utf-8")
        self._write(f"commit refs/tasks/{self.remote_name}/heads/main\n")
        self._write(f"mark :{commit_mark}\n")
        self._write(f"committer git-remote-tasks <tasks@local> {ts} +0000\n")
        self._write(f"data {len(mbytes)}\n")
        self._write(message + "\n")
        if parent:
            self._write(f"from {parent}\n")

    def _previous_tip(self) -> str | None:
        """Return the sha of refs/remotes/<remote>/main if it exists, else None.

        Producing `from <sha>` keeps successive imports as a linear history so
        that `git merge <remote>/main` and `git bisect` work without
        --allow-unrelated-histories.
        """
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet",
                 f"refs/remotes/{self.remote_name}/main"],
                capture_output=True, text=True, check=False,
                timeout=_GIT_SUBPROCESS_TIMEOUT,
            )
        except (FileNotFoundError, OSError):
            return None
        except subprocess.TimeoutExpired:
            return None
        sha = proc.stdout.strip()
        return sha or None

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
        self.export_errors.clear()
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
                self._handle_modify(stripped, marks, current_ref)
                continue
            if stripped.startswith("D "):
                self._handle_delete(stripped, current_ref)
                continue
            if stripped.startswith(("reset ", "tag ", "feature ", "option ", "progress ")):
                continue
            # ignore other lines silently
        if current_ref:
            results.append(current_ref)
        if not results:
            results = [f"refs/heads/main"]
        for ref in results:
            err = self.export_errors.get(ref)
            if err:
                self.had_errors = True
                self._write(f"error {ref} {err}\n")
            else:
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

    def _record_export_error(self, ref: str | None, msg: str) -> None:
        """Attach an error to the current ref so _cmd_export can emit 'error <ref>'."""
        key = ref or "refs/heads/main"
        # First failure wins; later ones still log but don't overwrite.
        self.export_errors.setdefault(key, msg)

    def _handle_modify(self, line: str, marks: dict[str, bytes],
                       current_ref: str | None) -> None:
        # M <mode> <sha-or-mark> <path>
        parts = line.split(" ", 3)
        if len(parts) < 4:
            return
        _, _mode, ref, path = parts
        if not path.startswith("tasks/"):
            return
        # Non-task companion files (e.g. tasks/.gitkeep, tasks/README.md)
        # are legitimately committed alongside task files. Silently skip
        # anything whose extension we don't serialize, so the safety check
        # never sees them and the push succeeds.
        if not _is_task_file_path(path):
            return
        if not _is_safe_tasks_path(path):
            self._log(f"refusing to push from suspicious path {path!r}")
            self._record_export_error(current_ref, f"unsafe path: {path}")
            return
        basename = Path(path).name
        task_id_from_path, _dot, _ext = basename.rpartition(".")
        if not is_safe_task_id(task_id_from_path):
            self._log(f"refusing to push unsafe task id from path {path!r}")
            self._record_export_error(current_ref, f"unsafe path: {path}")
            return
        try:
            serializer = serializer_for_extension(path)
        except ValueError as exc:
            self._log(str(exc))
            self._record_export_error(current_ref, f"unknown extension: {path}")
            return
        content = marks.get(ref, b"")
        if not content:
            self._log(f"no blob content for {path}")
            self._record_export_error(current_ref, f"no blob for {path}")
            return
        try:
            task = serializer.deserialize(content.decode("utf-8"))
            content_id = task.get("id") or ""
            if content_id and content_id != task_id_from_path:
                # The filename is the operator's intent. A stale or copy-pasted
                # file with a foreign `id:` field would otherwise PATCH the
                # wrong upstream issue silently.
                self._log(
                    f"refusing push: file {path!r} has content id "
                    f"{content_id!r} (expected {task_id_from_path!r})"
                )
                self._record_export_error(
                    current_ref,
                    f"id/path mismatch for {path}: content id is "
                    f"{content_id!r}, expected {task_id_from_path!r}",
                )
                return
            # Canonicalize so the driver always operates on the filename id,
            # even when the file omits `id:` entirely.
            task["id"] = task_id_from_path
            self.driver.upsert(task)
        except NotImplementedError as exc:
            self._log(f"upsert not implemented: {exc}")
            self._warn_once(
                "push-stub",
                f"{self.driver.__class__.__name__} write path is not implemented yet; "
                f"no changes were sent to the remote service.",
            )
            self._record_export_error(current_ref, "upsert not implemented")
        except Exception as exc:
            self._log(f"upsert failed: {exc}")
            if os.environ.get("GIT_REMOTE_TASKS_DEBUG"):
                import traceback
                self.stderr.write(traceback.format_exc())
                self.stderr.flush()
            self._record_export_error(current_ref, f"upsert failed: {exc}")

    def _handle_delete(self, line: str, current_ref: str | None) -> None:
        # D <path>
        parts = line.split(" ", 1)
        if len(parts) < 2:
            return
        path = parts[1]
        if not path.startswith("tasks/"):
            return
        # See _handle_modify: non-task companions under tasks/ are skipped
        # silently rather than failing the push.
        if not _is_task_file_path(path):
            return
        if not _is_safe_tasks_path(path):
            self._log(f"refusing to delete from suspicious path {path!r}")
            self._record_export_error(current_ref, f"unsafe path: {path}")
            return
        name = Path(path).name
        task_id, _, _ = name.rpartition(".")
        if not is_safe_task_id(task_id):
            self._log(f"refusing to delete unsafe task id from path {path!r}")
            self._record_export_error(current_ref, f"unsafe path: {path}")
            return
        try:
            self.driver.delete(task_id)
        except NotImplementedError as exc:
            self._log(f"delete not implemented: {exc}")
            self._warn_once(
                "push-stub",
                f"{self.driver.__class__.__name__} write path is not implemented yet; "
                f"no changes were sent to the remote service.",
            )
            self._record_export_error(current_ref, "delete not implemented")
        except Exception as exc:
            self._log(f"delete failed: {exc}")
            if os.environ.get("GIT_REMOTE_TASKS_DEBUG"):
                import traceback
                self.stderr.write(traceback.format_exc())
                self.stderr.flush()
            self._record_export_error(current_ref, f"delete failed: {exc}")


# ============================================================================
# Management subcommands
# ============================================================================

KNOWN_SUBCOMMANDS = {"install", "uninstall", "list-schemes", "check",
                      "init", "reset", "version"}

__version__ = "0.2.0"

INIT_SYMLINK_NAMES = ("tasks-init",)


def _script_path() -> Path:
    return Path(os.path.abspath(__file__))


def _install_symlink_names() -> list[str]:
    return [f"git-remote-{s}" for s in SCHEMES] + list(INIT_SYMLINK_NAMES)


def cmd_install(args) -> int:
    bin_dir = Path(os.path.expanduser(args.bin_dir)).resolve()
    bin_dir.mkdir(parents=True, exist_ok=True)
    src = _script_path()
    try:
        os.chmod(src, os.stat(src).st_mode | 0o111)
    except OSError as exc:
        print(f"warning: could not chmod {src}: {exc}", file=sys.stderr)
    for name in _install_symlink_names():
        link = bin_dir / name
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(str(src), str(link))
        print(f"installed {link}")
    # DX-03: absolute-path symlinks break silently when the script file
    # moves. Make that failure mode visible at install time and keep the
    # resolved path discoverable via `ls -l`.
    print(f"(symlinks point to {src} — re-run `install` if you move it)")
    path_env = os.environ.get("PATH", "")
    on_path = any(Path(os.path.expanduser(p)).resolve() == bin_dir
                  for p in path_env.split(os.pathsep) if p)
    if not on_path:
        print(f"warning: {bin_dir} is not on PATH", file=sys.stderr)
    return 0


def cmd_uninstall(args) -> int:
    bin_dir = Path(os.path.expanduser(args.bin_dir)).resolve()
    src = _script_path()
    for name in _install_symlink_names():
        link = bin_dir / name
        if not link.exists() and not link.is_symlink():
            print(f"skip {link} (not present)")
            continue
        if not link.is_symlink():
            print(f"skip {link} (not a symlink; refusing to remove unrelated file)",
                  file=sys.stderr)
            continue
        try:
            target = Path(os.readlink(link))
            if not target.is_absolute():
                target = (link.parent / target).resolve()
            else:
                target = target.resolve()
        except OSError as exc:
            print(f"warning: could not read {link}: {exc}", file=sys.stderr)
            continue
        if target != src:
            print(f"skip {link} (points to {target}, not {src})",
                  file=sys.stderr)
            continue
        try:
            os.unlink(link)
            print(f"removed {link}")
        except OSError as exc:
            print(f"warning: could not remove {link}: {exc}", file=sys.stderr)
    return 0


def cmd_list_schemes(args) -> int:
    for scheme, cls in SCHEMES.items():
        print(f"{scheme:10s} {cls.__name__}")
    return 0


def _prompt_format(input_fn=input, stdin=None) -> str:
    """Prompt the user for yaml|org, refusing non-interactive stdin.

    UX2-02: `input()` on a piped stdin hangs forever. If we can't
    interact, fail loudly with a clear message rather than block the
    user's terminal.
    """
    source = stdin if stdin is not None else sys.stdin
    if input_fn is input:
        # Real TTY check — mocks bypass this with a fake input_fn.
        if not hasattr(source, "isatty") or not source.isatty():
            raise RuntimeError(
                "tasks-init: --format must be given explicitly when stdin "
                "is not a terminal."
            )
    while True:
        choice = input_fn("Choose task file format [yaml/org]: ").strip()
        if choice in ("yaml", "org"):
            return choice
        print("  → please enter 'yaml' or 'org'.")


def cmd_version(args) -> int:
    print(f"git-remote-tasks {__version__}")
    return 0


def cmd_reset(args) -> int:
    """Wipe sync state for a remote so the next fetch does a full snapshot."""
    name = args.remote_name
    pattern = f"^tasks-remote\\.{re.escape(name)}\\.sync\\."
    ok = unset_config_values(pattern)
    if ok:
        print(f"reset sync state for remote {name!r}")
        return 0
    print(f"tasks-init: failed to reset some sync keys for {name!r}",
          file=sys.stderr)
    return 1


def cmd_init(args) -> int:
    """Initialize a repo for task sync. Mirrors `git init [path]`.

    Accepts an optional positional `path`; when given, the directory is
    created (if missing) and entered before `git init` runs. When omitted,
    init operates in the current directory.
    """
    fmt = getattr(args, "format", None)
    target = getattr(args, "path", None)
    input_fn = getattr(args, "_input_fn", input)

    if target:
        target_path = Path(target).expanduser()
        target_path.mkdir(parents=True, exist_ok=True)
        os.chdir(str(target_path))

    if not fmt:
        fmt = _prompt_format(input_fn=input_fn)
    if fmt not in ("yaml", "org"):
        print(f"tasks-init: --format must be 'yaml' or 'org' (got: {fmt!r})",
              file=sys.stderr)
        return 2

    if not Path(".git").is_dir():
        rc = subprocess.run(["git", "init", "--quiet"],
                             capture_output=True, text=True).returncode
        if rc != 0:
            print("tasks-init: git init failed", file=sys.stderr)
            return 1

    if not write_config_value("tasks.format", fmt):
        print("tasks-init: failed to write tasks.format to git config",
              file=sys.stderr)
        return 1

    gitignore = Path(".gitignore")
    if not gitignore.exists():
        gitignore.write_text(
            "# git-remote-tasks: nothing task-specific is ignored by default.\n"
        )

    tasks_dir = Path("tasks")
    tasks_dir.mkdir(exist_ok=True)
    (tasks_dir / ".gitkeep").touch()

    subprocess.run(["git", "add", ".gitignore", "tasks/.gitkeep"],
                    capture_output=True, text=True)
    # Commit only when the index has something new — re-running init on an
    # existing repo should be a no-op, not an empty-commit factory.
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        capture_output=True, text=True,
    )
    if diff.returncode != 0:
        subprocess.run(
            ["git", "commit", "--quiet", "-m", f"tasks: init ({fmt} format)"],
            capture_output=True, text=True,
        )

    cwd = Path.cwd()
    print(f"\nRepo initialized with task format: {fmt}  (in {cwd})\n")
    print("Next steps:")
    print("  1. Install helper symlinks (once per machine):")
    print(f"       python {Path(__file__).name} install --bin-dir ~/.local/bin")
    print("  2. Configure a remote. See README §8 for per-service setup.")
    print("  3. Fetch, edit, push:")
    print("       git fetch <remote>")
    print("       git merge <remote>/main --allow-unrelated-histories  # first time only")
    print(f"       $EDITOR tasks/<remote>-<id>.{fmt}")
    print("       git commit -am 'tasks: edit'")
    print("       git push <remote> main")
    return 0


_SAFE_CONFIG_KEYS = {
    "scheme", "baseurl", "email", "tenantid", "clientid",
    "databaseid", "databasetitle",
}
_SECRET_CONFIG_SUBSTRINGS = (
    "token", "password", "secret", "key", "credential", "bearer",
)


def _redact_config_value(key: str) -> bool:
    """Return True if the value of this config key should be redacted."""
    k = key.lower()
    if k in _SAFE_CONFIG_KEYS:
        return False
    # Be conservative: any key containing a secret-like substring is redacted.
    return any(sub in k for sub in _SECRET_CONFIG_SUBSTRINGS)


def _missing_required_keys(scheme: str, config: dict) -> list[str]:
    """Return a sorted list of required-key descriptions that are unset.

    One-of requirements (BUG-10 for mstodo auth) are rendered as
    'keyA or keyB' so the operator sees the choice.
    """
    required = REMOTE_REQUIRED_KEYS.get(scheme, ())
    missing = [k for k in required if not config.get(k)]
    if scheme == "mstodo":
        # Either accessToken OR clientId (MSAL device flow) must be set.
        if not config.get("accessToken") and not config.get("clientId"):
            missing.append("accessToken or clientId")
    return missing


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
    missing = _missing_required_keys(scheme, config)
    print(f"remote: {args.remote_name}")
    print(f"scheme: {scheme}")
    for k, v in sorted(config.items()):
        shown = "<redacted>" if _redact_config_value(k) else v
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

    p_init = sub.add_parser(
        "init",
        help="initialize a repo for task sync (like `git init [path]`)",
    )
    p_init.add_argument("--format", choices=("yaml", "org"),
                        help="task file format; prompts interactively if omitted")
    p_init.add_argument("path", nargs="?", default=None,
                        help="target directory (created if missing); defaults to cwd")

    p_reset = sub.add_parser(
        "reset",
        help="wipe tasks-remote.<name>.sync.* state so the next fetch is full",
    )
    p_reset.add_argument("remote_name")

    sub.add_parser("version", help="print the git-remote-tasks version")

    return parser


# ============================================================================
# Entry point
# ============================================================================

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    prog = argv[0] if argv else ""
    prog_base = os.path.basename(prog)

    # Case 1: invoked as git-remote-<scheme>
    scheme = scheme_for_name(prog)
    if scheme is not None:
        if len(argv) < 3:
            print("usage: git-remote-<scheme> <remote-name> <url>", file=sys.stderr)
            return 2
        return _run_helper(scheme, argv[1], argv[2])

    # Case 2a: invoked as `tasks-init` (symlink). Rewrite argv so the shared
    # argparser sees `init` as the subcommand.
    if prog_base in INIT_SYMLINK_NAMES:
        parser = build_argparser()
        args = parser.parse_args(["init", *argv[1:]])
        return cmd_init(args)

    # Case 2b: management subcommand.
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
        if args.cmd == "init":
            return cmd_init(args)
        if args.cmd == "reset":
            return cmd_reset(args)
        if args.cmd == "version":
            return cmd_version(args)
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
    # Non-zero when any ref's export reported an error so `git push` surfaces
    # the failure instead of silently appearing to succeed.
    return 1 if handler.had_errors else 0


if __name__ == "__main__":
    sys.exit(main())
