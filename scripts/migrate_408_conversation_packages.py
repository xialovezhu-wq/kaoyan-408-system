#!/usr/bin/env python3
"""Create and verify immutable legacy views under the 408 package root."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from conversation_package_408 import DEFAULT_ROOT, migrate_legacy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("dry-run", "migrate", "verify"))
    parser.add_argument("--legacy-root", required=True)
    parser.add_argument("--new-root", default=str(DEFAULT_ROOT))
    args = parser.parse_args()
    result = migrate_legacy(
        mode=args.mode,
        legacy_root=Path(args.legacy_root),
        private_root=Path(args.new_root),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("mismatch_count") == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
