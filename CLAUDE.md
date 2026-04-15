# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the full test suite (179 tests).
python -m unittest -v test_git_remote_tasks

# Run a single test / class / method.
python -m unittest -v test_git_remote_tasks.TestYAMLSerializer
python -m unittest -v test_git_remote_tasks.TestYAMLSerializer.test_roundtrip_full_task

# Coverage.
python -m coverage run -m unittest test_git_remote_tasks \
  && python -m coverage report -m --include=git_remote_tasks.py

# Install the per-scheme symlinks onto PATH so git can find the helpers.
python git_remote_tasks.py install --bin-dir ~/.local/bin

# Validate a configured remote without hitting the network.
python git_remote_tasks.py check <remote-name>
```

Tests use **unittest + unittest.mock only** — this is an explicit override of
the global "prefer pytest" rule. Do not convert tests to pytest.

## Architecture

`git_remote_tasks.py` is a single-file implementation of four git remote
helpers (`jira`, `vikunja`, `msftodo`, `notion`). The active scheme is
resolved from `argv[0]` basename when invoked by git, or from the URL scheme
when run directly. `install` materializes this as four symlinks all pointing
at the same script.

The code is built as strict layers; later layers depend only on earlier ones,
and this ordering is load-bearing for the test suite:

1. **Unified schema** (`empty_task`, `normalize_task`, `TASK_FIELDS`). Every
   task — regardless of source — is a dict with this exact shape.
2. **Serializers** (`YAMLSerializer`, `OrgSerializer`). Both round-trip to
   the same unified dict. The YAML parser/emitter is **hand-written** — there
   is no PyYAML dependency and none should be added. Only standard library is
   used; `msal` is the sole optional extra (guarded by `MSAL_AVAILABLE`).
3. **Git config reader** (`read_format`, `read_remote_config`). Thin wrapper
   over `git config --local`. All credentials live in `.git/config`, never
   the URL.
4. **Drivers** (`JiraDriver`, `VikunjaDriver`, `MSTodoDriver`, `NotionDriver`)
   inherit from `Driver`. Each implements `fetch_all`, `upsert`, `delete`.
   The HTTP seams are `_http_get` / `_http_post` — tests monkey-patch these,
   so preserve the seam when extending drivers.
5. **ProtocolHandler** speaks git's remote-helper line protocol on
   stdin/stdout: `capabilities`, `list`, `import`, `export`. Import writes a
   fast-import stream with `deleteall` so every fetch is a full snapshot;
   blobs are sorted by id for deterministic commit hashes.
6. **Management subcommands** (`install`, `uninstall`, `list-schemes`,
   `check`) are dispatched only when `argv[0]` is not a known helper name.

### Invariants to preserve

- **SerializerSymmetry.** A task serialized by one format and deserialized
  by the other (or the same) must equal `normalize_task(original)`. Any
  change to either serializer must keep this true for all `TASK_FIELDS`.
- **`logbook` is an extension key.** It is *not* in `TASK_FIELDS` but
  `normalize_task` preserves it, and both serializers round-trip it if
  present. This exists so org `:LOGBOOK:` drawers survive a YAML round-trip.
  Do not add it to `TASK_FIELDS`.
- **Date semantics.** Org timestamps are emitted in UTC. ISO strings with
  an offset are normalized to UTC (`…Z`); plain dates (`2025-04-20`) do not
  gain a synthetic time. Any change to `_iso_to_org_timestamp` /
  `_org_timestamp_to_iso` must keep round-trip stability.
- **Notion is pull-only.** `NotionDriver.upsert` / `delete` must raise
  `NotImplementedError("Notion is pull-only")` — not silently no-op.
- **Secret redaction in `check`.** Keys containing `token` or `password`
  (case-insensitive) are redacted in stdout. Adding a new credential-like
  key means extending this filter.
- **Fast-import blob bytes** are written through `sys.stdout.buffer` when
  available, with a UTF-8 text fallback. The fallback is test-only — real
  git invocations always expose `.buffer`. Do not drop the fallback without
  updating the tests that rely on it (`io.StringIO`).

### Write paths are stubs

`JiraDriver`, `VikunjaDriver`, `MSTodoDriver` have read/fetch fully wired
(with pagination) but `upsert` / `delete` raise `NotImplementedError` with
a TODO message. The `ProtocolHandler` export path still calls them and
surfaces the error to stderr. Treat this as deliberate scope — do not
silently turn the stubs into no-ops.

## Reference documents

- `SPEC.md` — original specification; the build order above mirrors it.
- `WORKLOG.md` — rationale for design decisions (logbook handling, date
  semantics, HTTP seams, Notion pull-only).
- `README.md` — user-facing docs, structured in 12 sections.
