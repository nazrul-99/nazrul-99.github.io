#!/usr/bin/env python3
"""Regression test for scripts/reveal.py.

    python scripts/test_reveal.py

reveal.py used to rebuild an entry's `links` block from only the fields it
prompted for, which silently deleted any other link -- notably the DOI on an
already-published entry. This runs reveal.py against a throwaway copy of the
repo and asserts the DOI survives.

Copies the repo rather than touching content/, so a failed run can never
damage real content.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COPY = ("content", "templates", "scripts", "static")
FIXTURE_ID = "bnlp-survey"          # published, carries a DOI
NEW_PAPER = "https://example.org/accepted-version.pdf"

failures = []


def check(condition, label):
    print(f"  {'ok  ' if condition else 'FAIL'} {label}")
    if not condition:
        failures.append(label)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp) / "repo"
        sandbox.mkdir()
        for name in COPY:
            shutil.copytree(ROOT / name, sandbox / name)
        for name in (".nojekyll", "cv.pdf"):
            if (ROOT / name).exists():
                shutil.copy(ROOT / name, sandbox / name)
        # build.py copies _source/cv.pdf to the root, so the sandbox needs
        # a source CV too; any PDF-shaped file will do for this test.
        (sandbox / "_source").mkdir()
        source_cv = ROOT / "_source" / "cv.pdf"
        if source_cv.exists():
            shutil.copy(source_cv, sandbox / "_source" / "cv.pdf")
        else:
            (sandbox / "_source" / "cv.pdf").write_bytes(b"%PDF-1.4\n%stub for test_reveal\n")

        pubs_path = sandbox / "content" / "publications.yml"
        before_text = pubs_path.read_text(encoding="utf-8")
        before = yaml.safe_load(before_text)
        target = next(p for p in before if p["id"] == FIXTURE_ID)
        original_doi = (target.get("links") or {}).get("doi")

        print(f"fixture: {FIXTURE_ID}")
        print(f"  starting links: {target.get('links')}\n")
        if not original_doi:
            print("fixture has no DOI to preserve -- test cannot run", file=sys.stderr)
            return 1

        # status, then one answer per LINK_FIELDS entry: set paper, keep the
        # rest (including the DOI) by answering blank.
        answers = "published\n" + NEW_PAPER + "\n" + "\n" * 6
        result = subprocess.run(
            [sys.executable, str(sandbox / "scripts" / "reveal.py"), FIXTURE_ID],
            input=answers, capture_output=True, text=True, cwd=sandbox,
        )
        print(f"reveal.py exit: {result.returncode}")
        if result.returncode != 0:
            print(result.stdout[-800:], result.stderr[-800:], file=sys.stderr)

        after_text = pubs_path.read_text(encoding="utf-8")
        after = yaml.safe_load(after_text)
        revealed = next(p for p in after if p["id"] == FIXTURE_ID)
        links = revealed.get("links") or {}
        print(f"  resulting links: {links}\n")

        check(result.returncode == 0, "reveal.py exits 0")
        check(links.get("doi") == original_doi, "DOI survives untouched")
        check(links.get("paper") == NEW_PAPER, "paper link is updated")
        check(revealed.get("anonymized") is False, "anonymized is false")
        check(revealed.get("status") == "published", "status is published")
        check(revealed.get("note") == target.get("note"), "note untouched")
        check(revealed.get("title") == target.get("title"), "title untouched")
        check(len(after) == len(before), "no entry added or lost")
        check(
            before_text.count("#") == after_text.count("#"),
            "comment lines preserved",
        )
        others_before = {p["id"]: p for p in before if p["id"] != FIXTURE_ID}
        others_after = {p["id"]: p for p in after if p["id"] != FIXTURE_ID}
        check(others_before == others_after, "every other entry byte-identical")

    print()
    if failures:
        print(f"{len(failures)} failure(s): {failures}", file=sys.stderr)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
