#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pointer_protocol import parse_ref, read_ref_text, resolve_ref_path, search_ref_text


def main() -> int:
    parser = argparse.ArgumentParser(description="Read/search AGN pointer artifacts")
    sub = parser.add_subparsers(dest="command", required=True)

    info_parser = sub.add_parser("info")
    info_parser.add_argument("--ref", required=True)

    read_parser = sub.add_parser("read")
    read_parser.add_argument("--ref", required=True)
    read_parser.add_argument("--mode", choices=["all", "tail", "range"], default="tail")
    read_parser.add_argument("--start-line", type=int, default=1)
    read_parser.add_argument("--end-line", type=int, default=200)
    read_parser.add_argument("--tail-lines", type=int, default=200)
    read_parser.add_argument("--max-bytes", type=int, default=32768)

    search_parser = sub.add_parser("search")
    search_parser.add_argument("--ref", required=True)
    search_parser.add_argument("--pattern", required=True)
    search_parser.add_argument("--max-matches", type=int, default=50)

    args = parser.parse_args()

    try:
        if args.command == "info":
            parsed = parse_ref(args.ref)
            resolved = resolve_ref_path(args.ref)
            payload = {
                "ok": True,
                "parsed": parsed,
                "path": str(resolved.relative_to(ROOT)),
                "bytes": resolved.stat().st_size,
            }
            print(json.dumps(payload, ensure_ascii=True))
            return 0

        if args.command == "read":
            text = read_ref_text(
                args.ref,
                mode=args.mode,
                start_line=int(args.start_line),
                end_line=int(args.end_line),
                tail_lines=int(args.tail_lines),
                max_bytes=int(args.max_bytes),
            )
            payload = {
                "ok": True,
                "ref": args.ref,
                "mode": args.mode,
                "text": text,
            }
            print(json.dumps(payload, ensure_ascii=True))
            return 0

        matches = search_ref_text(args.ref, pattern=args.pattern, max_matches=int(args.max_matches))
        payload = {
            "ok": True,
            "ref": args.ref,
            "pattern": args.pattern,
            "match_count": len(matches),
            "matches": matches,
        }
        print(json.dumps(payload, ensure_ascii=True))
        return 0

    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
