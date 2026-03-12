#!/usr/bin/env python3
"""
Publish markdown essays to laz.dev.

Usage:
    publish.py build [slug]    — convert drafts to HTML (optionally just one)
    publish.py deploy          — git push + SSH deploy to GCloud
    publish.py go [slug]       — build + deploy in one shot

Drafts live in ~/vault/project-cards/laz-dev/drafts/ as markdown files.
Each draft needs YAML frontmatter:

    ---
    title: My Essay Title
    date: 2026-03-12
    excerpt: One-line description for the listing page.
    slug: my-essay-title  (optional, derived from filename if missing)
    ---

    Your essay content in markdown...
"""

import os
import re
import sys
from datetime import date, datetime
from email.utils import format_datetime
from pathlib import Path

import markdown

VAULT_DRAFTS = Path.home() / "vault" / "project-cards" / "laz-dev" / "drafts"
SITE_DIR = Path.home() / "projects" / "laz-dev" / "site"
ARTICLES_DIR = SITE_DIR / "articles"
ARTICLES_PAGE = SITE_DIR / "articles.html"
RSS_PATH = SITE_DIR / "feed.xml"
SSH_HOST = "lazdev-gcloud"  # ~/.ssh/config alias

ARTICLE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title_raw} - laz.dev</title>
  <meta name="description" content="{excerpt}">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{excerpt}">
  <meta property="og:url" content="https://laz.dev/articles/{slug}.html">
  <meta property="og:type" content="article">
  <meta property="og:site_name" content="laz.dev">
  <meta name="twitter:card" content="summary">
  <meta name="twitter:title" content="{title}">
  <meta name="twitter:description" content="{excerpt}">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="alternate" type="application/rss+xml" title="laz.dev" href="/feed.xml">
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <header>
    <div class="container">
      <h1><a href="/">laz.dev</a></h1>
      <nav>
        <a href="/projects.html">projects</a>
        <a href="/articles.html">articles</a>
      </nav>
    </div>
  </header>

  <main class="container">
    <article>
      <div class="article-header">
        <div class="article-meta">
          <span>{date}</span>
          <div class="vote-widget" data-slug="{slug}">
            <button class="vote-btn vote-up" onclick="vote('{slug}','up')">&#9650;</button>
            <span class="vote-score">0</span>
            <button class="vote-btn vote-down" onclick="vote('{slug}','down')">&#9660;</button>
          </div>
        </div>
        <h1>{title_raw}</h1>
      </div>

      <div class="article-body">
        {body}
      </div>
    </article>

    <div class="subscribe-section" id="subscribe">
      <h2>get notified</h2>
      <p>new posts, no spam, unsubscribe whenever.</p>
      <form class="subscribe-form" onsubmit="return false">
        <input type="email" placeholder="you@example.com" required>
        <input type="text" name="website" class="hp-field" tabindex="-1" autocomplete="off" style="position:absolute;left:-9999px">
        <button type="submit">subscribe</button>
      </form>
      <div class="subscribe-msg"></div>
    </div>
  </main>

  <footer>
    <div class="container">&copy; {year} laz.dev</div>
  </footer>

  <script src="/script.js"></script>
</body>
</html>
"""

LISTING_ITEM_TEMPLATE = """\
      <li class="article-item">
        <div class="article-meta">
          <span>{date}</span>
          <div class="vote-widget" data-slug="{slug}">
            <button class="vote-btn vote-up" onclick="vote('{slug}','up')">&#9650;</button>
            <span class="vote-score">0</span>
            <button class="vote-btn vote-down" onclick="vote('{slug}','down')">&#9660;</button>
          </div>
        </div>
        <div class="article-title"><a href="/articles/{slug}.html">{title}</a></div>
        <div class="article-excerpt">{excerpt}</div>
      </li>"""


def parse_frontmatter(text):
    """Extract YAML frontmatter and body from markdown text."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', text, re.DOTALL)
    if not match:
        return {}, text

    meta = {}
    for line in match.group(1).strip().split('\n'):
        if ':' in line:
            key, val = line.split(':', 1)
            raw = val.strip()
            # Strip outer quotes, then unescape inner escaped quotes
            if (raw.startswith('"') and raw.endswith('"')):
                raw = raw[1:-1].replace('\\"', '"')
            elif (raw.startswith("'") and raw.endswith("'")):
                raw = raw[1:-1]
            meta[key.strip()] = raw
    return meta, match.group(2)


def get_drafts(slug_filter=None):
    """Find all markdown drafts, optionally filtered by slug."""
    drafts = []
    if not VAULT_DRAFTS.exists():
        return drafts

    for f in sorted(VAULT_DRAFTS.glob("*.md")):
        text = f.read_text()
        meta, body = parse_frontmatter(text)

        if not meta.get('title'):
            print(f"  skip {f.name}: no title in frontmatter")
            continue

        slug = meta.get('slug', f.stem)
        if slug_filter and slug != slug_filter:
            continue

        meta['slug'] = slug
        meta['body_md'] = body
        meta.setdefault('date', str(date.today()))
        meta.setdefault('excerpt', '')
        drafts.append(meta)

    return drafts


def _html_attr(s):
    """Escape a string for use in an HTML attribute value."""
    return s.replace('&', '&amp;').replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')


def build_article(meta):
    """Convert a draft's markdown body to a full HTML article page."""
    md = markdown.Markdown(extensions=['fenced_code', 'tables', 'smarty'])
    body_html = md.convert(meta['body_md'])

    html = ARTICLE_TEMPLATE.format(
        title=_html_attr(meta['title']),
        title_raw=meta['title'],
        date=meta['date'],
        slug=meta['slug'],
        excerpt=_html_attr(meta.get('excerpt', '')),
        body=body_html,
        year=date.today().year,
    )

    out_path = ARTICLES_DIR / f"{meta['slug']}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    print(f"  wrote {out_path.relative_to(SITE_DIR)}")
    return meta


def rebuild_articles_page(all_articles):
    """Regenerate articles.html with all articles sorted newest first.

    Reads existing articles from the listing page and merges with new ones.
    Returns the merged sorted list for use by RSS generation.
    """
    # Parse existing articles from the listing page
    existing = {}
    if ARTICLES_PAGE.exists():
        page_text = ARTICLES_PAGE.read_text()
        # Extract article items from existing page
        for item_match in re.finditer(
            r'data-slug="([^"]+)".*?'
            r'<div class="article-title"><a href="[^"]*">([^<]+)</a></div>\s*'
            r'<div class="article-excerpt">([^<]*)</div>',
            page_text, re.DOTALL
        ):
            slug = item_match.group(1)
            existing[slug] = {
                'slug': slug,
                'title': item_match.group(2),
                'excerpt': item_match.group(3),
            }
        # Also grab dates
        for item_match in re.finditer(
            r'<li class="article-item">\s*<div class="article-meta">\s*<span>([^<]+)</span>.*?data-slug="([^"]+)"',
            page_text, re.DOTALL
        ):
            d, slug = item_match.group(1), item_match.group(2)
            if slug in existing:
                existing[slug]['date'] = d

    # Merge: new articles override existing ones with same slug
    merged = dict(existing)
    for a in all_articles:
        merged[a['slug']] = a

    # Sort by date descending
    sorted_articles = sorted(merged.values(), key=lambda a: a.get('date', ''), reverse=True)

    items_html = "\n".join(
        LISTING_ITEM_TEMPLATE.format(**a) for a in sorted_articles
    )

    page_html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>articles - laz.dev</title>
  <meta name="description" content="Essays on robotics, AI, and building things.">
  <meta property="og:title" content="articles - laz.dev">
  <meta property="og:description" content="Essays on robotics, AI, and building things.">
  <meta property="og:url" content="https://laz.dev/articles.html">
  <meta property="og:type" content="website">
  <meta property="og:site_name" content="laz.dev">
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="alternate" type="application/rss+xml" title="laz.dev" href="/feed.xml">
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <header>
    <div class="container">
      <h1><a href="/">laz.dev</a></h1>
      <nav>
        <a href="/projects.html">projects</a>
        <a href="/articles.html">articles</a>
      </nav>
    </div>
  </header>

  <main class="container">
    <h2 class="page-title">articles</h2>

    <ul class="article-list">
{items_html}
    </ul>
  </main>

  <footer>
    <div class="container">&copy; {date.today().year} laz.dev</div>
  </footer>

  <script src="/script.js"></script>
</body>
</html>
"""
    ARTICLES_PAGE.write_text(page_html)
    print(f"  updated articles.html ({len(sorted_articles)} articles)")
    return sorted_articles


def build_rss(all_articles):
    """Generate RSS 2.0 feed from all articles."""
    import html as html_mod

    items_xml = []
    for a in all_articles:
        # Parse date string to RFC 822
        try:
            dt = datetime.strptime(a.get('date', ''), '%Y-%m-%d')
            pub_date = format_datetime(dt)
        except ValueError:
            pub_date = ''

        link = f"https://laz.dev/articles/{a['slug']}.html"
        items_xml.append(f"""    <item>
      <title>{html_mod.escape(a.get('title', ''))}</title>
      <link>{link}</link>
      <description>{html_mod.escape(a.get('excerpt', ''))}</description>
      <pubDate>{pub_date}</pubDate>
      <guid>{link}</guid>
    </item>""")

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>laz.dev</title>
    <link>https://laz.dev</link>
    <description>Essays by Laz Kopits</description>
    <language>en-us</language>
    <lastBuildDate>{format_datetime(datetime.now())}</lastBuildDate>
{chr(10).join(items_xml)}
  </channel>
</rss>
"""
    RSS_PATH.write_text(rss)
    print(f"  wrote feed.xml ({len(all_articles)} items)")


def cmd_build(slug_filter=None):
    print("building...")
    drafts = get_drafts(slug_filter)
    if not drafts:
        print("  no drafts found" + (f" matching '{slug_filter}'" if slug_filter else ""))
        return []

    built = []
    for meta in drafts:
        built.append(build_article(meta))

    sorted_articles = rebuild_articles_page(built)
    build_rss(sorted_articles)
    print(f"done. {len(built)} article(s) built.")
    return built


def cmd_deploy(built_articles=None):
    print("deploying...")
    project_dir = Path.home() / "projects" / "laz-dev"
    os.chdir(project_dir)

    # Stage, commit, push
    os.system("git add site/articles/ site/articles.html site/feed.xml")
    ret = os.system('git diff --cached --quiet')
    if ret == 0:
        print("  nothing to commit")
    else:
        os.system('git commit -m "publish articles"')
        os.system("git push origin main")

    # SSH deploy
    print("  deploying to server...")
    ret = os.system(f"ssh {SSH_HOST} 'cd /home/laszlokopits/laz.dev && git pull origin main && sudo systemctl reload caddy'")
    if ret != 0:
        print("  SSH deploy failed — you may need to set up the SSH key first")
        return False

    # Notify subscribers for each new article
    if built_articles:
        print("  notifying subscribers...")
        for article in built_articles:
            slug = article['slug']
            title = article['title'].replace("'", "'\\''")
            excerpt = article.get('excerpt', '').replace("'", "'\\''")
            os.system(
                f'ssh {SSH_HOST} \'curl -s -X POST http://localhost:8000/api/notify '
                f'-H "Content-Type: application/json" '
                f"""-d \'{{"slug": "{slug}", "title": "{title}", "excerpt": "{excerpt}"}}\'\' """
            )

    print("deployed.")
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    slug = sys.argv[2] if len(sys.argv) > 2 else None

    if cmd == "build":
        cmd_build(slug)
    elif cmd == "deploy":
        cmd_deploy()
    elif cmd == "go":
        built = cmd_build(slug)
        if built:
            cmd_deploy(built_articles=built)
    else:
        print(f"unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
