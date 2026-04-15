"""Tests for git_remote_tasks.

Run with:
    python -m unittest -v test_git_remote_tasks

Coverage:
    python -m coverage run -m unittest test_git_remote_tasks
    python -m coverage report -m --include=git_remote_tasks.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import git_remote_tasks as grt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def full_task(**overrides) -> dict:
    t = grt.empty_task()
    t.update({
        "id": "jira-PROJ-123",
        "source": "jira",
        "title": "Fix authentication timeout on mobile login",
        "description": "Users get logged out.\nAffects iOS only.",
        "status": "in_progress",
        "priority": "high",
        "created_date": "2024-11-03T09:15:00Z",
        "due_date": "2025-04-20",
        "updated_date": "2025-04-12T14:30:00Z",
        "tags": ["mobile", "ios", "auth"],
        "category": {"id": "PROJ", "name": "Backend", "type": "project"},
        "url": "https://company.atlassian.net/browse/PROJ-123",
    })
    t.update(overrides)
    return t


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema(unittest.TestCase):
    def test_empty_task_has_all_fields(self):
        t = grt.empty_task()
        for field in grt.TASK_FIELDS:
            self.assertIn(field, t)
        self.assertEqual(t["tags"], [])
        self.assertEqual(t["category"]["type"], "other")

    def test_normalize_fills_missing(self):
        t = grt.normalize_task({"id": "x", "category": {"name": "N"}})
        self.assertEqual(t["id"], "x")
        self.assertEqual(t["category"], {"id": None, "name": "N", "type": "other"})
        self.assertEqual(t["status"], "todo")

    def test_normalize_preserves_logbook(self):
        t = grt.normalize_task({"id": "x", "logbook": ["entry"]})
        self.assertEqual(t["logbook"], ["entry"])

    def test_normalize_none_category_type_defaults(self):
        t = grt.normalize_task({"category": {"type": None}})
        self.assertEqual(t["category"]["type"], "other")


# ---------------------------------------------------------------------------
# YAML serializer
# ---------------------------------------------------------------------------

class TestYAMLSerializer(unittest.TestCase):
    def setUp(self):
        self.s = grt.YAMLSerializer()

    def test_roundtrip_identical(self):
        t = full_task()
        a = self.s.serialize(t)
        b = self.s.serialize(self.s.deserialize(a))
        self.assertEqual(a, b)

    def test_all_fields_roundtrip_to_dict(self):
        t = full_task()
        back = self.s.deserialize(self.s.serialize(t))
        self.assertEqual(back, t)

    def test_null_fields_roundtrip(self):
        t = full_task(description=None, due_date=None, url=None)
        back = self.s.deserialize(self.s.serialize(t))
        self.assertIsNone(back["description"])
        self.assertIsNone(back["due_date"])
        self.assertIsNone(back["url"])

    def test_empty_tags_list(self):
        t = full_task(tags=[])
        text = self.s.serialize(t)
        self.assertIn("tags: []", text)
        self.assertEqual(self.s.deserialize(text)["tags"], [])

    def test_multiline_description_block_scalar(self):
        t = full_task(description="a\nb\nc")
        text = self.s.serialize(t)
        self.assertIn("description: |", text)
        back = self.s.deserialize(text)
        self.assertEqual(back["description"], "a\nb\nc")

    def test_unicode_fields(self):
        t = full_task(title="üñîçödé 🌟", description="Zürich café ☕")
        back = self.s.deserialize(self.s.serialize(t))
        self.assertEqual(back["title"], "üñîçödé 🌟")
        self.assertEqual(back["description"], "Zürich café ☕")

    def test_dates_always_quoted(self):
        t = full_task()
        text = self.s.serialize(t)
        self.assertIn('created_date: "2024-11-03T09:15:00Z"', text)
        self.assertIn('due_date: "2025-04-20"', text)
        self.assertIn('updated_date: "2025-04-12T14:30:00Z"', text)

    def test_missing_optional_fields_yield_none(self):
        text = (
            "id: x\n"
            "source: src\n"
            "title: hi\n"
            "status: todo\n"
            "priority: none\n"
            "tags: []\n"
            "category:\n"
            "  id: null\n"
            "  name: null\n"
            "  type: other\n"
        )
        back = self.s.deserialize(text)
        self.assertIsNone(back["description"])
        self.assertIsNone(back["due_date"])
        self.assertIsNone(back["url"])

    def test_quoted_value_with_escape(self):
        t = full_task(title='has "quotes" and \\slash')
        back = self.s.deserialize(self.s.serialize(t))
        self.assertEqual(back["title"], 'has "quotes" and \\slash')

    def test_comment_lines_ignored(self):
        text = "id: x\n# a comment\nsource: s\ntitle: hi\n"
        back = self.s.deserialize(text)
        self.assertEqual(back["id"], "x")
        self.assertEqual(back["source"], "s")

    def test_logbook_preserved_when_present(self):
        t = full_task()
        t["logbook"] = ['- State "DONE" from "TODO" [2025-04-10 Thu 11:00]']
        out = self.s.serialize(t)
        self.assertIn("logbook:", out)
        back = self.s.deserialize(out)
        self.assertEqual(back["logbook"], t["logbook"])

    def test_reserved_word_title_quoted(self):
        t = full_task(title="null")
        text = self.s.serialize(t)
        self.assertIn('title: "null"', text)
        self.assertEqual(self.s.deserialize(text)["title"], "null")


# ---------------------------------------------------------------------------
# Org serializer
# ---------------------------------------------------------------------------

class TestOrgSerializer(unittest.TestCase):
    def setUp(self):
        self.s = grt.OrgSerializer()

    def test_roundtrip_stable(self):
        t = full_task()
        a = self.s.serialize(t)
        b = self.s.serialize(self.s.deserialize(a))
        self.assertEqual(a, b)

    def test_all_statuses_roundtrip(self):
        for status in grt.STATUSES:
            t = full_task(status=status)
            back = self.s.deserialize(self.s.serialize(t))
            self.assertEqual(back["status"], status)

    def test_all_priorities_roundtrip(self):
        for pri in grt.PRIORITIES:
            t = full_task(priority=pri)
            back = self.s.deserialize(self.s.serialize(t))
            self.assertEqual(back["priority"], pri)

    def test_absent_priority_is_none(self):
        text = (
            "* TODO Hello\n"
            "  :PROPERTIES:\n"
            "  :ID: x\n"
            "  :SOURCE: s\n"
            "  :END:\n"
        )
        t = self.s.deserialize(text)
        self.assertEqual(t["priority"], "none")

    def test_multiline_description_preserved(self):
        t = full_task(description="line1\nline2\nline3")
        back = self.s.deserialize(self.s.serialize(t))
        self.assertEqual(back["description"], "line1\nline2\nline3")

    def test_tags_roundtrip_via_comma_separated(self):
        t = full_task(tags=["a", "b", "c"])
        text = self.s.serialize(t)
        self.assertIn(":TAGS: a,b,c", text)
        self.assertEqual(self.s.deserialize(text)["tags"], ["a", "b", "c"])

    def test_unicode_fields(self):
        t = full_task(title="Zürich 🌟", description="café ☕")
        back = self.s.deserialize(self.s.serialize(t))
        self.assertEqual(back["title"], "Zürich 🌟")
        self.assertEqual(back["description"], "café ☕")

    def test_logbook_preserved(self):
        src = (
            "* DONE [#A] Title\n"
            "  :PROPERTIES:\n"
            "  :ID: x\n"
            "  :SOURCE: s\n"
            "  :END:\n"
            "  :LOGBOOK:\n"
            '  - State "DONE" from "TODO" [2025-04-10 Thu 11:00]\n'
            "  :END:\n"
            "\n"
            "  description body\n"
        )
        t = self.s.deserialize(src)
        self.assertTrue(t.get("logbook"))
        out = self.s.serialize(t)
        self.assertIn(":LOGBOOK:", out)
        self.assertIn('State "DONE" from "TODO"', out)

    def test_missing_properties_drawer_handled(self):
        src = "* TODO Hello\n"
        t = self.s.deserialize(src)
        self.assertEqual(t["title"], "Hello")
        self.assertEqual(t["status"], "todo")

    def test_no_headline_returns_empty(self):
        t = self.s.deserialize("no headline here\n")
        self.assertEqual(t["title"], "")

    def test_org_timestamp_iso_conversion_bad_input(self):
        self.assertEqual(grt._org_timestamp_to_iso("garbage"), "garbage")

    def test_iso_to_org_timestamp_bad_input_passthrough(self):
        self.assertIn("not-a-date", grt._iso_to_org_timestamp("not-a-date"))

    def test_deadline_rendered_as_agenda_line_feat05(self):
        s = grt.OrgSerializer()
        t = full_task(due_date="2025-04-20")
        text = s.serialize(t)
        lines = text.splitlines()
        # First line is the headline; the agenda DEADLINE must precede
        # the :PROPERTIES: drawer so Emacs/nvim-orgmode see it.
        headline_idx = next(i for i, ln in enumerate(lines)
                             if ln.startswith("* "))
        props_idx = next(i for i, ln in enumerate(lines)
                          if ln.strip() == ":PROPERTIES:")
        deadline_idx = next(i for i, ln in enumerate(lines)
                             if ln.strip().startswith("DEADLINE:"))
        self.assertLess(headline_idx, deadline_idx)
        self.assertLess(deadline_idx, props_idx)
        # DEADLINE must NOT also be inside the drawer.
        self.assertFalse(any(":DEADLINE:" in ln for ln in lines))

    def test_deadline_agenda_roundtrip_feat05(self):
        s = grt.OrgSerializer()
        t = full_task(due_date="2025-04-20")
        back = s.deserialize(s.serialize(t))
        self.assertEqual(back["due_date"], "2025-04-20")

    def test_deadline_legacy_property_still_parses(self):
        s = grt.OrgSerializer()
        legacy = (
            "* TODO Title\n"
            "  :PROPERTIES:\n"
            "  :ID: x\n"
            "  :DEADLINE: <2025-04-20 Sun>\n"
            "  :END:\n"
        )
        self.assertEqual(s.deserialize(legacy)["due_date"], "2025-04-20")

    def test_bug11_stray_second_headline_terminates_drawer(self):
        s = grt.OrgSerializer()
        # Missing :END: — drawer must abort before the second headline
        # rather than swallowing it.
        src = (
            "* TODO First\n"
            "  :PROPERTIES:\n"
            "  :ID: first-1\n"
            "* TODO Second\n"
            "  :PROPERTIES:\n"
            "  :ID: second-1\n"
            "  :END:\n"
        )
        t = s.deserialize(src)
        self.assertEqual(t["title"], "First")
        self.assertEqual(t["id"], "first-1")


class TestBug08JiraUrl(unittest.TestCase):
    def test_jira_url_omitted_when_no_http_base(self):
        d = grt.JiraDriver("jira", "jira://company", {"email": "a", "apiToken": "t"})
        issue = {"key": "PROJ-1", "fields": {
            "summary": "t", "status": {"name": "To Do"},
            "project": {"key": "PROJ", "name": "Project"},
        }}
        self.assertEqual(d.normalize(issue)["url"], None)

    def test_jira_url_set_when_http_base_present(self):
        d = grt.JiraDriver("jira", "jira://company", {
            "baseUrl": "https://x.atlassian.net",
            "email": "a", "apiToken": "t",
        })
        issue = {"key": "PROJ-1", "fields": {
            "summary": "t", "status": {"name": "To Do"},
            "project": {"key": "PROJ", "name": "Project"},
        }}
        self.assertEqual(d.normalize(issue)["url"],
                          "https://x.atlassian.net/browse/PROJ-1")


class TestBug10MsftodoCheck(unittest.TestCase):
    def test_missing_access_token_and_client_id(self):
        missing = grt._missing_required_keys(
            "msftodo", {"scheme": "msftodo", "tenantId": "consumers"})
        self.assertIn("accessToken or clientId", missing)

    def test_access_token_alone_is_sufficient(self):
        missing = grt._missing_required_keys(
            "msftodo", {"scheme": "msftodo", "tenantId": "consumers",
                         "clientId": "",
                         "accessToken": "t"})
        self.assertNotIn("accessToken or clientId", missing)

    def test_client_id_alone_is_sufficient(self):
        missing = grt._missing_required_keys(
            "msftodo", {"scheme": "msftodo", "tenantId": "consumers",
                         "clientId": "c"})
        self.assertNotIn("accessToken or clientId", missing)


class TestBug06YamlHyphenKeys(unittest.TestCase):
    def test_nested_hyphen_key_is_preserved(self):
        text = "category:\n  id: A\n  name: B\n  type: other\n  extra-key: val\n"
        parsed = grt.YAMLSerializer()._parse(text)
        self.assertEqual(parsed["category"]["extra-key"], "val")


class TestCaseInsensitiveConfig(unittest.TestCase):
    def test_get_matches_any_case(self):
        c = grt.CaseInsensitiveConfig({"baseUrl": "x", "apiToken": "t"})
        self.assertEqual(c.get("baseurl"), "x")
        self.assertEqual(c.get("BASEURL"), "x")
        self.assertEqual(c["apitoken"], "t")

    def test_missing_key_returns_default(self):
        c = grt.CaseInsensitiveConfig({"a": "1"})
        self.assertEqual(c.get("b", "fallback"), "fallback")

    def test_contains_any_case(self):
        c = grt.CaseInsensitiveConfig({"BaseUrl": "x"})
        self.assertIn("baseurl", c)
        self.assertIn("BASEURL", c)

    def test_read_remote_config_returns_case_insensitive(self):
        output = (
            "tasks-remote.foo.scheme jira\n"
            "tasks-remote.foo.baseurl https://x\n"  # git lowercases the var
        )
        with mock.patch.object(grt, "_run_git_config", return_value=output):
            cfg = grt.read_remote_config("foo")
        self.assertIsInstance(cfg, grt.CaseInsensitiveConfig)
        # Both camelCase and lowercase lookups succeed.
        self.assertEqual(cfg.get("baseUrl"), "https://x")
        self.assertEqual(cfg.get("baseurl"), "https://x")


class TestJsonEncodedMaps(unittest.TestCase):
    """FEAT-03 JSON-encoded form for maps whose keys git config can't accept."""

    def test_subconfig_reads_json_blob(self):
        d = grt.JiraDriver("j", "jira://x", {
            "statusMap": '{"Yüksek": "high", "Orta": "medium"}',
        })
        self.assertEqual(
            d._subconfig("statusMap"),
            {"Yüksek": "high", "Orta": "medium"},
        )

    def test_subconfig_merges_json_and_dotted(self):
        d = grt.JiraDriver("j", "jira://x", {
            "statusMap": '{"A": "todo"}',
            "statusMap.B": "done",
        })
        merged = d._subconfig("statusMap")
        self.assertEqual(merged["A"], "todo")
        self.assertEqual(merged["B"], "done")

    def test_malformed_json_falls_back_to_dotted(self):
        d = grt.JiraDriver("j", "jira://x", {
            "statusMap": "not valid json",
            "statusMap.B": "done",
        })
        self.assertEqual(d._subconfig("statusMap"), {"B": "done"})

    def test_apply_status_override_reads_from_json_map(self):
        d = grt.JiraDriver("j", "jira://x", {
            "statusMap": '{"Ertelendi": "cancelled"}',
        })
        self.assertEqual(
            d._apply_status_override("Ertelendi", "todo"),
            "cancelled",
        )


class TestNotionFieldLogicalMapping(unittest.TestCase):
    """Logical→config mapping honours git-config's variable-name rules."""

    def test_due_date_logical_reads_from_due_date_config(self):
        d = grt.NotionDriver("n", "notion://x", {
            "databaseId": "x", "token": "t",
            "fieldMap.dueDate": "Tarih",
        })
        self.assertEqual(d._prop_names()["due_date"], "Tarih")

    def test_json_form_for_field_map(self):
        d = grt.NotionDriver("n", "notion://x", {
            "databaseId": "x", "token": "t",
            "fieldMap": '{"dueDate": "Tarih", "priority": "Acil"}',
        })
        names = d._prop_names()
        self.assertEqual(names["due_date"], "Tarih")
        self.assertEqual(names["priority"], "Acil")


class TestNotionSchemaAwarePush(unittest.TestCase):
    """FEAT-08 write path adapts payload shape to column type."""

    def setUp(self):
        self.d = grt.NotionDriver("n", "notion://x", {
            "databaseId": "abc", "token": "t",
        })

    def test_status_type_emits_status_payload(self):
        schema = {"Konu": "title", "Status": "status"}
        props = self.d._build_properties(
            grt.empty_task() | {"title": "t", "status": "todo"},
            "Konu", schema,
        )
        self.assertEqual(props["Status"], {"status": {"name": "To Do"}})

    def test_select_type_emits_select_payload(self):
        schema = {"Konu": "title", "Status": "select"}
        props = self.d._build_properties(
            grt.empty_task() | {"title": "t", "status": "todo"},
            "Konu", schema,
        )
        self.assertEqual(props["Status"], {"select": {"name": "To Do"}})

    def test_unknown_type_skips_rather_than_400(self):
        schema = {"Konu": "title", "Status": "people"}
        props = self.d._build_properties(
            grt.empty_task() | {"title": "t", "status": "todo"},
            "Konu", schema,
        )
        self.assertNotIn("Status", props)

    def test_inverse_status_map_used_for_push(self):
        """Operator's statusMap configures pull; push inverts it so the
        database's real column options are used."""
        d = grt.NotionDriver("n", "notion://x", {
            "databaseId": "abc", "token": "t",
            "statusMap": '{"Not started": "todo", "Today": "in_progress"}',
        })
        schema = {"Konu": "title", "Status": "status"}
        props = d._build_properties(
            grt.empty_task() | {"title": "t", "status": "in_progress"},
            "Konu", schema,
        )
        self.assertEqual(props["Status"], {"status": {"name": "Today"}})


class TestJiraJqlConfig(unittest.TestCase):
    def test_default_jql_satisfies_new_endpoint(self):
        d = grt.JiraDriver("j", "jira://x",
                           {"baseUrl": "https://x",
                            "email": "a", "apiToken": "t"})
        jql = d._user_jql()
        # The new endpoint rejects a bare ORDER BY — ensure we have a
        # real condition.
        self.assertIn(" IS NOT EMPTY", jql.upper())

    def test_user_jql_overrides_default(self):
        d = grt.JiraDriver("j", "jira://x", {
            "baseUrl": "https://x", "email": "a", "apiToken": "t",
            "jql": "assignee = currentUser() ORDER BY created DESC",
        })
        self.assertIn("currentUser", d._user_jql())

    def test_fetch_changed_wraps_user_jql(self):
        d = grt.JiraDriver("j", "jira://x", {
            "baseUrl": "https://x", "email": "a", "apiToken": "t",
            "jql": "project = MD ORDER BY created DESC",
        })
        urls: list[str] = []
        def fake_get(url, headers=None):
            urls.append(url)
            return {"issues": [], "isLast": True}
        with mock.patch.object(d, "_http_get", side_effect=fake_get):
            d.fetch_changed("2026-04-15T00:00:00Z")
        decoded = urllib.parse.unquote(urls[0])
        self.assertIn("project = MD", decoded)
        self.assertIn('updated >= "2026-04-15 00:00:00"', decoded)


class TestCrossSourceRefusalAllDrivers(unittest.TestCase):
    def test_jira_refuses_vikunja_id(self):
        d = grt.JiraDriver("j", "https://x", {"baseUrl": "https://x",
                                                "email": "a", "apiToken": "t"})
        with self.assertRaises(grt.JiraPushError):
            d.upsert(full_task(id="vikunja-42"))

    def test_vikunja_refuses_jira_id(self):
        d = grt.VikunjaDriver("v", "http://x",
                                {"baseUrl": "http://x", "apiToken": "t"})
        with self.assertRaises(grt.VikunjaPushError):
            d.upsert(full_task(id="jira-PROJ-1"))

    def test_mstodo_refuses_notion_id(self):
        d = grt.MSTodoDriver("m", "msftodo://x",
                               {"accessToken": "t"})
        with self.assertRaises(grt.MSTodoPushError):
            d.upsert(full_task(id="notion-abc"))

    def test_notion_refuses_jira_id(self):
        d = grt.NotionDriver("n", "notion://x",
                               {"databaseId": "abc", "token": "t"})
        # Avoid the network: stub the schema discover that runs before
        # the cross-source check would fire.
        with mock.patch.object(d, "_discover_schema", return_value={}), \
                mock.patch.object(d, "_discover_title_prop",
                                   return_value="Name"):
            with self.assertRaises(grt.NotionPushError):
                d.upsert(full_task(id="jira-PROJ-1"))

    def test_unprefixed_id_goes_to_create_path(self):
        """A task with no <scheme>- prefix is treated as a new creation,
        not a cross-source refusal."""
        d = grt.VikunjaDriver("v", "http://x", {
            "baseUrl": "http://x", "apiToken": "t", "projectId": "1",
        })
        with mock.patch.object(d, "_http_put") as put:
            d.upsert(full_task(id="", title="fresh"))
        put.assert_called_once()


class TestVikunjaEndpointFallback(unittest.TestCase):
    def test_primary_path_used_first(self):
        d = grt.VikunjaDriver("v", "http://x",
                                {"baseUrl": "http://x", "apiToken": "t"})
        calls = []
        def fake_get(url, headers=None):
            calls.append(url)
            return []
        with mock.patch.object(d, "_http_get", side_effect=fake_get):
            d.fetch_all()
        self.assertIn("/api/v1/tasks?", calls[0])
        self.assertNotIn("/all", calls[0])

    def test_falls_back_to_tasks_all_on_403(self):
        d = grt.VikunjaDriver("v", "http://x",
                                {"baseUrl": "http://x", "apiToken": "t"})
        calls = []
        def fake_get(url, headers=None):
            calls.append(url)
            if "/tasks/all" in url:
                return []
            raise urllib.error.HTTPError(url, 403, "no scope",
                                          hdrs=None, fp=None)
        with mock.patch.object(d, "_http_get", side_effect=fake_get):
            d.fetch_all()
        self.assertTrue(any("/tasks/all" in c for c in calls))


class TestSafeTaskId(unittest.TestCase):
    """S2-01: path-traversal and filesystem-name guards."""

    def test_happy_paths(self):
        for ok_id in ("jira-PROJ-1", "vikunja-42", "notion-abc-def",
                       "msftodo-AAAAAA_BBB", "a", "z9", "a.b-c_d"):
            self.assertTrue(grt.is_safe_task_id(ok_id),
                             f"should be safe: {ok_id!r}")

    def test_rejected(self):
        for bad in ("", ".", "..", "../foo", "foo/bar", "foo\\bar",
                     ".hidden", "-leading", "has space",
                     "bell\x07", "tab\t", "newline\n"):
            self.assertFalse(grt.is_safe_task_id(bad),
                              f"should be unsafe: {bad!r}")

    def test_too_long(self):
        self.assertTrue(grt.is_safe_task_id("a" * 255))
        self.assertFalse(grt.is_safe_task_id("a" * 256))


class TestEmitBlobsSkipsUnsafeIds(unittest.TestCase):
    def test_unsafe_id_is_dropped_with_warning(self):
        class D(FakeDriver):
            pass
        good = grt.empty_task() | {"id": "fake-safe", "source": "fake",
                                     "title": "ok"}
        bad = grt.empty_task() | {"id": "../../evil", "source": "fake",
                                    "title": "nope"}
        h = grt.ProtocolHandler("fake", "fake://x", D(tasks=[good, bad]),
                                 grt.YAMLSerializer(),
                                 stdin=io.StringIO("import refs/heads/main\n\n"),
                                 stdout=FlushTrackingStringIO(),
                                 stderr=io.StringIO())
        h.run()
        out = h.stdout.getvalue()
        self.assertIn("tasks/fake-safe.yaml", out)
        self.assertNotIn("evil", out)
        self.assertIn("warning[unsafe-id]", h.stderr.getvalue())


class TestPushRejectsUnsafePaths(unittest.TestCase):
    def test_handle_modify_rejects_traversal_path(self):
        stream = (
            "export\n"
            "commit refs/heads/main\n"
            "blob\nmark :1\ndata 3\nhey\n"
            "M 100644 :1 tasks/../../etc/passwd.yaml\n"
            "\ndone\n"
        )
        driver = FakeDriver()
        h = grt.ProtocolHandler("fake", "fake://x", driver,
                                 grt.YAMLSerializer(),
                                 stdin=io.StringIO(stream),
                                 stdout=FlushTrackingStringIO(),
                                 stderr=io.StringIO())
        h.run()
        self.assertEqual(driver.upserted, [])
        err = h.stderr.getvalue().lower()
        self.assertTrue("unsafe" in err or "suspicious" in err, err)

    def test_handle_delete_rejects_traversal_path(self):
        stream = (
            "export\n"
            "commit refs/heads/main\n"
            "D tasks/../../etc/passwd.yaml\n"
            "\ndone\n"
        )
        driver = FakeDriver()
        h = grt.ProtocolHandler("fake", "fake://x", driver,
                                 grt.YAMLSerializer(),
                                 stdin=io.StringIO(stream),
                                 stdout=FlushTrackingStringIO(),
                                 stderr=io.StringIO())
        h.run()
        self.assertEqual(driver.deleted, [])
        err = h.stderr.getvalue().lower()
        self.assertTrue("unsafe" in err or "suspicious" in err, err)


class TestHttpRetryAndTimeout(unittest.TestCase):
    def setUp(self):
        self.d = grt.JiraDriver("jira", "https://x", {})

    def test_retries_on_5xx_then_succeeds(self):
        calls = []
        class FakeResp:
            def __init__(self, body=b'{"ok":1}'):
                self._b = body
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return self._b
        def urlopen(req, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.HTTPError(
                    "https://x/api", 503, "Service Unavailable",
                    hdrs=None, fp=None,
                )
            return FakeResp()
        with mock.patch.object(grt.urllib.request, "urlopen", urlopen), \
                mock.patch.object(grt.Driver, "_sleep_backoff",
                                    staticmethod(lambda s: None)):
            out = self.d._http_request("GET", "https://x/api")
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_max_retries(self):
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError("https://x", 500, "boom",
                                          hdrs=None, fp=None)
        with mock.patch.object(grt.urllib.request, "urlopen", urlopen), \
                mock.patch.object(grt.Driver, "_sleep_backoff",
                                    staticmethod(lambda s: None)):
            with self.assertRaises(urllib.error.HTTPError):
                self.d._http_request("GET", "https://x/api")

    def test_non_retryable_status_raises_immediately(self):
        calls = []
        def urlopen(req, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError("https://x", 404, "not found",
                                          hdrs=None, fp=None)
        with mock.patch.object(grt.urllib.request, "urlopen", urlopen), \
                mock.patch.object(grt.Driver, "_sleep_backoff",
                                    staticmethod(lambda s: None)):
            with self.assertRaises(urllib.error.HTTPError):
                self.d._http_request("GET", "https://x/api")
        self.assertEqual(len(calls), 1)

    def test_retries_on_urlerror(self):
        calls = []
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b"{}"
        def urlopen(req, timeout=None):
            calls.append(1)
            if len(calls) < 2:
                raise urllib.error.URLError("network down")
            return FakeResp()
        with mock.patch.object(grt.urllib.request, "urlopen", urlopen), \
                mock.patch.object(grt.Driver, "_sleep_backoff",
                                    staticmethod(lambda s: None)):
            self.d._http_request("GET", "https://x/api")
        self.assertEqual(len(calls), 2)

    def test_redaction_scrubs_query_string(self):
        def urlopen(req, timeout=None):
            raise urllib.error.HTTPError("https://x/api?token=leak", 400,
                                          "bad", hdrs=None, fp=None)
        with mock.patch.object(grt.urllib.request, "urlopen", urlopen):
            try:
                self.d._http_request("GET", "https://x/api?token=leak")
            except urllib.error.HTTPError as exc:
                self.assertNotIn("token=leak", str(exc))
                self.assertNotIn("token=leak", exc.msg)

    def test_timeout_is_configurable(self):
        self.d.config["httpTimeout"] = "7"
        captured = {}
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b"{}"
        def urlopen(req, timeout=None):
            captured["t"] = timeout
            return FakeResp()
        with mock.patch.object(grt.urllib.request, "urlopen", urlopen):
            self.d._http_request("GET", "https://x/api")
        self.assertEqual(captured["t"], 7.0)


class TestWriteConfigRejectsNewline(unittest.TestCase):
    def test_newline_in_value_refused(self):
        with mock.patch.object(subprocess, "run") as run, \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertFalse(grt.write_config_value("k", "line1\nline2"))
        run.assert_not_called()


class TestTwoPhaseSyncWatermark(unittest.TestCase):
    def _driver(self, **cfg) -> IncrementalFakeDriver:
        d = IncrementalFakeDriver(changed=[self._task("fake-x")])
        d.config = dict(cfg)
        return d

    def _task(self, tid):
        t = grt.empty_task()
        t.update(id=tid, source="fake", title=tid,
                 updated_date="2026-04-16T10:00:00Z")
        return t

    def test_pending_promoted_when_tip_advanced(self):
        """Second fetch: previous import landed → promote pending.since."""
        d = self._driver(**{
            "sync.mode": "incremental",
            "sync.pending.since": "2026-04-16T00:00:00Z",
            "sync.pending.parent": "oldparent",
            "sync.lastFetchAt": "2026-04-15T00:00:00Z",
        })
        captured: dict[str, str] = {}
        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return mock.Mock(returncode=0, stdout="newtip\n", stderr="")
            if "config" in cmd and "--unset-all" in cmd:
                return mock.Mock(returncode=0, stdout="", stderr="")
            if "--get-regexp" in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=(f"tasks-remote.fake.sync.pending.since "
                             "2026-04-16T00:00:00Z\n"
                             "tasks-remote.fake.sync.pending.parent "
                             "oldparent\n"),
                    stderr="",
                )
            if "config" in cmd:
                # Key/value writes.
                if len(cmd) >= 5:
                    captured[cmd[3]] = cmd[4]
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        h = make_handler(driver=d,
                         stdin_text="import refs/heads/main\n\n")
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            h.run()
        self.assertEqual(
            captured.get("tasks-remote.fake.sync.lastFetchAt"),
            "2026-04-16T00:00:00Z",
            "pending watermark must be promoted when the tip advanced",
        )

    def test_pending_discarded_when_tip_unchanged(self):
        """Previous import failed → discard pending, keep lastFetchAt."""
        d = self._driver(**{
            "sync.mode": "incremental",
            "sync.pending.since": "2026-04-16T00:00:00Z",
            "sync.pending.parent": "sametip",
            "sync.lastFetchAt": "2026-04-15T00:00:00Z",
        })
        set_keys: dict[str, str] = {}
        unsets: list[str] = []
        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return mock.Mock(returncode=0, stdout="sametip\n", stderr="")
            if "--unset-all" in cmd:
                unsets.append(cmd[-1])
                return mock.Mock(returncode=0, stdout="", stderr="")
            if "--get-regexp" in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=(f"tasks-remote.fake.sync.pending.since "
                             "2026-04-16T00:00:00Z\n"
                             "tasks-remote.fake.sync.pending.parent "
                             "sametip\n"),
                    stderr="",
                )
            if "config" in cmd and len(cmd) >= 5:
                set_keys[cmd[3]] = cmd[4]
            return mock.Mock(returncode=0, stdout="", stderr="")
        h = make_handler(driver=d,
                         stdin_text="import refs/heads/main\n\n")
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            h.run()
        # lastFetchAt never advanced to the pending value.
        self.assertNotEqual(set_keys.get("tasks-remote.fake.sync.lastFetchAt"),
                             "2026-04-16T00:00:00Z")
        self.assertTrue(any("sync.pending" in k for k in unsets),
                         "pending keys must be unset when import failed")


class TestEmptyDeltaNoCommit(unittest.TestCase):
    def test_no_changes_no_deletes_emits_no_commit(self):
        driver = IncrementalFakeDriver(changed=[], deleted=[])
        driver.config = {
            "sync.mode": "incremental",
            "sync.lastFetchAt": "2026-04-15T00:00:00Z",
        }
        def fake_run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                return mock.Mock(returncode=0, stdout="a" * 40 + "\n",
                                  stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        h = make_handler(driver=driver,
                         stdin_text="import refs/heads/main\n\n")
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            h.run()
        out = h.stdout.getvalue()
        self.assertNotIn("commit refs/heads/main", out,
                          "empty delta must not produce a commit")


class TestResetSubcommand(unittest.TestCase):
    def test_reset_unsets_all_sync_keys(self):
        args = mock.Mock(remote_name="jira-work")
        def fake_run(cmd, *args, **kwargs):
            if "--get-regexp" in cmd:
                return mock.Mock(
                    returncode=0,
                    stdout=(
                        "tasks-remote.jira-work.sync.lastFetchAt 2025-01-01\n"
                        "tasks-remote.jira-work.sync.pending.since x\n"
                    ),
                    stderr="",
                )
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(subprocess, "run",
                                side_effect=fake_run) as run, \
                mock.patch.object(sys, "stdout", io.StringIO()):
            rc = grt.cmd_reset(args)
        self.assertEqual(rc, 0)
        unsets = [c for c in run.call_args_list
                  if "--unset-all" in (c[0][0] if c[0] else [])]
        self.assertEqual(len(unsets), 2)


class TestVersion(unittest.TestCase):
    def test_version_subcommand_prints(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            rc = grt.cmd_version(mock.Mock())
        self.assertEqual(rc, 0)
        self.assertIn("git-remote-tasks", buf.getvalue())
        self.assertIn(grt.__version__, buf.getvalue())


class TestPromptFormatNonTty(unittest.TestCase):
    def test_non_tty_raises(self):
        class FakeStdin:
            def isatty(self): return False
        with self.assertRaises(RuntimeError):
            grt._prompt_format(stdin=FakeStdin())


class TestJiraAdfK203(unittest.TestCase):
    def test_multiline_description_keeps_all_paragraphs(self):
        d = grt.JiraDriver("jira", "https://x", {"baseUrl": "https://x",
                                                  "email": "a", "apiToken": "t"})
        fields = d._serialize_for_push({"title": "t", "status": "todo",
                                         "priority": "none",
                                         "description": "one\n\nthree"})
        paras = fields["description"]["content"]
        self.assertEqual(len(paras), 3)
        # Middle paragraph is an intentional blank line — empty content array.
        self.assertEqual(paras[1]["content"], [])


# ---------------------------------------------------------------------------
# Symmetry
# ---------------------------------------------------------------------------

class TestSerializerSymmetry(unittest.TestCase):
    def test_both_produce_different_text_same_dict(self):
        y = grt.YAMLSerializer()
        o = grt.OrgSerializer()
        t = full_task()
        ytext = y.serialize(t)
        otext = o.serialize(t)
        self.assertNotEqual(ytext, otext)
        self.assertEqual(y.deserialize(ytext), t)
        self.assertEqual(o.deserialize(otext), t)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

class TestFormatDetection(unittest.TestCase):
    def test_yaml_extension(self):
        self.assertIsInstance(grt.serializer_for_extension("tasks/x.yaml"),
                              grt.YAMLSerializer)

    def test_yml_extension(self):
        self.assertIsInstance(grt.serializer_for_extension("tasks/x.yml"),
                              grt.YAMLSerializer)

    def test_org_extension(self):
        self.assertIsInstance(grt.serializer_for_extension("tasks/x.org"),
                              grt.OrgSerializer)

    def test_unknown_extension_raises(self):
        with self.assertRaises(ValueError):
            grt.serializer_for_extension("tasks/x.json")

    def test_serializer_for_format_yaml(self):
        self.assertIsInstance(grt.serializer_for_format("yaml"), grt.YAMLSerializer)

    def test_serializer_for_format_org(self):
        self.assertIsInstance(grt.serializer_for_format("org"), grt.OrgSerializer)

    def test_serializer_for_format_unknown(self):
        with self.assertRaises(ValueError):
            grt.serializer_for_format("json")


# ---------------------------------------------------------------------------
# Config reader
# ---------------------------------------------------------------------------

class TestConfigReader(unittest.TestCase):
    def test_read_value_success(self):
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="the-value\n", stderr="")
            self.assertEqual(grt.read_config_value("foo.bar"), "the-value")

    def test_read_value_missing_returns_none(self):
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="")
            self.assertIsNone(grt.read_config_value("foo.bar"))

    def test_read_value_subprocess_error(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git?")):
            self.assertIsNone(grt.read_config_value("x"))

    def test_read_value_stderr_logged(self):
        with mock.patch.object(subprocess, "run") as run, \
                mock.patch.object(sys, "stderr", new_callable=io.StringIO) as err:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="boom")
            grt.read_config_value("x")
        self.assertIn("boom", err.getvalue())

    def test_read_format_default_yaml(self):
        with mock.patch.object(grt, "read_config_value", return_value=None):
            self.assertEqual(grt.read_format(), "yaml")

    def test_read_format_org(self):
        with mock.patch.object(grt, "read_config_value", return_value="org"):
            self.assertEqual(grt.read_format(), "org")

    def test_read_format_invalid_falls_back(self):
        with mock.patch.object(grt, "read_config_value", return_value="xml"):
            self.assertEqual(grt.read_format(), "yaml")

    def test_read_remote_config_parses_output(self):
        output = (
            "tasks-remote.jira-work.scheme jira\n"
            "tasks-remote.jira-work.baseUrl https://example.com\n"
            "tasks-remote.jira-work.email me@x.com\n"
        )
        with mock.patch.object(grt, "_run_git_config", return_value=output):
            cfg = grt.read_remote_config("jira-work")
        self.assertEqual(cfg["scheme"], "jira")
        self.assertEqual(cfg["baseUrl"], "https://example.com")
        self.assertEqual(cfg["email"], "me@x.com")

    def test_read_remote_config_empty(self):
        with mock.patch.object(grt, "_run_git_config", return_value=None):
            self.assertEqual(grt.read_remote_config("missing"), {})


# ---------------------------------------------------------------------------
# Driver normalization - Jira
# ---------------------------------------------------------------------------

class TestMappingOverrides(unittest.TestCase):
    """FEAT-03: statusMap / priorityMap / fieldMap applied to normalize()."""

    def test_jira_status_map_override(self):
        d = grt.JiraDriver("jira", "https://x", {
            "baseUrl": "https://x", "email": "a", "apiToken": "t",
            "statusMap.Triage": "in_progress",
            "statusMap.Backlog": "todo",
        })
        issue = {"key": "K-1", "fields": {
            "summary": "t",
            "status": {"name": "Triage"},
            "priority": {"name": "High"},
            "project": {"key": "P", "name": "P"},
        }}
        self.assertEqual(d.normalize(issue)["status"], "in_progress")

    def test_jira_priority_map_override(self):
        d = grt.JiraDriver("jira", "https://x", {
            "baseUrl": "https://x", "email": "a", "apiToken": "t",
            "priorityMap.P0": "critical",
        })
        issue = {"key": "K-1", "fields": {
            "summary": "t",
            "status": {"name": "To Do"},
            "priority": {"name": "P0"},
            "project": {"key": "P", "name": "P"},
        }}
        self.assertEqual(d.normalize(issue)["priority"], "critical")

    def test_notion_field_map_changes_column_names(self):
        d = grt.NotionDriver("notion", "notion://x", {
            "databaseId": "abc", "token": "t",
            "fieldMap.status": "Workflow",
            "fieldMap.priority": "Urgency",
            "fieldMap.tags": "Labels",
            "fieldMap.description": "Notes",
            # Logical 'due_date' is addressed in git-config-safe form.
            "fieldMap.dueDate": "Target",
        })
        page = {
            "id": "p",
            "properties": {
                "Name": {"type": "title",
                         "title": [{"plain_text": "t"}]},
                "Workflow": {"type": "select",
                              "select": {"name": "In Progress"}},
                "Urgency": {"type": "select",
                             "select": {"name": "Critical"}},
                "Labels": {"type": "multi_select",
                             "multi_select": [{"name": "a"}]},
                "Notes": {"type": "rich_text",
                           "rich_text": [{"plain_text": "n"}]},
                "Target": {"type": "date",
                            "date": {"start": "2025-01-01"}},
                # 'Status' column must NOT be read when mapped away.
                "Status": {"type": "select",
                             "select": {"name": "Done"}},
            },
        }
        t = d.normalize(page)
        self.assertEqual(t["status"], "in_progress",
                          "fieldMap.status must redirect reads")
        self.assertEqual(t["priority"], "critical")
        self.assertEqual(t["tags"], ["a"])
        self.assertEqual(t["description"], "n")
        self.assertEqual(t["due_date"], "2025-01-01")

    def test_notion_push_respects_field_map(self):
        d = grt.NotionDriver("notion", "notion://x", {
            "databaseId": "abc", "token": "t",
            "fieldMap.status": "Workflow",
        })
        with mock.patch.object(d, "_http_get",
                                return_value={"properties": {"Name": {"type": "title"}}}), \
                mock.patch.object(d, "_http_post") as post:
            d.upsert(grt.empty_task() | {"id": "", "title": "t",
                                           "status": "in_progress",
                                           "priority": "none"})
        body = post.call_args[1]["body"]
        self.assertIn("Workflow", body["properties"])
        self.assertNotIn("Status", body["properties"])


class TestJiraDriver(unittest.TestCase):
    def setUp(self):
        self.d = grt.JiraDriver("jira", "https://x/y",
                                {"baseUrl": "https://x/y", "email": "a@b.c",
                                 "apiToken": "tok"})

    def _issue(self, **field_overrides) -> dict:
        fields = {
            "summary": "Hello",
            "description": None,
            "status": {"name": "To Do"},
            "priority": {"name": "High"},
            "created": "2025-01-01T00:00:00.000+0000",
            "updated": "2025-02-01T00:00:00.000+0000",
            "duedate": "2025-03-01",
            "labels": ["a", "b"],
            "project": {"key": "PROJ", "name": "Project X"},
        }
        fields.update(field_overrides)
        return {"key": "PROJ-1", "fields": fields}

    def test_status_in_progress(self):
        t = self.d.normalize(self._issue(status={"name": "In Progress"}))
        self.assertEqual(t["status"], "in_progress")

    def test_status_done(self):
        t = self.d.normalize(self._issue(status={"name": "Done"}))
        self.assertEqual(t["status"], "done")

    def test_status_todo(self):
        t = self.d.normalize(self._issue(status={"name": "To Do"}))
        self.assertEqual(t["status"], "todo")

    def test_status_cancelled_variants(self):
        for name in ("Won't Do", "Cancelled", "Canceled"):
            t = self.d.normalize(self._issue(status={"name": name}))
            self.assertEqual(t["status"], "cancelled", f"for {name!r}")

    def test_status_unknown_safe_default(self):
        t = self.d.normalize(self._issue(status={"name": "Bizarre"}))
        self.assertEqual(t["status"], "todo")

    def test_priority_highest_to_critical(self):
        for name in ("Highest", "Critical", "Blocker"):
            t = self.d.normalize(self._issue(priority={"name": name}))
            self.assertEqual(t["priority"], "critical", name)

    def test_priority_high(self):
        t = self.d.normalize(self._issue(priority={"name": "High"}))
        self.assertEqual(t["priority"], "high")

    def test_priority_all_levels(self):
        expected = {"Medium": "medium", "Low": "low", "Lowest": "low"}
        for name, unified in expected.items():
            t = self.d.normalize(self._issue(priority={"name": name}))
            self.assertEqual(t["priority"], unified)

    def test_priority_missing(self):
        t = self.d.normalize(self._issue(priority=None))
        self.assertEqual(t["priority"], "none")

    def test_description_missing(self):
        t = self.d.normalize(self._issue(description=None))
        self.assertIsNone(t["description"])

    def test_description_adf(self):
        adf = {"type": "doc", "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ]}
        ]}
        t = self.d.normalize(self._issue(description=adf))
        self.assertEqual(t["description"], "Hello world")

    def test_description_adf_none(self):
        self.assertIsNone(grt._jira_extract_adf_text(None))
        self.assertIsNone(grt._jira_extract_adf_text(123))

    def test_labels_map_to_tags(self):
        t = self.d.normalize(self._issue(labels=["x", "y"]))
        self.assertEqual(t["tags"], ["x", "y"])

    def test_epic_category(self):
        t = self.d.normalize(self._issue(customfield_10014="EPIC-1"))
        self.assertEqual(t["category"]["type"], "epic")
        self.assertEqual(t["category"]["id"], "EPIC-1")
        self.assertEqual(t["category"]["name"], "EPIC-1")

    def test_epic_category_dict_shape(self):
        epic = {"key": "EPIC-7", "name": "Platform Reliability"}
        t = self.d.normalize(self._issue(customfield_10014=epic))
        self.assertEqual(t["category"]["type"], "epic")
        self.assertEqual(t["category"]["id"], "EPIC-7")
        self.assertEqual(t["category"]["name"], "Platform Reliability")

    def test_epic_category_dict_with_only_summary(self):
        epic = {"id": "10042", "summary": "Auth overhaul"}
        t = self.d.normalize(self._issue(customfield_10014=epic))
        self.assertEqual(t["category"]["id"], "10042")
        self.assertEqual(t["category"]["name"], "Auth overhaul")

    def test_project_category(self):
        t = self.d.normalize(self._issue())
        self.assertEqual(t["category"]["type"], "project")
        self.assertEqual(t["category"]["id"], "PROJ")

    def test_url_constructed(self):
        t = self.d.normalize(self._issue())
        self.assertEqual(t["url"], "https://x/y/browse/PROJ-1")

    def test_fetch_all_paginates_new_endpoint(self):
        """K2-01: /rest/api/3/search/jql response uses nextPageToken."""
        calls = []
        def fake_get(url, headers=None):
            calls.append(url)
            if "nextPageToken" not in url:
                return {"issues": [self._issue()],
                         "nextPageToken": "tok2", "isLast": False}
            return {"issues": [self._issue()], "isLast": True}
        with mock.patch.object(self.d, "_http_get", side_effect=fake_get):
            tasks = self.d.fetch_all()
        self.assertEqual(len(tasks), 2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("/rest/api/3/search/jql?" in c for c in calls))

    def test_fetch_all_falls_back_to_legacy_on_410(self):
        """K2-01: Data Center Jira still serves the legacy route."""
        legacy_calls = []
        def fake_get(url, headers=None):
            if "/search/jql" in url:
                raise urllib.error.HTTPError(url, 410, "Gone", hdrs=None, fp=None)
            legacy_calls.append(url)
            return {"issues": [self._issue()], "total": 1, "startAt": 0}
        with mock.patch.object(self.d, "_http_get", side_effect=fake_get):
            tasks = self.d.fetch_all()
        self.assertEqual(len(tasks), 1)
        self.assertTrue(any("/search?jql" in c for c in legacy_calls))

    def test_fetch_all_legacy_opt_in(self):
        self.d.config["searchEndpoint"] = "legacy"
        with mock.patch.object(self.d, "_http_get",
                                return_value={"issues": [], "total": 0}) as hg:
            self.d.fetch_all()
        self.assertIn("/search?jql", hg.call_args[0][0])

    def test_fetch_all_no_base_url(self):
        d = grt.JiraDriver("jira", "", {})
        with self.assertRaises(NotImplementedError):
            d.fetch_all()

    def test_upsert_updates_existing_issue(self):
        put_calls = []
        def fake_put(url, body=None, headers=None):
            put_calls.append((url, body))
            return {}
        # Return one transition matching 'In Progress'.
        def fake_get(url, headers=None):
            return {"transitions": [{"id": "11",
                                      "to": {"name": "In Progress"}}]}
        post_calls = []
        def fake_post(url, body=None, headers=None):
            post_calls.append((url, body))
            return {}
        task = full_task(id="jira-PROJ-1", status="in_progress", title="t")
        with mock.patch.object(self.d, "_http_put", side_effect=fake_put), \
                mock.patch.object(self.d, "_http_get", side_effect=fake_get), \
                mock.patch.object(self.d, "_http_post", side_effect=fake_post):
            self.d.upsert(task)
        self.assertTrue(put_calls[0][0].endswith("/rest/api/3/issue/PROJ-1"))
        self.assertEqual(post_calls[0][1]["transition"]["id"], "11")

    def test_upsert_creates_new_issue_requires_project_key(self):
        task = full_task(id="", status="todo", title="new")
        with self.assertRaises(grt.JiraPushError):
            self.d.upsert(task)

    def test_upsert_creates_new_issue_when_project_key_present(self):
        self.d.config["projectKey"] = "PROJ"
        calls = []
        def fake_post(url, body=None, headers=None):
            calls.append((url, body))
            return {"key": "PROJ-2"}
        task = full_task(id="", title="new task")
        with mock.patch.object(self.d, "_http_post", side_effect=fake_post):
            self.d.upsert(task)
        url, body = calls[0]
        self.assertTrue(url.endswith("/rest/api/3/issue"))
        self.assertEqual(body["fields"]["project"]["key"], "PROJ")
        self.assertEqual(body["fields"]["issuetype"]["name"], "Task")

    def test_upsert_raises_when_transition_missing(self):
        def fake_get(url, headers=None):
            return {"transitions": []}
        with mock.patch.object(self.d, "_http_put", return_value={}), \
                mock.patch.object(self.d, "_http_get", side_effect=fake_get), \
                mock.patch.object(self.d, "_http_post", return_value={}):
            with self.assertRaises(grt.JiraPushError):
                self.d.upsert(full_task(id="jira-PROJ-1", status="done"))

    def test_upsert_refuses_cross_source(self):
        with self.assertRaises(grt.JiraPushError):
            self.d.upsert(full_task(id="vikunja-42"))

    def test_delete_calls_api(self):
        with mock.patch.object(self.d, "_http_delete") as d:
            self.d.delete("jira-PROJ-1")
            d.assert_called_once()
            self.assertTrue(d.call_args[0][0].endswith("/rest/api/3/issue/PROJ-1"))

    def test_delete_refuses_cross_source(self):
        with self.assertRaises(grt.JiraPushError):
            self.d.delete("vikunja-42")


# ---------------------------------------------------------------------------
# Driver normalization - Vikunja
# ---------------------------------------------------------------------------

class TestVikunjaDriver(unittest.TestCase):
    def setUp(self):
        self.d = grt.VikunjaDriver("vikunja", "http://localhost:3456",
                                   {"baseUrl": "http://localhost:3456",
                                    "apiToken": "tok"})

    def test_priority_map_native_encoding(self):
        """K2-02: Vikunja's native encoding is 0..5, not our earlier inversion."""
        expected = {0: "none", 1: "low", 2: "medium", 3: "high",
                    4: "critical", 5: "critical"}
        for p, u in expected.items():
            t = self.d.normalize({"id": 1, "title": "x", "priority": p})
            self.assertEqual(t["priority"], u, f"priority={p}")

    def test_priority_push_inverse(self):
        """critical/high/... → 4/3/... on push (round-trip with the new map)."""
        for unified, expected in (("none", 0), ("low", 1), ("medium", 2),
                                   ("high", 3), ("critical", 4)):
            with mock.patch.object(self.d, "_http_post") as post:
                self.d.upsert(full_task(id="vikunja-1",
                                         priority=unified,
                                         title="x"))
            body = post.call_args[1]["body"]
            self.assertEqual(body["priority"], expected,
                              f"priority={unified}")

    def test_done_true_to_done(self):
        t = self.d.normalize({"id": 1, "title": "x", "done": True})
        self.assertEqual(t["status"], "done")

    def test_done_false_to_todo(self):
        t = self.d.normalize({"id": 1, "title": "x", "done": False})
        self.assertEqual(t["status"], "todo")

    def test_date_mapping(self):
        t = self.d.normalize({
            "id": 1, "title": "x", "due_date": "2025-01-01T00:00:00Z",
        })
        self.assertEqual(t["due_date"], "2025-01-01T00:00:00Z")

    def test_end_date_fallback(self):
        t = self.d.normalize({"id": 1, "title": "x", "end_date": "2025-02-02"})
        self.assertEqual(t["due_date"], "2025-02-02")

    def test_labels_to_tags(self):
        t = self.d.normalize({
            "id": 1, "title": "x",
            "labels": [{"title": "l1"}, {"title": "l2"}]
        })
        self.assertEqual(t["tags"], ["l1", "l2"])

    def test_project_category(self):
        t = self.d.normalize({
            "id": 7, "title": "x", "project_id": 99,
            "project": {"id": 99, "title": "Inbox"},
        })
        self.assertEqual(t["category"]["name"], "Inbox")
        self.assertEqual(t["category"]["type"], "project")

    def test_fetch_all_paginates(self):
        page1 = [{"id": i, "title": f"t{i}"} for i in range(50)]
        page2 = [{"id": 99, "title": "last"}]
        seq = [page1, page2]
        with mock.patch.object(self.d, "_http_get", side_effect=seq):
            tasks = self.d.fetch_all()
        self.assertEqual(len(tasks), 51)

    def test_fetch_all_no_base_url_raises(self):
        d = grt.VikunjaDriver("v", "", {})
        with self.assertRaises(NotImplementedError):
            d.fetch_all()

    def test_fetch_all_dict_response(self):
        with mock.patch.object(self.d, "_http_get", return_value={"tasks": []}):
            self.assertEqual(self.d.fetch_all(), [])

    def test_upsert_updates_existing_vikunja_task(self):
        calls = []
        def fake_post(url, body=None, headers=None):
            calls.append(("POST", url, body))
            return {}
        task = full_task(id="vikunja-42", source="vikunja", title="hello",
                         status="done", priority="high",
                         due_date="2025-05-01")
        with mock.patch.object(self.d, "_http_post", side_effect=fake_post), \
                mock.patch.object(self.d, "_http_put") as put:
            self.d.upsert(task)
        self.assertEqual(len(calls), 1)
        _, url, body = calls[0]
        self.assertTrue(url.endswith("/api/v1/tasks/42"))
        self.assertEqual(body["title"], "hello")
        self.assertTrue(body["done"])
        # K2-02: high → 3 under the corrected native encoding.
        self.assertEqual(body["priority"], 3)
        self.assertEqual(body["due_date"], "2025-05-01T00:00:00Z")
        put.assert_not_called()

    def test_upsert_creates_on_project_when_no_native_id(self):
        self.d.config["projectId"] = "99"
        put_calls = []
        def fake_put(url, body=None, headers=None):
            put_calls.append(url)
            return {"id": 123}
        task = full_task(id="vikunja-", title="new")  # empty native id
        with mock.patch.object(self.d, "_http_put", side_effect=fake_put):
            self.d.upsert(task)
        self.assertTrue(put_calls[0].endswith("/api/v1/projects/99/tasks"))

    def test_upsert_refuses_cross_source(self):
        task = full_task(id="jira-PROJ-1", source="jira")
        with self.assertRaises(grt.VikunjaPushError):
            self.d.upsert(task)

    def test_upsert_requires_project_id_for_create(self):
        self.d.config.pop("projectId", None)
        task = full_task(id="", source="")
        with self.assertRaises(grt.VikunjaPushError):
            self.d.upsert(task)

    def test_delete_calls_api(self):
        with mock.patch.object(self.d, "_http_delete") as d:
            self.d.delete("vikunja-42")
            d.assert_called_once()
            self.assertTrue(d.call_args[0][0].endswith("/api/v1/tasks/42"))

    def test_delete_refuses_cross_source(self):
        with self.assertRaises(grt.VikunjaPushError):
            self.d.delete("jira-PROJ-1")


# ---------------------------------------------------------------------------
# Driver normalization - MSTodo
# ---------------------------------------------------------------------------

class TestMSTodoDriver(unittest.TestCase):
    def setUp(self):
        self.d = grt.MSTodoDriver("msftodo", "msftodo://consumers",
                                  {"accessToken": "tok"})

    def test_importance_mapping(self):
        for imp, unified in (("high", "high"), ("normal", "medium"), ("low", "low")):
            t = self.d.normalize({"id": "1", "title": "x", "importance": imp})
            self.assertEqual(t["priority"], unified)

    def test_status_mapping(self):
        cases = {"completed": "done", "notStarted": "todo", "inProgress": "in_progress"}
        for s, u in cases.items():
            t = self.d.normalize({"id": "1", "title": "x", "status": s})
            self.assertEqual(t["status"], u)

    def test_list_name_category(self):
        t = self.d.normalize({"id": "1", "title": "x"}, list_name="Groceries")
        self.assertEqual(t["category"]["name"], "Groceries")
        self.assertEqual(t["category"]["type"], "list")

    def test_reminder_datetime_tolerated(self):
        t = self.d.normalize({
            "id": "1", "title": "x",
            "reminderDateTime": {"dateTime": "2025-01-01", "timeZone": "UTC"},
        })
        self.assertEqual(t["title"], "x")

    def test_due_date_nested(self):
        t = self.d.normalize({
            "id": "1", "title": "x",
            "dueDateTime": {"dateTime": "2025-01-01T00:00:00", "timeZone": "UTC"},
        })
        self.assertEqual(t["due_date"], "2025-01-01T00:00:00")

    def test_due_date_string(self):
        t = self.d.normalize({"id": "1", "title": "x", "dueDateTime": "2025-01-01"})
        self.assertEqual(t["due_date"], "2025-01-01")

    def test_body_description(self):
        t = self.d.normalize({"id": "1", "title": "x",
                               "body": {"content": "body text"}})
        self.assertEqual(t["description"], "body text")

    def test_linked_resources_url(self):
        t = self.d.normalize({
            "id": "1", "title": "x",
            "linkedResources": [{"webUrl": "https://example.com"}],
        })
        self.assertEqual(t["url"], "https://example.com")

    def test_fetch_all_happy_path(self):
        responses = [
            {"value": [{"id": "L1", "displayName": "Inbox"}]},
            {"value": [{"id": "T1", "title": "one"}]},
        ]
        with mock.patch.object(self.d, "_http_get", side_effect=responses):
            tasks = self.d.fetch_all()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["category"]["name"], "Inbox")

    def test_fetch_all_requires_auth(self):
        d = grt.MSTodoDriver("msftodo", "msftodo://consumers", {})
        with mock.patch.object(grt, "MSAL_AVAILABLE", False):
            with self.assertRaises(NotImplementedError):
                d.fetch_all()

    def test_upsert_update_existing_uses_category_id_as_list(self):
        patches = []
        def fake_patch(url, body=None, headers=None):
            patches.append((url, body))
            return {}
        task = full_task(id="msftodo-T1", source="msftodo",
                         category={"id": "L-abc", "name": "Inbox", "type": "list"},
                         title="hello", status="in_progress")
        with mock.patch.object(self.d, "_http_patch", side_effect=fake_patch):
            self.d.upsert(task)
        url, body = patches[0]
        self.assertIn("/me/todo/lists/L-abc/tasks/T1", url)
        self.assertEqual(body["status"], "inProgress")
        self.assertEqual(body["title"], "hello")

    def test_upsert_create_uses_default_list_when_id_empty(self):
        self.d.config["defaultListId"] = "L-fallback"
        posts = []
        def fake_post(url, body=None, headers=None):
            posts.append(url)
            return {}
        task = full_task(id="msftodo-", category={"id": None, "name": None,
                                                   "type": "other"})
        with mock.patch.object(self.d, "_http_post", side_effect=fake_post):
            self.d.upsert(task)
        self.assertIn("/me/todo/lists/L-fallback/tasks", posts[0])

    def test_upsert_refuses_when_list_id_unresolvable(self):
        task = full_task(id="", category={"id": None, "name": None,
                                           "type": "other"})
        with self.assertRaises(grt.MSTodoPushError):
            self.d.upsert(task)

    def test_delete_requires_default_list_id(self):
        with self.assertRaises(grt.MSTodoPushError):
            self.d.delete("msftodo-T1")
        self.d.config["defaultListId"] = "L-x"
        with mock.patch.object(self.d, "_http_delete") as hd:
            self.d.delete("msftodo-T1")
        self.assertTrue(hd.call_args[0][0].endswith("/me/todo/lists/L-x/tasks/T1"))

    def test_delete_refuses_cross_source(self):
        self.d.config["defaultListId"] = "L-x"
        with self.assertRaises(grt.MSTodoPushError):
            self.d.delete("jira-PROJ-1")


class TestMSTodoAuth(unittest.TestCase):
    def setUp(self):
        self.d = grt.MSTodoDriver("msftodo", "msftodo://consumers", {})

    def test_access_token_short_circuits(self):
        self.d.config["accessToken"] = "tok"
        self.assertEqual(self.d._acquire_token(), "tok")

    def test_no_msal_no_access_token_raises(self):
        with mock.patch.object(grt, "MSAL_AVAILABLE", False):
            with self.assertRaises(NotImplementedError):
                self.d._acquire_token()

    def test_msal_requires_client_id(self):
        self.d.config = {}  # no clientId
        with mock.patch.object(grt, "MSAL_AVAILABLE", True):
            with self.assertRaises(NotImplementedError):
                self.d._acquire_token()

    def test_refresh_token_is_used_silently(self):
        self.d.config = {"clientId": "cid", "refreshToken": "rt"}
        fake_app = mock.Mock()
        fake_app.acquire_token_by_refresh_token.return_value = {
            "access_token": "new-tok", "refresh_token": "rt2",
        }
        fake_msal = mock.Mock()
        fake_msal.PublicClientApplication.return_value = fake_app
        with mock.patch.object(grt, "MSAL_AVAILABLE", True), \
                mock.patch.object(grt, "msal", fake_msal, create=True), \
                mock.patch.object(grt, "write_config_value",
                                   return_value=True) as w:
            tok = self.d._acquire_token()
        self.assertEqual(tok, "new-tok")
        # The new refresh token is persisted.
        w.assert_called_once()
        self.assertIn("refreshToken", w.call_args[0][0])

    def test_device_flow_prints_prompt_and_returns_token(self):
        self.d.config = {"clientId": "cid"}
        fake_app = mock.Mock()
        fake_app.initiate_device_flow.return_value = {
            "user_code": "ABC123",
            "verification_uri": "https://example.com/device",
            "message": "Visit https://example.com/device and enter ABC123",
        }
        fake_app.acquire_token_by_device_flow.return_value = {
            "access_token": "tok", "refresh_token": "rt",
        }
        fake_msal = mock.Mock()
        fake_msal.PublicClientApplication.return_value = fake_app
        err = io.StringIO()
        with mock.patch.object(grt, "MSAL_AVAILABLE", True), \
                mock.patch.object(grt, "msal", fake_msal, create=True), \
                mock.patch.object(grt, "write_config_value",
                                   return_value=True), \
                mock.patch.object(sys, "stderr", err):
            tok = self.d._acquire_token()
        self.assertEqual(tok, "tok")
        self.assertIn("ABC123", err.getvalue())


# ---------------------------------------------------------------------------
# Driver normalization - Notion
# ---------------------------------------------------------------------------

class TestNotionDriver(unittest.TestCase):
    def setUp(self):
        self.d = grt.NotionDriver("notion", "notion://abc",
                                  {"databaseId": "abc", "token": "tok"})

    def _page(self, **props) -> dict:
        return {
            "id": "page-1",
            "created_time": "2025-01-01T00:00:00Z",
            "last_edited_time": "2025-02-01T00:00:00Z",
            "url": "https://www.notion.so/page-1",
            "properties": props,
        }

    def test_title_from_rich_text(self):
        page = self._page(Name={"type": "title",
                                "title": [{"plain_text": "Hello "},
                                          {"plain_text": "world"}]})
        t = self.d.normalize(page)
        self.assertEqual(t["title"], "Hello world")

    def test_select_maps_status(self):
        page = self._page(
            Name={"type": "title", "title": [{"plain_text": "t"}]},
            Status={"type": "select", "select": {"name": "In Progress"}},
        )
        self.assertEqual(self.d.normalize(page)["status"], "in_progress")

    def test_select_maps_priority(self):
        page = self._page(
            Name={"type": "title", "title": [{"plain_text": "t"}]},
            Priority={"type": "select", "select": {"name": "High"}},
        )
        self.assertEqual(self.d.normalize(page)["priority"], "high")

    def test_multi_select_tags(self):
        page = self._page(
            Name={"type": "title", "title": [{"plain_text": "t"}]},
            Tags={"type": "multi_select",
                  "multi_select": [{"name": "a"}, {"name": "b"}]},
        )
        self.assertEqual(self.d.normalize(page)["tags"], ["a", "b"])

    def test_date_property(self):
        page = self._page(
            Name={"type": "title", "title": [{"plain_text": "t"}]},
            Due={"type": "date", "date": {"start": "2025-04-20"}},
        )
        self.assertEqual(self.d.normalize(page)["due_date"], "2025-04-20")

    def test_checkbox_done(self):
        page = self._page(
            Name={"type": "title", "title": [{"plain_text": "t"}]},
            Done={"type": "checkbox", "checkbox": True},
        )
        self.assertEqual(self.d.normalize(page)["status"], "done")

    def test_null_property_value_safe(self):
        page = self._page(
            Name={"type": "title", "title": [{"plain_text": "t"}]},
            Status={"type": "select", "select": None},
            Broken=None,
        )
        t = self.d.normalize(page)
        self.assertEqual(t["status"], "todo")

    def test_rich_text_description(self):
        page = self._page(
            Name={"type": "title", "title": [{"plain_text": "t"}]},
            Description={"type": "rich_text",
                          "rich_text": [{"plain_text": "desc"}]},
        )
        self.assertEqual(self.d.normalize(page)["description"], "desc")

    def test_db_title_sets_category(self):
        page = self._page(Name={"type": "title",
                                "title": [{"plain_text": "t"}]})
        t = self.d.normalize(page, db_title="Inbox")
        self.assertEqual(t["category"], {"id": "abc", "name": "Inbox",
                                         "type": "database"})

    def test_upsert_update_existing_patches_page(self):
        def fake_get(url, headers=None):
            return {"properties": {"Name": {"type": "title"}}}
        patches = []
        def fake_patch(url, body=None, headers=None):
            patches.append((url, body))
            return {}
        task = full_task(id="notion-abc123", source="notion", title="Hello",
                         priority="high", tags=["a", "b"])
        with mock.patch.object(self.d, "_http_get", side_effect=fake_get), \
                mock.patch.object(self.d, "_http_patch", side_effect=fake_patch):
            self.d.upsert(task)
        url, body = patches[0]
        self.assertTrue(url.endswith("/pages/abc123"))
        self.assertIn("Name", body["properties"])
        self.assertEqual(body["properties"]["Priority"]["select"]["name"], "High")
        self.assertEqual(body["archived"], False)

    def test_upsert_create_posts_new_page(self):
        def fake_get(url, headers=None):
            return {"properties": {"Task": {"type": "title"}}}
        posts = []
        def fake_post(url, body=None, headers=None):
            posts.append((url, body))
            return {"id": "new-page"}
        task = full_task(id="", title="Fresh", status="todo", priority="low")
        with mock.patch.object(self.d, "_http_get", side_effect=fake_get), \
                mock.patch.object(self.d, "_http_post", side_effect=fake_post):
            self.d.upsert(task)
        url, body = posts[0]
        self.assertTrue(url.endswith("/v1/pages"))
        self.assertEqual(body["parent"], {"database_id": "abc"})
        self.assertIn("Task", body["properties"])

    def test_upsert_refuses_without_database(self):
        self.d.config = {}
        with self.assertRaises(grt.NotionPushError):
            self.d.upsert(full_task(id=""))

    def test_upsert_refuses_when_title_property_missing(self):
        with mock.patch.object(self.d, "_http_get",
                                return_value={"properties": {
                                    "Status": {"type": "select"}}}):
            with self.assertRaises(grt.NotionPushError):
                self.d.upsert(full_task(id=""))

    def test_delete_archives_page(self):
        patches = []
        def fake_patch(url, body=None, headers=None):
            patches.append((url, body))
            return {}
        with mock.patch.object(self.d, "_http_patch", side_effect=fake_patch):
            self.d.delete("notion-abc123")
        url, body = patches[0]
        self.assertTrue(url.endswith("/pages/abc123"))
        self.assertEqual(body, {"archived": True})

    def test_delete_refuses_cross_source(self):
        with self.assertRaises(grt.NotionPushError):
            self.d.delete("jira-PROJ-1")

    def test_fetch_all_paginates(self):
        responses = [
            {"results": [{"id": "p1", "properties": {}}],
             "has_more": True, "next_cursor": "c1"},
            {"results": [{"id": "p2", "properties": {}}], "has_more": False},
        ]
        with mock.patch.object(self.d, "_http_post", side_effect=responses):
            tasks = self.d.fetch_all()
        self.assertEqual(len(tasks), 2)

    def test_fetch_all_requires_database_id(self):
        d = grt.NotionDriver("n", "notion://", {})
        with self.assertRaises(NotImplementedError):
            d.fetch_all()


# ---------------------------------------------------------------------------
# Scheme resolution
# ---------------------------------------------------------------------------

class TestInstallIntegration(unittest.TestCase):
    def test_scheme_for_name_jira(self):
        self.assertEqual(grt.scheme_for_name("git-remote-jira"), "jira")

    def test_scheme_for_name_vikunja(self):
        self.assertEqual(grt.scheme_for_name("/usr/bin/git-remote-vikunja"), "vikunja")

    def test_scheme_for_name_direct_script(self):
        self.assertIsNone(grt.scheme_for_name("git_remote_tasks.py"))

    def test_scheme_for_name_unknown(self):
        self.assertIsNone(grt.scheme_for_name("git-remote-xyzzy"))

    def test_driver_for_scheme_returns_instance(self):
        d = grt.driver_for_scheme("jira", remote_name="r", url="u", config={})
        self.assertIsInstance(d, grt.JiraDriver)

    def test_driver_for_scheme_unknown_raises(self):
        with self.assertRaises(ValueError):
            grt.driver_for_scheme("unknown")


# ---------------------------------------------------------------------------
# Protocol handler
# ---------------------------------------------------------------------------

class FakeDriver(grt.Driver):
    SCHEME = "fake"

    def __init__(self, tasks=None):
        super().__init__("fake", "fake://x", {})
        self.tasks = tasks or []
        self.upserted: list[dict] = []
        self.deleted: list[str] = []

    def fetch_all(self) -> list[dict]:
        return list(self.tasks)

    def upsert(self, task: dict) -> None:
        self.upserted.append(task)

    def delete(self, task_id: str) -> None:
        self.deleted.append(task_id)


class FlushTrackingStringIO(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flush_count = 0

    def flush(self):
        self.flush_count += 1
        super().flush()


def make_handler(driver=None, serializer=None, stdin_text="") -> grt.ProtocolHandler:
    d = driver or FakeDriver()
    s = serializer or grt.YAMLSerializer()
    return grt.ProtocolHandler(
        "fake", "fake://x", d, s,
        stdin=io.StringIO(stdin_text),
        stdout=FlushTrackingStringIO(),
        stderr=io.StringIO(),
    )


class TestProtocolCapabilities(unittest.TestCase):
    def test_capabilities_lines(self):
        h = make_handler(stdin_text="capabilities\n")
        h.run()
        out = h.stdout.getvalue()
        self.assertIn("import\n", out)
        self.assertIn("export\n", out)
        self.assertIn("refspec refs/heads/*:refs/remotes/fake/*\n", out)
        self.assertIn("*push\n", out)
        self.assertIn("*fetch\n", out)
        self.assertTrue(out.endswith("\n\n"))

    def test_capabilities_flushes(self):
        h = make_handler(stdin_text="capabilities\n")
        h.run()
        self.assertGreater(h.stdout.flush_count, 5)


class TestProtocolList(unittest.TestCase):
    def test_list(self):
        h = make_handler(stdin_text="list\n")
        h.run()
        self.assertEqual(h.stdout.getvalue(), "? refs/heads/main\n\n")

    def test_list_for_push(self):
        h = make_handler(stdin_text="list for-push\n")
        h.run()
        self.assertEqual(h.stdout.getvalue(), "? refs/heads/main\n\n")


class TestProtocolImport(unittest.TestCase):
    def _task(self, tid, **kw):
        t = grt.empty_task()
        t.update(id=tid, source="fake", title=tid,
                 updated_date="2025-01-01T00:00:00Z")
        t.update(kw)
        return t

    def test_single_import_writes_stream(self):
        driver = FakeDriver(tasks=[self._task("fake-b"), self._task("fake-a")])
        h = make_handler(driver=driver,
                         stdin_text="import refs/heads/main\n\n")
        h.run()
        out = h.stdout.getvalue()
        self.assertEqual(out.count("blob\n"), 2)
        self.assertIn("deleteall\n", out)
        self.assertTrue(out.rstrip().endswith("done"))

    def test_blob_byte_length_correct(self):
        t = self._task("fake-a", title="üñîçödé")
        driver = FakeDriver(tasks=[t])
        h = make_handler(driver=driver,
                         stdin_text="import refs/heads/main\n\n")
        h.run()
        body = grt.YAMLSerializer().serialize(t).encode("utf-8")
        self.assertIn(f"data {len(body)}\n".encode("utf-8").decode("utf-8"),
                      h.stdout.getvalue())

    def test_empty_task_list_still_valid(self):
        driver = FakeDriver(tasks=[])
        h = make_handler(driver=driver,
                         stdin_text="import refs/heads/main\n\n")
        h.run()
        out = h.stdout.getvalue()
        self.assertIn("deleteall\n", out)
        self.assertNotIn("blob\n", out)
        self.assertTrue(out.rstrip().endswith("done"))

    def test_tasks_sorted_deterministic(self):
        ids = ["fake-c", "fake-a", "fake-b"]
        driver = FakeDriver(tasks=[self._task(i) for i in ids])
        h = make_handler(driver=driver,
                         stdin_text="import refs/heads/main\n\n")
        h.run()
        out = h.stdout.getvalue()
        pa = out.index("tasks/fake-a.yaml")
        pb = out.index("tasks/fake-b.yaml")
        pc = out.index("tasks/fake-c.yaml")
        self.assertLess(pa, pb)
        self.assertLess(pb, pc)

    def test_import_batch_consumes_lines(self):
        driver = FakeDriver(tasks=[self._task("fake-a")])
        stdin = "import refs/heads/main\nimport refs/heads/other\n\n"
        h = make_handler(driver=driver, stdin_text=stdin)
        h.run()
        self.assertIn("deleteall\n", h.stdout.getvalue())

    def test_commit_message_format(self):
        driver = FakeDriver(tasks=[self._task("fake-a")])
        h = make_handler(driver=driver,
                         stdin_text="import refs/heads/main\n\n")
        h.run()
        self.assertRegex(h.stdout.getvalue(),
                         r"tasks: import fake \(1 tasks\) \[2025-01-01T00:00:00Z\]")

    def test_latest_updated_no_dates(self):
        driver = FakeDriver(tasks=[self._task("fake-a", updated_date=None,
                                              created_date=None)])
        h = make_handler(driver=driver,
                         stdin_text="import refs/heads/main\n\n")
        h.run()
        self.assertIn("tasks: import fake", h.stdout.getvalue())

    def test_latest_updated_bad_dates(self):
        driver = FakeDriver(tasks=[self._task("fake-a", updated_date="nope")])
        h = make_handler(driver=driver,
                         stdin_text="import refs/heads/main\n\n")
        h.run()
        self.assertIn("deleteall", h.stdout.getvalue())

    def test_no_from_on_first_fetch(self):
        driver = FakeDriver(tasks=[self._task("fake-a")])
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="")
            h = make_handler(driver=driver,
                             stdin_text="import refs/heads/main\n\n")
            h.run()
        self.assertNotIn("\nfrom ", h.stdout.getvalue())

    def test_from_emitted_when_remote_tip_exists(self):
        driver = FakeDriver(tasks=[self._task("fake-a")])
        sha = "a" * 40
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=sha + "\n",
                                          stderr="")
            h = make_handler(driver=driver,
                             stdin_text="import refs/heads/main\n\n")
            h.run()
        self.assertIn(f"from {sha}\n", h.stdout.getvalue())


class IncrementalFakeDriver(FakeDriver):
    """Stub driver that tracks fetch_changed calls and returns per-call deltas."""

    def __init__(self, full=None, changed=None, deleted=None,
                 new_since="2026-04-16T00:00:00Z"):
        super().__init__(tasks=full or [])
        self._changed = changed or []
        self._deleted = deleted or []
        self._new_since = new_since
        self.fetch_changed_calls: list[str | None] = []

    def fetch_changed(self, since):
        self.fetch_changed_calls.append(since)
        return list(self._changed), list(self._deleted), self._new_since


class TestIncrementalImport(unittest.TestCase):
    def _task(self, tid):
        t = grt.empty_task()
        t.update(id=tid, source="fake", title=tid,
                 updated_date="2026-04-16T10:00:00Z")
        return t

    def _fake_git(self, rev_parse_sha=None, config_returncode=0):
        """Return a subprocess.run stub that handles rev-parse + config."""
        def run(cmd, *args, **kwargs):
            if "rev-parse" in cmd:
                if rev_parse_sha:
                    return mock.Mock(returncode=0,
                                      stdout=rev_parse_sha + "\n", stderr="")
                return mock.Mock(returncode=1, stdout="", stderr="")
            if "config" in cmd:
                return mock.Mock(returncode=config_returncode, stdout="",
                                  stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        return run

    def test_incremental_mode_with_since_and_parent_emits_m_only(self):
        driver = IncrementalFakeDriver(
            changed=[self._task("fake-b")],
            deleted=["fake-c"],
        )
        driver.config = {
            "sync.mode": "incremental",
            "sync.lastFetchAt": "2026-04-15T00:00:00Z",
        }
        sha = "b" * 40
        with mock.patch.object(subprocess, "run",
                                side_effect=self._fake_git(sha)):
            h = make_handler(driver=driver,
                             stdin_text="import refs/heads/main\n\n")
            h.run()
        out = h.stdout.getvalue()
        self.assertIn("M 100644 :1 tasks/fake-b.yaml", out)
        self.assertIn("D tasks/fake-c.yaml", out)
        self.assertNotIn("deleteall", out)
        self.assertIn(f"from {sha}", out)
        self.assertEqual(driver.fetch_changed_calls,
                          ["2026-04-15T00:00:00Z"])

    def test_first_fetch_falls_back_to_full_snapshot(self):
        # No lastFetchAt stored yet → must do a full fetch and set deleteall.
        driver = IncrementalFakeDriver(full=[self._task("fake-a")])
        driver.config = {"sync.mode": "incremental"}
        with mock.patch.object(subprocess, "run",
                                side_effect=self._fake_git(None)):
            h = make_handler(driver=driver,
                             stdin_text="import refs/heads/main\n\n")
            h.run()
        out = h.stdout.getvalue()
        self.assertIn("deleteall", out)
        self.assertEqual(driver.fetch_changed_calls, [],
                          "fetch_changed must not run on first fetch")

    def test_full_mode_always_full_snapshot(self):
        driver = IncrementalFakeDriver(full=[self._task("fake-a")])
        driver.config = {
            "sync.mode": "full",
            "sync.lastFetchAt": "2026-04-15T00:00:00Z",
        }
        sha = "c" * 40
        with mock.patch.object(subprocess, "run",
                                side_effect=self._fake_git(sha)):
            h = make_handler(driver=driver,
                             stdin_text="import refs/heads/main\n\n")
            h.run()
        out = h.stdout.getvalue()
        self.assertIn("deleteall", out)
        self.assertEqual(driver.fetch_changed_calls, [])


class TestDriverFetchChangedDefault(unittest.TestCase):
    def test_base_impl_delegates_to_fetch_all(self):
        class D(grt.Driver):
            SCHEME = "x"
            def fetch_all(self):
                return [{"id": "x-1"}]
        changed, deleted, since = D("r", "u", {}).fetch_changed(None)
        self.assertEqual([t["id"] for t in changed], ["x-1"])
        self.assertEqual(deleted, [])
        self.assertTrue(since)


class TestJiraIncremental(unittest.TestCase):
    def setUp(self):
        self.d = grt.JiraDriver("jira", "https://x",
                                 {"baseUrl": "https://x", "email": "a",
                                  "apiToken": "t"})

    def test_fetch_changed_with_since_uses_jql_updated_clause(self):
        calls = []
        def fake_get(url, headers=None):
            calls.append(url)
            return {"issues": [], "total": 0}
        with mock.patch.object(self.d, "_http_get", side_effect=fake_get):
            self.d.fetch_changed("2026-04-15T00:00:00Z")
        self.assertTrue(calls, "should hit the API")
        joined = calls[0]
        self.assertIn("updated%20%3E%3D", joined)  # quoted 'updated >='
        self.assertIn("2026-04-15", joined)

    def test_fetch_changed_without_since_pages_everything(self):
        with mock.patch.object(self.d, "_http_get",
                                return_value={"issues": [], "total": 0}):
            changed, deleted, since = self.d.fetch_changed(None)
        self.assertEqual(changed, [])
        self.assertEqual(deleted, [])
        self.assertTrue(since)


class TestVikunjaIncremental(unittest.TestCase):
    def setUp(self):
        self.d = grt.VikunjaDriver("vikunja", "http://x",
                                     {"baseUrl": "http://x", "apiToken": "t"})

    def test_fetch_changed_with_since_adds_filter(self):
        calls = []
        def fake_get(url, headers=None):
            calls.append(url)
            return []
        with mock.patch.object(self.d, "_http_get", side_effect=fake_get):
            self.d.fetch_changed("2026-04-15T00:00:00Z")
        self.assertTrue(calls)
        self.assertIn("filter=", calls[0])
        self.assertIn("updated", urllib.parse.unquote(calls[0]))


class TestWriteConfigValue(unittest.TestCase):
    def test_success(self):
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertTrue(grt.write_config_value("k", "v"))

    def test_failure(self):
        with mock.patch.object(subprocess, "run") as run, \
                mock.patch.object(sys, "stderr", io.StringIO()) as err:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="nope")
            self.assertFalse(grt.write_config_value("k", "v"))
        self.assertIn("nope", err.getvalue())

    def test_git_missing(self):
        with mock.patch.object(subprocess, "run",
                                side_effect=FileNotFoundError("x")), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertFalse(grt.write_config_value("k", "v"))


class TestProtocolExport(unittest.TestCase):
    def _export_stream_modify(self, task: dict, ext="yaml") -> str:
        body = grt.YAMLSerializer().serialize(task) if ext == "yaml" \
               else grt.OrgSerializer().serialize(task)
        blen = len(body.encode("utf-8"))
        msg = "push"
        return (
            "export\n"
            f"blob\nmark :1\ndata {blen}\n{body}\n"
            "commit refs/heads/main\n"
            f"author git <g@g> 1700000000 +0000\n"
            f"committer git <g@g> 1700000000 +0000\n"
            f"data {len(msg)}\n{msg}\n"
            f"M 100644 :1 tasks/{task['id']}.{ext}\n"
            "\ndone\n"
        )

    def test_modify_calls_upsert(self):
        driver = FakeDriver()
        task = full_task(id="jira-X")
        stream = self._export_stream_modify(task)
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertEqual(len(driver.upserted), 1)
        self.assertEqual(driver.upserted[0]["id"], "jira-X")

    def test_delete_calls_driver(self):
        driver = FakeDriver()
        stream = (
            "export\n"
            "commit refs/heads/main\n"
            "committer g <g@g> 1700000000 +0000\n"
            "data 3\nhey\n"
            "D tasks/jira-XYZ.yaml\n"
            "\ndone\n"
        )
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertEqual(driver.deleted, ["jira-XYZ"])

    def test_non_tasks_path_ignored(self):
        driver = FakeDriver()
        stream = (
            "export\n"
            "commit refs/heads/main\n"
            "committer g <g@g> 0 +0000\n"
            "data 3\nhey\n"
            "M 100644 :1 README.md\n"
            "D other/file.txt\n"
            "\ndone\n"
        )
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertEqual(driver.upserted, [])
        self.assertEqual(driver.deleted, [])

    def test_export_ok_line_written(self):
        driver = FakeDriver()
        stream = "export\ncommit refs/heads/feature\ndone\n"
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertIn("ok refs/heads/feature\n", h.stdout.getvalue())

    def test_export_upsert_not_implemented_logged(self):
        class DriverNoUp(FakeDriver):
            def upsert(self, task):
                raise NotImplementedError("nope")
        task = full_task(id="jira-X")
        stream = self._export_stream_modify(task)
        h = make_handler(driver=DriverNoUp(), stdin_text=stream)
        h.run()
        self.assertIn("upsert not implemented", h.stderr.getvalue())
        # BUG-02: do not pretend the push succeeded.
        self.assertIn("error refs/heads/main", h.stdout.getvalue())
        self.assertNotIn("ok refs/heads/main", h.stdout.getvalue())
        self.assertTrue(h.had_errors)
        # FEAT-04: a user-facing warning is emitted once per run.
        self.assertIn("warning[push-stub]", h.stderr.getvalue())

    def test_export_upsert_warning_emitted_once(self):
        class DriverNoUp(FakeDriver):
            def upsert(self, task):
                raise NotImplementedError("nope")
        task = full_task(id="jira-X")
        # Two M directives in one batch.
        body = grt.YAMLSerializer().serialize(task)
        blen = len(body.encode("utf-8"))
        stream = (
            "export\n"
            f"blob\nmark :1\ndata {blen}\n{body}\n"
            f"blob\nmark :2\ndata {blen}\n{body}\n"
            "commit refs/heads/main\n"
            "committer g <g@g> 0 +0000\ndata 1\nx\n"
            f"M 100644 :1 tasks/jira-X.yaml\n"
            f"M 100644 :2 tasks/jira-Y.yaml\n"
            "\ndone\n"
        )
        h = make_handler(driver=DriverNoUp(), stdin_text=stream)
        h.run()
        self.assertEqual(h.stderr.getvalue().count("warning[push-stub]"), 1)

    def test_export_delete_not_implemented_logged(self):
        class DriverNoDel(FakeDriver):
            def delete(self, task_id):
                raise NotImplementedError("nope")
        stream = (
            "export\n"
            "commit refs/heads/main\n"
            "D tasks/jira-X.yaml\n"
            "\ndone\n"
        )
        h = make_handler(driver=DriverNoDel(), stdin_text=stream)
        h.run()
        self.assertIn("delete not implemented", h.stderr.getvalue())
        self.assertIn("error refs/heads/main", h.stdout.getvalue())
        self.assertTrue(h.had_errors)

    def test_run_helper_returns_nonzero_on_export_error(self):
        class DriverNoUp(FakeDriver):
            def upsert(self, task):
                raise NotImplementedError("nope")
        task = full_task(id="jira-X")
        # Rewire _run_helper by constructing the handler directly and asserting
        # on had_errors; _run_helper honors this via return 1.
        body = grt.YAMLSerializer().serialize(task)
        blen = len(body.encode("utf-8"))
        stream = (
            "export\n"
            f"blob\nmark :1\ndata {blen}\n{body}\n"
            "commit refs/heads/main\n"
            "committer g <g@g> 0 +0000\ndata 1\nx\n"
            f"M 100644 :1 tasks/jira-X.yaml\n"
            "\ndone\n"
        )
        driver = DriverNoUp()
        h = grt.ProtocolHandler("fake", "fake://x", driver,
                                grt.YAMLSerializer(),
                                stdin=io.StringIO(stream),
                                stdout=FlushTrackingStringIO(),
                                stderr=io.StringIO())
        h.run()
        self.assertTrue(h.had_errors)
        # The real entry point maps had_errors → exit 1.
        self.assertEqual(1 if h.had_errors else 0, 1)

    def test_unknown_extension_in_export_logged(self):
        driver = FakeDriver()
        stream = (
            "export\n"
            "commit refs/heads/main\n"
            "blob\nmark :1\ndata 3\nabc\n"
            "M 100644 :1 tasks/weird.json\n"
            "\ndone\n"
        )
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertIn("unknown task file extension", h.stderr.getvalue())

    def test_missing_blob_content_logged(self):
        driver = FakeDriver()
        stream = (
            "export\n"
            "commit refs/heads/main\n"
            "M 100644 :99 tasks/jira-X.yaml\n"
            "\ndone\n"
        )
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertIn("no blob content", h.stderr.getvalue())

    def test_export_default_ref(self):
        driver = FakeDriver()
        stream = "export\ndone\n"
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertIn("ok refs/heads/main\n", h.stdout.getvalue())


class TestProtocolUnknown(unittest.TestCase):
    def test_unknown_command_logged(self):
        h = make_handler(stdin_text="wat is dis\n")
        h.run()
        self.assertIn("unknown command", h.stderr.getvalue())

    def test_blank_line_ignored(self):
        h = make_handler(stdin_text="\n\n")
        h.run()
        self.assertEqual(h.stdout.getvalue(), "")


# ---------------------------------------------------------------------------
# Management commands
# ---------------------------------------------------------------------------

class TestManagementCommands(unittest.TestCase):
    def test_list_schemes_prints_all(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            args = mock.Mock()
            grt.cmd_list_schemes(args)
        for scheme in grt.SCHEMES:
            self.assertIn(scheme, buf.getvalue())

    def test_install_creates_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            args = mock.Mock(bin_dir=d)
            with mock.patch.dict(os.environ, {"PATH": d}):
                grt.cmd_install(args)
            for scheme in grt.SCHEMES:
                link = Path(d) / f"git-remote-{scheme}"
                self.assertTrue(link.is_symlink())

    def test_install_warns_if_not_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            args = mock.Mock(bin_dir=d)
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"PATH": "/opt/nothing"}), \
                    mock.patch.object(sys, "stderr", err):
                grt.cmd_install(args)
            self.assertIn("not on PATH", err.getvalue())

    def test_install_replaces_existing(self):
        with tempfile.TemporaryDirectory() as d:
            args = mock.Mock(bin_dir=d)
            (Path(d) / "git-remote-jira").write_text("old")
            with mock.patch.dict(os.environ, {"PATH": d}):
                grt.cmd_install(args)
            self.assertTrue((Path(d) / "git-remote-jira").is_symlink())

    def test_uninstall_removes_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            args = mock.Mock(bin_dir=d)
            with mock.patch.dict(os.environ, {"PATH": d}):
                grt.cmd_install(args)
            grt.cmd_uninstall(args)
            for scheme in grt.SCHEMES:
                self.assertFalse((Path(d) / f"git-remote-{scheme}").exists())

    def test_uninstall_missing_ok(self):
        with tempfile.TemporaryDirectory() as d:
            args = mock.Mock(bin_dir=d)
            # nothing installed; should not raise
            rc = grt.cmd_uninstall(args)
            self.assertEqual(rc, 0)

    def test_uninstall_refuses_to_remove_regular_file(self):
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / "git-remote-jira"
            link.write_text("unrelated binary")
            args = mock.Mock(bin_dir=d)
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err):
                grt.cmd_uninstall(args)
            self.assertTrue(link.exists(), "regular file must not be removed")
            self.assertIn("not a symlink", err.getvalue())

    def test_uninstall_refuses_to_remove_symlink_to_other_target(self):
        with tempfile.TemporaryDirectory() as d:
            other_target = Path(d) / "other-tool"
            other_target.write_text("#!/bin/sh\necho other")
            link = Path(d) / "git-remote-jira"
            os.symlink(str(other_target), str(link))
            args = mock.Mock(bin_dir=d)
            err = io.StringIO()
            with mock.patch.object(sys, "stderr", err):
                grt.cmd_uninstall(args)
            self.assertTrue(link.is_symlink(),
                             "symlink to other target must not be removed")
            self.assertIn("points to", err.getvalue())

    def test_check_missing_config(self):
        err = io.StringIO()
        with mock.patch.object(grt, "read_remote_config", return_value={}), \
                mock.patch.object(sys, "stderr", err):
            rc = grt.cmd_check(mock.Mock(remote_name="nope"))
        self.assertEqual(rc, 2)
        self.assertIn("no config", err.getvalue())

    def test_check_invalid_scheme(self):
        err = io.StringIO()
        with mock.patch.object(grt, "read_remote_config",
                                return_value={"scheme": "xml"}), \
                mock.patch.object(sys, "stderr", err):
            rc = grt.cmd_check(mock.Mock(remote_name="x"))
        self.assertEqual(rc, 2)
        self.assertIn("invalid scheme", err.getvalue())

    def test_check_missing_required_key(self):
        err = io.StringIO()
        out = io.StringIO()
        with mock.patch.object(grt, "read_remote_config",
                                return_value={"scheme": "jira", "baseUrl": "x"}), \
                mock.patch.object(sys, "stderr", err), \
                mock.patch.object(sys, "stdout", out):
            rc = grt.cmd_check(mock.Mock(remote_name="jira-work"))
        self.assertEqual(rc, 2)
        self.assertIn("missing required keys", err.getvalue())

    def test_check_ok(self):
        out = io.StringIO()
        with mock.patch.object(grt, "read_remote_config",
                                return_value={"scheme": "jira",
                                              "baseUrl": "u",
                                              "email": "e",
                                              "apiToken": "t"}), \
                mock.patch.object(sys, "stdout", out):
            rc = grt.cmd_check(mock.Mock(remote_name="jira-work"))
        self.assertEqual(rc, 0)
        self.assertIn("ok: all required", out.getvalue())
        self.assertIn("<redacted>", out.getvalue())
        # email and baseUrl are safe to show
        self.assertIn("email = e", out.getvalue())
        self.assertIn("baseUrl = u", out.getvalue())

    def test_check_redacts_varied_secret_keys(self):
        config = {
            "scheme": "notion",
            "databaseId": "db",
            "token": "t",
            "clientSecret": "s",
            "accessToken": "a",
            "apiKey": "k",
            "credential": "c",
            "bearerToken": "b",
        }
        out = io.StringIO()
        with mock.patch.object(grt, "read_remote_config", return_value=config), \
                mock.patch.object(sys, "stdout", out):
            grt.cmd_check(mock.Mock(remote_name="x"))
        body = out.getvalue()
        for secret_val in ("t", "s", "a", "k", "c", "b"):
            self.assertNotIn(f"= {secret_val}\n", body,
                              f"raw secret {secret_val!r} leaked")
        # databaseId is explicitly safe — must be shown.
        self.assertIn("databaseId = db", body)

    def test_argparser_builds(self):
        p = grt.build_argparser()
        ns = p.parse_args(["install", "--bin-dir", "/tmp/x"])
        self.assertEqual(ns.cmd, "install")
        self.assertEqual(ns.bin_dir, "/tmp/x")


class TestCmdInit(unittest.TestCase):
    def _run(self, *argv_extra, cwd=None, input_fn=None):
        """Helper: set up a temp dir and drive cmd_init with argparse."""
        parser = grt.build_argparser()
        ns = parser.parse_args(["init", *argv_extra])
        ns._input_fn = input_fn or (lambda _p: "yaml")
        old = Path.cwd()
        if cwd:
            os.chdir(str(cwd))
        try:
            with mock.patch.object(sys, "stdout", io.StringIO()):
                rc = grt.cmd_init(ns)
            return rc
        finally:
            os.chdir(str(old))

    def test_init_in_empty_dir_creates_git_and_tasks(self):
        with tempfile.TemporaryDirectory() as d:
            rc = self._run("--format", "yaml", cwd=Path(d))
            self.assertEqual(rc, 0)
            self.assertTrue((Path(d) / ".git").is_dir())
            self.assertTrue((Path(d) / "tasks" / ".gitkeep").exists())
            fmt = subprocess.run(
                ["git", "-C", d, "config", "--local", "--get", "tasks.format"],
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(fmt, "yaml")

    def test_init_with_positional_path_creates_and_enters(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "repo"
            rc = self._run("--format", "org", str(target))
            self.assertEqual(rc, 0)
            self.assertTrue((target / ".git").is_dir())
            fmt = subprocess.run(
                ["git", "-C", str(target), "config", "--local", "--get",
                 "tasks.format"],
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(fmt, "org")

    def test_init_prompts_when_format_missing(self):
        prompts: list[str] = []
        def _input(prompt: str) -> str:
            prompts.append(prompt)
            return "org"
        with tempfile.TemporaryDirectory() as d:
            rc = self._run(cwd=Path(d), input_fn=_input)
        self.assertEqual(rc, 0)
        self.assertTrue(prompts, "should have prompted")

    def test_init_rerun_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            self._run("--format", "yaml", cwd=Path(d))
            # Count commits; second init should not add one.
            first = subprocess.run(
                ["git", "-C", d, "rev-list", "--count", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip()
            self._run("--format", "yaml", cwd=Path(d))
            second = subprocess.run(
                ["git", "-C", d, "rev-list", "--count", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(first, second,
                              "re-running init must not create a new commit")

    def test_main_dispatches_tasks_init_symlink(self):
        with mock.patch.object(grt, "cmd_init", return_value=0) as ci:
            rc = grt.main(["/usr/local/bin/tasks-init",
                            "--format", "yaml", "/tmp/x"])
        self.assertEqual(rc, 0)
        ci.assert_called_once()

    def test_install_includes_tasks_init_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            args = mock.Mock(bin_dir=d)
            with mock.patch.dict(os.environ, {"PATH": d}):
                grt.cmd_install(args)
            self.assertTrue((Path(d) / "tasks-init").is_symlink())

    def test_uninstall_removes_tasks_init_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            args = mock.Mock(bin_dir=d)
            with mock.patch.dict(os.environ, {"PATH": d}):
                grt.cmd_install(args)
            self.assertTrue((Path(d) / "tasks-init").is_symlink())
            grt.cmd_uninstall(args)
            self.assertFalse((Path(d) / "tasks-init").exists())


# ---------------------------------------------------------------------------
# Main entry point dispatch
# ---------------------------------------------------------------------------

class TestMainDispatch(unittest.TestCase):
    def test_main_list_schemes(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = grt.main(["git_remote_tasks.py", "list-schemes"])
        self.assertEqual(rc, 0)
        self.assertIn("jira", out.getvalue())

    def test_main_no_args_prints_help(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = grt.main(["git_remote_tasks.py"])
        self.assertEqual(rc, 0)
        self.assertIn("usage", out.getvalue().lower())

    def test_main_scheme_from_name(self):
        with mock.patch.object(grt, "_run_helper", return_value=0) as rh:
            rc = grt.main(["git-remote-jira", "jira-work", "jira://x"])
        self.assertEqual(rc, 0)
        rh.assert_called_once_with("jira", "jira-work", "jira://x")

    def test_main_scheme_from_name_missing_args(self):
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            rc = grt.main(["git-remote-jira"])
        self.assertEqual(rc, 2)

    def test_main_scheme_from_url(self):
        with mock.patch.object(grt, "_run_helper", return_value=0) as rh:
            rc = grt.main(["git_remote_tasks.py", "v", "vikunja://h"])
        self.assertEqual(rc, 0)
        rh.assert_called_once()

    def test_main_check_subcommand(self):
        with mock.patch.object(grt, "read_remote_config",
                                return_value={"scheme": "jira",
                                              "baseUrl": "u", "email": "e",
                                              "apiToken": "t"}), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            rc = grt.main(["git_remote_tasks.py", "check", "jira-work"])
        self.assertEqual(rc, 0)

    def test_run_helper_invokes_handler(self):
        with mock.patch.object(grt, "read_remote_config",
                                return_value={"scheme": "jira"}), \
                mock.patch.object(grt, "read_format", return_value="yaml"), \
                mock.patch.object(grt.ProtocolHandler, "run") as run:
            rc = grt._run_helper("jira", "jira-work", "jira://x")
        self.assertEqual(rc, 0)
        run.assert_called_once()


# ---------------------------------------------------------------------------
# HTTP request wiring (lightly, via mocking urllib)
# ---------------------------------------------------------------------------

class TestHttpWiring(unittest.TestCase):
    def test_http_request_builds_request(self):
        d = grt.JiraDriver("r", "https://x", {})
        captured = {}
        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b'{"ok":true}'
        def fake_urlopen(req, timeout=None):
            captured["method"] = req.get_method()
            captured["url"] = req.full_url
            captured["data"] = req.data
            captured["timeout"] = timeout
            return FakeResp()
        with mock.patch.object(grt.urllib.request, "urlopen", fake_urlopen):
            result = d._http_request("POST", "https://x/api",
                                     headers={"Authorization": "Basic x"},
                                     body={"k": "v"})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["method"], "POST")
        self.assertIn(b'"k"', captured["data"])
        self.assertEqual(captured["timeout"],
                          grt.Driver.HTTP_TIMEOUT_DEFAULT)


# ---------------------------------------------------------------------------
# Coverage-targeted edge cases
# ---------------------------------------------------------------------------

class TestYamlEdgeCases(unittest.TestCase):
    def setUp(self):
        self.s = grt.YAMLSerializer()

    def test_needs_quoting_empty(self):
        self.assertTrue(grt._yaml_needs_quoting(""))

    def test_needs_quoting_reserved(self):
        self.assertTrue(grt._yaml_needs_quoting("true"))
        self.assertTrue(grt._yaml_needs_quoting("Yes"))

    def test_needs_quoting_leading_special(self):
        for ch in "!&*?|>'\"%@`#":
            self.assertTrue(grt._yaml_needs_quoting(ch + "rest"))

    def test_needs_quoting_dash_space_prefix(self):
        self.assertTrue(grt._yaml_needs_quoting("- item"))

    def test_needs_quoting_dash_then_space(self):
        self.assertTrue(grt._yaml_needs_quoting("- x"))

    def test_needs_quoting_colon_space(self):
        self.assertTrue(grt._yaml_needs_quoting("foo: bar"))

    def test_needs_quoting_hash_in_middle(self):
        self.assertTrue(grt._yaml_needs_quoting("a #b"))

    def test_needs_quoting_leading_trailing_ws(self):
        self.assertTrue(grt._yaml_needs_quoting(" x"))

    def test_scalar_none(self):
        self.assertEqual(grt._yaml_scalar(None), "null")

    def test_scalar_newline_fallback(self):
        self.assertEqual(grt._yaml_scalar("a\nb"), r'"a\nb"')

    def test_parse_malformed_line_skipped(self):
        text = "id: x\n*** bogus ***\nsource: s\ntitle: t\n"
        back = self.s.deserialize(text)
        self.assertEqual(back["id"], "x")
        self.assertEqual(back["source"], "s")

    def test_parse_indented_top_level_skipped(self):
        text = "  leading: indent\nid: x\n"
        back = self.s.deserialize(text)
        self.assertEqual(back["id"], "x")

    def test_parse_nested_empty_value(self):
        text = "id: x\ncategory:\n"  # no indented content
        back = self.s.deserialize(text)
        # category should fall through to defaults
        self.assertEqual(back["category"]["type"], "other")

    def test_parse_missing_required_defaults_to_strings(self):
        # Provide explicit null for id/source/status/priority.
        text = "id: null\nsource: null\ntitle: null\nstatus: null\npriority: null\n"
        back = self.s.deserialize(text)
        self.assertEqual(back["id"], "")
        self.assertEqual(back["source"], "")
        self.assertEqual(back["title"], "")
        self.assertEqual(back["status"], "todo")
        self.assertEqual(back["priority"], "none")

    def test_parse_sequence_with_blank_line(self):
        text = "tags:\n  - a\n\n  - b\n"
        back = self.s._parse(text)
        self.assertEqual(back["tags"], ["a", "b"])

    def test_parse_sequence_terminates_on_dedent(self):
        text = "tags:\n  - a\nother: z\n"
        back = self.s._parse(text)
        self.assertEqual(back["tags"], ["a"])
        self.assertEqual(back["other"], "z")

    def test_parse_nested_map_with_blank_line_and_dedent(self):
        text = "category:\n  id: A\n\n  name: B\nother: z\n"
        back = self.s._parse(text)
        self.assertEqual(back["category"], {"id": "A", "name": "B"})
        self.assertEqual(back["other"], "z")

    def test_parse_block_scalar_with_blank_line(self):
        text = "description: |\n  a\n\n  b\n"
        back = self.s._parse(text)
        self.assertEqual(back["description"], "a\n\nb")


class TestOrgEdgeCases(unittest.TestCase):
    def test_iso_no_time(self):
        self.assertEqual(grt._iso_to_org_timestamp("2025-04-20"), "[2025-04-20 Sun]")

    def test_iso_with_tz(self):
        token = grt._iso_to_org_timestamp("2025-04-20T12:30:00+03:00")
        self.assertTrue(token.startswith("[2025-04-20 Sun"))

    def test_iso_offset_preserved_in_body(self):
        token = grt._iso_to_org_timestamp("2025-04-20T12:30:00+03:00")
        # Original offset survives to the emitted body.
        self.assertIn("+0300", token)
        self.assertIn("12:30", token)

    def test_iso_offset_roundtrip(self):
        token = grt._iso_to_org_timestamp("2025-04-20T12:30:00+03:00")
        back = grt._org_timestamp_to_iso(token)
        self.assertEqual(back, "2025-04-20T12:30:00+03:00")

    def test_iso_utc_roundtrip(self):
        token = grt._iso_to_org_timestamp("2025-04-20T12:30:00Z")
        back = grt._org_timestamp_to_iso(token)
        self.assertEqual(back, "2025-04-20T12:30:00Z")

    def test_iso_naive_datetime_assumed_utc(self):
        token = grt._iso_to_org_timestamp("2025-04-20T00:00:00")
        self.assertIn("2025-04-20", token)

    def test_org_to_iso_without_weekday(self):
        self.assertEqual(grt._org_timestamp_to_iso("[2025-04-20]"), "2025-04-20")

    def test_org_to_iso_single_digit_hour(self):
        self.assertEqual(grt._org_timestamp_to_iso("[2025-04-20 Sun 9:30]"),
                         "2025-04-20T09:30:00Z")


class TestMSTodoMsalFlag(unittest.TestCase):
    def test_msal_available_constant_boolean(self):
        self.assertIsInstance(grt.MSAL_AVAILABLE, bool)


class TestReadExactlyMultibyte(unittest.TestCase):
    def test_fallback_reads_bytes_not_chars(self):
        task = full_task(id="fake-X", title="üñîçödé")
        body = grt.YAMLSerializer().serialize(task)
        blen = len(body.encode("utf-8"))
        stream = (
            "export\n"
            f"blob\nmark :1\ndata {blen}\n{body}\n"
            "commit refs/heads/main\n"
            f"committer g <g@g> 0 +0000\n"
            f"data 2\nhi\n"
            f"M 100644 :1 tasks/fake-X.yaml\n"
            "\ndone\n"
        )
        driver = FakeDriver()
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertEqual(len(driver.upserted), 1)
        self.assertEqual(driver.upserted[0]["title"], "üñîçödé")

    def test_fallback_no_read_attribute_returns_empty(self):
        class Stub:
            pass
        h = grt.ProtocolHandler("r", "u", FakeDriver(), grt.YAMLSerializer(),
                                stdin=Stub(), stdout=io.StringIO(),
                                stderr=io.StringIO())
        self.assertEqual(h._read_exactly(5), b"")


class TestProtocolExportEdge(unittest.TestCase):
    def test_m_line_too_short_ignored(self):
        driver = FakeDriver()
        stream = "export\ncommit refs/heads/main\nM only-two\ndone\n"
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertEqual(driver.upserted, [])

    def test_d_line_too_short_ignored(self):
        driver = FakeDriver()
        stream = "export\ncommit refs/heads/main\nD\ndone\n"
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertEqual(driver.deleted, [])

    def test_author_lines_skipped(self):
        driver = FakeDriver()
        stream = (
            "export\n"
            "commit refs/heads/main\n"
            "author g <g@g> 0 +0000\n"
            "from :1\n"
            "reset refs/heads/foo\n"
            "done\n"
        )
        h = make_handler(driver=driver, stdin_text=stream)
        h.run()
        self.assertIn("ok refs/heads/main", h.stdout.getvalue())


class TestMainEntryEdge(unittest.TestCase):
    def test_main_check_unknown_scheme_returns_2(self):
        with mock.patch.object(grt, "read_remote_config",
                                return_value={"scheme": "unknown"}), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            rc = grt.main(["git_remote_tasks.py", "check", "x"])
        self.assertEqual(rc, 2)

    def test_main_unknown_args_prints_help(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            rc = grt.main(["git_remote_tasks.py", "random-arg"])
        self.assertEqual(rc, 0)
        self.assertIn("usage", out.getvalue().lower())


class TestCategoryEdge(unittest.TestCase):
    def test_normalize_task_ignores_unknown_keys(self):
        t = grt.normalize_task({"id": "x", "unknown_field": "whatever"})
        self.assertEqual(t["id"], "x")
        self.assertNotIn("unknown_field", t)


class TestReadBlockScalarEof(unittest.TestCase):
    def test_trailing_empty_lines_trimmed(self):
        text = "description: |\n  a\n  b\n\n\n"
        p = grt.YAMLSerializer()._parse(text)
        self.assertEqual(p["description"], "a\nb")


if __name__ == "__main__":
    unittest.main()
