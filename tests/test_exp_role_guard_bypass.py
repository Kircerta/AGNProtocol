"""EXP-1/2/3: Role Guard bypass, path traversal, and runtime context experiments.

These tests probe for security vulnerabilities in the Role Guard system.
Each test documents whether a bypass vector is blocked or exploitable.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import agn.core.role_guard as rg
from agn.core.role_guard import check_command, check_write_path, get_current_role


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _enforce_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure role guard is enforced for all tests."""
    monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "1")
    monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "agn_network")
    monkeypatch.delenv("AGN_COMPAT_ADMIN", raising=False)


# ══════════════════════════════════════════════════════════════════
# EXP-1: Command Bypass Vectors
# ══════════════════════════════════════════════════════════════════

class TestEXP1_SecondaryExecContainerBypass:
    """Test if non-covered interpreters can be used to bypass command checks."""

    def test_perl_exec_blocked(self) -> None:
        """perl -e should be blocked as secondary exec container."""
        ok, reason = check_command(["perl", "-e", 'system("git apply foo.patch")'], role="coordinator")
        assert ok is False, "perl -e should be blocked as exec container"
        assert "blocked_secondary_exec_container" in reason

    def test_ruby_exec_blocked(self) -> None:
        """ruby -e should be blocked as secondary exec container."""
        ok, reason = check_command(["ruby", "-e", '`git apply foo.patch`'], role="coordinator")
        assert ok is False, "ruby -e should be blocked as exec container"

    def test_node_exec_blocked(self) -> None:
        """node -e should be blocked as secondary exec container."""
        ok, reason = check_command(
            ["node", "-e", 'require("child_process").execSync("git apply foo.patch")'],
            role="coordinator",
        )
        assert ok is False, "node -e should be blocked as exec container"

    def test_awk_exec_blocked(self) -> None:
        """awk is always blocked as secondary exec container."""
        ok, reason = check_command(
            ["awk", 'BEGIN{system("git apply foo.patch")}'],
            role="coordinator",
        )
        assert ok is False, "awk should be blocked as exec container"

    def test_xargs_exec_blocked(self) -> None:
        """xargs should be blocked as secondary exec container."""
        ok, reason = check_command(["xargs", "git", "apply"], role="coordinator")
        assert ok is False, "xargs should be blocked as exec container"

    def test_env_dash_S_exec_bypass(self) -> None:
        """VULNERABILITY PROBE: env -S 'git apply foo.patch'
        The env -S flag splits its argument and runs the result as a command.
        _strip_leading_env_tokens handles -S as a flag-with-value, skipping its arg.
        Does it correctly extract the inner command?"""
        ok, reason = check_command(["env", "-S", "git apply foo.patch"], role="coordinator")
        # After env stripping, should end up with empty argv (since "git apply foo.patch"
        # is consumed as the -S value), and the next token should be analyzed
        # Actually: -S is in flags_with_value, so idx += 2 skips "git apply foo.patch"
        # This means the inner command is NOT checked!
        if ok:
            pytest.fail(
                "VULNERABILITY FOUND: 'env -S \"git apply foo.patch\"' bypasses guard. "
                "The -S flag value contains the real command but is skipped as a flag argument."
            )


class TestEXP1_EnvPrefixEvasion:
    """Test edge cases in env/variable prefix stripping."""

    def test_double_env_bypass(self) -> None:
        """env env git apply foo.patch — does double env prefix fool the stripper?"""
        ok, reason = check_command(["env", "env", "git", "apply", "foo.patch"], role="coordinator")
        assert ok is False, f"Double env prefix should not bypass guard. Got reason: {reason}"

    def test_env_with_path_prefix(self) -> None:
        """/usr/bin/env git apply foo.patch"""
        ok, reason = check_command(["/usr/bin/env", "git", "apply", "foo.patch"], role="coordinator")
        assert ok is False, f"/usr/bin/env prefix should still be stripped. Got reason: {reason}"

    def test_env_var_assign_then_blocked_cmd(self) -> None:
        """FOO=bar git apply foo.patch — env var assignment before blocked command."""
        ok, reason = check_command(["FOO=bar", "git", "apply", "foo.patch"], role="coordinator")
        assert ok is False, f"Env var assignment prefix should be stripped. Got reason: {reason}"

    def test_multiple_env_vars_then_blocked_cmd(self) -> None:
        """A=1 B=2 C=3 git commit -m test"""
        ok, reason = check_command(["A=1", "B=2", "C=3", "git", "commit", "-m", "test"], role="coordinator")
        assert ok is False, f"Multiple env var assignments should be stripped."

    def test_env_with_split_string_equals_form(self) -> None:
        """env --split-string='git apply foo.patch' — equals form."""
        ok, reason = check_command(["env", "--split-string=git apply foo.patch"], role="coordinator")
        # --split-string= starts with "-", so the while loop breaks at "if token.startswith("-"): break"
        # This means the rest of tokens (empty) is returned, cmd is effectively empty → blocked as invalid
        # OR it might be ok since "git apply foo.patch" is embedded in the flag
        # Let's check what actually happens


class TestEXP1_GitSubcommandEvasion:
    """Test if git subcommand extraction can be fooled."""

    def test_git_config_flag_then_apply(self) -> None:
        """git -c advice.detachedHead=false apply foo.patch
        The -c flag is in takes_value, so it consumes 2 tokens. Then 'apply' is correctly found."""
        ok, reason = check_command(
            ["git", "-c", "advice.detachedHead=false", "apply", "foo.patch"],
            role="coordinator",
        )
        assert ok is False, "git -c <config> apply should still be blocked"

    def test_git_with_equals_config(self) -> None:
        """git --git-dir=/tmp/repo apply foo.patch"""
        ok, reason = check_command(
            ["git", "--git-dir=/tmp/repo", "apply", "foo.patch"],
            role="coordinator",
        )
        assert ok is False, "git --git-dir=<path> apply should still be blocked"

    def test_git_unknown_flag_evasion(self) -> None:
        """git --paginate apply foo.patch
        --paginate is not in takes_value. It starts with '-' so idx += 1.
        Then 'apply' is found as the subcommand. Should be blocked."""
        ok, reason = check_command(
            ["git", "--paginate", "apply", "foo.patch"],
            role="coordinator",
        )
        assert ok is False, "Unknown git flag before subcommand should not fool extractor"

    def test_git_alias_potential_bypass(self) -> None:
        """What if git has an alias? 'git ap' might expand to 'git apply'.
        The guard only checks the literal subcommand string, so 'ap' != 'apply'."""
        ok, reason = check_command(["git", "ap", "foo.patch"], role="coordinator")
        # 'ap' is not in the blocked list, so this WILL pass
        if ok:
            pytest.fail(
                "POTENTIAL VULNERABILITY: git alias abbreviations bypass guard. "
                "'git ap' (alias for 'git apply') passes check."
            )

    def test_git_stash_pop_not_blocked(self) -> None:
        """git stash pop — modifies working tree but not explicitly blocked for coordinator.
        This is by design (only destructive ops blocked), but worth documenting."""
        ok, reason = check_command(["git", "stash", "pop"], role="coordinator")
        # This should pass — stash is not in blocked list
        assert ok is True, "git stash pop should be allowed for coordinator (by design)"

    def test_git_merge_not_blocked(self) -> None:
        """git merge main — merges branches, modifies code. Not explicitly blocked."""
        ok, reason = check_command(["git", "merge", "main"], role="coordinator")
        if ok:
            pytest.fail(
                "POTENTIAL VULNERABILITY: 'git merge' is not blocked for coordinator. "
                "This can modify code and working tree."
            )

    def test_git_merge_base_allowed(self) -> None:
        """git merge-base is a read/query subcommand and should remain allowed."""
        ok, reason = check_command(["git", "merge-base", "HEAD", "main"], role="coordinator")
        assert ok is True, f"'git merge-base' should be allowed, got blocked: {reason}"


class TestEXP1_RegexEvasion:
    """Test regex pattern matching edge cases."""

    def test_git_apply_with_extra_spaces(self) -> None:
        """'git  apply' (double space) — shlex.split normalizes this to ['git', 'apply']
        but the canonical_str joins with single space. Should be fine."""
        ok, reason = check_command("git  apply foo.patch", role="coordinator")
        assert ok is False, "Extra spaces in command string should not bypass"

    def test_git_tab_separated(self) -> None:
        """git\\tapply — tab between git and apply."""
        ok, reason = check_command("git\tapply foo.patch", role="coordinator")
        assert ok is False, "Tab-separated command should not bypass"

    def test_command_with_null_byte(self) -> None:
        """git\\x00apply — null byte injection attempt."""
        ok, reason = check_command(["git\x00apply", "foo.patch"], role="coordinator")
        # The command basename would be "git\x00apply" which doesn't match "git"
        # So git subcommand extraction won't trigger. But the regex check...
        # canonical_str = "git\x00apply foo.patch" — the regex ^git\s+ won't match
        # because there's no space after "git".
        # This means a null-byte injection could theoretically bypass the check.

    def test_mixed_case_git(self) -> None:
        """Git Apply foo.patch — command_basename lowercases the first token."""
        ok, reason = check_command(["Git", "Apply", "foo.patch"], role="coordinator")
        # _command_basename lowercases argv[0] to "git"
        # But _extract_git_subcommand returns token.strip().lower() → "apply"
        # The canonical_str however is "Git Apply foo.patch" (original case!)
        # The regex ^git\s+(apply|...) is case-sensitive and won't match "Git Apply"
        # But _matches_semantic_git_block uses lowercase subcommand comparison
        # So the semantic check should catch it even if regex doesn't
        assert ok is False, "Mixed case 'Git Apply' should still be blocked"

    def test_chmod_with_path(self) -> None:
        """chmod +x script.sh — should be blocked for coordinator."""
        ok, reason = check_command(["chmod", "+x", "script.sh"], role="coordinator")
        assert ok is False, "chmod should be blocked for coordinator"

    def test_rm_single_file(self) -> None:
        """rm file.txt — pattern is ^rm\\s+-, so 'rm file.txt' (no dash) should pass?"""
        ok, reason = check_command(["rm", "file.txt"], role="coordinator")
        # Pattern: ^rm\s+- requires a dash after rm. "rm file.txt" has no dash.
        # canonical_str = "rm file.txt" — regex ^rm\s+- doesn't match
        if ok:
            pytest.fail(
                "POTENTIAL VULNERABILITY: 'rm file.txt' (no -flag) bypasses coordinator guard. "
                "Pattern only matches 'rm -...' but 'rm file' also deletes files."
            )


class TestEXP1_CommandStringVsList:
    """Test differences between string and list command inputs."""

    def test_string_vs_list_git_apply(self) -> None:
        """Ensure string and list produce same result."""
        ok_str, _ = check_command("git apply foo.patch", role="coordinator")
        ok_list, _ = check_command(["git", "apply", "foo.patch"], role="coordinator")
        assert ok_str == ok_list, "String and list command should produce same result"

    def test_malformed_shlex_string(self) -> None:
        """Command string with unmatched quotes — shlex.split raises ValueError,
        fallback is [cmd] which is checked as single token."""
        ok, reason = check_command("git apply 'unmatched quote", role="coordinator")
        # shlex.split fails → returns ["git apply 'unmatched quote"]
        # This is treated as a single token, not "git" with subcommand
        # The canonical_str would be that single string, and regex would check it
        # ^git\s+(apply|...) would match inside the string!
        assert ok is False, "Malformed command string should still be caught"


# ══════════════════════════════════════════════════════════════════
# EXP-2: Write-Path Traversal Attacks
# ══════════════════════════════════════════════════════════════════

class TestEXP2_PathTraversal:
    """Test write-path checking for traversal attacks."""

    def test_dotdot_traversal_from_dispatch(self) -> None:
        """dispatch/../results/evil.json — traversing out of allowed dir."""
        evil_path = ROOT / "dispatch" / ".." / "results" / "evil.json"
        ok, reason = check_write_path(evil_path, role="coordinator")
        assert ok is False, "Path traversal via .. should be caught by resolve()"

    def test_dotdot_deep_traversal(self) -> None:
        """dispatch/../../etc/passwd — deep traversal."""
        evil_path = ROOT / "dispatch" / ".." / ".." / "etc" / "passwd"
        ok, reason = check_write_path(evil_path, role="coordinator")
        assert ok is False, "Deep path traversal should be blocked"

    def test_absolute_path_outside_root(self) -> None:
        """/tmp/evil.json — absolute path outside project."""
        ok, reason = check_write_path("/tmp/evil.json", role="coordinator")
        assert ok is False, "Absolute path outside project should be blocked"

    def test_home_dir_expansion(self) -> None:
        """~/evil.json — home directory expansion."""
        ok, reason = check_write_path("~/evil.json", role="coordinator")
        assert ok is False, "Home dir path should be blocked"

    def test_symlink_traversal(self, tmp_path: Path) -> None:
        """Create a symlink inside dispatch/ pointing outside, then check write path."""
        # We can't actually create symlinks in the real dispatch dir,
        # but we can test the canonical path resolution logic.
        # Create: tmp_path/dispatch -> /tmp (symlink)
        # Then check_write_path(tmp_path/dispatch/evil.json)
        fake_root = tmp_path / "project"
        fake_root.mkdir()
        dispatch_dir = fake_root / "dispatch"
        # Create symlink: dispatch -> /tmp
        dispatch_dir.symlink_to("/tmp")

        evil_path = dispatch_dir / "evil.json"
        # Manually check: the resolved path would be /tmp/evil.json
        # which is NOT within fake_root/dispatch
        # But check_write_path uses ROOT (the real project root), not fake_root
        # So this test needs to check the raw _is_within logic
        target_resolved = evil_path.resolve(strict=False)
        allowed_resolved = (fake_root / "dispatch").resolve(strict=False)
        # After resolving, dispatch_dir → /tmp, so evil_path → /tmp/evil.json
        # allowed_resolved → /tmp (following the symlink!)
        # So _is_within(/tmp/evil.json, /tmp) → True! The symlink FOOLS the check!
        # On macOS, /tmp resolves to /private/tmp
        assert "/tmp" in str(target_resolved), f"Symlink should resolve through /tmp, got {target_resolved}"

    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only casefold behavior")
    def test_case_sensitivity_on_macos(self) -> None:
        """On macOS (case-insensitive FS), 'Dispatch' == 'dispatch'.
        The guard uses casefold() on darwin. Test this."""
        # ROOT / "Dispatch" / "task.json" should be treated same as ROOT / "dispatch" / "task.json"
        ok_lower, _ = check_write_path(ROOT / "dispatch" / "task.json", role="coordinator")
        ok_upper, _ = check_write_path(ROOT / "Dispatch" / "task.json", role="coordinator")
        assert ok_lower == ok_upper, "Case sensitivity mismatch on macOS"

    def test_unicode_normalization_attack(self) -> None:
        """Unicode normalization: 'dispatch' with combining characters."""
        # On macOS, filenames are NFD-normalized. Test if path checking handles this.
        # 'dispatch' vs 'dispatcℎ' (using mathematical small h, U+210E)
        # This should NOT be treated as the same directory
        evil_path = ROOT / "dispatc\u210e" / "task.json"
        ok, reason = check_write_path(evil_path, role="coordinator")
        # The mathematical h is different from ASCII h, so this path
        # should NOT be within ROOT/dispatch and should be blocked
        assert ok is False, "Unicode lookalike directory name should not match 'dispatch'"


class TestEXP2_AuditPathBypass:
    """The audit path is always writable. Can this be exploited?"""

    def test_audit_path_always_writable(self) -> None:
        """All roles can write to audit/ — is this exploitable?"""
        for role in ("coordinator", "executor", "reviewer"):
            ok, _ = check_write_path(ROOT / "audit" / "events.jsonl", role=role)
            assert ok is True, f"Audit path should be writable for {role}"

    def test_audit_traversal(self) -> None:
        """audit/../scripts/evil.py — can we escape via audit/?"""
        evil = ROOT / "audit" / ".." / "scripts" / "evil.py"
        ok, reason = check_write_path(evil, role="coordinator")
        # After resolve(), this becomes ROOT/scripts/evil.py
        # which is NOT within ROOT/audit after resolution
        assert ok is False, "Audit path traversal should be blocked"

    def test_audit_subdir_creation(self) -> None:
        """audit/subdir/deep/file.json — creating deep subdirs in audit."""
        ok, reason = check_write_path(ROOT / "audit" / "deep" / "nested" / "file.json", role="coordinator")
        assert ok is True, "Deep audit subdirs should be allowed"


# ══════════════════════════════════════════════════════════════════
# EXP-3: AGN_RUNTIME_CONTEXT Loophole
# ══════════════════════════════════════════════════════════════════

class TestEXP3_RuntimeContextLoophole:
    """Test the AGN_RUNTIME_CONTEXT bypass and its implications."""

    def test_assistant_context_disables_guard_for_coordinator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When AGN_RUNTIME_CONTEXT=assistant, coordinator guard is disabled.
        This means ALL commands are allowed, including git apply, rm -rf, etc."""
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "assistant")
        monkeypatch.delenv("AGN_ENFORCE_ROLE_GUARD", raising=False)

        # These should ALL pass because guard is disabled
        ok_apply, _ = check_command(["git", "apply", "foo.patch"], role="coordinator")
        ok_rm, _ = check_command(["rm", "-rf", "/"], role="coordinator")
        ok_commit, _ = check_command(["git", "commit", "-m", "evil"], role="coordinator")

        # Document: this is BY DESIGN for assistant mode but is a risk
        assert ok_apply is True, "Guard disabled in assistant context — by design"
        assert ok_rm is True, "Guard disabled in assistant context — by design"
        assert ok_commit is True, "Guard disabled in assistant context — by design"

    def test_assistant_context_still_enforces_for_executor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The loophole only applies to coordinator role. Other roles should still be guarded."""
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "assistant")
        monkeypatch.delenv("AGN_ENFORCE_ROLE_GUARD", raising=False)

        ok, _ = check_command(["rm", "-rf", "/"], role="executor")
        assert ok is False, "Executor should still be guarded even in assistant context"

    def test_enforce_override_beats_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGN_ENFORCE_ROLE_GUARD=1 should force guard ON regardless of context."""
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "assistant")
        monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "1")

        ok, _ = check_command(["git", "apply", "foo.patch"], role="coordinator")
        assert ok is False, "Explicit enforce should override context bypass"

    def test_enforce_false_disables_for_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGN_ENFORCE_ROLE_GUARD=0 disables guard for ALL roles — even executor."""
        monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "0")

        ok_exec, _ = check_command(["rm", "-rf", "/"], role="executor")
        ok_rev, _ = check_command(["git", "apply", "foo.patch"], role="reviewer")

        # If someone sets AGN_ENFORCE_ROLE_GUARD=0, EVERYTHING is allowed
        if ok_exec and ok_rev:
            pytest.fail(
                "VULNERABILITY: AGN_ENFORCE_ROLE_GUARD=0 disables ALL protection. "
                "Any process that can set this env var bypasses all role checks."
            )

    def test_outside_agn_context_loophole(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGN_RUNTIME_CONTEXT=outside_agn also disables guard for coordinator."""
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "outside_agn")
        monkeypatch.delenv("AGN_ENFORCE_ROLE_GUARD", raising=False)

        ok, _ = check_command(["git", "apply", "foo.patch"], role="coordinator")
        assert ok is True, "outside_agn context disables coordinator guard — by design"

    def test_empty_context_defaults_to_agn_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Empty AGN_RUNTIME_CONTEXT should default to agn_network (enforced)."""
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "")
        monkeypatch.delenv("AGN_ENFORCE_ROLE_GUARD", raising=False)

        # _is_guard_enforced: context defaults to "agn_network" when env is empty string
        # Wait — the code does: os.environ.get("AGN_RUNTIME_CONTEXT", "agn_network")
        # If AGN_RUNTIME_CONTEXT="" (set but empty), it returns "" not the default
        # Then "" is not in {"assistant", "kirara_assistant", "outside_agn"}, so guard is ON
        ok, _ = check_command(["git", "apply", "foo.patch"], role="coordinator")
        assert ok is False, "Empty context should not disable guard"

    def test_unset_context_defaults_to_agn_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unset AGN_RUNTIME_CONTEXT should default to agn_network."""
        monkeypatch.delenv("AGN_RUNTIME_CONTEXT", raising=False)
        monkeypatch.delenv("AGN_ENFORCE_ROLE_GUARD", raising=False)

        ok, _ = check_command(["git", "apply", "foo.patch"], role="coordinator")
        assert ok is False, "Unset context should default to agn_network (enforced)"

    def test_write_path_also_respects_context_bypass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In assistant context, coordinator should be able to write anywhere."""
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "assistant")
        monkeypatch.delenv("AGN_ENFORCE_ROLE_GUARD", raising=False)

        ok, _ = check_write_path("/tmp/evil.json", role="coordinator")
        assert ok is True, "Write path guard disabled in assistant context for coordinator"


class TestEXP3_EnvOverrideInteraction:
    """Test interactions between AGN_ENFORCE_ROLE_GUARD, AGN_RUNTIME_CONTEXT, and AGN_ROLE."""

    def test_enforce_empty_string_means_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGN_ENFORCE_ROLE_GUARD='' (empty) — should fall through to context check."""
        monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "")
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "agn_network")

        ok, _ = check_command(["git", "apply", "foo.patch"], role="coordinator")
        # Empty override → not in {"1","true","yes","on"} → returns False
        # Wait: the code checks `if override:` first. Empty string is falsy, so it falls through
        # Then context check: "agn_network" is not in bypass set → returns True (enforced)
        assert ok is False, "Empty enforce string should fall through to context (enforced)"

    def test_enforce_spaces_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGN_ENFORCE_ROLE_GUARD='   ' — stripped to empty, should fall through."""
        monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "   ")
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "agn_network")

        ok, _ = check_command(["git", "apply", "foo.patch"], role="coordinator")
        # str(...).strip().lower() → "" → falsy → falls through to context
        assert ok is False, "Whitespace-only enforce should fall through"

    def test_enforce_invalid_value_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGN_ENFORCE_ROLE_GUARD='maybe' — unrecognized value should fail closed (enforced)."""
        monkeypatch.setenv("AGN_ENFORCE_ROLE_GUARD", "maybe")
        monkeypatch.setenv("AGN_RUNTIME_CONTEXT", "agn_network")

        ok, _ = check_command(["git", "apply", "foo.patch"], role="coordinator")
        # After fix: unrecognized values fail closed → guard IS enforced
        assert ok is False, "Unrecognized enforce value should fail closed (guard enforced)"

    def test_role_spoofing_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If a process can set AGN_ROLE=admin, it bypasses everything.
        This is mitigated by the fact that worker processes set AGN_ROLE at startup,
        but if the model can influence subprocess env vars..."""
        monkeypatch.setenv("AGN_ROLE", "admin")
        ok, _ = check_command(["git", "apply", "foo.patch"])
        assert ok is True, "Admin role allows everything — env var spoofing is the risk"

    def test_unknown_role_falls_back_to_coordinator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGN_ROLE=superuser — unknown role falls back to coordinator (safe default)."""
        monkeypatch.setenv("AGN_ROLE", "superuser")
        role = get_current_role()
        assert role == "coordinator", f"Unknown role should fall back to coordinator, got {role}"

        ok, _ = check_command(["git", "apply", "foo.patch"], role="superuser")
        assert ok is False, "Unknown role should be treated as coordinator (restricted)"
