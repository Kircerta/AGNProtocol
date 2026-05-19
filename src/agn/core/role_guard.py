"""AGN Role Guard.

This is the real package implementation for AGN's role-based command and
write-path constraints. The legacy script remains only as a compatibility shim.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agn.core.admin_control import repo_root


PACKAGE_PATH = "agn.core.role_guard"
LEGACY_SCRIPT_SHIM = "scripts/role_guard.py"

ROOT = repo_root()
CONFIG_PATH = ROOT / "config" / "role_permissions.json"
AUDIT_PATH = ROOT / "audit" / "events.jsonl"

_SAFE_DEFAULT_ROLE = "coordinator"
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_SHELL_BINARIES = {"sh", "bash", "zsh", "ksh", "dash", "fish"}
_EXEC_INTERPRETERS = {"perl", "ruby", "node"}
_EXEC_TOOLS = {"xargs", "find"}

_cached_config: dict[str, Any] | None = None
_cached_mtime: float = 0.0


def _current_root() -> Path:
    override = str(os.environ.get("AGN_REPO_ROOT", "")).strip()
    if override:
        return Path(override).expanduser().resolve()
    root = globals().get("ROOT")
    if isinstance(root, Path):
        return root.expanduser().resolve()
    return repo_root()


def _default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "roles": {
            "coordinator": {
                "writable_dirs": ["dispatch", "ssot", "memory", "runtime", ".agn_workspace", "reports"],
                "blocked_command_patterns": [
                    r"^git\s+(apply|commit|push|cherry-pick|rebase|reset)\b",
                    r"^sed\s+-i",
                    r"^codex\s+exec",
                    r"^rm\b",
                    r"^chmod",
                ],
                "utility_request_commands": ["git clone", "git checkout"],
                "blocked_git_subcommands": ["apply", "commit", "push", "cherry-pick", "rebase", "reset", "merge"],
            },
            "executor": {
                "writable_dirs": ["results", "dispatch/acks", "reports", ".agn_workspace", "runtime"],
                "blocked_command_patterns": [r"^rm\s+-rf\s+/"],
                "utility_request_commands": [],
                "blocked_git_subcommands": [],
            },
            "reviewer": {
                "writable_dirs": ["verdicts", ".agn_workspace"],
                "blocked_command_patterns": [
                    r"^git\s+(apply|commit|push)",
                    r"^rm\s+-rf",
                ],
                "utility_request_commands": [],
                "blocked_git_subcommands": ["apply", "commit", "push"],
            },
            "worker": {
                "writable_dirs": ["reports"],
                "blocked_command_patterns": [
                    r"^git\s+(apply|commit|push|cherry-pick|rebase|reset|merge)\b",
                    r"^sed\s+-i",
                    r"^codex\s+exec",
                    r"^rm\b",
                    r"^chmod",
                    r"^mv\b",
                    r"^cp\b",
                    r"^curl\b",
                    r"^wget\b",
                ],
                "utility_request_commands": [],
                "blocked_git_subcommands": ["apply", "commit", "push", "cherry-pick", "rebase", "reset", "merge"],
            },
            "coordinator_agent": {
                "writable_dirs": [],
                "blocked_command_patterns": [
                    r"^git\s+(apply|commit|push|cherry-pick|rebase|reset)\b",
                    r"^sed\s+-i",
                    r"^codex\s+exec",
                    r"^rm\b",
                    r"^chmod",
                    r"^mv\b",
                    r"^cp\b",
                ],
                "utility_request_commands": ["git clone", "git checkout"],
                "blocked_git_subcommands": ["apply", "commit", "push", "cherry-pick", "rebase", "reset", "merge"],
            },
            "admin": {
                "writable_dirs": ["*"],
                "blocked_command_patterns": [],
                "utility_request_commands": [],
                "blocked_git_subcommands": [],
            },
        },
    }


def _fail_closed_config() -> dict[str, Any]:
    blocked_all = [r".+"]
    return {
        "version": 1,
        "roles": {
            "coordinator": {
                "writable_dirs": [],
                "blocked_command_patterns": blocked_all,
                "utility_request_commands": [],
                "blocked_git_subcommands": ["*"],
            },
            "executor": {
                "writable_dirs": [],
                "blocked_command_patterns": blocked_all,
                "utility_request_commands": [],
                "blocked_git_subcommands": ["*"],
            },
            "reviewer": {
                "writable_dirs": [],
                "blocked_command_patterns": blocked_all,
                "utility_request_commands": [],
                "blocked_git_subcommands": ["*"],
            },
            "worker": {
                "writable_dirs": [],
                "blocked_command_patterns": blocked_all,
                "utility_request_commands": [],
                "blocked_git_subcommands": ["*"],
            },
            "coordinator_agent": {
                "writable_dirs": [],
                "blocked_command_patterns": blocked_all,
                "utility_request_commands": [],
                "blocked_git_subcommands": ["*"],
            },
            "admin": {
                "writable_dirs": ["*"],
                "blocked_command_patterns": [],
                "utility_request_commands": [],
                "blocked_git_subcommands": [],
            },
        },
    }


def _load_config() -> dict[str, Any]:
    global _cached_config, _cached_mtime
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        if _cached_config is not None:
            return _cached_config
        return _default_config()

    if _cached_config is not None and mtime == _cached_mtime:
        return _cached_config

    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        loaded = _fail_closed_config()

    if not isinstance(loaded, dict):
        loaded = _fail_closed_config()

    _cached_config = loaded
    _cached_mtime = mtime
    return _cached_config


def _known_roles() -> set[str]:
    cfg = _load_config()
    roles = cfg.get("roles", {})
    if not isinstance(roles, dict):
        return {"admin", _SAFE_DEFAULT_ROLE}
    return {str(name).strip().lower() for name in roles.keys() if str(name).strip()}


def _resolve_role_for_lookup(role: str) -> str:
    role_norm = role.strip().lower()
    if role_norm in _known_roles():
        return role_norm
    if _SAFE_DEFAULT_ROLE in _known_roles():
        return _SAFE_DEFAULT_ROLE
    if "admin" in _known_roles():
        return "admin"
    return role_norm or _SAFE_DEFAULT_ROLE


def _get_role_config(role: str) -> dict[str, Any]:
    cfg = _load_config()
    roles = cfg.get("roles", {})
    if not isinstance(roles, dict):
        return {}
    role_norm = _resolve_role_for_lookup(role)
    candidate = roles.get(role_norm)
    if isinstance(candidate, dict):
        return candidate
    admin_cfg = roles.get("admin")
    if isinstance(admin_cfg, dict):
        return admin_cfg
    return {}


_pattern_cache: dict[str, list[re.Pattern[str]]] = {}
_pattern_cache_mtime: float = 0.0


def _compiled_patterns(role: str) -> list[re.Pattern[str]]:
    global _pattern_cache, _pattern_cache_mtime
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    if mtime != _pattern_cache_mtime:
        _pattern_cache.clear()
        _pattern_cache_mtime = mtime
    role_norm = _resolve_role_for_lookup(role)
    if role_norm in _pattern_cache:
        return _pattern_cache[role_norm]
    rc = _get_role_config(role_norm)
    patterns: list[re.Pattern[str]] = []
    for raw in rc.get("blocked_command_patterns", []):
        if not isinstance(raw, str):
            continue
        try:
            patterns.append(re.compile(raw))
        except re.error:
            continue
    _pattern_cache[role_norm] = patterns
    return patterns


def _normalize_command(cmd: list[str] | tuple[str, ...] | str) -> list[str]:
    if isinstance(cmd, str):
        try:
            return shlex.split(cmd)
        except ValueError:
            return [cmd]
    return [str(part) for part in cmd]


def _strip_leading_env_tokens(argv: list[str]) -> list[str]:
    tokens = list(argv)
    for _ in range(5):
        idx = 0
        while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
            idx += 1

        if idx >= len(tokens):
            return []

        if Path(tokens[idx]).name != "env":
            return tokens[idx:]

        idx += 1
        flags_no_value = {"-i"}
        flags_with_value = {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
        while idx < len(tokens):
            token = tokens[idx]
            if _ENV_ASSIGN_RE.match(token):
                idx += 1
                continue
            if token in flags_no_value:
                idx += 1
                continue
            if token in flags_with_value:
                idx += 2
                continue
            if token.startswith("--unset=") or token.startswith("--chdir="):
                idx += 1
                continue
            if token.startswith("-"):
                break
            break
        tokens = tokens[idx:]
    return tokens


def _command_basename(token: str) -> str:
    return Path(token.strip()).name.lower()


def _flag_contains_c(token: str) -> bool:
    if not token.startswith("-") or token.startswith("--"):
        return False
    body = token[1:]
    return "c" in body


def _is_secondary_exec_container(argv: list[str]) -> bool:
    if not argv:
        return False
    base = _command_basename(argv[0])
    args = argv[1:]
    if base in _SHELL_BINARIES and any(arg == "-c" or _flag_contains_c(arg) for arg in args):
        return True
    if base.startswith("python") and "-c" in args:
        return True
    if base in _EXEC_INTERPRETERS and any(arg == "-e" for arg in args):
        return True
    if base in {"awk", "gawk", "mawk", "nawk"}:
        return True
    if base in _EXEC_TOOLS:
        return True
    return False


def _extract_git_subcommand(args: list[str]) -> str:
    idx = 0
    takes_value = {
        "-C",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--super-prefix",
        "--config-env",
    }
    while idx < len(args):
        token = args[idx]
        if token in takes_value:
            idx += 2
            continue
        if token.startswith("--git-dir=") or token.startswith("--work-tree="):
            idx += 1
            continue
        if token.startswith("-"):
            idx += 1
            continue
        return token.strip().lower()
    return ""


def _matches_utility_request(rc: dict[str, Any], argv: list[str]) -> str:
    if not argv:
        return ""
    base = _command_basename(argv[0])
    subcommand = _extract_git_subcommand(argv[1:]) if base == "git" else ""

    for raw_prefix in rc.get("utility_request_commands", []):
        if not isinstance(raw_prefix, str):
            continue
        try:
            prefix = shlex.split(raw_prefix)
        except ValueError:
            prefix = raw_prefix.split()
        prefix = [part.strip().lower() for part in prefix if part.strip()]
        if not prefix:
            continue

        if prefix[0] == "git" and len(prefix) >= 2:
            if base == "git" and subcommand == prefix[1]:
                return raw_prefix
            continue

        if len(argv) >= len(prefix):
            normalized = [part.lower() for part in argv[: len(prefix)]]
            if normalized == prefix:
                return raw_prefix
    return ""


def _matches_semantic_git_block(rc: dict[str, Any], argv: list[str]) -> str:
    if not argv:
        return ""
    base = _command_basename(argv[0])
    if base != "git":
        return ""
    blocked = {
        str(item).strip().lower()
        for item in rc.get("blocked_git_subcommands", [])
        if str(item).strip()
    }
    if not blocked:
        return ""
    subcommand = _extract_git_subcommand(argv[1:])
    if "*" in blocked:
        return subcommand or "*"
    if subcommand and subcommand in blocked:
        return subcommand
    if subcommand:
        for item in sorted(blocked):
            if item.startswith(subcommand):
                return f"{subcommand}~{item}"
    return ""


def _canonical_path(path: Path | str) -> str:
    real = os.path.realpath(os.path.abspath(os.fspath(path)))
    if sys.platform == "darwin":
        return real.casefold()
    return real


def _is_within(target: Path | str, root: Path | str) -> bool:
    target_c = _canonical_path(target)
    root_c = _canonical_path(root)
    try:
        return os.path.commonpath([target_c, root_c]) == root_c
    except ValueError:
        return False


def _is_guard_enforced(role: str) -> bool:
    role_norm = _resolve_role_for_lookup(role)
    override = str(os.environ.get("AGN_ENFORCE_ROLE_GUARD", "")).strip().lower()
    context = str(os.environ.get("AGN_RUNTIME_CONTEXT", "agn_network")).strip().lower()
    if override:
        if override in {"1", "true", "yes", "on"}:
            return True
        if override in {"0", "false", "no", "off"}:
            return not (context in {"assistant", "kirara_assistant", "outside_agn"} and role_norm == "coordinator")
        return True

    if context in {"assistant", "kirara_assistant", "outside_agn"} and role_norm == "coordinator":
        return False
    return True


def get_current_role() -> str:
    explicit = str(os.environ.get("AGN_ROLE", "")).strip().lower()
    known = _known_roles()
    if explicit:
        if explicit in known:
            return explicit
        return _SAFE_DEFAULT_ROLE if _SAFE_DEFAULT_ROLE in known else ("admin" if "admin" in known else explicit)

    compat_admin = str(os.environ.get("AGN_COMPAT_ADMIN", "")).strip() == "1"
    if compat_admin and "admin" in known:
        return "admin"
    if _SAFE_DEFAULT_ROLE in known:
        return _SAFE_DEFAULT_ROLE
    if "admin" in known:
        return "admin"
    return _SAFE_DEFAULT_ROLE


def check_command(cmd: list[str], role: str | None = None) -> tuple[bool, str]:
    role_norm = _resolve_role_for_lookup(role or get_current_role())
    if not _is_guard_enforced(role_norm):
        return True, ""
    if role_norm == "admin":
        return True, ""

    original = _normalize_command(cmd)
    argv = _strip_leading_env_tokens(original)
    if not argv:
        return False, "invalid_command:empty"
    if str(argv[0]).startswith("-"):
        return False, "invalid_command:malformed_env_prefix"

    if _is_secondary_exec_container(argv):
        return False, f"blocked_secondary_exec_container:{_command_basename(argv[0])}"

    rc = _get_role_config(role_norm)

    utility = _matches_utility_request(rc, argv)
    if utility:
        return False, f"utility_request_required:{utility}"

    blocked_git = _matches_semantic_git_block(rc, argv)
    if blocked_git:
        return False, f"blocked_git_subcommand:{blocked_git}"

    canonical_str = " ".join(argv)
    for pat in _compiled_patterns(role_norm):
        if pat.search(canonical_str):
            return False, f"blocked_command_pattern:{pat.pattern}"

    return True, ""


def check_write_path(path: Path | str, role: str | None = None) -> tuple[bool, str]:
    role_norm = _resolve_role_for_lookup(role or get_current_role())
    if not _is_guard_enforced(role_norm):
        return True, ""
    if role_norm == "admin":
        return True, ""

    rc = _get_role_config(role_norm)
    allowed_dirs = [str(item) for item in rc.get("writable_dirs", []) if str(item).strip()]
    if "*" in allowed_dirs:
        return True, ""

    target = Path(path).expanduser()
    target_real = target.resolve(strict=False)

    root = _current_root()

    for directory in allowed_dirs:
        allowed_path = (root / directory).resolve(strict=False)
        if _is_within(target_real, allowed_path):
            return True, ""

    if _is_within(target_real, root / "audit"):
        return True, ""

    return False, f"write_dir_not_allowed:target={target_real},allowed={allowed_dirs}"


def require_write_access(path: Path | str, role: str | None = None, *, task_id: str | None = None) -> None:
    effective_role = _resolve_role_for_lookup(role or get_current_role())
    if not _is_guard_enforced(effective_role):
        return
    ok, reason = check_write_path(path, effective_role)
    if ok:
        return
    log_violation(effective_role, "write", f"path={path} reason={reason}", task_id=task_id)
    raise PermissionError(f"ROLE_GUARD_BLOCKED: {reason}")


def log_violation(
    role: str,
    blocked_action: str,
    detail: str,
    *,
    task_id: str | None = None,
) -> None:
    event = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "route": "/agn/role_guard",
        "status": 403,
        "action": "role_violation",
        "role": _resolve_role_for_lookup(role),
        "blocked_action": blocked_action,
        "detail": detail[:500],
    }
    if task_id:
        event["task_id"] = task_id
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True) + "\n")
    except OSError:
        pass
