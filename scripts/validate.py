#!/usr/bin/env python3
"""Validate content/ and the generated root HTML.

Non-zero exit on any failure. Run from anywhere: python scripts/validate.py

Checks (CLAUDE.md, Script contracts):
  - required fields present in every content file
  - no TODO / FIXME / Lorem in content or generated output
  - internal links in generated HTML resolve to a real file, and every
    #fragment link names an id that exists in its target page
  - every content YAML file parses
  - cv.pdf exists at repo root, is over 20 KB, yields extractable text, and
    that text carries no seu.edu.bd and no PLACEHOLDER (CLAUDE.md § Two CVs;
    the file is hand-made in _source/ and copied by build.py). The phone
    checks below do not apply to it: the CV carries the number by choice.
  - none of the planning and working files is tracked or staged in git:
    CLAUDE.md, DESIGN.md, DESIGN-v1.md, PAPERS.md, WEBSITE-PLAN.md,
    PROGRESS.md, MAINTENANCE.md, _source/, build/, cv-full.pdf, .claude/,
    *.docx, *_draft*, *private* (the .gitignore list, mirrored here)
  - no phone-number-shaped pattern, and no SEU address, in content or output
    (cv.pdf excepted from the phone checks only)
  - no page over the 150 KB budget
  - no publication still carrying the placeholder paper link
  - every code URL (publications links.code, projects repo, research
    threads and earlier-work code) is absolute https and, on github.com,
    answers 200 -- a repository renamed or made private fails the build

Checks (CLAUDE.md, Anonymity rules):
  - rule 3: an anonymized publication has no populated links
  - rule 2: a suppressed entry's title and link URLs are absent from every
    generated file, entities decoded (see note below)

Warnings (non-fatal):
  - rule 4: status: under-review with anonymized: false
  - rule 7: an anonymized entry's summary contains the first word of its title
  - rule 2: a suppressed entry's venue appears in output and is not
    attributable to a visible entry
  - rule 7, extended: an anonymized entry's title word appears in research
    thread text
  - a timeline entry (earlier work, teaching, honors, service) whose date
    cannot be parsed, so build.py cannot place it and appends it last

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
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import parse_span  # noqa: E402  -- the same parser build.py sorts with

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"

BANNED_WORDS = ["TODO", "FIXME", "Lorem"]

# Stand-in for a paper link that has not been supplied yet. The build must fail
# while any entry still carries it, so it can never reach a deploy.
PLACEHOLDER_PAPER_LINK = "https://drive.google.com/TODO"

# Phone detection. A digit run of 10-13 digits is phone-length; dots are not
# accepted as separators, which keeps DOIs and version strings out of it, and
# runs containing '--' or an en dash are numeric ranges (bibtex page ranges
# like 38999--39044), not numbers. Of what is left, a Bangladeshi prefix or
# phone-shaped grouping fails the build; anything else only warns, so a
# legitimate page range can never block a deploy.
# A run may only start at a digit whose predecessor is not a digit, '.' or
# '/'. Excluding digits stops the engine from restarting one character inside
# a DOI tail ("...2022.10054710" followed by a year) and reading that as a
# number.
DIGIT_RUN = re.compile(r"(?<![0-9./])[0-9][0-9\s\-()+]{7,}[0-9]")
RANGE_MARKERS = ("--", "–", "—")
SEU_PATTERN = re.compile(
    r"[\w.+-]+@seu\.edu\.bd|[\[(]at[\])]\s*seu\.edu\.bd", re.IGNORECASE
)
PAGE_BUDGET_BYTES = 150 * 1024
CV_MIN_BYTES = 20 * 1024          # CLAUDE.md § Two CVs; the Phase 1 stub was 536 bytes

CONTENT_FILES = [
    "site.yml",
    "research_threads.yml",
    "publications.yml",
    "projects.yml",
    "education.yml",
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
    require(site, ["name", "email"], "site.yml")
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


def split_research(data):
    """research_threads.yml is a mapping {threads, earlier}; a bare list (the
    original shape) is read as threads with no earlier work."""
    if data is None:
        return None, []
    if isinstance(data, dict):
        return data.get("threads") or [], data.get("earlier") or []
    return data, []


def check_research_threads(threads, earlier, project_ids):
    if threads is None:
        return
    for t in threads or []:
        require(
            t,
            ["id", "title", "description", "claim", "correction"],
            f"research_threads.yml[{t.get('id', '?')}]",
        )
    for e in earlier or []:
        label = f"research_threads.yml earlier[{e.get('title', '?')[:40]}]"
        require(e, ["years", "title", "line"], label)
        check_dated(e, "years", label)
        # `project` is an anchor into projects.html; a dangling one is a
        # broken link, so it must name an existing projects.yml id.
        if e.get("project") and e["project"] not in project_ids:
            fail(f"{label}: project '{e['project']}' matches no id in projects.yml")


PUBLICATION_REQUIRED = ["id", "title", "authors", "year", "type", "status", "summary"]
VALID_TYPES = {"conference", "journal", "workshop", "preprint", "thesis"}
VALID_STATUSES = {"published", "in-press", "under-review", "in-preparation", "preprint"}


def is_suppressed(pub):
    """Anonymity rule 1: only `anonymized: true` renders as status + year +
    summary. (Rule 6: in-preparation entries render like any other.)"""
    return bool(pub.get("anonymized"))


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


CODE_URL_TIMEOUT = 10


def collect_code_urls(pubs, projects, threads, earlier):
    """(label, url) for every place a Code pill can render."""
    found = []
    for p in pubs or []:
        url = (p.get("links") or {}).get("code")
        if url:
            found.append((f"publications.yml[{p.get('id', '?')}]: links.code", str(url)))
    for proj in projects or []:
        if proj.get("repo"):
            found.append((f"projects.yml[{proj.get('id') or proj.get('title', '?')}]: repo", str(proj["repo"])))
    for t in threads or []:
        if t.get("code"):
            found.append((f"research_threads.yml[{t.get('id', '?')}]: code", str(t["code"])))
    for e in earlier or []:
        if e.get("code"):
            found.append((f"research_threads.yml earlier[{str(e.get('title', '?'))[:40]}]: code", str(e["code"])))
    return found


def check_code_urls(pubs, projects, threads, earlier):
    """A code URL must be absolute https. On github.com it must also answer
    200, so a repository that was renamed, deleted or made private cannot
    ship as a dead Code button. A request that cannot be made at all (no
    network) only warns: it says nothing about the repository."""
    for label, url in collect_code_urls(pubs, projects, threads, earlier):
        if not re.fullmatch(r"https://[^\s/]+\.[^\s/]+(/\S*)?", url):
            fail(f"{label}: '{url}' is not an absolute https URL")
            continue
        host = url.split("/")[2].lower()
        if host not in ("github.com", "www.github.com"):
            continue
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "portfolio-validate/1.0"}
        )
        try:
            with urllib.request.urlopen(request, timeout=CODE_URL_TIMEOUT) as response:
                status = response.status
        except urllib.error.HTTPError as e:
            status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            warn(f"{label}: could not reach '{url}' ({e}); status not checked")
            continue
        if status != 200:
            fail(f"{label}: '{url}' returned HTTP {status}, expected 200")


def check_projects(projects):
    if projects is None:
        return
    for proj in projects or []:
        # description is optional: the CV gives one sentence per project, which
        # is the pitch; projects.html shows the description only when present.
        require(
            proj,
            ["title", "pitch"],
            f"projects.yml[{proj.get('title', '?')}]",
        )


def check_dated(entry, field, label):
    """build.py sorts timelines by parse_span(entry[field]); an entry it
    cannot parse is appended last in YAML order. Warn so the date gets fixed."""
    if entry.get(field) and parse_span(entry[field]) is None:
        warn(f"{label}: {field} '{entry[field]}' has no parseable date; it will "
             "sort last")


def check_education(edu):
    if edu is None:
        return
    for d in edu.get("degrees") or []:
        require(d, ["degree", "institution", "period"], "education.yml: degree")
    for t in edu.get("teaching") or []:
        label = f"education.yml: teaching[{str(t.get('institution', '?'))[:40]}]"
        require(t, ["role", "institution", "period"], label)
        check_dated(t, "period", label)
        if len(t.get("bullets") or []) > 3:
            fail(f"{label}: {len(t['bullets'])} bullets, the card form allows at most 3")
    # honors and service share one shape: title, organisation, years,
    # optional location, optional bullets (at most three)
    for key in ("honors", "service"):
        for e in edu.get(key) or []:
            label = f"education.yml: {key}[{str(e.get('title', '?'))[:40]}]"
            require(e, ["title", "organisation", "years"], label)
            check_dated(e, "years", label)
            bullets = e.get("bullets") or []
            if len(bullets) > 3:
                fail(f"{label}: {len(bullets)} bullets, the card form allows at most 3")


def check_title_words_in_threads(pubs, threads):
    """Rule 7, extended: an anonymized paper's title should not be echoed in
    research thread text either. Warning only -- a title's first word is often
    an ordinary English word ('Anchored', 'Low')."""
    for p in pubs or []:
        if not p.get("anonymized") or not p.get("title"):
            continue
        first = p["title"].split()[0].strip(",.:;")
        if not first:
            continue
        for t in threads or []:
            text = " ".join(
                str(t.get(k, "")) for k in ("title", "description", "claim", "correction")
            )
            if re.search(rf"\b{re.escape(first)}\b", text, re.IGNORECASE):
                warn(
                    f"research_threads.yml[{t.get('id', '?')}]: contains '{first}', "
                    f"the first word of anonymized entry '{p.get('id')}' (rule 7)"
                )


def check_banned_words(text, label):
    # The paper-link placeholder has its own check, which names the entry it
    # sits in; strip it here so it is not also reported as a generic 'TODO'.
    text = text.replace(PLACEHOLDER_PAPER_LINK, "")
    for word in BANNED_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", text):
            fail(f"{label}: contains banned placeholder word '{word}'")


def check_privacy_patterns(text, label):
    """Phone numbers and the SEU address, in content or in output. The
    hand-made cv.pdf carries a phone number by choice (CLAUDE.md § Two CVs),
    so it is checked for the SEU address only."""
    if label == "cv.pdf":
        if SEU_PATTERN.search(text):
            fail(f"{label}: contains an SEU institutional address")
        return

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
        fail("cv.pdf: missing at repo root (build.py copies it from _source/cv.pdf)")


def check_cv_text(outputs):
    """CLAUDE.md § Two CVs: the hand-made CV's text must be readable and must
    carry no seu.edu.bd and no PLACEHOLDER. The SEU email form is also caught
    by check_privacy_patterns; this adds the bare domain and the placeholder
    word for the CV, where the affiliation link that legitimately carries
    seu.edu.bd on the site never appears. No phone check: the CV carries the
    number by choice."""
    for path, text in outputs:
        if path.name != "cv.pdf":
            continue
        if not text.strip():
            fail("cv.pdf: no text could be extracted, so the privacy checks cannot run")
            return
        lowered = text.lower()
        if "seu.edu.bd" in lowered:
            fail("cv.pdf: contains 'seu.edu.bd'")
        if "placeholder" in lowered:
            fail("cv.pdf: contains 'PLACEHOLDER'")


# The .gitignore list, mirrored: planning and working files that must never
# be tracked or staged. Basenames, directory prefixes and name patterns.
FORBIDDEN_NAMES = {
    "CLAUDE.md", "DESIGN.md", "DESIGN-v1.md", "PAPERS.md", "WEBSITE-PLAN.md",
    "PROGRESS.md", "MAINTENANCE.md", "cv-full.pdf",
}
FORBIDDEN_DIRS = ("_source/", "build/", ".claude/")


def is_forbidden_path(path):
    name = Path(path).name
    lowered = path.lower()
    return (
        name in FORBIDDEN_NAMES
        or any(path.startswith(d) or f"/{d}" in path for d in FORBIDDEN_DIRS)
        or lowered.endswith(".docx")
        or "_draft" in lowered
        or "private" in lowered
    )


def git_lines(*args):
    """Output lines of a git command, or None when this is not a git repo."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.splitlines()


def check_nothing_forbidden_in_git():
    """Neither the index (tracked files) nor the staging area may hold a
    planning or working file. deploy.sh stops on this failure before anything
    is committed."""
    tracked = git_lines("ls-files")
    staged = git_lines("diff", "--cached", "--name-only")
    if tracked is None or staged is None:
        return  # not a git repo yet, or git unavailable -- nothing to check
    for path in sorted(set(tracked)):
        if is_forbidden_path(path):
            fail(f"git: '{path}' is tracked and must never be committed "
                 "(git rm --cached it; .gitignore lists it)")
    for path in sorted(set(staged) - set(tracked)):
        if is_forbidden_path(path):
            fail(f"git: '{path}' is staged and must never be committed")


INTERNAL_HREF = re.compile(r'href="(/[^"]*)"')
FRAGMENT_HREF = re.compile(r'href="((?:/[^"#]*)?#[^"]+)"')
ID_ATTR = re.compile(r'\bid="([^"]+)"')


def check_anchors(outputs):
    """Every #fragment link in generated HTML -- same-page ('#geotoken') or
    cross-page ('/projects.html#sql-injection') -- must name an id that exists
    in the target page. A research card's Project button or a thread's Paper
    button pointing at a deleted id is a broken link the file check above
    cannot see."""
    pages = {p.name: set(ID_ATTR.findall(t)) for p, t in outputs if p.suffix == ".html"}
    for path, text in outputs:
        if path.suffix != ".html":
            continue
        for href in FRAGMENT_HREF.findall(text):
            page, frag = href.split("#", 1)
            if page == "":
                target = path.name            # same-page link
            elif page == "/":
                target = "index.html"
            else:
                target = page.lstrip("/")
            if target not in pages:
                continue                  # reported by the internal-link check
            if frag not in pages[target]:
                fail(f"{path.name}: anchor '{href}' matches no id in {target}")


def _pdf_bytes(token):
    """Raw bytes of one PDF string: hex <...> or literal (...) with escapes."""
    if token.startswith(b"<"):
        return bytes.fromhex(re.sub(rb"\s+", b"", token[1:-1]).decode("ascii", "ignore"))
    body = token[1:-1]
    escapes = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
               b"(": b"(", b")": b")", b"\\": b"\\"}
    buf = bytearray()
    i = 0
    while i < len(body):
        c = body[i:i + 1]
        if c == b"\\" and i + 1 < len(body):
            nxt = body[i + 1:i + 2]
            if nxt in escapes:
                buf += escapes[nxt]
                i += 2
                continue
            octal = re.match(rb"[0-7]{1,3}", body[i + 1:i + 4])
            if octal:
                buf.append(int(octal.group(0), 8))
                i += 1 + len(octal.group(0))
                continue
            i += 1
            continue
        buf += c
        i += 1
    return bytes(buf)


def _decode(data, font):
    """Glyph codes -> text through the font's (code width, ToUnicode table).
    A font with no ToUnicode CMap is read as single-byte Latin-1, which is
    what the PDF standard fonts with WinAnsi encoding amount to for our
    purposes (digits and ASCII survive, which is all the scan needs)."""
    width, table = font
    if table is None:
        return data.decode("latin-1", "replace")
    codes = [int.from_bytes(data[j:j + width], "big") for j in range(0, len(data) - width + 1, width)]
    return "".join(table.get(c, "") for c in codes)


def pdf_text(path):
    """Text of a PDF with embedded fonts and ToUnicode CMaps, stdlib only.

    Handles both shapes we have met: fpdf2 (two-byte glyph codes, literal
    strings) and LibreOffice / Word exports (one-byte codes, hex strings).
    The code width comes from each CMap's codespacerange. Walks every font
    object's ToUnicode CMap, then decodes every Tj/TJ string in the content
    streams under the font last selected with Tf. Returns "" for a PDF it
    cannot read; the caller treats that as a failure, because a scan of
    nothing proves nothing.
    """
    raw = path.read_bytes()
    objs = {int(n): body for n, body in re.findall(rb"(?m)^(\d+) 0 obj(.*?)endobj", raw, re.S)}

    def stream(body):
        m = re.search(rb"stream\r?\n(.*?)\r?\nendstream", body, re.S)
        if not m:
            return b""
        data = m.group(1)
        if b"FlateDecode" in body:
            try:
                data = zlib.decompress(data)
            except zlib.error:
                return b""
        return data

    def font_of(font_num):
        m = re.search(rb"/ToUnicode\s+(\d+) 0 R", objs.get(font_num, b""))
        if not m:
            return (1, None)
        text = stream(objs.get(int(m.group(1)), b""))
        width = 2
        cs = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", text)
        if cs:
            width = max(1, len(cs.group(1)) // 2)
        table = {}
        for section in re.findall(rb"beginbfchar(.*?)endbfchar", text, re.S):
            for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", section):
                table[int(src, 16)] = bytes.fromhex(dst.decode()).decode("utf-16-be", "replace")
        for section in re.findall(rb"beginbfrange(.*?)endbfrange", text, re.S):
            for lo, hi, dst in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", section
            ):
                start = int(dst, 16)
                for offset, code in enumerate(range(int(lo, 16), int(hi, 16) + 1)):
                    table[code] = chr(start + offset)
        return (width, table)

    # Resource name -> font, for every name that points at a /Type /Font object.
    fonts = {}
    for name, num in re.findall(rb"/([A-Za-z0-9_.+-]+)\s+(\d+) 0 R", raw):
        num = int(num)
        if re.search(rb"/Type\s*/Font\b", objs.get(num, b"")):
            fonts.setdefault(name.decode(), font_of(num))

    string = rb"\((?:\\.|[^\\)])*\)|<[0-9A-Fa-f\s]*>"
    ops = re.compile(
        rb"/([A-Za-z0-9_.+-]+)\s+[\d.]+\s+Tf|(" + string + rb")\s*Tj|\[((?:" + string + rb"|[^\]])*)\]\s*TJ",
        re.S,
    )
    out = []
    for body in objs.values():
        data = stream(body)
        if b"BT" not in data:
            continue
        current = (1, None)
        for tok in ops.finditer(data):
            if tok.group(1):
                current = fonts.get(tok.group(1).decode(), (1, None))
            elif tok.group(2):
                out.append(_decode(_pdf_bytes(tok.group(2)), current) + "\n")
            elif tok.group(3) is not None:
                out.append("".join(_decode(_pdf_bytes(t), current)
                                   for t in re.findall(string, tok.group(3), re.S)) + "\n")
        out.append("\n")
    return "".join(out).strip()


def output_files():
    """Every generated file a crawler could read, with entities decoded.

    cv.pdf contributes its extracted text (see pdf_text). If extraction
    yields nothing the entry is empty and check_cv_text fails the build.
    """
    files = []
    for pattern in ("*.html", "*.xml", "*.json", "*.txt"):
        for path in sorted(ROOT.glob(pattern)):
            raw = path.read_text(encoding="utf-8")
            files.append((path, raw + "\n" + html.unescape(raw)))
    pdf = ROOT / "cv.pdf"
    if pdf.exists():
        files.append((pdf, pdf_text(pdf)))
    return files


def check_output_files(outputs):
    html_files = [p for p, _ in outputs if p.suffix == ".html"]
    if not html_files:
        fail("no generated HTML files found at repo root -- run scripts/build.py first")
        return
    for path, text in outputs:
        check_privacy_patterns(text, path.name)
        check_banned_words(text, path.name)
        size = path.stat().st_size
        if path.suffix == ".html" and size > PAGE_BUDGET_BYTES:
            fail(f"{path.name}: {size // 1024} KB exceeds the 150 KB page budget")
        if path.name == "cv.pdf" and size < CV_MIN_BYTES:
            fail(f"cv.pdf: {size} bytes -- under 20 KB, so this is not a real CV "
                 "(check _source/cv.pdf, then run scripts/build.py)")
        for href in INTERNAL_HREF.findall(text):
            target = href.split("#")[0].split("?")[0]
            if target in ("", "/"):
                continue
            if not (ROOT / target.lstrip("/")).exists():
                fail(f"{path.name}: internal link '{href}' does not resolve")


def check_placeholder_links(pubs, outputs):
    """Every publication now carries a public paper link (CLAUDE.md
    § Information architecture). Fail while any of them is still the
    stand-in, so a deploy cannot ship a dead Paper button."""
    for pub in pubs or []:
        for key, url in (pub.get("links") or {}).items():
            if url and PLACEHOLDER_PAPER_LINK in str(url):
                fail(
                    f"publications.yml[{pub.get('id', '?')}]: links.{key} is still "
                    f"the placeholder {PLACEHOLDER_PAPER_LINK} -- replace it with "
                    "the real Drive URL"
                )
    for path, text in outputs:
        if PLACEHOLDER_PAPER_LINK in text:
            fail(f"{path.name}: contains the placeholder paper link "
                 f"{PLACEHOLDER_PAPER_LINK}")


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
    visible_urls = [
        normalize(url)
        for p in visible
        for url in (p.get("links") or {}).values()
        if url
    ]

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
                # As with titles and venues: only a leak if no visible entry
                # accounts for the string. Two entries can share a URL -- most
                # obviously while they all still hold the same placeholder.
                if normalize(url) in haystack:
                    if not any(normalize(url) in vu for vu in visible_urls):
                        fail(
                            f"{label}: suppressed link '{url}' appears in "
                            f"{path.name} (anonymity rule 2)"
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
    threads, earlier = split_research(load_yaml("research_threads.yml"))
    projects = load_yaml("projects.yml")
    check_research_threads(threads, earlier, {p.get("id") for p in projects or []})
    publications = load_yaml("publications.yml")
    check_publications(publications)
    check_title_words_in_threads(publications, threads)
    check_projects(projects)
    check_code_urls(publications, projects, threads, earlier)
    check_education(load_yaml("education.yml"))

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
    check_nothing_forbidden_in_git()

    outputs = output_files()
    check_output_files(outputs)
    check_cv_text(outputs)
    check_anchors(outputs)
    check_placeholder_links(publications, outputs)
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
