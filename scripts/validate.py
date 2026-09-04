#!/usr/bin/env python3
"""Validate content/ and the generated root HTML.

Non-zero exit on any failure. Run from anywhere: python scripts/validate.py

Checks (CLAUDE.md, Script contracts):
  - required fields present in every content file
  - no TODO / FIXME / Lorem in content or generated output
  - internal links in generated HTML resolve to a real file
  - every content YAML file parses
  - cv.pdf exists at repo root
  - cv-full.pdf, *_draft*, *private* and *.docx are never staged in git
  - no phone-number-shaped pattern, and no SEU address, in content or output
  - no page over the 150 KB budget

Checks (CLAUDE.md, Anonymity rules):
  - rule 3: an anonymized publication has no populated links
  - rule 2: a suppressed entry's title and link URLs are absent from every
    generated file, entities decoded (see note below)

Warnings (non-fatal):
  - rule 4: status: under-review with anonymized: false
  - rule 7: an anonymized entry's summary contains the first word of its title
  - rule 2: a suppressed entry's venue appears in output and is not
    attributable to a visible entry
  - rule 6: an in-preparation entry carries links (suppressed in render, but
    it suggests the entry is mislabelled)

Note on rule 2 scanning. Titles and link URLs are long and unique to one
entry, so a match is a real leak and fails the build. Venue strings and
co-author names legitimately recur across entries -- a co-author on an
anonymized submission is often also a co-author on a published paper, and two
entries can share a venue -- so a bare name or venue in the output is not by
itself evidence of a leak. Venue matches that cannot be attributed to a
visible entry warn; author names are not scanned at all, because build.py
never assembles an author list for a suppressed entry in the first place.
"""
import html
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"

BANNED_WORDS = ["TODO", "FIXME", "Lorem"]

# Phone detection. A digit run of 10-13 digits is phone-length; dots are not
# accepted as separators, which keeps DOIs and version strings out of it, and
# runs containing '--' or an en dash are numeric ranges (bibtex page ranges
# like 38999--39044), not numbers. Of what is left, a Bangladeshi prefix or
# phone-shaped grouping fails the build; anything else only warns, so a
# legitimate page range can never block a deploy.
DIGIT_RUN = re.compile(r"[0-9][0-9\s\-()+]{7,}[0-9]")
RANGE_MARKERS = ("--", "–", "—")
SEU_PATTERN = re.compile(
    r"[\w.+-]+@seu\.edu\.bd|[\[(]at[\])]\s*seu\.edu\.bd", re.IGNORECASE
)
PAGE_BUDGET_BYTES = 150 * 1024

CONTENT_FILES = [
    "site.yml",
    "research_threads.yml",
    "publications.yml",
    "projects.yml",
    "education.yml",
    "writing.yml",
]

errors = []
warnings = []


def fail(message):
    errors.append(message)


def warn(message):
    warnings.append(message)


def load_yaml(name):
    path = CONTENT / name
    if not path.exists():
        fail(f"{name}: file missing")
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        fail(f"{name}: does not parse as YAML ({e})")
        return None


def require(record, fields, label):
    for field in fields:
        if record.get(field) in (None, ""):
            fail(f"{label}: missing required field '{field}'")


def check_site(site):
    if site is None:
        return
    require(site, ["name", "tagline", "email"], "site.yml")
    email = site.get("email") or {}
    require(email, ["user", "domain"], "site.yml: email")


def strip_comments(text):
    while "<!--" in text:
        start = text.index("<!--")
        end = text.index("-->", start)
        text = text[:start] + text[end + 3:]
    return text


def check_about():
    path = CONTENT / "about.md"
    if not path.exists():
        fail("about.md: file missing")
        return
    stripped = strip_comments(path.read_text(encoding="utf-8"))
    word_count = len(stripped.split())
    if not (120 <= word_count <= 180):
        fail(f"about.md: bio is {word_count} words, expected 120-180")
    check_banned_words(stripped, "about.md")


def check_research_threads(threads):
    if threads is None:
        return
    for t in threads or []:
        require(
            t,
            ["id", "title", "description", "claim", "correction"],
            f"research_threads.yml[{t.get('id', '?')}]",
        )


PUBLICATION_REQUIRED = ["id", "title", "authors", "year", "type", "status", "summary"]
VALID_TYPES = {"conference", "journal", "workshop", "preprint", "thesis"}
VALID_STATUSES = {"published", "in-press", "under-review", "in-preparation", "preprint"}


def is_suppressed(pub):
    """Anonymity rules 1 and 6: these entries render as status + year + summary."""
    return bool(pub.get("anonymized")) or pub.get("status") == "in-preparation"


def check_publications(pubs):
    if pubs is None:
        return
    seen_ids = set()
    for p in pubs or []:
        label = f"publications.yml[{p.get('id', '?')}]"
        require(p, PUBLICATION_REQUIRED, label)
        # venue is required unless the work has no venue yet (in-preparation)
        if p.get("status") != "in-preparation":
            require(p, ["venue"], label)
        pid = p.get("id")
        if pid in seen_ids:
            fail(f"{label}: duplicate id")
        seen_ids.add(pid)
        if p.get("type") and p["type"] not in VALID_TYPES:
            fail(f"{label}: invalid type '{p['type']}'")
        if p.get("status") and p["status"] not in VALID_STATUSES:
            fail(f"{label}: invalid status '{p['status']}'")

        populated = [k for k, v in (p.get("links") or {}).items() if v]

        # Rule 3: anonymized entries may not have any populated link.
        if p.get("anonymized") and populated:
            fail(f"{label}: anonymized but has populated links {populated}")

        # Rule 6: in-preparation is suppressed the same way, so links on one
        # are dead weight and usually mean the status is wrong.
        if not p.get("anonymized") and p.get("status") == "in-preparation" and populated:
            warn(f"{label}: in-preparation entries render no links, but has {populated}")

        # Rule 4: under-review and not anonymized -- re-check venue policy.
        if p.get("status") == "under-review" and not p.get("anonymized"):
            warn(
                f"{label}: status is under-review but anonymized is false -- "
                "re-check the venue's preprint policy"
            )

        # Rule 7: an anonymized summary should not name the method.
        if p.get("anonymized") and p.get("title") and p.get("summary"):
            first_word = p["title"].split()[0].lower().strip(",.:;")
            if first_word and re.search(
                rf"\b{re.escape(first_word)}\b", p["summary"], re.IGNORECASE
            ):
                warn(
                    f"{label}: summary may contain the method name "
                    f"('{p['title'].split()[0]}') from the title"
                )


def check_projects(projects):
    if projects is None:
        return
    for proj in projects or []:
        require(
            proj,
            ["title", "pitch", "description"],
            f"projects.yml[{proj.get('title', '?')}]",
        )


def check_education(edu):
    if edu is None:
        return
    for d in edu.get("degrees") or []:
        require(d, ["degree", "institution", "period"], "education.yml: degree")


def check_writing(items):
    if items is None:
        return
    for item in items or []:
        require(
            item,
            ["title", "date", "blurb", "link"],
            f"writing.yml[{item.get('title', '?')}]",
        )


def check_banned_words(text, label):
    for word in BANNED_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", text):
            fail(f"{label}: contains banned placeholder word '{word}'")


def check_privacy_patterns(text, label):
    """Phone numbers and the SEU address, in content or in output."""
    if "+880" in text:
        fail(f"{label}: contains '+880'")

    for match in DIGIT_RUN.finditer(text):
        run = match.group()
        if any(marker in run for marker in RANGE_MARKERS):
            continue                      # numeric range, not a number
        digits = re.sub(r"\D", "", run)
        if not (10 <= len(digits) <= 13):
            continue
        separator_groups = len(re.findall(r"[\s\-()+]+", run.strip()))
        if digits.startswith("880") or digits.startswith("01"):
            fail(f"{label}: contains a phone number ('{run.strip()}')")
        elif separator_groups >= 2:
            fail(f"{label}: contains a phone-number-shaped pattern ('{run.strip()}')")
        else:
            warn(
                f"{label}: '{run.strip()}' is phone-number length -- confirm it "
                "is not a phone number"
            )

    if SEU_PATTERN.search(text):
        fail(f"{label}: contains an SEU institutional address")


def check_cv_exists():
    if not (ROOT / "cv.pdf").exists():
        fail("cv.pdf: missing at repo root")


FORBIDDEN_STAGED = ("cv-full.pdf",)


def staged_files():
    """Files in the git index, or None when this is not a git repo."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.splitlines()


def check_nothing_forbidden_staged():
    staged = staged_files()
    if staged is None:
        return  # not a git repo yet, or git unavailable -- nothing to check
    for path in staged:
        name = Path(path).name
        lowered = name.lower()
        if (
            name in FORBIDDEN_STAGED
            or "draft" in lowered
            or "private" in lowered
            or lowered.endswith(".docx")
        ):
            fail(f"git: '{path}' is staged and must never be committed")


INTERNAL_HREF = re.compile(r'href="(/[^"]*)"')


def output_files():
    """Every generated file a crawler could read, with entities decoded.

    cv.pdf is included as raw bytes: that catches a leak in an uncompressed
    PDF text stream, but it cannot see inside a compressed one, so it is a
    backstop rather than a guarantee.
    """
    files = []
    for pattern in ("*.html", "*.xml", "*.json", "*.txt"):
        for path in sorted(ROOT.glob(pattern)):
            raw = path.read_text(encoding="utf-8")
            files.append((path, raw + "\n" + html.unescape(raw)))
    pdf = ROOT / "cv.pdf"
    if pdf.exists():
        files.append((pdf, pdf.read_bytes().decode("latin-1")))
    return files


def check_output_files(outputs):
    html_files = [p for p, _ in outputs if p.suffix == ".html"]
    if not html_files:
        fail("no generated HTML files found at repo root -- run scripts/build.py first")
        return
    for path, text in outputs:
        check_privacy_patterns(text, path.name)
        if path.suffix != ".pdf":
            check_banned_words(text, path.name)
        size = path.stat().st_size
        if path.suffix == ".html" and size > PAGE_BUDGET_BYTES:
            fail(f"{path.name}: {size // 1024} KB exceeds the 150 KB page budget")
        for href in INTERNAL_HREF.findall(text):
            target = href.split("#")[0].split("?")[0]
            if target in ("", "/"):
                continue
            if not (ROOT / target.lstrip("/")).exists():
                fail(f"{path.name}: internal link '{href}' does not resolve")


def normalize(text):
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def check_anonymity_in_output(pubs, outputs):
    """Anonymity rule 2: suppressed fields must be absent, not merely hidden."""
    if not pubs:
        return
    suppressed = [p for p in pubs if is_suppressed(p)]
    if not suppressed:
        return
    visible = [p for p in pubs if not is_suppressed(p)]
    visible_titles = [normalize(p.get("title", "")) for p in visible]
    visible_venues = [normalize(p.get("venue", "")) for p in visible]

    for pub in suppressed:
        label = f"publications.yml[{pub.get('id', '?')}]"
        title = normalize(pub.get("title", ""))
        venue = normalize(pub.get("venue", ""))
        urls = [v for v in (pub.get("links") or {}).values() if v]

        for path, text in outputs:
            haystack = normalize(text)

            if title and title in haystack:
                # Only a leak if no visible entry accounts for the string.
                if not any(title in vt for vt in visible_titles):
                    fail(
                        f"{label}: suppressed title appears in {path.name} "
                        "(anonymity rule 2)"
                    )

            for url in urls:
                if normalize(url) in haystack:
                    fail(
                        f"{label}: suppressed link '{url}' appears in {path.name} "
                        "(anonymity rule 2)"
                    )

            if venue and venue in haystack:
                if not any(venue in vv for vv in visible_venues):
                    warn(
                        f"{label}: venue '{pub.get('venue')}' appears in "
                        f"{path.name} and no visible entry accounts for it -- "
                        "confirm it is not attached to the suppressed entry"
                    )


def main():
    site = load_yaml("site.yml")
    check_site(site)
    check_about()
    check_research_threads(load_yaml("research_threads.yml"))
    publications = load_yaml("publications.yml")
    check_publications(publications)
    check_projects(load_yaml("projects.yml"))
    check_education(load_yaml("education.yml"))
    check_writing(load_yaml("writing.yml"))

    for name in CONTENT_FILES:
        path = CONTENT / name
        if path.exists():
            text = path.read_text(encoding="utf-8")
            check_banned_words(text, name)
            check_privacy_patterns(text, name)

    about = CONTENT / "about.md"
    if about.exists():
        check_privacy_patterns(about.read_text(encoding="utf-8"), "about.md")

    check_cv_exists()
    check_nothing_forbidden_staged()

    outputs = output_files()
    check_output_files(outputs)
    check_anonymity_in_output(publications, outputs)

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        print(f"\n{len(errors)} error(s).", file=sys.stderr)
        return 1

    print(f"OK ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
