#!/usr/bin/env python3
"""Fail if the version is not identical across every file that declares one.

Four files carry the version and nothing keeps them in step, so a release can
easily ship a plugin.json that disagrees with the marketplace entry. Run this
before tagging.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def from_json(path: str, *keys) -> str | None:
    data = json.loads((ROOT / path).read_text(encoding="utf-8"))
    for key in keys:
        if data is None:
            return None
        data = data[key] if isinstance(key, str) else data[key]
    return data


def from_source(path: str) -> str | None:
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"',
                      (ROOT / path).read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def main() -> int:
    found = {
        ".claude-plugin/plugin.json": from_json(".claude-plugin/plugin.json", "version"),
        ".claude-plugin/marketplace.json": from_json(
            ".claude-plugin/marketplace.json", "plugins", 0, "version"),
        "shared/relay_config.py": from_source("shared/relay_config.py"),
        "bridge/claude_bridge.py": from_source("bridge/claude_bridge.py"),
    }

    expected = found[".claude-plugin/plugin.json"]
    failures = [f"{path} is missing a version" for path, version in found.items()
                if not version]
    failures += [f"{path} has {version}, expected {expected}"
                 for path, version in found.items() if version and version != expected]

    if failures:
        for failure in failures:
            print(f"version check failed: {failure}", file=sys.stderr)
        return 1
    print(f"version {expected} consistent across {len(found)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
