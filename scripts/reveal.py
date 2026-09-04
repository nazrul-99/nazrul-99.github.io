#!/usr/bin/env python3
"""Flip a publication from anonymized to revealed.

    python scripts/reveal.py <pub-id>

Asks for the new status and the links, rewrites that one entry in
content/publications.yml, rebuilds, and shows the diff.

The edit is line-surgical on purpose. Loading the file with PyYAML and dumping
it back would silently delete the commented example block at the top and
reformat every entry, so this rewrites only the lines it has to.
"""
import difflib
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PUBS = ROOT / "content" / "publications.yml"

LINK_FIELDS = ["paper", "arxiv", "code", "project", "slides", "poster"]
REVEALED_STATUSES = ["published", "in-press"]


def ask(prompt, default=""):
    try:
        answer = input(prompt).strip()
    except EOFError:
        return default
    return answer or default


def quote(value):
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def find_entry_bounds(lines, pub_id):
    """Return (start, end) line indices of the top-level entry for pub_id."""
    start_re = re.compile(rf"^-\s+id:\s*[\"']?{re.escape(pub_id)}[\"']?\s*$")
    start = None
    for i, line in enumerate(lines):
        if start_re.match(line.rstrip("\n")):
            start = i
            break
    if start is None:
        return None, None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^-\s", lines[j]):     # next top-level list item
            end = j
            break
    return start, end


def block_extent(lines, index):
    """Extent of the mapping that starts on `index`, including nested lines."""
    key_indent = len(lines[index]) - len(lines[index].lstrip())
    end = index + 1
    while end < len(lines):
        line = lines[end]
        if not line.strip():                # blank line: keep going, it may
            end += 1                        # sit inside the block
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= key_indent:
            break
        end += 1
    return end


def main():
    if len(sys.argv) != 2:
        print("usage: python scripts/reveal.py <pub-id>", file=sys.stderr)
        return 1
    pub_id = sys.argv[1]

    original = PUBS.read_text(encoding="utf-8")
    pubs = yaml.safe_load(original) or []
    entry = next((p for p in pubs if p.get("id") == pub_id), None)
    if entry is None:
        print(f"no publication with id '{pub_id}' in {PUBS.name}", file=sys.stderr)
        print("ids: " + ", ".join(str(p.get("id")) for p in pubs), file=sys.stderr)
        return 1

    print(f"\n  {entry.get('title', '(no title)')}")
    print(f"  currently: status={entry.get('status')} anonymized={entry.get('anonymized')}\n")

    if not entry.get("anonymized"):
        print("note: this entry is already not anonymized; continuing will still "
              "update its status and links.\n")

    status = ""
    while status not in REVEALED_STATUSES:
        status = ask(f"status [{'/'.join(REVEALED_STATUSES)}] (published): ", "published")
        if status not in REVEALED_STATUSES:
            print(f"  must be one of: {', '.join(REVEALED_STATUSES)}")

    print("\nlinks -- leave blank to skip:")
    links = {}
    for field in LINK_FIELDS:
        value = ask(f"  {field}: ")
        if value:
            links[field] = value

    lines = original.splitlines(keepends=True)
    start, end = find_entry_bounds(lines, pub_id)
    if start is None:
        print(f"could not locate the '- id: {pub_id}' line to edit", file=sys.stderr)
        return 1

    out = lines[:start]
    i = start
    while i < end:
        line = lines[i]
        stripped = line.strip()
        indent = line[: len(line) - len(line.lstrip())]

        if re.match(r"^anonymized:\s", stripped):
            out.append(f"{indent}anonymized: false\n")
            i += 1
        elif re.match(r"^status:\s", stripped):
            out.append(f"{indent}status: {quote(status)}\n")
            i += 1
        elif re.match(r"^links:", stripped):
            if links:
                out.append(f"{indent}links:\n")
                for field, value in links.items():
                    out.append(f"{indent}  {field}: {quote(value)}\n")
            else:
                out.append(f"{indent}links: {{}}\n")
            i = block_extent(lines, i)
        else:
            out.append(line)
            i += 1

    out.extend(lines[end:])
    updated = "".join(out)

    # Never leave the file in a state the build cannot read.
    try:
        reparsed = yaml.safe_load(updated) or []
    except yaml.YAMLError as e:
        print(f"edit produced invalid YAML, leaving the file untouched: {e}",
              file=sys.stderr)
        return 1

    check = next((p for p in reparsed if p.get("id") == pub_id), None)
    if check is None or check.get("anonymized") is not False:
        print("edit did not apply as expected, leaving the file untouched",
              file=sys.stderr)
        return 1
    if {k: v for k, v in (check.get("links") or {}).items() if v} != links:
        print("links did not apply as expected, leaving the file untouched",
              file=sys.stderr)
        return 1

    PUBS.write_text(updated, encoding="utf-8")

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/content/{PUBS.name}",
        tofile=f"b/content/{PUBS.name}",
    )
    print("\n" + "".join(diff))

    print("==> rebuilding")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build.py")])
    if result.returncode != 0:
        return result.returncode

    print(f"\n'{pub_id}' is now {status}, with {len(links)} link(s).")
    print("Next: ./scripts/deploy.sh \"reveal " + pub_id + "\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
