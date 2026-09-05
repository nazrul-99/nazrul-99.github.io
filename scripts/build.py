#!/usr/bin/env python3
"""Clean rebuild: content/ + templates/ -> root HTML, sitemap and cv.pdf.

Run from anywhere: python scripts/build.py
"""
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"

# The public CV is maintained by hand (CLAUDE.md § Two CVs) and copied to the
# root on every build, so ./cv.pdf is always byte-identical to the source.
CV_SOURCE = ROOT / "_source" / "cv.pdf"
CV_OUTPUT = ROOT / "cv.pdf"

# The one place an absolute URL is defined. Everything the site emits --
# canonical tags, Open Graph tags, JSON-LD, sitemap.xml -- derives from this.
BASE_URL = "https://nazrul-99.github.io"

# template name -> (output file, canonical path)
OUTPUT_PAGES = {
    "index.html": ("index.html", "/"),
    "publications.html": ("publications.html", "/publications.html"),
    "projects.html": ("projects.html", "/projects.html"),
}

# cv.pdf is public and crawlable per CLAUDE.md, so it belongs in the sitemap.
SITEMAP_PATHS = ["/", "/publications.html", "/projects.html", "/cv.pdf"]


class ContentError(Exception):
    """A content file the build cannot read. Reported without a traceback."""


def load_yaml(name):
    path = CONTENT / name
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ContentError(f"content/{name}: file is missing")
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(e, "problem", None) or str(e).splitlines()[0]
        raise ContentError(f"content/{name}:{where}: {problem}")
    return data if data is not None else []


def load_about_paragraphs():
    """Split about.md into paragraphs. No markdown library per CLAUDE.md's
    hard constraints (Python 3 + Jinja2 + PyYAML only) -- about.md is plain
    prose, so blank-line-separated paragraphs is all the format needs."""
    text = (CONTENT / "about.md").read_text(encoding="utf-8")
    # Drop HTML comments (used for the schema example at the top of the file).
    while "<!--" in text:
        start = text.index("<!--")
        end = text.index("-->", start)
        text = text[:start] + text[end + 3:]
    paragraphs = [p.strip().replace("\n", " ") for p in text.strip().split("\n\n")]
    return [p for p in paragraphs if p]


def obfuscate(text):
    """Numeric HTML entity encoding -- readable and clickable with no JS,
    resistant to plain-text scraping of the page source."""
    return "".join(f"&#{ord(c)};" for c in text)


LINK_LABELS = {
    "paper": "Paper",
    "doi": "DOI",
    "arxiv": "arXiv",
    "code": "Code",
    "project": "Project",
    "slides": "Slides",
    "poster": "Poster",
}


# Standing shown for work that is not yet published. _source/DESIGN.md sets
# this as italic prose, the way a CV does it. Only in-preparation carries the
# year: the others sit under a venue line that already names it.
STATUS_LABELS = {
    "under-review": "Under review",
    "in-preparation": "In preparation, {year}",
    "in-press": "In press",
    "preprint": "Preprint",
}


def status_label(status, year):
    template = STATUS_LABELS.get(status)
    return template.format(year=year) if template else None


def venue_line(venue, year, status, note=""):
    """Venue, year and any volume/pages note on one line.

    Work under review is prefixed 'Submitted to', so a venue name can never
    read as an acceptance. The year is not appended when the venue string
    already carries it -- _source/PAPERS.md writes venues as 'ICCIT 2026'.
    """
    venue = (venue or "").strip()
    if not venue:
        return ""
    line = venue if venue.endswith(str(year)) else f"{venue}, {year}"
    if status == "under-review":
        line = f"Submitted to {line}"
    if note:
        line = f"{line}, {note}"
    return line


def build_authors_html(authors, equal_contribution, site_name):
    marked = []
    for i, author in enumerate(authors):
        name = f"<strong>{author}</strong>" if author == site_name else author
        if i in (equal_contribution or []):
            name += "*"
        marked.append(name)
    html = ", ".join(marked)
    if equal_contribution:
        html += " (* equal contribution)"
    return html


def is_suppressed(pub):
    """Anonymity rule 1: only `anonymized: true` suppresses an entry.
    (Rule 6: in-preparation entries render like any other.)"""
    return bool(pub.get("anonymized", False))


def view_publication(pub, site_name):
    """Return the dict a template is allowed to see for one publication.

    Anonymized entries get ONLY status/year/summary -- title, authors, venue,
    links and even the id are never assembled into their view, so they cannot
    leak into generated HTML by accident (CLAUDE.md anonymity rule 2)."""
    if is_suppressed(pub):
        return {
            "suppressed": True,
            "status": pub.get("status"),
            "year": pub.get("year"),
            "summary": pub.get("summary", ""),
        }
    links = [
        (LINK_LABELS.get(key, key), url)
        for key, url in (pub.get("links") or {}).items()
        if url
    ]
    return {
        "suppressed": False,
        "id": pub.get("id"),
        "title": pub.get("title", ""),
        "authors_html": build_authors_html(
            pub.get("authors", []), pub.get("equal_contribution", []), site_name
        ),
        "venue_line": venue_line(
            pub.get("venue", ""), pub.get("year"), pub.get("status"), pub.get("note", "")
        ),
        "status_label": status_label(pub.get("status"), pub.get("year")),
        "status": pub.get("status"),
        # venue line without the volume/pages note, for the thread -> paper line
        "venue_short": venue_line(pub.get("venue", ""), pub.get("year"), pub.get("status")),
        "year": pub.get("year"),
        "links": links,
        "bibtex": pub.get("bibtex", ""),
        "summary": pub.get("summary", ""),
    }


def group_by_year(pub_views):
    years = {}
    for view in pub_views:
        years.setdefault(view["year"], []).append(view)
    return sorted(years.items(), key=lambda item: item[0], reverse=True)


def short_title(title):
    """'GeoToken: Differentiable ...' -> 'GeoToken'. Titles without a colon are
    used whole."""
    return title.split(":")[0].strip()


def attach_related(threads, views_by_id):
    """The thread -> publication line under each claim/correction block, e.g.
    'GeoToken · submitted to AAAI 2027'. Built from the *views*, so an
    anonymized paper contributes only 'In submission, [year]' with no link."""
    for thread in threads:
        related = []
        # The Code pill: the thread's own `code:` wins; otherwise the first
        # linked publication that carries links.code, so a repository entered
        # once on the paper also appears on its thread. A suppressed paper's
        # links are never in its view, so nothing can leak through here.
        code_url = (thread.get("code") or "").strip()
        for pid in thread.get("related_publications") or []:
            view = views_by_id.get(pid)
            if view is None:
                continue
            if view["suppressed"]:
                related.append({"label": "In submission", "standing": str(view["year"]), "href": ""})
                continue
            if not code_url:
                code_url = next((url for label, url in view["links"] if label == "Code"), "")
            # Lower-case only a sentence-like opener ("Submitted to ...",
            # "In preparation, ..."); a venue that starts with an acronym
            # ("ACL", "IEEE") must keep its case.
            if view["status"] == "in-preparation":
                text = view["status_label"] or "In preparation"
                standing = text[0].lower() + text[1:]
            elif view["venue_short"].startswith("Submitted to"):
                standing = "s" + view["venue_short"][1:]
            else:
                standing = view["venue_short"] or (view["status_label"] or "")
            related.append({
                "label": short_title(view["title"]),
                "standing": standing,
                "href": f"/publications.html#{view['id']}",
            })
        thread["related"] = related
        thread["code_url"] = code_url
    return threads


MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
PRESENT = (9999, 12)
_DATE_TOKEN = re.compile(r"(?:([A-Za-z]{3,9})\.?\s+)?((?:19|20)\d{2})|(present|current|ongoing|now)", re.I)


def parse_span(text):
    """(start_year, start_month, end_year, end_month) from a free-text date
    range as the content files write them: '2020', '2024–2025',
    'February 2024 – Present', 'AY 2018–2019 and 2019–2020'. The first date
    token is the start and the last one the end; a lone date is both. A month
    that is not given is 0, so 'March 2019' sorts after '2019'. None when the
    text holds no year at all."""
    points = []
    for month, year, present in _DATE_TOKEN.findall(str(text or "")):
        if present:
            points.append(PRESENT)
        else:
            points.append((int(year), MONTHS.get(month.lower()[:9], 0) if month else 0))
    if not points:
        return None
    start, end = points[0], points[-1]
    return (start[0], start[1], end[0], end[1])


def sort_by_start(entries, field):
    """Timeline order: start date, newest first; entries that share a start
    year by end date, newest first. Entries with no parseable date keep their
    YAML order and go last, so a typo never hides an entry."""
    dated, undated = [], []
    for e in entries or []:
        span = parse_span(e.get(field))
        (dated if span else undated).append((span, e))
    dated.sort(key=lambda item: (item[0][0], item[0][2], item[0][3], item[0][1]), reverse=True)
    return [e for _, e in dated] + [e for _, e in undated]


def build_jsonld(site):
    """Person + WebSite only.

    Deliberately carries no publication entries: JSON-LD is one of the
    surfaces anonymity rule 2 names, and the cheapest way to guarantee a
    suppressed title or venue can never leak into it is to never put
    publication data there at all. Also carries no email address, so the
    obfuscation in the page body is not undone by the metadata.
    """
    person = {
        "@type": "Person",
        "@id": f"{BASE_URL}/#person",
        "name": site["name"],
        "url": f"{BASE_URL}/",
    }
    same_as = [link["url"] for link in (site.get("links") or []) if link.get("url")]
    if same_as:
        person["sameAs"] = same_as
    # site.affiliation is "Role — Institution" (the About column splits it the
    # same way). schema.org wants the institution as an Organization and the
    # role as jobTitle, not one free-text string.
    role, _, institution = (site.get("affiliation") or "").partition(" — ")
    if institution:
        person["jobTitle"] = role.strip()
        org = {"@type": "Organization", "name": institution.strip()}
        if site.get("affiliation_url"):
            org["url"] = site["affiliation_url"]
        person["affiliation"] = org
    elif role:
        person["affiliation"] = role.strip()
    if site.get("portrait"):
        person["image"] = f"{BASE_URL}{site['portrait']}"

    website = {
        "@type": "WebSite",
        "@id": f"{BASE_URL}/#website",
        "url": f"{BASE_URL}/",
        "name": site["name"],
        "publisher": {"@id": f"{BASE_URL}/#person"},
    }

    payload = {"@context": "https://schema.org", "@graph": [person, website]}
    # Escape < so the JSON can never close the surrounding <script> element.
    return json.dumps(payload, indent=2).replace("<", "\\u003c")


def asset_version(name):
    """Short content hash of a file under static/, appended to its URL as
    ?v=... so a browser that cached the previous stylesheet fetches the new
    one on the first visit after a deploy. The file name itself never
    changes, so no other link needs updating."""
    path = STATIC / name
    if not path.exists():
        return ""
    return hashlib.sha1(path.read_bytes()).hexdigest()[:10]


def build_context():
    site = load_yaml("site.yml")
    research = load_yaml("research_threads.yml")
    # research_threads.yml is a mapping {threads, earlier}. A bare list -- the
    # original shape -- is still accepted as threads with no earlier work.
    if isinstance(research, dict):
        research_threads = research.get("threads") or []
        earlier_work = research.get("earlier") or []
    else:
        research_threads = research or []
        earlier_work = []
    publications = load_yaml("publications.yml")
    projects = load_yaml("projects.yml")
    education = load_yaml("education.yml")

    # Every timeline is sorted here, not in the YAML (CLAUDE.md § Information
    # architecture). The CV copy is hand-made, so this is the only sort.
    earlier_work = sort_by_start(earlier_work, "years")
    if isinstance(education, dict):
        education["teaching"] = sort_by_start(education.get("teaching"), "period")
        education["honors"] = sort_by_start(education.get("honors"), "years")
        education["service"] = sort_by_start(education.get("service"), "years")

    pub_views = [view_publication(p, site["name"]) for p in publications]
    views_by_id = {
        p["id"]: v for p, v in zip(publications, pub_views)
    }
    attach_related(research_threads, views_by_id)

    return {
        "site": site,
        "email_display": obfuscate(f"{site['email']['user']}@{site['email']['domain']}"),
        "email_href": obfuscate(f"{site['email']['user']}@{site['email']['domain']}"),
        "about_html": load_about_paragraphs(),
        "research_threads": research_threads,
        "earlier_work": earlier_work,
        "publications_by_year": group_by_year(pub_views),
        "projects": projects,
        "education": education,
        "base_url": BASE_URL,
        "css_version": asset_version("style.css"),
        "jsonld": build_jsonld(site),
        "sitemap_paths": SITEMAP_PATHS,
    }


def main():
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    try:
        context = build_context()
    except ContentError as e:
        print(f"build failed: {e}", file=sys.stderr)
        return 1

    for template_name, (output_name, path) in OUTPUT_PAGES.items():
        template = env.get_template(template_name)
        html = template.render(canonical_url=f"{BASE_URL}{path}", **context)
        (ROOT / output_name).write_text(html, encoding="utf-8")
        print(f"wrote {output_name}")

    sitemap = env.get_template("sitemap.xml").render(**context)
    (ROOT / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    print("wrote sitemap.xml")

    if not CV_SOURCE.exists():
        print(f"build failed: {CV_SOURCE.relative_to(ROOT)} is missing -- the public CV "
              "is maintained by hand there and copied to ./cv.pdf", file=sys.stderr)
        return 1
    shutil.copyfile(CV_SOURCE, CV_OUTPUT)
    print(f"copied _source/cv.pdf -> cv.pdf ({CV_OUTPUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
