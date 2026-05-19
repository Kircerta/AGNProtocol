# Release Checklist

Use this checklist before publishing a tagged release or archive.

1. Run `git status -sb`.
2. Run `python3 scripts/maintenance/check_portability.py`.
3. Run `git diff --check`.
4. Run the secret scan from `SECURITY.md`.
5. Run targeted tests for the changed surface.
6. Confirm runtime directories and local configuration files are ignored.
7. Confirm provider credentials are not present in tracked files.
