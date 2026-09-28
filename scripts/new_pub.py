#!/usr/bin/env python3
"""Append a new publication to content/publications.yml, interactively.

    python scripts/new_pub.py

Nine questions (links include a repository URL for the Code pill), then the block is appended to the end of the file. Appending
text rather than round-tripping through PyYAML keeps the commented example
block and every existing entry byte-identical.

Anonymity rule 3 is enforced at the prompt: an anonymized entry is never
offered the link questions, so it cannot be given a link to begin with.
"""
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PUBS = ROOT / "content" / "publications.yml"

TYPES = ["conference", "journal", "workshop", "preprint", "thesis"]
STATUSES = ["published", "in-press", "under-review", "in-preparation", "preprint"]
# Asked in this order. `code` renders as the Code pill (after PDF, before
# Cite/DOI); validate.py requires every link to be absolute https and checks
# that a github.com URL answers 200.
LINK_FIELDS = ["paper", "code", "doi", "arxiv", "project", "slides", "poster"]
LINK_HINTS = {"code": "repository, e.g. https://github.com/user/repo", "doi": "https://doi.org/..."}


def ask(prompt, default=""):
    try:
        answer = input(prompt).strip()
    except EOFError:
        return default
    return answer or default


def ask_required(prompt):
    while True:
        value = ask(prompt)
        if value:
            return value
        print("  required")


def ask_choice(prompt, choices, default):
    while True:
        value = ask(f"{prompt} [{'/'.join(choices)}] ({default}): ", default)
        if value in choices:
            return value
        print(f"  must be one of: {', '.join(choices)}")


def ask_yes_no(prompt, default):
    suffix = "Y/n" if default else "y/N"
    value = ask(f"{prompt} [{suffix}]: ").lower()
    if not value:
        return default
    return value.startswith("y")


def quote(value):
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def main():
    original = PUBS.read_text(encoding="utf-8")
    existing = yaml.safe_load(original) or []
    existing_ids = {p.get("id") for p in existing}

    print("\nNew publication. Ctrl-c to abort.\n")

    while True:
        pub_id = ask_required("id (short slug, e.g. geotoken): ")
        if pub_id in existing_ids:
            print(f"  '{pub_id}' already exists in {PUBS.name}")
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", pub_id):
            print("  use lowercase letters, digits and hyphens")
            continue
        break

    title = ask_required("title: ")
    authors_raw = ask_required("authors (comma-separated, in order): ")
    authors = [a.strip() for a in authors_raw.split(",") if a.strip()]

    equal = []
    if len(authors) > 1:
        raw = ask("equal contribution -- author numbers, e.g. 1,2 (none): ")
        for token in raw.replace(" ", "").split(","):
            if token.isdigit() and 1 <= int(token) <= len(authors):
                equal.append(int(token) - 1)

    status = ask_choice("status", STATUSES, "under-review")
    venue = ask("venue (blank if none yet): ") if status != "in-preparation" else ""
    year = ""
    while not re.fullmatch(r"\d{4}", year):
        year = ask_required("year (YYYY): ")
    pub_type = ask_choice("type", TYPES, "conference")

    anonymized = ask_yes_no(
        "anonymized (suppress title, authors, venue, links)?",
        status == "under-review",
    )

    summary = ask_required("summary (one or two sentences): ")

    links = {}
    if anonymized:
        print("\n  anonymized: skipping the link questions (anonymity rule 3).")
    else:
        print("\nlinks -- absolute https URLs, leave blank to skip:")
        for field in LINK_FIELDS:
            hint = f" ({LINK_HINTS[field]})" if field in LINK_HINTS else ""
            while True:
                value = ask(f"  {field}{hint}: ")
                if not value or value.startswith("https://"):
                    break
                print("  must start with https://")
            if value:
                links[field] = value

    block = [f"\n- id: {quote(pub_id)}\n"]
    block.append(f"  title: {quote(title)}\n")
    block.append("  authors: [" + ", ".join(quote(a) for a in authors) + "]\n")
    block.append(f"  venue: {quote(venue)}\n")
    block.append(f"  year: {year}\n")
    block.append(f"  type: {quote(pub_type)}\n")
    block.append(f"  status: {quote(status)}\n")
    block.append(f"  anonymized: {'true' if anonymized else 'false'}\n")
    block.append("  equal_contribution: [" + ", ".join(str(i) for i in equal) + "]\n")
    if links:
        block.append("  links:\n")
        for field, value in links.items():
            block.append(f"    {field}: {quote(value)}\n")
    else:
        block.append("  links: {}\n")
    block.append('  bibtex: ""\n')
    block.append('  note: ""\n')
    block.append("  summary: >\n")
    for line in textwrap.wrap(summary, width=72):
        block.append(f"    {line}\n")

    updated = original
    if not updated.endswith("\n"):
        updated += "\n"
    updated += "".join(block)

    try:
        reparsed = yaml.safe_load(updated) or []
    except yaml.YAMLError as e:
        print(f"\nthat produced invalid YAML, leaving the file untouched: {e}",
              file=sys.stderr)
        return 1

    added = next((p for p in reparsed if p.get("id") == pub_id), None)
    if added is None:
        print("\nthe new entry did not parse back, leaving the file untouched",
              file=sys.stderr)
        return 1

    PUBS.write_text(updated, encoding="utf-8")

    print("\nappended to content/publications.yml:\n")
    print("".join(block))

    print("==> rebuilding")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build.py")])
    if result.returncode != 0:
        return result.returncode

    print(f'\nNext: ./scripts/deploy.sh "add {pub_id}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
