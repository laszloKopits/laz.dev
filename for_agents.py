#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["markdown>=3.5", "markdownify>=0.13"]
# ///
"""
Build laz.dev/for-agents/: technical notes and results written up for agents and search.

Sources live in for-agents/ at the repo root (NOT under site/):
    <slug>.md    a markdown note with frontmatter (title, date, excerpt, confidence,
                 confidence_note). Rendered to
                 site/for-agents/<slug>.html; the markdown is copied to <slug>.md.
    <slug>.html  a hand-made page (full HTML document; confidence via
                 <meta name="for-agents:confidence"> and "for-agents:confidence-note"). Copied to site/for-agents/<slug>.html;
                 a markdown twin is generated at <slug>.md from its <div class="article-body">.

confidence is "confirmed" (it worked and was checked afterwards: the author confirmed it, or a
re-measurement after the fix proves it) or "measured" (a recorded test supports it, but the
end-to-end outcome wasn't independently re-confirmed). Anything weaker isn't published.

Every note is published in both formats: HTML for people and search engines, markdown for
agents. The build also regenerates:
    site/for-agents/index.html   the visible section landing page
    site/llms.txt                https://llmstxt.org index for agents
    site/sitemap.xml             every public page on the site
    site/robots.txt              allow everything, point at the sitemap

Usage:
    ./for_agents.py              build into site/
Deploy is the normal laz.dev flow: commit, push, then `git pull` in ~/laz.dev on the VM
(Caddy serves site/ as static files, so no restart is needed).
"""

import html
import json
import re
import sys
from datetime import date
from pathlib import Path

import markdown
from markdownify import markdownify

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "for-agents"
SITE = ROOT / "site"
OUT = SITE / "for-agents"
BASE = "https://laz.dev"

SECTION_BLURB = (
    "This area is for agents. Each page is a technical note or result, written up "
    "programmatically (mostly by daimon, my AI agent) so the next person or agent "
    "searching for it finds the answer. Every page is also available as plain markdown: "
    "add <code>.md</code> to the URL, or start from <a href=\"/llms.txt\">/llms.txt</a>."
)

HEAD = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} - laz.dev</title>
  <meta name="description" content="{description}">
  <link rel="canonical" href="{url}">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{description}">
  <meta property="og:url" content="{url}">
  <meta property="og:type" content="{og_type}">
  <meta property="og:site_name" content="laz.dev">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="{title}">
  <meta name="twitter:description" content="{description}">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="stylesheet" href="/style.css">{extra_head}
</head>
<body>
  <header>
    <div class="container">
      <h1><a href="/">laz.dev</a></h1>
      <nav>
        <a href="/projects.html">projects</a>
        <a href="/articles.html">articles</a>
        <a href="/products.html">products i stand by</a>
        <a href="/for-agents/">for agents</a>
      </nav>
    </div>
  </header>

  <main class="container">
"""

FOOT = """\
  </main>

  <footer>
    <div class="container">&copy; {year} laz.dev</div>
  </footer>
</body>
</html>
"""

CONFIDENCE_TEXT = {
    "confirmed": "confirmed: it worked, checked after the fact",
    "measured": "measured: backed by a recorded test, not independently re-confirmed",
}

NOTE_BODY = """\
    <p class="for-agents-banner" style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 1.5rem;">
      <a href="/for-agents/">for agents</a> &middot; written programmatically &middot;
      <a href="/for-agents/{slug}.md">markdown version</a>
    </p>
    <article>
      <div class="article-header">
        <div class="article-meta">
          <span>{date}</span>
        </div>
        <h1>{title}</h1>
      </div>
{confidence_block}
      <div class="article-body">
{body}
      </div>
    </article>
"""


def confidence_html(note):
    c = note.get("confidence")
    if not c:
        return ""
    label = CONFIDENCE_TEXT.get(c, c)
    why = f" {html.escape(note['confidence_note'])}" if note.get("confidence_note") else ""
    return (f'      <p class="confidence" style="font-size: 0.8rem; border-left: 2px solid var(--text-dim); '
            f'padding-left: 0.75rem; margin: 0 0 1.5rem;"><strong>Confidence: {html.escape(label)}.</strong>{why}</p>\n')


def confidence_md(note):
    c = note.get("confidence")
    if not c:
        return ""
    why = f" {note['confidence_note']}" if note.get("confidence_note") else ""
    return f"**Confidence: {CONFIDENCE_TEXT.get(c, c)}.**{why}\n\n"


def parse_frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        sys.exit("note is missing --- frontmatter ---")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    return meta, m.group(2)


def json_ld(note):
    return "\n  <script type=\"application/ld+json\">" + json.dumps({
        "@context": "https://schema.org",
        "@type": "TechArticle",
        "headline": note["title"],
        "description": note["excerpt"],
        "datePublished": note["date"],
        "url": note["url"],
        "encoding": {"@type": "MediaObject", "encodingFormat": "text/markdown", "contentUrl": note["md_url"]},
        "author": {"@type": "Person", "name": "Laszlo Kopits", "url": BASE + "/"},
    }) + "</script>\n  <link rel=\"alternate\" type=\"text/markdown\" href=\"" + note["md_url"] + "\">"


def note_urls(slug):
    return f"{BASE}/for-agents/{slug}.html", f"{BASE}/for-agents/{slug}.md"


def build_markdown_note(path):
    meta, body = parse_frontmatter(path.read_text())
    slug = meta.get("slug") or path.stem
    url, md_url = note_urls(slug)
    note = {"slug": slug, "title": meta["title"], "date": meta["date"], "excerpt": meta["excerpt"],
            "url": url, "md_url": md_url, "confidence": meta.get("confidence", ""),
            "confidence_note": meta.get("confidence_note", "")}
    if note["confidence"] and note["confidence"] not in CONFIDENCE_TEXT:
        sys.exit(f"{path.name}: confidence must be one of {sorted(CONFIDENCE_TEXT)}")
    rendered = markdown.markdown(body, extensions=["fenced_code", "tables"])
    page = HEAD.format(title=html.escape(note["title"]), description=html.escape(note["excerpt"]),
                       url=url, og_type="article", extra_head=json_ld(note))
    page += NOTE_BODY.format(slug=slug, date=note["date"], title=html.escape(note["title"]),
                             confidence_block=confidence_html(note), body=rendered)
    page += FOOT.format(year=note["date"][:4])
    (OUT / f"{slug}.html").write_text(page)
    md = (f"# {note['title']}\n\n{note['date']} · {url}\n\n> {note['excerpt']}\n\n"
          f"{confidence_md(note)}{body.strip()}\n")
    (OUT / f"{slug}.md").write_text(md)
    return note


def meta_tag(doc, attr, name):
    m = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', doc)
    return html.unescape(m.group(1)) if m else ""


def build_html_note(path):
    doc = path.read_text()
    slug = path.stem
    url, md_url = note_urls(slug)
    title = meta_tag(doc, "property", "og:title") or re.search(r"<title>(.*?)</title>", doc).group(1)
    excerpt = meta_tag(doc, "name", "description")
    d = re.search(r'<div class="article-meta">\s*<span>([\d-]+)</span>', doc)
    note = {"slug": slug, "title": title, "date": d.group(1) if d else "", "excerpt": excerpt,
            "url": url, "md_url": md_url,
            # Hand-made pages declare confidence in <meta name="for-agents:confidence[-note]">.
            "confidence": meta_tag(doc, "name", "for-agents:confidence"),
            "confidence_note": meta_tag(doc, "name", "for-agents:confidence-note")}
    if note["confidence"] and note["confidence"] not in CONFIDENCE_TEXT:
        sys.exit(f"{path.name}: confidence must be one of {sorted(CONFIDENCE_TEXT)}")
    # Point the page at its new home, add the section banner and the markdown alternate.
    doc = re.sub(r'<meta property="og:url" content="[^"]*">',
                 f'<meta property="og:url" content="{url}">\n  <link rel="canonical" href="{url}">', doc)
    doc = doc.replace('<link rel="stylesheet" href="/style.css">',
                      '<link rel="stylesheet" href="/style.css">' + json_ld(note), 1)
    doc = re.sub(r"<nav>.*?</nav>", HEAD.split("<nav>")[1].split("</nav>")[0].join(["<nav>", "</nav>"]),
                 doc, count=1, flags=re.DOTALL)
    banner = NOTE_BODY.split("<article>")[0].format(slug=slug, confidence_block="")
    doc = doc.replace('<main class="container">\n', '<main class="container">\n' + banner, 1)
    if note["confidence"]:
        doc = doc.replace('<div class="article-body">', confidence_html(note) + '      <div class="article-body">', 1)
    (OUT / f"{slug}.html").write_text(doc)
    body = re.search(r'<div class="article-body">(.*)</div>\s*</article>', doc, re.DOTALL).group(1)
    body_md = markdownify(body, heading_style="ATX", bullets="-").strip()
    body_md = re.sub(r"\(/(?!/)", f"({BASE}/", body_md)  # absolute links for agents
    body_md = re.sub(r"\n{3,}", "\n\n", body_md)
    md = f"# {title}\n\n{note['date']} · {url}\n\n> {excerpt}\n\n{confidence_md(note)}{body_md}\n"
    (OUT / f"{slug}.md").write_text(md)
    return note


def build_index(notes):
    items = "\n".join(
        f"""      <li class="article-item">
        <div class="article-meta"><span>{n['date']}</span></div>
        <div class="article-title"><a href="/for-agents/{n['slug']}.html">{html.escape(n['title'])}</a></div>
        <div class="article-excerpt">{html.escape(n['excerpt'])} <a href="/for-agents/{n['slug']}.md">markdown</a>{(' &middot; confidence: ' + n['confidence']) if n.get('confidence') else ''}</div>
      </li>""" for n in notes)
    page = HEAD.format(title="for agents", description="Technical notes and results from laz.dev, written up programmatically for agents and search engines. Every page is also available as markdown.",
                       url=f"{BASE}/for-agents/", og_type="website", extra_head="")
    page += f"""    <h2 class="page-title">for agents</h2>
    <p style="font-size: 0.8rem; color: var(--text-dim); margin-bottom: 1.5rem;">{SECTION_BLURB}</p>

    <ul class="article-list">
{items}
    </ul>
"""
    page += FOOT.format(year=date.today().year)
    (OUT / "index.html").write_text(page)


def build_llms(notes):
    lines = [
        "# laz.dev",
        "",
        "> Laszlo Kopits's site: robotics, AI and things he builds. The /for-agents/ section holds "
        "technical notes and results written up programmatically for agents. Every note has a "
        "markdown version, linked below.",
        "",
        "## For agents",
        "",
    ]
    lines += [f"- [{n['title']}]({n['md_url']}): {n['excerpt']}"
              + (f" (confidence: {n['confidence']})" if n.get("confidence") else "") for n in notes]
    lines += [
        "",
        "## Optional",
        "",
        f"- [Articles]({BASE}/articles.html): essays on robotics, AI and building things",
        f"- [Projects]({BASE}/projects.html): things Laz has built",
        f"- [Products I stand by]({BASE}/products.html)",
        "",
    ]
    (SITE / "llms.txt").write_text("\n".join(lines))


def build_sitemap(notes):
    urls = []
    for p in sorted(SITE.rglob("*.html")):
        rel = p.relative_to(SITE).as_posix()
        if rel.startswith("staging/"):
            continue
        if rel == "index.html":
            urls.append(f"{BASE}/")
        elif rel.endswith("/index.html"):
            urls.append(f"{BASE}/{rel[:-len('index.html')]}")
        else:
            urls.append(f"{BASE}/{rel}")
    dated = {n["url"]: n["date"] for n in notes}
    body = "\n".join(
        f"  <url><loc>{u}</loc>" + (f"<lastmod>{dated[u]}</lastmod>" if dated.get(u) else "") + "</url>"
        for u in urls)
    (SITE / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "\n</urlset>\n")
    (SITE / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {BASE}/sitemap.xml\n")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    notes = []
    for path in sorted(SRC.iterdir()):
        if path.suffix == ".md":
            notes.append(build_markdown_note(path))
        elif path.suffix == ".html":
            notes.append(build_html_note(path))
    notes.sort(key=lambda n: n["date"], reverse=True)
    build_index(notes)
    build_llms(notes)
    build_sitemap(notes)
    for n in notes:
        print(f"{n['date']}  {n['url']}  (+ .md)")
    print(f"{len(notes)} notes; wrote index.html, llms.txt, sitemap.xml, robots.txt")


if __name__ == "__main__":
    main()
