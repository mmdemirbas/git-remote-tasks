#!/usr/bin/env bash
# git-remote-tasks driver: one function per operation declared in ajans.toml,
# over the commands CLAUDE.md lists. Extra arguments go to the command each
# one wraps.
set -euo pipefail
cd "$(dirname "$0")"

blocked() {
	printf '⏸ %s\n' "$*" >&2
	exit 75
}

op_test() { python3 -m unittest -v "${@:-test_git_remote_tasks}"; }

op_test_fuzz() {
	[[ -x .venv/bin/python ]] || blocked "no .venv with the test-only dependencies: ./ctl setup"
	.venv/bin/python -m unittest -v test_yaml_parser_fuzz "$@"
}

# The harness skips every test when GRT_LIVE_CONFIG is unset and still exits 0,
# which would read as a pass that tested nothing.
op_test_live() {
	[[ -n "${GRT_LIVE_CONFIG:-}" ]] ||
		blocked "GRT_LIVE_CONFIG is not set: point it at a todo-harvest-style config.yaml"
	python3 test_live_integration.py "$@"
}

op_check() {
	./ctl --check
	python3 -m unittest test_git_remote_tasks "$@"
}

op_setup() {
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt "$@"
}

op_deploy_cli() { python3 git_remote_tasks.py install --bin-dir "$HOME/.local/bin" "$@"; }

op_uninstall() { python3 git_remote_tasks.py uninstall --bin-dir "$HOME/.local/bin" "$@"; }

# >>> ajans runtime 1 >>>
# Written by `ajans contract sync`; edits here are overwritten. The operations
# are declared in ajans.toml and implemented above as op_<verb>[_<target>].

_ajans_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_ajans_fn() {
	local name="op_$*"
	name="${name// /_}"
	name="${name//-/_}"
	printf '%s' "${name//./_}"
}

# Reads ajans.toml with the first python3 that has tomllib. Modes: list [verb], keys.
_ajans_toml() {
	local py
	if [[ ! -f "$_ajans_root/ajans.toml" ]]; then
		printf '✗ no ajans.toml beside %s\n' "$_ajans_root/ctl" >&2
		return 66
	fi
	for py in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
		if ! command -v "$py" >/dev/null 2>&1 || ! "$py" -c 'import tomllib' 2>/dev/null; then
			continue
		fi
		"$py" - "$_ajans_root/ajans.toml" "$@" <<'PY'
import re, sys, tomllib
try:
    with open(sys.argv[1], 'rb') as f:
        manifest = tomllib.load(f)
except tomllib.TOMLDecodeError as e:
    sys.exit(f'✗ ajans.toml: {e}')
ops, verbs = manifest.get('operations', {}), manifest.get('verbs', {})
mode, rest = sys.argv[2], sys.argv[3:]
if mode == 'keys':
    for key in ops:
        print('op_' + re.sub(r'[ .-]', '_', key))
elif rest:
    for key, op in ops.items():
        verb, _, target = key.partition(' ')
        if verb == rest[0] and target:
            print(f"{target}\t{op.get('summary', '')}")
else:
    seen = []
    for key in ops:
        verb = key.partition(' ')[0]
        if verb not in seen:
            seen.append(verb)
            print(f"{verb}\t{ops[verb]['summary'] if verb in ops else verbs.get(verb, '')}")
PY
		return
	done
	printf '✗ reading ajans.toml needs python3 3.11 or later (tomllib)\n' >&2
	return 69
}

_ajans_check() {
	local declared defined missing extra fn count noun
	declared="$(_ajans_toml keys)" || return
	declared="$(printf '%s\n' "$declared" | sort)"
	defined="$(declare -F | awk '$3 ~ /^op_/ { print $3 }' | sort)"
	missing="$(comm -23 <(printf '%s\n' "$declared") <(printf '%s\n' "$defined") | sed '/^$/d')"
	extra="$(comm -13 <(printf '%s\n' "$declared") <(printf '%s\n' "$defined") | sed '/^$/d')"
	if [[ -z "$missing" && -z "$extra" ]]; then
		count="$(printf '%s\n' "$declared" | grep -c .)"
		if [[ "$count" == 1 ]]; then noun=operation; else noun=operations; fi
		printf '✓ ajans.toml and ctl agree: %s %s\n' "$count" "$noun"
		return 0
	fi
	if [[ -n "$missing" ]]; then
		while IFS= read -r fn; do printf '✗ declared, no function: %s\n' "$fn" >&2; done <<<"$missing"
	fi
	if [[ -n "$extra" ]]; then
		while IFS= read -r fn; do printf '✗ function, not declared: %s\n' "$fn" >&2; done <<<"$extra"
	fi
	return 1
}

_ajans_main() {
	case "${1:-}" in
	'' | --list | -h | --help)
		_ajans_toml list
		return
		;;
	--check)
		_ajans_check
		return
		;;
	esac
	if [[ "${2:-}" == --list ]]; then
		_ajans_toml list "$1"
		return
	fi
	local fn
	if [[ $# -ge 2 ]]; then
		fn="$(_ajans_fn "$1 $2")"
		if declare -F "$fn" >/dev/null; then
			shift 2
			"$fn" "$@"
			return
		fi
	fi
	fn="$(_ajans_fn "$1")"
	if declare -F "$fn" >/dev/null; then
		shift
		"$fn" "$@"
		return
	fi
	if declare -F | awk -v p="$fn"_ '$3 ~ "^" p { found = 1 } END { exit !found }'; then
		printf '✗ %s needs a target: ./ctl %s --list\n' "$1" "$1" >&2
	else
		printf '✗ %s: not an operation of this project (./ctl --list)\n' "$*" >&2
	fi
	return 64
}

_ajans_main "$@"
# <<< ajans runtime <<<
