#!/usr/bin/env python3
"""Chaos Experiment 3: Telegram admin impersonation.

Tests whether an unknown chat_id can:
1. Pass the allowlist gate
2. Dispatch commands (/agn, /research)
3. Submit task payloads

Also tests whether setting ALLOWED_CHAT_IDS correctly blocks unknown users.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from telegram_listener import parse_allowed_chat_ids, should_allow_chat

ADMIN_CHAT_ID = "6882426592"
ATTACKER_CHAT_ID = "9999999999"


def main():
    print("=" * 70)
    print("EXPERIMENT 3: Telegram Admin Impersonation")
    print("=" * 70)

    vulns = 0

    # Scenario 1: No allowlist set (current production state)
    print("\n--- Scenario 1: ALLOWED_CHAT_IDS not set (production default) ---")
    allowed = parse_allowed_chat_ids("")
    print(f"  parse_allowed_chat_ids('') = {allowed}")

    admin_ok = should_allow_chat(ADMIN_CHAT_ID, allowed)
    attacker_ok = should_allow_chat(ATTACKER_CHAT_ID, allowed)
    print(f"  Admin   ({ADMIN_CHAT_ID}): allowed={admin_ok}")
    print(f"  Attacker({ATTACKER_CHAT_ID}): allowed={attacker_ok}")
    if attacker_ok:
        print("  !! VULNERABLE: Any Telegram user can dispatch commands")
        vulns += 1
    else:
        print("  SAFE: Unknown users are blocked")

    # Scenario 2: Allowlist set correctly
    print("\n--- Scenario 2: ALLOWED_CHAT_IDS set to admin only ---")
    allowed = parse_allowed_chat_ids(ADMIN_CHAT_ID)
    print(f"  parse_allowed_chat_ids('{ADMIN_CHAT_ID}') = {allowed}")

    admin_ok = should_allow_chat(ADMIN_CHAT_ID, allowed)
    attacker_ok = should_allow_chat(ATTACKER_CHAT_ID, allowed)
    print(f"  Admin   ({ADMIN_CHAT_ID}): allowed={admin_ok}")
    print(f"  Attacker({ATTACKER_CHAT_ID}): allowed={attacker_ok}")
    if attacker_ok:
        print("  !! STILL VULNERABLE even with allowlist")
        vulns += 1
    else:
        print("  SAFE: Attacker correctly blocked")

    # Scenario 3: Multiple allowed IDs
    print("\n--- Scenario 3: Multiple allowed chat IDs ---")
    allowed = parse_allowed_chat_ids(f"{ADMIN_CHAT_ID},1111111111")
    attacker_ok = should_allow_chat(ATTACKER_CHAT_ID, allowed)
    print(f"  Attacker({ATTACKER_CHAT_ID}): allowed={attacker_ok}")
    if not attacker_ok:
        print("  SAFE: Multi-ID allowlist works correctly")
    else:
        vulns += 1

    # Scenario 4: What commands would an attacker be able to run?
    print("\n--- Scenario 4: Attacker command capabilities (if allowlist bypassed) ---")
    dangerous_commands = [
        "/agn status",
        "/research start minimal",
        "/research auto on",
        "/research set-morning 03:00",
        "TASK_ID=evil-task\nREQUEST_TEXT=delete all files\nRISK_LEVEL=low",
    ]
    print("  If an attacker bypasses the chat allowlist, they can send:")
    for cmd in dangerous_commands:
        print(f"    - {cmd.split(chr(10))[0]}")
    print("  All of these would be executed without further auth checks.")

    # Summary
    print(f"\n{'='*70}")
    print(f"VERDICT: {vulns} vulnerability(ies) found")
    if vulns > 0:
        print("FIX REQUIRED: Set ALLOWED_CHAT_IDS or change default behavior to fail-closed")
        print(f"RECOMMENDED: Set ALLOWED_CHAT_IDS={ADMIN_CHAT_ID} in LaunchAgent env")
    print("=" * 70)

    return 1 if vulns > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
