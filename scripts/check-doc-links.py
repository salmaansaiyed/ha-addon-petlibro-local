#!/usr/bin/env python3
"""Validate local inline Markdown links without network access."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "build"}
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def github_anchor(heading: str) -> str:
    heading = re.sub(r"<[^>]+>", "", heading.lower())
    heading = re.sub(r"[`*_~]", "", heading)
    heading = re.sub(r"[^\w\- ]", "", heading, flags=re.UNICODE)
    return re.sub(r"[ ]+", "-", heading.strip())


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not EXCLUDED_PARTS.intersection(path.relative_to(ROOT).parts)
    )


def main() -> int:
    failures: list[str] = []
    anchors: dict[Path, set[str]] = {}

    for document in markdown_files():
        text = document.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            raw_target = match.group(1).strip()
            if raw_target.startswith("<") and raw_target.endswith(">"):
                raw_target = raw_target[1:-1]
            if not raw_target or raw_target.startswith(
                ("http://", "https://", "mailto:", "tel:")
            ):
                continue

            target_text, separator, fragment = raw_target.partition("#")
            target = document if not target_text else (
                document.parent / unquote(target_text)
            ).resolve()

            try:
                target.relative_to(ROOT)
            except ValueError:
                failures.append(
                    f"{document.relative_to(ROOT)}: link escapes repository: {raw_target}"
                )
                continue

            if not target.exists():
                failures.append(
                    f"{document.relative_to(ROOT)}: missing target: {raw_target}"
                )
                continue

            if separator and fragment and target.is_file() and target.suffix == ".md":
                if target not in anchors:
                    target_text_content = target.read_text(encoding="utf-8")
                    anchors[target] = {
                        github_anchor(heading)
                        for heading in HEADING_RE.findall(target_text_content)
                    }
                if unquote(fragment).lower() not in anchors[target]:
                    failures.append(
                        f"{document.relative_to(ROOT)}: missing anchor: {raw_target}"
                    )

    if failures:
        print("Markdown link validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"Validated local links in {len(markdown_files())} Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
