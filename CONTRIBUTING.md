# Contributing

Thanks for the interest. This is a single-file Python tool with a deliberately
tight surface; most changes boil down to one of three things:

1. A new remote scheme.
2. A new invariant enforced at a trust boundary.
3. Behaviour the current release gets wrong.

## Ground rules

- **Single-file runtime, standard library only.** `git_remote_tasks.py`
  must remain installable by copying one file. `msal` is the sole
  optional runtime dep, guarded by `MSAL_AVAILABLE`. Test-time deps
  belong in `requirements-dev.txt` and must not be imported from the
  shipped script.
- **Tests stay as `unittest`.** The project has an explicit override of
  the global "prefer pytest" rule. Fuzz tests live in
  `test_yaml_parser_fuzz.py` and use `hypothesis`, gated on `.venv`.
- **Every change comes with tests.** Positive *and* negative — if you
  change a trust boundary (auth, path handling, push stubs),
  demonstrate that the guard still fires in the failure mode.
- **Docs are a last-resort warning, never a fix.** If something surfaces
  a surprise at runtime, `ProtocolHandler._warn_once` is the first
  stop; README edits are additive.

## Running the tests

```bash
# Default stdlib suite (no external deps).
python -m unittest test_git_remote_tasks

# With hypothesis fuzz tests.
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m unittest test_git_remote_tasks test_yaml_parser_fuzz

# Live end-to-end tests against real services. Reads credentials from
# $GRT_LIVE_CONFIG (path to a todo-harvest-style config.yaml).
GRT_LIVE_CONFIG=/path/to/config.yaml python test_live_integration.py
```

The live harness never deletes or modifies items it did not create,
and caps created items at five per service per run.

## Adding a new remote scheme

Outline; the Jira and Vikunja drivers are the most complete worked examples.

1. Add a driver class under the "Driver base + service drivers" section
   of `git_remote_tasks.py`:
   - `SCHEME` class attribute (e.g. `"linear"`).
   - `_cross_source_error` override pointing at your `PushError`.
   - `_auth_header()` / `_acquire_token()` as needed.
   - `normalize(raw) -> unified task dict`, threading status and
     priority through `self._apply_status_override` and
     `self._apply_priority_override`.
   - `fetch_all()` and `fetch_changed(since)` — the latter should hit
     a narrower API (see the Jira JQL `updated >=` or MS Todo delta
     pattern).
   - `upsert(task)` and `delete(task_id)`. Call `self._native_id` first
     so a cross-source push fails synchronously.
2. Register it in `SCHEMES` at the bottom of the file.
3. Add required keys to `REMOTE_REQUIRED_KEYS`; widen
   `_missing_required_keys` if the service has a one-of requirement.
4. Tests: normalize fixtures, fetch pagination, fetch_changed with
   `since`, upsert (update + create), delete, cross-source refusal.
5. README §7 add a row; §8 add a setup block.

## Commit style

- Short subject (≤72 chars), imperative mood. Reference the PLAN / DONE
  task id when one exists (e.g. `FEAT-09`, `BUG-12`).
- The body explains *why*, not *what*. The diff already shows what.
- One logical change per commit.

## Before opening a PR

- `python -m unittest test_git_remote_tasks` passes.
- `.venv/bin/python -m unittest test_yaml_parser_fuzz` passes if your
  change touches the YAML parser.
- README + `CLAUDE.md` updated for behaviour changes.
- `PLAN.md` / `DONE.md` updated for plan items.

## Security issues

Email the maintainer directly for anything in the auth / path / crypto
area; don't open a public issue first.
