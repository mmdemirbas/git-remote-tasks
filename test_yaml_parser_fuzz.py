"""Property and adversarial tests for the hand-written YAML serializer.

Run with:

    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/python -m unittest test_yaml_parser_fuzz

The file is skipped cleanly when `hypothesis` is not installed so that
`python -m unittest test_git_remote_tasks` still works against a bare
stdlib Python. `git_remote_tasks.py` itself never imports anything from
here — all of this is test-time only.
"""

from __future__ import annotations

import unittest

import git_remote_tasks as grt

try:
    from hypothesis import HealthCheck, assume, given, settings
    from hypothesis import strategies as st
    HYPOTHESIS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when hypothesis missing
    HYPOTHESIS_AVAILABLE = False

    # Stubs so the class definitions below parse without hypothesis;
    # the property tests are skipUnless-guarded and never run.
    class _NoHealthCheck:
        too_slow = None
    HealthCheck = _NoHealthCheck  # type: ignore[assignment]

    def assume(*_a, **_k):  # type: ignore[no-redef]
        return True

    def given(*_a, **_k):  # type: ignore[no-redef]
        def deco(fn):
            return fn
        return deco

    def settings(*_a, **_k):  # type: ignore[no-redef]
        def deco(fn):
            return fn
        return deco

    class _StNothing:
        @staticmethod
        def nothing():
            return None
    st = _StNothing()  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

if HYPOTHESIS_AVAILABLE:
    # Constrain text so we never feed control bytes or YAML-document markers
    # into a subset parser that does not claim to handle them. The supported
    # subset is documented on YAMLSerializer.
    _SAFE_TEXT = st.text(
        alphabet=st.characters(
            blacklist_categories=("Cc", "Cs"),
            blacklist_characters="\r",  # CRLF is out of subset
        ),
        min_size=0,
        max_size=80,
    )
    _SAFE_ONELINER = st.text(
        alphabet=st.characters(
            blacklist_categories=("Cc", "Cs"),
            blacklist_characters="\r\n",
        ),
        min_size=0,
        max_size=60,
    )
    _STATUS = st.sampled_from(grt.STATUSES)
    _PRIORITY = st.sampled_from(grt.PRIORITIES)
    _CAT_TYPE = st.sampled_from(grt.CATEGORY_TYPES)
    _ISO_DATE = st.sampled_from([
        None,
        "2024-01-01",
        "2025-04-20",
        "2025-04-20T12:30:00Z",
        "2025-04-20T12:30:00+03:00",
        "2026-12-31T23:59:59-05:00",
    ])

    def _task_strategy():
        return st.builds(
            lambda tid, src, title, desc, status, pri, cd, ud, dd, tags, url, cat_id, cat_name, cat_type: {
                "id": tid or "x",
                "source": src,
                "title": title,
                "description": desc,
                "status": status,
                "priority": pri,
                "created_date": cd,
                "updated_date": ud,
                "due_date": dd,
                "tags": tags,
                "url": url,
                "category": {
                    "id": cat_id,
                    "name": cat_name,
                    "type": cat_type or "other",
                },
            },
            tid=st.text(alphabet="abcdefghijklmnopqrstuvwxyz-0123456789",
                        min_size=1, max_size=20),
            src=st.sampled_from(["jira", "vikunja", "mstodo", "notion", "fake"]),
            title=_SAFE_ONELINER,
            desc=st.one_of(st.none(), _SAFE_TEXT),
            status=_STATUS,
            pri=_PRIORITY,
            cd=_ISO_DATE,
            ud=_ISO_DATE,
            dd=_ISO_DATE,
            tags=st.lists(_SAFE_ONELINER.filter(lambda s: s.strip() != ""),
                          min_size=0, max_size=6, unique=True),
            url=st.one_of(st.none(), _SAFE_ONELINER),
            cat_id=st.one_of(st.none(), _SAFE_ONELINER),
            cat_name=st.one_of(st.none(), _SAFE_ONELINER),
            cat_type=_CAT_TYPE,
        )


# ---------------------------------------------------------------------------
# Adversarial corpus — inputs that stress the parser without relying on
# hypothesis so they run in the default suite too.
# ---------------------------------------------------------------------------

ADVERSARIAL_TITLES = [
    "",                                      # empty
    " leading space",                        # strip-sensitive
    "trailing space ",
    "  ",                                    # whitespace-only
    "contains: colon",                        # colon-space triggers quoting
    "has # hash",                            # mid-line comment marker
    "ends with #",
    "'single quotes'",
    '"double quotes"',
    'back\\slash',
    "mixed \\\" escapes",
    "line1\nline2\nline3",                    # multi-line → block scalar
    "line1\n\nline2",                        # blank line inside block
    "null",                                  # reserved word
    "True",                                  # reserved word variant
    "- leading dash",
    "-notadash",                              # leading '-' but no space
    "üñîçödé 🌟",                            # high BMP + astral plane
    "weird chars: !@#$%^&*()",
    "tabs\tand spaces",
]

ADVERSARIAL_DESCRIPTIONS = [
    None,
    "",
    "single line",
    "two\nlines",
    # "ends with newline\n" — NOT round-trippable; the emitter writes `|`
    # (clip-strip) only, so a trailing newline is normalized away.
    # Documented as a subset limitation on YAMLSerializer.
    "#starts with hash",
    "  indented first line",
    "\n leading blank line then text",
    "emoji 🚀 in prose",
]


class TestAdversarialCorpus(unittest.TestCase):
    """Titles and descriptions that have historically broken ad-hoc YAML."""

    def setUp(self):
        self.s = grt.YAMLSerializer()

    def _base_task(self) -> dict:
        t = grt.empty_task()
        t.update({
            "id": "fake-1",
            "source": "fake",
            "title": "t",
            "status": "todo",
            "priority": "none",
        })
        return t

    def test_titles_roundtrip(self):
        for title in ADVERSARIAL_TITLES:
            with self.subTest(title=repr(title)):
                t = self._base_task()
                t["title"] = title
                back = self.s.deserialize(self.s.serialize(t))
                self.assertEqual(back["title"], title,
                                  f"round-trip failed for {title!r}")

    def test_descriptions_roundtrip(self):
        for desc in ADVERSARIAL_DESCRIPTIONS:
            with self.subTest(desc=repr(desc)):
                t = self._base_task()
                t["description"] = desc
                back = self.s.deserialize(self.s.serialize(t))
                # Empty string → None is acceptable: empty_task has None as
                # the neutral default and the parser canonicalizes.
                if desc in (None, ""):
                    self.assertIn(back["description"], (None, ""))
                else:
                    self.assertEqual(back["description"], desc)

    def test_tags_with_special_chars_roundtrip(self):
        t = self._base_task()
        t["tags"] = ["a: colon", "hash #b", "'quote", '"dq"', "ünî"]
        back = self.s.deserialize(self.s.serialize(t))
        self.assertEqual(back["tags"], t["tags"])

    def test_long_description_block_scalar(self):
        t = self._base_task()
        t["description"] = "\n".join(f"line {i}" for i in range(200))
        back = self.s.deserialize(self.s.serialize(t))
        self.assertEqual(back["description"], t["description"])

    def test_deeply_nested_indent_is_rejected_cleanly(self):
        # Not supported by the subset; must not crash — parser should
        # either yield a defaulted task or drop the unrecognized nesting.
        text = (
            "id: x\nsource: s\ntitle: t\n"
            "status: todo\npriority: none\ntags: []\n"
            "category:\n  id: a\n  name: b\n  type: other\n"
            "extra:\n  deep:\n    deeper:\n      still: 1\n"
        )
        t = self.s.deserialize(text)
        self.assertEqual(t["id"], "x")

    def test_crlf_is_not_advertised_but_must_not_crash(self):
        text = ("id: x\r\nsource: s\r\ntitle: t\r\n"
                "status: todo\r\npriority: none\r\ntags: []\r\n")
        # The supported subset forbids CRLF — but an accidental hand-edit
        # on Windows must not hang or raise.
        t = self.s.deserialize(text)
        self.assertIsInstance(t, dict)

    def test_empty_file_yields_defaulted_task(self):
        t = self.s.deserialize("")
        self.assertEqual(t, grt.empty_task())

    def test_only_comments_yields_defaulted_task(self):
        t = self.s.deserialize("# nothing\n# here\n")
        self.assertEqual(t, grt.empty_task())


# ---------------------------------------------------------------------------
# Property tests — random task dicts must round-trip losslessly.
# ---------------------------------------------------------------------------

@unittest.skipUnless(HYPOTHESIS_AVAILABLE,
                     "hypothesis not installed; pip install -r requirements-dev.txt")
class TestYamlRoundTripProperty(unittest.TestCase):
    @settings(max_examples=400, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(task=_task_strategy() if HYPOTHESIS_AVAILABLE else st.nothing())
    def test_roundtrip_equals_normalize(self, task):
        s = grt.YAMLSerializer()
        text = s.serialize(task)
        back = s.deserialize(text)
        self.assertEqual(grt.normalize_task(back), grt.normalize_task(task))

    @settings(max_examples=200, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(task=_task_strategy() if HYPOTHESIS_AVAILABLE else st.nothing())
    def test_serialize_is_idempotent(self, task):
        s = grt.YAMLSerializer()
        once = s.serialize(task)
        twice = s.serialize(s.deserialize(once))
        self.assertEqual(once, twice,
                          "serialize(deserialize(serialize(t))) must equal serialize(t)")

    @settings(max_examples=200, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(task=_task_strategy() if HYPOTHESIS_AVAILABLE else st.nothing())
    def test_org_roundtrip_equals_normalize(self, task):
        # Symmetry invariant also exercised under fuzz.
        o = grt.OrgSerializer()
        assume(task["title"] and "\n" not in task["title"])  # org titles are single-line
        text = o.serialize(task)
        back = o.deserialize(text)
        self.assertEqual(grt.normalize_task(back)["id"],
                          grt.normalize_task(task)["id"])
        self.assertEqual(grt.normalize_task(back)["status"],
                          grt.normalize_task(task)["status"])


if __name__ == "__main__":
    unittest.main()
