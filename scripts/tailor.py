#!/usr/bin/env python3
"""Tailor a one-page, ATS-friendly LaTeX resume to a job description.

Sources
-------
data/profile.json           every fact: name, contact, experience, education,
                            projects, skills, certifications, awards.
templates/base_resume.tex   the approved layout. Everything before
                            \\begin{document} (packages, colours, section and
                            list styles) is copied from it unchanged.

The LLM only writes wording: experience bullets, skill items,
project bullets, gaps, keywords and notes. Its reply is checked before use:
  * JSON that does not parse is retried with the error message
  * unknown companies, titles, projects or skill rows are ignored
  * skills that are not in PROFILE are dropped
  * bullets or summaries with numbers that are not in PROFILE are dropped
  * LaTeX and markdown markers are removed
  * a missing or unusable entry falls back to PROFILE text
Problems handled are printed in the terminal. Only output/<slug>.tex is written to the repo;
the raw model reply goes to the system temp folder for debugging.

Call flow
---------
main()
 ├─ load_dotenv(), read_jd(), load_profile(), load_preamble()
 ├─ ReplySource                 Ollama/OpenAI-compatible server, or LLM_REPLY_FILE
 ├─ request_wording()           reply -> JSON, retried on bad JSON
 │   └─ extract_json()
 ├─ clean_llm()                 checks the reply against PROFILE, returns issues
 ├─ build_resume()              PROFILE facts + checked wording -> resume dict
 ├─ render_tex()                preamble from the template + body
 ├─ compile_check()             pdflatex: does it compile, and is it one page?
 ├─ retry with the LLM, then trim_once() in code until it fits one page
 └─ write output/<slug>.tex and print the problems handled

Reads:   data/profile.json, templates/base_resume.tex, prompts/system.md
Env:     JD_TEXT or JD_FILE, EXTRA_INSTRUCTIONS, JOB_SLUG,
         LLM_MODEL, LLM_BASE_URL, LLM_API_KEY   (a local .env file is also read)
         LLM_REPLY_FILE  optional: use a saved reply instead of calling the model
Writes:  output/<slug>.tex (only file written to the repo)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_FILE = ROOT / "data" / "profile.json"
TEMPLATE_FILE = ROOT / "templates" / "base_resume.tex"
SYSTEM_FILE = ROOT / "prompts" / "system.md"

MAX_RETRIES = 2                 # extra LLM calls after a bad reply or a page/compile problem
CONNECT_TRIES = 3               # attempts when the server is unreachable
MAX_ROLE_BULLETS = 3
MAX_PROJECT_BULLETS = 2
MAX_PROJECTS = 4
MAX_SKILLS_PER_ROW = 10
MAX_SUMMARY_WORDS = 50
MAX_BULLET_WORDS = 35           # longer bullets are kept but reported
MIN_BULLET_WORDS = 15           # shorter bullets are too thin; PROFILE highlights are used instead
MIN_SKILLS_PER_ROW = 4
SEP = " $\\cdot$ "              # separator used in the header and awards, as in the template

# Fixed skill rows, in template order. The model fills items under these names only.
SKILL_CATEGORIES = [
    "AI & Machine Learning",
    "Languages & Cloud Infrastructure",
    "DevOps, GitOps & Observability",
    "Frameworks & Integration",
]
# Used only to place a profile skill in a row when the model did not pick it.
CATEGORY_HINTS = {
    "AI & Machine Learning": ["machine learning", "neural", "prompt", "computer vision", "opencv",
                              "llm", "faiss", "mcp", "sentiment", "generative", "vector", "artificial intelligence"],
    "Languages & Cloud Infrastructure": ["python", "java", "sql", "node", "javascript", "bash", "aws", "eks",
                                         "s3", "rds", "terraform", "docker", "kubernetes", "cloud"],
    "DevOps, GitOps & Observability": ["helm", "argocd", "jenkins", "gitops", "github actions", "ci/cd",
                                       "elk", "kibana", "monitor", "telemetry", "logging", "incident"],
}
DEFAULT_CATEGORY = "Frameworks & Integration"

# ---------- escaping ----------
UNICODE_MAP = {                     # Unicode -> LaTeX. Values are already LaTeX.
    "\u2014": "-", "\u2013": "-",     # em and en dashes become plain hyphens (ATS-safe)
    "\u00b7": r"$\cdot$",             # middle dot
    "\u2022": r"$\bullet$",           # bullet
    "\u2192": r"$\rightarrow$",       # arrow
    "\u2026": r"\ldots{}",            # ellipsis
    "\u2019": "'", "\u2018": "'",
    "\u201c": "``", "\u201d": "''",
    "\u00a0": " ",
    "\u2229": r"$\cap$",              # intersection symbol
}
SPECIAL_MAP = {                     # ASCII characters that LaTeX treats specially
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$",
    "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "<": r"$<$", ">": r"$>$", "|": r"$|$",
}
EMPHASIS_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\+|\d[\d,]*(?:\.\d+)?%|\d{1,3}(?:,\d{3})+")
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def esc(value) -> str:
    """Convert plain text to LaTeX-safe text. Input is plain text, output is LaTeX.
    Leading and trailing spaces are kept, so pieces can be joined with rich()."""
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text)
    out = []
    for ch in text:
        if ch in UNICODE_MAP:
            out.append(UNICODE_MAP[ch])
        elif ch in SPECIAL_MAP:
            out.append(SPECIAL_MAP[ch])
        elif ord(ch) <= 0x024F:        # Latin letters, including accents
            out.append(ch)
        # other characters (emoji, most symbols) are dropped, since pdfLaTeX cannot typeset them
    return "".join(out)


def dash(text) -> str:
    """Date ranges use a plain hyphen with spaces: '2025 - 2026'. ATS parsers read this reliably."""
    return re.sub(r"\s*[-\u2013\u2014]+\s*", " - ", str(text or "")).strip()


def rich(text) -> str:
    """Format one bullet: bold the leading verb and bold numeric metrics, as in the template.
    Returns LaTeX."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return ""
    lead, rest = re.match(r"(\S+)(.*)", text).groups()
    rest = rest.lstrip()
    pieces, last = [], 0
    for m in EMPHASIS_RE.finditer(rest):          # metrics such as 30%, 20+, 10,000
        pieces.append(esc(rest[last:m.start()]))
        pieces.append(f"\\textbf{{{esc(m.group(0))}}}")
        last = m.end()
    pieces.append(esc(rest[last:]))
    return f"\\textbf{{{esc(lead)}}} " + "".join(pieces)


def href(url, label, underline: bool = True) -> str:
    """Build \\href{url}{label}. Returns LaTeX. The URL is cleaned of characters that break \\href."""
    clean = re.sub(r"[\s%#{}\\]", "", str(url))
    text = esc(label).strip()
    if underline:
        text = f"\\underline{{{text}}}"
    return f"\\href{{{clean}}}{{{text}}}"


# ---------- helpers ----------
def dicts(items) -> list:
    """Keep only dict entries. Returns [] for anything that is not a list."""
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def items_list(value) -> list:
    """Normalise a value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def clean_text(value) -> str:
    """Remove markdown and LaTeX markers the model sometimes adds, and collapse spaces."""
    text = str(value or "")
    text = text.replace("**", "").replace("__", "").replace("\\", "")
    text = re.sub(r"^\s*[-*\u2022]\s+", "", text)     # leading bullet characters
    return re.sub(r"\s+", " ", text).strip()


EXPOSURE_ALIASES = {"ai", "ml"}   # short forms of PROFILE exposure topics


def exposure_mentions(text: str, profile: dict) -> list:
    """Exposure-only skills (for example AI or machine learning) found in a piece of text.
    Such skills may not be claimed as expertise in summaries or bullets."""
    terms = {x.lower() for x in items_list(profile.get("skills_exposure"))} | EXPOSURE_ALIASES
    low = text.lower()
    return sorted(t for t in terms
                  if re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", low))


def numbers_in(text: str) -> set:
    """Every number in a text, with thousands separators removed: '10,000' -> '10000'."""
    return {m.group(0).replace(",", "") for m in NUMBER_RE.finditer(text)}


def as_url(value) -> str:
    """Add https:// to a bare domain such as 'arshdeepdubey.github.io/Portfolio/'."""
    value = str(value or "").strip()
    if not value or re.match(r"^(https?://|mailto:)", value):
        return value
    return "https://" + value


def category_for(item: str) -> str:
    """Pick the skill row for a profile skill the model did not place."""
    low = item.lower()
    for cat, hints in CATEGORY_HINTS.items():
        if any(h in low for h in hints):
            return cat
    return DEFAULT_CATEGORY


def skill_filter(profile: dict):
    """Return a function that says whether a skill may appear: it must be in PROFILE and not
    skill rows may list exposure skills, but bullets never claim them."""
    corpus = json.dumps(profile, ensure_ascii=False).lower()

    def allowed(item: str) -> bool:
        low = item.lower().strip()
        whole_word = re.compile(r"(?<![a-z0-9])" + re.escape(low) + r"(?![a-z0-9])")
        return bool(whole_word.search(corpus))
    return allowed


# ---------- LaTeX building blocks ----------
def section(title: str, body: str) -> str:
    """Wrap a body in a titled section. title must already be LaTeX."""
    return f"\\section*{{{title}}}\n{body}\n"


def itemize(items, options: str = "") -> str:
    """Bulleted list. Items must already be LaTeX."""
    opt = f"[{options}]" if options else ""
    return f"\\begin{{itemize}}{opt}\n" + "\n".join(f"\\item {i}" for i in items) + "\n\\end{itemize}"


COMMON_CAPITALIZED = {"the", "and", "with", "for", "across", "using", "while", "from", "to", "of", "in",
                      "on", "by", "at", "a", "an", "as", "into", "through", "that", "which", "its", "their",
                      "our", "all", "via", "per", "i"}


def unsupported_terms(text: str, profile: dict) -> list:
    """Capitalised words (tools, products, programmes) in a piece of text that do not appear in
    PROFILE. A bullet may not name a technology the candidate has not listed, even if the JD does."""
    corpus = json.dumps(profile, ensure_ascii=False).lower()
    bad = []
    for idx, word in enumerate(re.findall(r"[A-Za-z][A-Za-z0-9+#]*", text)):
        if idx == 0 or len(word) < 2 or not word[0].isupper() or word.lower() in COMMON_CAPITALIZED:
            continue   # the first word is the action verb, and common words are not terms
        if not re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", corpus):
            bad.append(word)
    return sorted(set(bad))


def mentioned(text: str, term: str) -> bool:
    """Whole-word, case-insensitive check that a term appears in a text."""
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text.lower()))


# ---------- check the model's reply against PROFILE ----------
def clean_llm(raw: dict, profile: dict):
    """Check the model's reply and return (wording, issues). Nothing invented is kept:
    numbers not in PROFILE, unknown names and non-PROFILE skills are removed and reported."""
    issues = []
    known = numbers_in(json.dumps(profile, ensure_ascii=False))
    allowed = skill_filter(profile)

    def ungrounded(text):
        return sorted(numbers_in(text) - known)

    # Summary: dropped. The approved sample has no SUMMARY section.
    summary = ""

    # Experience: match each entry to a PROFILE role and keep only grounded bullets.
    roles = [(str(j.get("company", "")).strip(), str(j.get("title", "")).strip())
             for j in dicts(profile.get("experience"))]
    roles = [r for r in roles if r[0] and r[1]]
    highlights_by_role = {(str(j.get("company", "")).strip(), str(j.get("title", "")).strip()):
                          items_list(j.get("highlights")) for j in dicts(profile.get("experience"))}
    attempted, wanted = set(), {}
    for item in dicts(raw.get("experience")):
        company = clean_text(item.get("company")).lower()
        title = clean_text(item.get("title") or item.get("role")).lower()
        match = [r for r in roles if r[1].lower() == title and (not company or r[0].lower() == company)]
        if not match:
            issues.append(f"experience entry not in PROFILE ignored: {company or '?'} / {title or '?'}")
            continue
        key = match[0]
        if key in attempted:
            continue
        attempted.add(key)
        bullets = []
        for b in items_list(item.get("bullets")):
            text = clean_text(b)
            if not text:
                continue
            if ungrounded(text):
                issues.append(f"bullet dropped for {key[1]}: numbers not in PROFILE {ungrounded(text)}")
                continue
            if exposure_mentions(text, profile):
                issues.append(f"bullet dropped for {key[1]}: claims exposure-only skill(s) "
                              f"{exposure_mentions(text, profile)}")
                continue
            if unsupported_terms(text, profile):
                issues.append(f"bullet dropped for {key[1]}: terms not in PROFILE "
                              f"{unsupported_terms(text, profile)}")
                continue
            if len(text.split()) < MIN_BULLET_WORDS:
                issues.append(f"bullet too thin for {key[1]} ({len(text.split())} words), dropped")
                continue
            if len(text.split()) > MAX_BULLET_WORDS:
                issues.append(f"long bullet for {key[1]} ({len(text.split())} words), kept")
            bullets.append(text)
        role_highlights = highlights_by_role.get(key, [])
        role_metrics = set().union(*(numbers_in(h) for h in role_highlights)) if role_highlights else set()
        if bullets and role_metrics and not (set().union(*(numbers_in(b) for b in bullets)) & role_metrics):
            issues.append(f"model bullets for {key[1]} lost every metric in PROFILE, so PROFILE highlights are used")
            bullets = []
        if bullets:
            wanted[key] = bullets[:MAX_ROLE_BULLETS]
        else:
            issues.append(f"no usable bullets for {key[1]}, so PROFILE highlights are used")
    for key in roles:
        if key not in attempted:
            issues.append(f"no entry returned for {key[1]} at {key[0]}, so PROFILE highlights are used")
    experience = [{"company": c, "title": t, "bullets": b} for (c, t), b in wanted.items()]

    # Skills: only known rows and items that are in PROFILE.
    skills = {}
    raw_skills = raw.get("skills")
    if not isinstance(raw_skills, dict):
        if raw_skills is not None:
            issues.append("skills was not an object, so PROFILE skills are used")
        raw_skills = {}
    for name, value in raw_skills.items():
        row = [c for c in SKILL_CATEGORIES if c.lower() == str(name).strip().lower()]
        if not row:
            issues.append(f"unknown skill row ignored: {name}")
            continue
        values = value if isinstance(value, list) else str(value or "").split(",")
        kept = []
        for v in values:
            item = clean_text(v)
            if not item:
                continue
            if allowed(item):
                kept.append(item)
            else:
                issues.append(f"skill not in PROFILE dropped: {item}")
        skills[row[0]] = kept

    # Projects: only names that exist in PROFILE, with grounded bullets.
    profile_projects = {str(p.get("name", "")).strip().lower(): str(p.get("name", "")).strip()
                        for p in dicts(profile.get("projects")) if p.get("name")}
    projects, seen = [], set()
    for item in dicts(raw.get("projects")):
        key = clean_text(item.get("name")).lower()
        if key not in profile_projects or key in seen:
            if key not in profile_projects:
                issues.append(f"project not in PROFILE ignored: {key or '?'}")
            continue
        seen.add(key)
        bullets = []
        for b in items_list(item.get("bullets")):
            text = clean_text(b)
            if not text:
                continue
            if ungrounded(text):
                issues.append(f"project bullet dropped for {profile_projects[key]}: "
                              f"numbers not in PROFILE {ungrounded(text)}")
                continue
            if exposure_mentions(text, profile):
                issues.append(f"project bullet dropped for {profile_projects[key]}: claims exposure-only "
                              f"skill(s) {exposure_mentions(text, profile)}")
                continue
            if unsupported_terms(text, profile):
                issues.append(f"project bullet dropped for {profile_projects[key]}: terms not in PROFILE "
                              f"{unsupported_terms(text, profile)}")
                continue
            bullets.append(text)
        projects.append({"name": profile_projects[key], "bullets": bullets[:MAX_PROJECT_BULLETS]})

    corpus = json.dumps(profile, ensure_ascii=False).lower()
    keywords = [clean_text(x) for x in items_list(raw.get("keywords_used"))]
    for kw in keywords:
        if kw.lower() not in corpus:
            issues.append(f"keyword not found in PROFILE, check before using it: {kw}")

    wording = {
        "summary": summary,
        "experience": experience,
        "skills": skills,
        "projects": projects,
        "gaps": [clean_text(x) for x in items_list(raw.get("gaps"))],
        "keywords_used": keywords,
        "notes": [clean_text(x) for x in items_list(raw.get("notes"))],
    }
    return wording, issues


# ---------- build the resume from PROFILE facts and checked wording ----------
def build_header(profile: dict) -> dict:
    """Name, phone, location, email and links. All copied from PROFILE."""
    c = profile.get("contact") if isinstance(profile.get("contact"), dict) else {}
    links = []
    for label, key in (("GitHub", "github"), ("LinkedIn", "linkedin"),
                       ("LeetCode", "leetcode"), ("Portfolio", "portfolio")):
        if c.get(key):
            links.append({"label": label, "url": as_url(c[key])})
    return {
        "name": str(profile.get("name", "")).strip(),
        "phone": str(c.get("phone", "")).strip(),
        "location": str(c.get("location", "")).strip(),
        "email": str(c.get("email", "")).strip(),
        "links": links,
    }


def build_experience(profile: dict, wording_items) -> list:
    """Companies with their roles, in PROFILE order. Wording comes from the checked model output
    when it supplied bullets for that role, otherwise from the PROFILE highlights."""
    wanted = {}
    for item in dicts(wording_items):
        title = str(item.get("title") or "").strip().lower()
        company = str(item.get("company") or "").strip().lower()
        bullets = items_list(item.get("bullets"))
        if title and bullets:
            wanted[(company, title)] = bullets
    groups = {}
    for job in dicts(profile.get("experience")):
        company = str(job.get("company", "")).strip()
        title = str(job.get("title", "")).strip()
        if not company or not title:
            continue
        bullets = wanted.get((company.lower(), title.lower())) or items_list(job.get("highlights"))
        group = groups.setdefault(company, {
            "company": company,
            "location": str(job.get("location", "")).strip(),
            "roles": [],
        })
        group["roles"].append({
            "title": title,
            "dates": f"{job.get('start', '')} - {job.get('end', '')}".strip(" -"),
            "bullets": bullets[:MAX_ROLE_BULLETS],
        })
    return list(groups.values())


def build_education(profile: dict) -> list:
    """Schools and degrees, copied from PROFILE. Entries with "resume": false are left out."""
    rows = []
    for e in dicts(profile.get("education")):
        if e.get("resume") is False:
            continue
        rows.append({
            "school": str(e.get("school", "")).strip(),
            "location": str(e.get("location", "")).strip(),
            "degree": str(e.get("degree", "")).strip(),
            "dates": str(e.get("years", "")).strip(),
        })
    return rows


def build_projects(profile: dict, wording_items) -> list:
    """Up to MAX_PROJECTS projects. Model's choice and order first, then PROFILE order."""
    projects = {}
    for p in dicts(profile.get("projects")):
        key = str(p.get("name", "")).strip().lower()
        if key:
            projects[key] = p
    order, wording = [], {}
    for item in dicts(wording_items):
        key = str(item.get("name") or "").strip().lower()
        if key in projects and key not in wording:
            order.append(key)
            wording[key] = items_list(item.get("bullets"))
    for key in projects:
        if key not in order:
            order.append(key)
    result = []
    for key in order[:MAX_PROJECTS]:
        p = projects[key]
        bullets = wording.get(key)
        if not bullets:
            # PROFILE text is the fallback, so it must pass the same exposure rule as model text
            bullets = [b for b in items_list(p.get("highlights")) if not exposure_mentions(b, profile)]
            if not bullets and items_list(p.get("highlights")):
                continue   # every fallback bullet claims an exposure-only skill: leave the project out
        result.append({
            "name": str(p.get("name", "")).strip(),
            "tech": str(p.get("tech", "")).strip(),
            "url": as_url(p.get("url", "")),
            "bullets": bullets[:MAX_PROJECT_BULLETS],
        })
    return result


def build_skills(profile: dict, wording_skills) -> list:
    """Skill rows in template order. Items the model chose come first; any row left empty is
    filled from skills_strong, so no row disappears."""
    allowed = skill_filter(profile)
    wording = wording_skills if isinstance(wording_skills, dict) else {}
    chosen = {cat: [] for cat in SKILL_CATEGORIES}
    used = set()
    for cat in SKILL_CATEGORIES:
        for item in items_list(wording.get(cat)):
            if allowed(item) and item.lower() not in used:
                chosen[cat].append(item)
                used.add(item.lower())
    spare = {cat: [] for cat in SKILL_CATEGORIES}
    for item in items_list(profile.get("skills_strong")):
        if allowed(item) and item.lower() not in used:
            spare[category_for(item)].append(item)
            used.add(item.lower())
    rows = []
    for cat in SKILL_CATEGORIES:
        items = chosen[cat][:MAX_SKILLS_PER_ROW]
        if len(items) < MIN_SKILLS_PER_ROW:
            items = (items + spare[cat])[:max(MIN_SKILLS_PER_ROW, len(items))]
        if items:
            rows.append((cat, items))
    return rows


def build_awards(profile: dict) -> list:
    """Awards and certifications as labelled rows, copied from PROFILE."""
    rows = []
    awards = items_list(profile.get("honors_awards"))
    certs = items_list(profile.get("certifications"))
    if awards:
        rows.append(("Awards", awards))
    if certs:
        rows.append(("Certifications", certs))
    return rows


def build_resume(profile: dict, wording: dict) -> dict:
    """Combine PROFILE facts with the checked wording. Called by main() on every attempt."""
    return {
        "header": build_header(profile),
        "summary": wording.get("summary", ""),
        "skills": build_skills(profile, wording.get("skills")),
        "experience": build_experience(profile, wording.get("experience")),
        "education": build_education(profile),
        "projects": build_projects(profile, wording.get("projects")),
        "awards": build_awards(profile),
    }


# ---------- section renderers ----------
def render_header(h: dict) -> str:
    """Name, contact line, and link line, as in the template."""
    name = esc(h["name"].upper())
    line2 = [esc(h[k]) for k in ("phone", "location") if h.get(k)]
    line3 = []
    if h.get("email"):
        line3.append(href("mailto:" + h["email"], h["email"], underline=False))
    for link in h["links"]:
        line3.append(href(link["url"], link["label"], underline=False))
    return (
        "\\begin{center}\n"
        f"    {{\\LARGE \\bfseries \\color{{primary}} {name}}} \\\\[4pt]\n"
        "    \\small\n"
        f"    {SEP.join(line2)} \\\\[3pt]\n"
        f"    {SEP.join(line3)}\n"
        "\\end{center}\n"
        "\\vspace{-4pt}\n"
    )


def render_summary(text: str) -> str:
    """Short role-focused summary. Left out when the model gave none."""
    return section("SUMMARY", esc(text)) if text else ""


def render_skills(rows: list) -> str:
    """One bold-labelled line per skill row, in template order."""
    if not rows:
        return ""
    lines = [f"\\textbf{{{esc(cat)}:}} " + ", ".join(esc(v) for v in items) for cat, items in rows]
    return section("SKILLS", "\\small\n\n" + itemize(lines))


def render_experience(groups: list) -> str:
    """Companies with roles. The company line appears only above the first role, as in the template."""
    parts = []
    for g in groups:
        for i, role in enumerate(g["roles"]):
            if parts:
                parts.append("\\vspace{2pt}" if i == 0 else "\\vspace{1pt}")
            if i == 0:
                parts.append(f"\\noindent \\textbf{{{esc(g['company'])}}} \\hfill "
                             f"\\textbf{{{esc(g['location'])}}} \\\\")
            parts.append(f"\\textit{{{esc(role['title'])}}} \\hfill "
                         f"\\textit{{{esc(dash(role['dates']))}}}")
            bullets = [rich(b) for b in role["bullets"]]
            if bullets:
                parts.append(itemize(bullets))
    return section("WORK EXPERIENCE", "\n".join(parts)) if parts else ""


def render_education(items: list) -> str:
    """School and degree entries, as in the template."""
    parts = []
    for e in items:
        if parts:
            parts.append("\\par\\vspace{1pt}")
        parts.append(f"\\noindent \\textbf{{{esc(e['school'])}}} \\hfill "
                     f"\\textbf{{{esc(e['location'])}}} \\\\")
        parts.append(f"\\textit{{\\parbox[t]{{0.74\\linewidth}}{{{esc(e['degree'])}}}}} \\hfill "
                     f"\\textit{{{esc(dash(e['dates']))}}}")
    return section("EDUCATION", "\n".join(parts)) if parts else ""


def render_projects(items: list) -> str:
    """Project title, tech stack, and link, as in the template."""
    parts = []
    for p in items:
        if parts:
            parts.append("\\vspace{2pt}")
        link = ""
        if p.get("url"):
            label = "GitHub Link" if "github.com" in p["url"] else "Link"
            link = f" \\hfill {href(p['url'], label)}"
        parts.append(f"\\noindent \\textbf{{{esc(p['name'])}}} \\ $|$ \\ "
                     f"\\textit{{{esc(p['tech'])}}}{link}")
        bullets = [rich(b) for b in p["bullets"]]
        if bullets:
            parts.append(itemize(bullets))
    return section("PROJECT WORK", "\n".join(parts)) if parts else ""


def render_awards(rows: list) -> str:
    """Awards and certifications, one bold-labelled line each."""
    lines = [f"\\textbf{{{esc(label)}:}} " + SEP.join(esc(v) for v in items)
             for label, items in rows]
    if not lines:
        return ""
    return section("AWARDS \\& CERTIFICATIONS",
                   itemize(lines, options="leftmargin=0pt, label={}, itemsep=2pt, parsep=0pt"))


def render_tex(preamble: str, resume: dict) -> str:
    """Preamble from the template, then the body in template order, then the end of document."""
    body = [
        render_header(resume["header"]),
        render_summary(resume["summary"]),
        render_skills(resume["skills"]),
        render_experience(resume["experience"]),
        render_education(resume["education"]),
        render_projects(resume["projects"]),
        render_awards(resume["awards"]),
    ]
    return preamble + "\n".join(b for b in body if b) + "\n\\end{document}\n"


# ---------- files ----------
def load_profile() -> dict:
    """Read data/profile.json. The file is the only source of facts."""
    try:
        return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"Cannot read {PROFILE_FILE}: {exc}")


def load_preamble() -> str:
    """Everything in templates/base_resume.tex before \\begin{document}, copied unchanged,
    plus the opening lines of the body."""
    text = TEMPLATE_FILE.read_text(encoding="utf-8")
    match = re.search(r"\\begin\{document\}", text)
    if not match:
        sys.exit(f"{TEMPLATE_FILE} has no \\begin{{document}}")
    return text[:match.start()].rstrip() + "\n\n\\begin{document}\n\\pagestyle{empty}\n\n"


def load_dotenv() -> None:
    """Read key=value pairs from .env into os.environ. Called by main() first."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_jd() -> str:
    """Return the job description from JD_TEXT, or from the file named in JD_FILE."""
    text = os.environ.get("JD_TEXT", "").strip()
    if text:
        return text
    jd_file = os.environ.get("JD_FILE", "").strip()
    if jd_file:
        path = (ROOT / jd_file).resolve()
        if ROOT not in path.parents:
            sys.exit("jd_file must be inside the repository")
        if not path.is_file():
            sys.exit(f"JD file not found: {jd_file}")
        return path.read_text(encoding="utf-8").strip()
    sys.exit("Provide JD_TEXT or JD_FILE")


def slugify(value: str) -> str:
    """Make a safe file name from JOB_SLUG."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    return slug or "tailored"


# ---------- LLM ----------
OUTPUT_SHAPE = """Return ONE JSON object with exactly these keys and this shape:
{
  "experience": [
    {"company": "exact company from EXPERIENCE ENTRIES", "title": "exact title from EXPERIENCE ENTRIES",
     "bullets": ["up to 3 short bullets"]}
  ],
  "skills": {
    "AI & Machine Learning": ["items from PROFILE skills_strong or skills_exposure"],
    "Languages & Cloud Infrastructure": ["..."],
    "DevOps, GitOps & Observability": ["..."],
    "Frameworks & Integration": ["..."]
  },
  "projects": [
    {"name": "exact name from PROJECT NAMES", "bullets": ["up to 2 short bullets"]}
  ],
  "gaps": ["JD requirement not supported by PROFILE"],
  "keywords_used": ["JD terms that truthfully match PROFILE"],
  "notes": ["things the user should double check"]
}
"""


def build_user_message(jd: str, extra: str, profile: dict) -> str:
    """The user turn: output shape, the entries to fill, the JD, and the PROFILE."""
    targets = [{"company": j.get("company"), "title": j.get("title")}
               for j in dicts(profile.get("experience"))]
    names = [p.get("name") for p in dicts(profile.get("projects")) if p.get("name")]
    return (
        "Tailor the resume wording to the job description below.\n\n"
        f"{OUTPUT_SHAPE}\n"
        "EXPERIENCE ENTRIES (return one object for each, with the same company and title):\n"
        f"{json.dumps(targets, ensure_ascii=False, indent=1)}\n\n"
        f"PROJECT NAMES (pick up to {MAX_PROJECTS}, most relevant first):\n"
        f"{json.dumps(names, ensure_ascii=False)}\n\n"
        f"EXTRA_INSTRUCTIONS:\n{extra}\n\n"
        f"JOB_DESCRIPTION:\n{jd}\n\n"
        f"PROFILE (JSON, the only source of facts):\n"
        f"{json.dumps(profile, ensure_ascii=False, indent=1)}\n"
    )


class LLMUnavailable(Exception):
    """The server could not be reached or refused the request."""


class ReplySource:
    """Where the model's reply comes from: a saved file (LLM_REPLY_FILE) or an
    OpenAI-compatible server such as Ollama."""

    def __init__(self):
        self.file = os.environ.get("LLM_REPLY_FILE", "").strip()
        self.client = None
        self.model = os.environ.get("LLM_MODEL", "").strip()
        self.base_url = os.environ.get("LLM_BASE_URL", "").strip()
        if self.file:
            if not Path(self.file).is_file():
                sys.exit(f"LLM_REPLY_FILE not found: {self.file}")
            return
        missing = [n for n in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not os.environ.get(n)]
        if missing:
            sys.exit(f"Missing in .env or environment: {', '.join(missing)}")
        try:
            from openai import OpenAI   # imported here so the render code runs without openai
        except ImportError:
            sys.exit("The openai package is missing. Install it: pip install -r requirements.txt")
        self.client = OpenAI(base_url=self.base_url, api_key=os.environ["LLM_API_KEY"], timeout=600)

    def complete(self, messages: list) -> str:
        """Return the model's reply text. Connection problems are retried, then reported plainly."""
        if self.file:
            return Path(self.file).read_text(encoding="utf-8")
        from openai import APIConnectionError, APIStatusError
        for attempt in range(CONNECT_TRIES):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    temperature=0.2,
                    max_tokens=4096,
                    response_format={"type": "json_object"},
                    messages=messages,
                )
            except APIConnectionError as exc:
                if attempt == CONNECT_TRIES - 1:
                    raise LLMUnavailable(
                        f"Cannot reach the LLM at {self.base_url}. Start it (for Ollama: `ollama serve`) "
                        f"and check the URL in .env. ({exc})")
                time.sleep(2 ** attempt)
                continue
            except APIStatusError as exc:
                raise LLMUnavailable(
                    f"The LLM returned HTTP {exc.status_code} for model '{self.model}'. Check LLM_MODEL "
                    f"(for Ollama: `ollama list`). {exc.message}")
            if not resp.choices:
                raise LLMUnavailable("The LLM returned no choices")
            choice = resp.choices[0]
            if choice.finish_reason == "length":
                print("Warning: the reply was cut off (finish_reason=length). "
                      "Raise num_ctx in the Modelfile if this repeats.", file=sys.stderr)
            return choice.message.content or ""
        return ""


def extract_json(raw: str) -> dict:
    """Pull the first {...} object out of the model's reply, even if it adds code fences or prose."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    return json.loads(raw[start:end + 1])


def request_wording(source: ReplySource, messages: list, raw_path: Path) -> dict:
    """Ask the model for wording and return it as a dict. A reply that is not JSON is retried
    with the error message. The raw reply is always saved for inspection."""
    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            content = source.complete(messages)
        except LLMUnavailable as exc:
            sys.exit(f"LLM problem: {exc}")
        raw_path.write_text(content, encoding="utf-8")
        try:
            data = extract_json(content)
            if not isinstance(data, dict):
                raise ValueError("the top level is not a JSON object")
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if source.file:
                sys.exit(f"LLM_REPLY_FILE is not valid JSON: {exc}")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content":
                             f"Your reply could not be read as JSON ({exc}). Return ONLY the JSON "
                             "object described above: no prose, no code fences."})
            continue
        messages.append({"role": "assistant", "content": content})
        return data
    sys.exit(f"The LLM did not return usable JSON after {MAX_RETRIES + 1} tries ({last_error}). "
             f"Last reply saved to {raw_path}")


# ---------- JD requirements checked against PROFILE by the program ----------
TECH_TERMS = [
    "Kafka", "Event Hub", "Pub/Sub", "Azure", "GCP", "Google Cloud", "dbt", "Spark", "Databricks",
    "GDPR", "CCPA", "Go", "SQL", "Kubernetes", "Terraform", "AWS", "Docker", "CI/CD", "monitoring",
    "microservices", "OAuth", "event-driven", "distributed systems", "machine learning",
    "generative AI", "AI", "Java", "Python", "GraphQL", "Redis", "Snowflake", "BigQuery",
    "Node.js", "Spring Boot", "data pipelines", "data modeling", "open source",
]


def jd_gaps(jd: str, profile: dict) -> list:
    """Terms the JD asks for that PROFILE does not support (missing, or exposure-only)."""
    corpus = json.dumps(profile, ensure_ascii=False).lower()
    exposure = {x.lower() for x in items_list(profile.get("skills_exposure"))} | EXPOSURE_ALIASES
    jd_low = jd.lower()

    def mentioned(text: str, term: str) -> bool:
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text))

    gaps = []
    for term in TECH_TERMS:
        if not mentioned(jd_low, term):
            continue
        supported = mentioned(corpus, term) and term.lower() not in exposure
        if not supported:
            gaps.append(term)
    return gaps


FIXABLE_MARKERS = ("exposure-only", "numbers not in PROFILE", "skill not in PROFILE",
                   "not in PROFILE ignored", "unknown skill row", "keyword not found",
                   "terms not in PROFILE", "too thin", "lost every metric")


def is_fixable(issue: str) -> bool:
    """Problems the model can correct when asked (as opposed to layout notes)."""
    return any(marker in issue for marker in FIXABLE_MARKERS)


# ---------- compile and fit ----------
PDFLATEX_DIRS = ["/Library/TeX/texbin", "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin"]


def find_pdflatex():
    """Return the pdflatex path, checking PATH and the usual install folders (BasicTeX and MacTeX
    install to /Library/TeX/texbin, which a new Terminal may not have on PATH yet)."""
    found = shutil.which("pdflatex")
    if found:
        return found
    for folder in PDFLATEX_DIRS:
        candidate = Path(folder) / "pdflatex"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def compile_check(tex: str):
    """Compile the .tex with pdflatex in a temp folder. Returns (page_count, error_text).
    Both are None if pdflatex is not installed."""
    exe = find_pdflatex()
    if not exe:
        return None, None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "resume.tex"
        src.write_text(tex, encoding="utf-8")
        try:
            proc = subprocess.run(
                [exe, "-interaction=nonstopmode", "-halt-on-error", src.name],
                cwd=tmp, capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return None, "pdflatex timed out"
        if proc.returncode != 0:
            missing = re.search(r"File `([^']+)\.sty' not found", proc.stdout)
            if missing:
                pkg = missing.group(1)
                return None, (f"Missing LaTeX package '{pkg}'. Install it once with: "
                              f"sudo tlmgr install {pkg}")
            errors = [l for l in proc.stdout.splitlines() if l.startswith("!")]
            return None, "\n".join(errors[:5]) or "pdflatex failed"
        match = re.search(r"Output written on .*?\((\d+) page", proc.stdout)
        return (int(match.group(1)) if match else None), None


def trim_once(resume: dict) -> bool:
    """Remove one piece of optional wording, in code, until the resume fits one page.
    Order: project bullets, role bullets (down to 2), extra projects, role bullets (down to 1).
    Returns False when nothing is left to remove."""
    projects = resume["projects"]
    roles = [r for g in resume["experience"] for r in g["roles"]]

    def shrink(entries: list, keep: int) -> bool:
        for entry in reversed(entries):
            if len(entry["bullets"]) > keep:
                entry["bullets"].pop()
                return True
        return False

    if shrink(projects, 1):
        return True
    if shrink(roles, 2):
        return True
    if len(projects) > 1:
        projects.pop()
        return True
    return shrink(roles, 1)


# ---------- entry point ----------
def main() -> None:
    """Orchestrate one run: read inputs, get wording, check it, build, render, compile, write."""
    load_dotenv()
    jd = read_jd()
    extra = os.environ.get("EXTRA_INSTRUCTIONS", "").strip() or "None"
    slug = slugify(os.environ.get("JOB_SLUG", "tailored"))
    out = ROOT / "output"
    out.mkdir(exist_ok=True)
    raw_path = Path(tempfile.gettempdir()) / f"{slug}_llm_raw.txt"   # debugging copy, kept out of the repo

    profile = load_profile()
    preamble = load_preamble()
    system = SYSTEM_FILE.read_text(encoding="utf-8")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": build_user_message(jd, extra, profile)},
    ]

    source = ReplySource()
    raw = request_wording(source, messages, raw_path)
    first_text = raw_path.read_text(encoding="utf-8")
    wording, issues = clean_llm(raw, profile)
    problems = [i for i in issues if is_fixable(i)]
    if problems and not source.file:
        messages.append({"role": "user", "content":
                         "Parts of your reply broke the rules and were removed. Fix each problem below "
                         "and return the full JSON object again, same keys. Use only PROFILE facts, "
                         "and never claim an exposure-only skill as experience:\n- "
                         + "\n- ".join(problems)})
        raw2 = request_wording(source, messages, raw_path)
        wording2, issues2 = clean_llm(raw2, profile)
        if sum(is_fixable(i) for i in issues2) < len(problems):
            wording, issues = wording2, issues2
            print("The model corrected its reply after the first check.")
        else:
            raw_path.write_text(first_text, encoding="utf-8")
            print("The model's correction did not improve the reply; the first reply is used.")
    resume = build_resume(profile, wording)

    tex, pages, error = "", None, None
    for attempt in range(MAX_RETRIES + 1):
        tex = render_tex(preamble, resume)
        pages, error = compile_check(tex)
        if error is None and (pages is None or pages <= 1):
            break
        if attempt == MAX_RETRIES or source.file or (error and error.startswith("Missing LaTeX package")):
            break   # a missing LaTeX package is an install problem, not a wording problem
        if error:
            problem = f"The resume failed to compile: {error}. Fix the wording so it compiles."
        else:
            problem = (f"The resume rendered to {pages} pages. It must be exactly one page: "
                       "shorten the least relevant bullets.")
        messages.append({"role": "user",
                         "content": problem + " Return the full JSON object again, same keys."})
        raw = request_wording(source, messages, raw_path)
        wording, issues = clean_llm(raw, profile)
        resume = build_resume(profile, wording)

    trimmed = 0
    while error is None and pages and pages > 1 and trim_once(resume):
        trimmed += 1
        tex = render_tex(preamble, resume)
        pages, error = compile_check(tex)

    (out / f"{slug}.tex").write_text(tex, encoding="utf-8")   # saved even if it still fails

    if error:
        sys.exit(f"Compile still failing (see output/{slug}.tex): {error}")
    if pages and pages > 1:
        print(f"Warning: still {pages} pages after retries and trimming", file=sys.stderr)

    page_note = ("page count not verified (pdflatex not found)" if pages is None
                 else f"compiled to {pages} page(s)")
    trim_note = f", {trimmed} line(s) trimmed to fit" if trimmed else ""
    print(f"Wrote output/{slug}.tex ({page_note}{trim_note}).")
    if issues:
        print("LLM reply problems handled:")
        for issue in issues:
            print(f"  - {issue}")
    checked_gaps = jd_gaps(jd, profile)
    if checked_gaps:
        print("JD requirements not supported by your profile: " + ", ".join(checked_gaps))
    jd_terms = [t for t in TECH_TERMS if mentioned(jd, t)]
    resume_words = " ".join(
        [resume["summary"]] + [i for _, items in resume["skills"] for i in items]
        + [b for g in resume["experience"] for r in g["roles"] for b in r["bullets"]]
        + [b for p in resume["projects"] for b in p["bullets"]] + [p["tech"] for p in resume["projects"]])
    found = [t for t in jd_terms if mentioned(resume_words, t)]
    missing = [t for t in jd_terms if t not in found]
    print(f"JD keywords in the resume: {len(found)} of {len(jd_terms)}"
          + (f". Not in the resume: {', '.join(missing)}" if missing else ""))


if __name__ == "__main__":
    main()
