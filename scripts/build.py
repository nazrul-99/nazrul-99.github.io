#!/usr/bin/env python3
"""Clean rebuild: content/ + templates/ -> root HTML.

Run from anywhere: python scripts/build.py
"""
import json
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
TEMPLATES = ROOT / "templates"

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


def load_yaml(name):
    path = CONTENT / name
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
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
    "arxiv": "arXiv",
    "code": "Code",
    "project": "Project",
    "slides": "Slides",
    "poster": "Poster",
}


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


def view_publication(pub, site_name):
    """Return the dict a template is allowed to see for one publication.

    Anonymized and in-preparation entries get ONLY status/year/summary --
    title, authors, venue and links are never assembled into their view, so
    they cannot leak into generated HTML by accident (CLAUDE.md anonymity
    rule 2)."""
    suppressed = pub.get("anonymized", False) or pub.get("status") == "in-preparation"
    if suppressed:
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
        "title": pub.get("title", ""),
        "authors_html": build_authors_html(
            pub.get("authors", []), pub.get("equal_contribution", []), site_name
        ),
        "venue": pub.get("venue", ""),
        "year": pub.get("year"),
        "links": links,
        "bibtex": pub.get("bibtex", ""),
        "note": pub.get("note", ""),
        "summary": pub.get("summary", ""),
    }


def group_by_year(pub_views):
    years = {}
    for view in pub_views:
        years.setdefault(view["year"], []).append(view)
    return sorted(years.items(), key=lambda item: item[0], reverse=True)


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
    if site.get("affiliation"):
        person["affiliation"] = site["affiliation"]

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


def build_context():
    site = load_yaml("site.yml")
    research_threads = load_yaml("research_threads.yml")
    publications = load_yaml("publications.yml")
    projects = load_yaml("projects.yml")
    education = load_yaml("education.yml")

    pub_views = [view_publication(p, site["name"]) for p in publications]

    return {
        "site": site,
        "email_display": obfuscate(f"{site['email']['user']}@{site['email']['domain']}"),
        "email_href": obfuscate(f"{site['email']['user']}@{site['email']['domain']}"),
        "about_html": load_about_paragraphs(),
        "research_threads": research_threads,
        "publications_by_year": group_by_year(pub_views),
        "projects": projects,
        "education": education,
        "base_url": BASE_URL,
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
    context = build_context()

    for template_name, (output_name, path) in OUTPUT_PAGES.items():
        template = env.get_template(template_name)
        html = template.render(canonical_url=f"{BASE_URL}{path}", **context)
        (ROOT / output_name).write_text(html, encoding="utf-8")
        print(f"wrote {output_name}")

    sitemap = env.get_template("sitemap.xml").render(**context)
    (ROOT / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    print("wrote sitemap.xml")


if __name__ == "__main__":
    sys.exit(main())
