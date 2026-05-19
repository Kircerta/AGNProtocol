# AGNProtocol Contributor Guide

Applies to work anywhere in this repository.

## Start

1. Read `README.md`.
2. Read `RUNBOOK.md`.
3. Read `SECURITY.md`.
4. Check the current git state with `git status -sb`.

## Working Rules

- Keep runtime state, credentials, logs, reports, caches, archives, and host
  files out of git.
- Use `rg` for text search and `git diff --check` before handoff.
- Keep documentation short and current-state focused.
- Keep generated state under ignored runtime directories.
- Prefer existing scripts and package modules over new entrypoints.
- Preserve explicit lifecycle, policy, emergency-stop, and review boundaries.

## Validation

Use the smallest validation set that matches the change:

```bash
python3 scripts/agn_bootstrap_check.py check
python3 scripts/agn2_system.py validate
python3 -m py_compile scripts/agn_tool_reality_cards.py scripts/agn_mcp_server.py scripts/awakening_daemon.py scripts/agn_host_state_probe.py
python3 -m pytest -q
git diff --check
```

Record any skipped checks with the command, reason, and remaining risk.
