#!/usr/bin/env python3
"""
topicwatch.py — daily topic + term monitor for a competitive content space.

What it does, once a day:
  1. Pulls new posts from a list of RSS/Atom feeds (sources.json)
  2. Extracts full article text (trafilatura if installed, else feed summary)
  3. Stores everything in SQLite so you build a longitudinal corpus
  4. Scores terms by RISING-ness (recent window vs. trailing baseline),
     not just raw frequency — raw counts just tell you "AI" is popular
  5. Pulls out question-shaped headings, which are the raw material
     for AI-SEO / answer-engine coverage
  6. Writes a dated markdown report

Usage:
    python topicwatch.py init                 # create sources.json + db
    python topicwatch.py fetch                # run daily (cron this)
    python topicwatch.py report               # write today's report
    python topicwatch.py run                  # fetch + report
    python topicwatch.py gaps                 # your coverage vs. the space

Deps (minimal):
    pip install feedparser
Optional but strongly recommended (real article text, not summaries):
    pip install trafilatura
"""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

# Politeness. Sequential article fetches with no delay will get you rate
# limited or IP-blocked, especially on a first run against a big sitemap.
FETCH_DELAY = float(os.environ.get("TOPICWATCH_DELAY", "1.5"))  # seconds
MAX_NEW_PER_SOURCE = int(os.environ.get("TOPICWATCH_MAX_NEW", "60"))

DB_PATH = os.environ.get("TOPICWATCH_DB", "topicwatch.db")
SOURCES_PATH = os.environ.get("TOPICWATCH_SOURCES", "sources.json")
REPORT_DIR = os.environ.get("TOPICWATCH_REPORTS", "reports")

# --------------------------------------------------------------------------
# Sources. Two forms are accepted:
#   "https://site.com/feed"                       -> RSS/Atom feed
#   {"sitemap": "https://site.com/sitemap.xml",
#    "match": "/thinking/"}                       -> sitemap crawl, path-filtered
#
# Use the sitemap form for agency/consultancy sites, which very often have no
# working feed for their insights hub. Run `python topicwatch.py discover
# <domain>` to find out which form a given site needs.
# --------------------------------------------------------------------------
DEFAULT_SOURCES = {
    # --- Named competitors -------------------------------------------------
    "competitors": [
        {"sitemap": "https://prophet.com/sitemap.xml", "match": "/thinking/"},
        {"sitemap": "https://www.dentsu.com/sitemap.xml", "match": "/blog"},
        {"sitemap": "https://www.dentsu.com/sitemap.xml",
         "match": "/our-latest-thinking"},
        "https://www.group.dentsu.com/en/news/rss.xml",
        # Hearst's marketing-services arm publishes on WordPress; /feed
        # usually resolves. Verify with `discover` before trusting it.
        # "https://marketing.hearstbayarea.com/feed",
    ],

    # --- The trade press your buyers actually read -------------------------
    # This is the corpus that matters. These outlets set the vocabulary that
    # agency content then echoes, so they lead the trend by a few weeks.
    "trade_press": [
        "https://www.marketingdive.com/feeds/news/",
        "https://digiday.com/feed/",
        "https://www.adexchanger.com/feed/",
        "https://martech.org/feed/",
        "https://www.marketingweek.com/feed/",
        "https://adage.com/rss.xml",
    ],

    # --- SEO / answer-engine specialists -----------------------------------
    # Narrower, but this is where AI-SEO vocabulary gets coined first.
    "seo_press": [
        "https://searchengineland.com/feed",
        "https://www.searchenginejournal.com/feed/",
        "https://moz.com/posts/rss/blog",
    ],

    # --- Boutique agencies -------------------------------------------------
    # Mostly WordPress -> <domain>/feed almost always works. Add 8-12 of the
    # shops that actually show up when you search your money terms.
    "boutique": [
    ],

    # --- Your own blog, for the `gaps` command -----------------------------
    "own": [
    ],
}

# --------------------------------------------------------------------------
# Stopwords: English core + content-marketing boilerplate that would otherwise
# dominate every report ("read", "post", "guide", "learn"...).
# --------------------------------------------------------------------------
STOP = set("""
a about above after again against all am an and any are aren't as at be because been
before being below between both but by can can't cannot could couldn't did didn't do
does doesn't doing don't down during each few for from further had hadn't has hasn't
have haven't having he he'd he'll he's her here here's hers herself him himself his how
how's i i'd i'll i'm i've if in into is isn't it it's its itself let's me more most
mustn't my myself no nor not of off on once only or other ought our ours ourselves out
over own same shan't she she'd she'll she's should shouldn't so some such than that
that's the their theirs them themselves then there there's these they they'd they'll
they're they've this those through to too under until up very was wasn't we we'd we'll
we're we've were weren't what what's when when's where where's which while who who's
whom why why's with won't would wouldn't you you'd you'll you're you've your yours
yourself yourselves also just get got make made need new now one two three like well
much many way ways lot lots thing things time times use used using user users
read post posts blog blogs article articles guide guides learn more subscribe
newsletter share comment comments today week year years day days minute minutes
best top great good better see look looking know think want going come first last
help helps helping work works working via according says said say new latest
""".split())

# Agency/consultancy boilerplate. This vocabulary is pure connective tissue in
# this space — it appears in literally every post and signals nothing.
STOP |= set("""
leverage leveraging unlock unlocking harness harnessing empower empowering
enable enabling drive driving deliver delivering deliver ensure ensuring
seamless seamlessly holistic robust bespoke tailored cutting edge world class
today's tomorrow's ever evolving rapidly landscape journey partner partners
partnership solutions offering offerings capability capabilities expertise
approach approaches framework frameworks insight insights thinking latest
client clients customer customers consumer consumers audience audiences
across within throughout increasingly truly really critical key crucial
essential important powerful proven trusted leading global
""".split())

# Words so generic to this space they tell you nothing on their own.
# Keep them OUT of unigrams but ALLOW them inside bigrams/trigrams,
# where they carry real meaning ("brand relevance", "agentic workflow",
# "answer engine", "marketing transformation").
CATEGORY_NOISE = {
    # AI category
    "ai", "artificial", "intelligence", "llm", "llms", "model", "models",
    "tech", "technology", "software", "platform", "digital",
    # Agency / brand-consulting category
    "brand", "brands", "branding", "marketing", "marketer", "marketers",
    "growth", "strategy", "strategic", "agency", "agencies", "campaign",
    "campaigns", "media", "content", "creative", "experience", "experiences",
    "transformation", "data", "business", "company", "companies", "team",
    "teams", "market", "markets",
}

# Which vertical each bucket rolls up into, for per-vertical reports.
# Buckets not listed here become their own vertical.
VERTICAL_OF = {
    "competitors": "agencies",
    "hearst": "agencies",
    "boutique": "agencies",
    "trade_press": "marketing",
    "seo_press": "seo",
    "ai_press": "ai",
    "ai_business": "ai",
    "tech_press": "tech",
    "fintech": "fintech",
    "devtools": "software",
    "smb_growth": "smb",
    "own": "own",
}

TOKEN_RE = re.compile(r"[a-z][a-z0-9'\-\.]{1,}")
QUESTION_RE = re.compile(
    r"^\s*(how|what|why|when|which|who|where|can|should|do|does|is|are|will)\b.*",
    re.I)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            url         TEXT PRIMARY KEY,
            source      TEXT,
            bucket      TEXT,
            title       TEXT,
            published   TEXT,
            fetched     TEXT,
            body        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pub ON articles(published);
        CREATE TABLE IF NOT EXISTS term_daily (
            day   TEXT,
            term  TEXT,
            n     INTEGER,
            gram  INTEGER,
            PRIMARY KEY (day, term)
        );
    """)
    return conn


# --------------------------------------------------------------------------
# Feed discovery + sitemap crawling
# Agencies and consultancies frequently have no feed for their insights hub.
# --------------------------------------------------------------------------
COMMON_FEED_PATHS = ["/feed", "/feed/", "/rss", "/rss.xml", "/index.xml",
                     "/atom.xml", "/blog/feed", "/blog/rss.xml",
                     "/insights/feed", "/feed.xml", "/news/rss.xml"]

COMMON_SITEMAPS = ["/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml",
                   "/sitemap-index.xml"]

UA = {"User-Agent": "Mozilla/5.0 (compatible; topicwatch/1.0)"}


def http_get(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def looks_like_feed(text):
    head = text[:2000].lower()
    return "<rss" in head or "<feed" in head or "<rdf" in head


def discover(domain):
    """Find the best ingestion route for a site: feed, or sitemap + path."""
    if not domain.startswith("http"):
        domain = "https://" + domain
    domain = domain.rstrip("/")
    print(f"\nProbing {domain}\n" + "-" * 60)

    # 1. Declared feed in the HTML <head>
    try:
        html = http_get(domain)
        for m in re.finditer(
                r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>',
                html, re.I):
            href = re.search(r'href=["\']([^"\']+)["\']', m.group(0))
            if href:
                u = href.group(1)
                if u.startswith("/"):
                    u = domain + u
                print(f"  [declared feed] {u}")
    except Exception as e:
        print(f"  [warn] couldn't read homepage: {e}")

    # 2. Brute-force the usual suspects
    found_feed = False
    for path in COMMON_FEED_PATHS:
        try:
            body = http_get(domain + path, timeout=10)
            if looks_like_feed(body):
                print(f"  [FEED OK] {domain + path}")
                found_feed = True
        except Exception:
            pass

    # 3. Sitemap fallback — report which path prefixes hold the articles
    for sm in COMMON_SITEMAPS:
        try:
            body = http_get(domain + sm, timeout=15)
        except Exception:
            continue
        urls = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", body)
        if not urls:
            continue
        print(f"\n  [SITEMAP] {domain + sm} -> {len(urls)} entries")
        if any(u.endswith(".xml") for u in urls[:20]):
            print("    (index file — child sitemaps:)")
            for u in urls[:15]:
                print(f"      {u}")
            break
        prefixes = Counter()
        for u in urls:
            parts = [p for p in u.replace(domain, "").split("/") if p]
            if parts:
                prefixes["/" + parts[0]] += 1
        print("    top path prefixes (use one as \"match\"):")
        for p, c in prefixes.most_common(10):
            print(f"      {p:<30} {c}")
        break

    if not found_feed:
        print("\n  No usable feed. Use the sitemap form in sources.json:")
        print(f'    {{"sitemap": "{domain}/sitemap.xml", "match": "/insights/"}}')


def sitemap_urls(sitemap_url, match, cap=300):
    """Pull article URLs from a sitemap (following one level of index)."""
    out = []
    try:
        body = http_get(sitemap_url)
    except Exception as e:
        print(f"     [skip] {e}")
        return out

    locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", body)
    children = [u for u in locs if u.endswith(".xml")]
    if children:
        for child in children[:25]:
            if match and match.strip("/").split("/")[0] not in child \
                    and not re.search(r"post|page|article|blog|insight", child, re.I):
                continue
            try:
                sub = http_get(child)
                locs += re.findall(r"<loc>\s*([^<]+?)\s*</loc>", sub)
            except Exception:
                pass

    for u in locs:
        if u.endswith(".xml"):
            continue
        if match and match not in u:
            continue
        out.append(u)
    return out[:cap]


def parse_source(entry):
    """Normalize a sources.json entry to (kind, url, match). Returns None for
    anything malformed, so one bad line can't take down the whole run."""
    if isinstance(entry, str):
        if not entry.startswith("http"):
            print(f"     [bad entry] not a URL, skipping: {entry[:60]!r}")
            return None
        return ("feed", entry, None)
    if isinstance(entry, dict) and "sitemap" in entry:
        return ("sitemap", entry["sitemap"], entry.get("match", ""))
    print(f"     [bad entry] expected a URL string or "
          f"{{\"sitemap\": ..., \"match\": ...}}, got: {entry!r}")
    return None


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------
def _ingest_sitemap(conn, url, match, bucket, trafilatura):
    """Crawl one sitemap source. Returns count of new articles."""
    urls = sitemap_urls(url, match)
    print(f"     {len(urls)} candidate URLs")
    site = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
    added = new_here = 0
    for u in urls:
        if new_here >= MAX_NEW_PER_SOURCE:
            print(f"     [cap] stopping at {MAX_NEW_PER_SOURCE} new — "
                  "rerun tomorrow to keep backfilling")
            break
        if conn.execute("SELECT 1 FROM articles WHERE url=?", (u,)).fetchone():
            continue
        try:
            time.sleep(FETCH_DELAY)
            raw = trafilatura.fetch_url(u)
            if not raw:
                continue
            body = trafilatura.extract(raw, include_comments=False) or ""
            meta = trafilatura.extract_metadata(raw)
            title = (getattr(meta, "title", "") if meta else "") or ""
            date = (getattr(meta, "date", None) if meta else None)
        except Exception:
            continue
        if len(body) < 400:
            continue
        pub_iso = (f"{date}T00:00:00+00:00" if date
                   else datetime.now(timezone.utc).isoformat())
        conn.execute("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?)",
                     (u, site, bucket, title, pub_iso,
                      datetime.now(timezone.utc).isoformat(), body))
        added += 1
        new_here += 1
    return added


def _ingest_feed(conn, url, bucket, limit, feedparser, trafilatura, have_traf):
    """Read one RSS/Atom feed. Returns count of new articles."""
    parsed = feedparser.parse(url)
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        print(f"     [dead feed] no entries — run `discover` on this domain")
        return 0
    site = parsed.feed.get("title", url)
    added = 0
    for item in parsed.entries[:limit]:
        link = item.get("link")
        if not link:
            continue
        if conn.execute("SELECT 1 FROM articles WHERE url=?",
                        (link,)).fetchone():
            continue
        pub = item.get("published_parsed") or item.get("updated_parsed")
        pub_iso = (datetime(*pub[:6], tzinfo=timezone.utc).isoformat()
                   if pub else datetime.now(timezone.utc).isoformat())
        body = ""
        if have_traf:
            try:
                time.sleep(FETCH_DELAY)
                raw = trafilatura.fetch_url(link)
                if raw:
                    body = trafilatura.extract(raw, include_comments=False,
                                               include_tables=False) or ""
            except Exception:
                pass
        if not body:
            body = re.sub(r"<[^>]+>", " ", item.get("summary", "") or "")
        conn.execute("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?)",
                     (link, site, bucket, item.get("title", ""), pub_iso,
                      datetime.now(timezone.utc).isoformat(), body))
        added += 1
    print(f"     {added} new from {site}")
    return added


def fetch(limit_per_feed=40):
    try:
        import feedparser
    except ImportError:
        sys.exit("Missing dep. Run: pip install feedparser")

    trafilatura = None
    try:
        import trafilatura
        have_traf = True
    except ImportError:
        have_traf = False
        print("[warn] trafilatura not installed — falling back to feed "
              "summaries, and sitemap sources will be skipped.")

    if not os.path.exists(SOURCES_PATH):
        sys.exit(
            f"\nFATAL: {SOURCES_PATH} not found (working dir: {os.getcwd()}).\n"
            f"Files here: {sorted(os.listdir('.'))[:25]}\n\n"
            "Likely causes: named sources.JSON or sources.json.txt; committed\n"
            "into a subfolder; or never committed. It must sit next to\n"
            "topicwatch.py.\n")
    try:
        sources = json.load(open(SOURCES_PATH))
    except json.JSONDecodeError as e:
        sys.exit(f"\nFATAL: {SOURCES_PATH} is not valid JSON.\n  {e}\n"
                 "Usual culprit is a trailing comma after the last item in a\n"
                 "list. Paste the file into jsonlint.com to locate it.\n")
    if not isinstance(sources, dict):
        sys.exit(f"\nFATAL: {SOURCES_PATH} must be an object of named buckets, "
                 'e.g. {"competitors": [...]}\n')

    conn = db()
    added = 0
    failures = []

    for bucket, entries in sources.items():
        # Buckets starting with "_" are notes, not sources. JSON has no
        # comment syntax, so this keeps sources.json self-documenting.
        if bucket.startswith("_"):
            continue
        if not isinstance(entries, list):
            print(f"[warn] bucket {bucket!r} is not a list — skipping")
            continue
        for entry in entries:
            src = parse_source(entry)
            if src is None:
                continue
            kind, url, match = src
            print(f"  -> [{kind}] {url}" + (f"  match={match}" if match else ""))
            try:
                if kind == "sitemap":
                    if not have_traf:
                        print("     [skip] needs trafilatura")
                        continue
                    added += _ingest_sitemap(conn, url, match, bucket,
                                             trafilatura)
                else:
                    added += _ingest_feed(conn, url, bucket, limit_per_feed,
                                          feedparser, trafilatura, have_traf)
            except Exception as e:
                # One bad source must never end the run.
                print(f"     [error] {type(e).__name__}: {str(e)[:200]}")
                failures.append(url)
            conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    print(f"\n[ok] {added} new articles. Corpus is now {total} docs.")
    if failures:
        print(f"[note] {len(failures)} source(s) errored: "
              + ", ".join(failures[:8]))
    if total == 0:
        print("\n[warn] Corpus is empty. Every source failed, or none has "
              "published yet. Run `discover` on a couple of domains to find "
              "working feed URLs. Not treating this as a fatal error.")
    return added


# --------------------------------------------------------------------------
# Text -> terms
# --------------------------------------------------------------------------
def tokens(text):
    out = []
    for t in TOKEN_RE.findall((text or "").lower()):
        t = t.strip(".-'")          # "growth." -> "growth", so noise lists match
        if len(t) > 2 and t not in STOP and not t.replace(".", "").isdigit():
            out.append(t)
    return out


def ngrams(toks, n):
    return [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]


TITLE_WEIGHT = 3


def doc_terms(text, title=""):
    """
    Title gets triple weight — it's the strongest intent signal a post has.
    N-grams are built per-field so phrases never straddle the title/body seam
    (that seam produces junk like "visibility matter visibility").
    """
    body_t, title_t = tokens(text), tokens(title)
    out = {1: [], 2: [], 3: []}
    for toks, weight in ((body_t, 1), (title_t, TITLE_WEIGHT)):
        if not toks:
            continue
        out[1] += [t for t in toks if t not in CATEGORY_NOISE] * weight
        out[2] += ngrams(toks, 2) * weight
        out[3] += ngrams(toks, 3) * weight
    return out


# --------------------------------------------------------------------------
# Analysis: rising terms
# --------------------------------------------------------------------------
def window_counts(conn, start, end, buckets=None):
    """Return {gram_size: Counter} plus doc frequency, for a date window.
    buckets: optional list of bucket names to restrict to."""
    q = "SELECT title, body FROM articles WHERE published >= ? AND published < ?"
    params = [start.isoformat(), end.isoformat()]
    if buckets:
        q += " AND bucket IN (%s)" % ",".join("?" * len(buckets))
        params += list(buckets)
    rows = conn.execute(q, params).fetchall()

    counts = {1: Counter(), 2: Counter(), 3: Counter()}
    docfreq = {1: Counter(), 2: Counter(), 3: Counter()}
    for r in rows:
        terms = doc_terms(r["body"], r["title"])
        for n, lst in terms.items():
            counts[n].update(lst)
            docfreq[n].update(set(lst))
    return counts, docfreq, len(rows)


def rising(recent, baseline, recent_docs, base_docs, docfreq=None,
           min_docs=2, top=25):
    """
    Lift score: how much more concentrated a term is now vs. its own history.
    Log-odds-ish with smoothing so a term appearing 3x this week beats
    a term appearing 400x every week forever.

    docfreq gates on SPREAD: a term must show up in min_docs separate articles.
    Without this, one article's title (weighted 3x) looks identical to three
    articles independently converging on a phrase — and the second is the only
    one that means anything.
    """
    out = []
    r_total = sum(recent.values()) or 1
    b_total = sum(baseline.values()) or 1
    for term, n in recent.items():
        if n < 2:
            continue
        if docfreq is not None and docfreq.get(term, 0) < min_docs:
            continue
        r_rate = n / r_total
        b_rate = (baseline.get(term, 0) + 0.5) / b_total
        lift = math.log(r_rate / b_rate)
        score = lift * math.log(1 + n)
        out.append((term, n, baseline.get(term, 0), round(lift, 2),
                    round(score, 2), docfreq.get(term, 0) if docfreq else 0))
    out.sort(key=lambda x: -x[4])
    return out[:top]


def questions(conn, days=30, top=30, buckets=None):
    """Question-shaped titles = the queries the space is trying to own."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = "SELECT title, url, source FROM articles WHERE published >= ?"
    params = [cutoff]
    if buckets:
        q += " AND bucket IN (%s)" % ",".join("?" * len(buckets))
        params += list(buckets)
    rows = conn.execute(q, params).fetchall()
    qs = []
    for r in rows:
        t = (r["title"] or "").strip()
        if QUESTION_RE.match(t) or t.endswith("?"):
            qs.append((t, r["source"], r["url"]))
    return qs[:top]


def source_share(conn, days=30):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT source, COUNT(*) c FROM articles WHERE published >= ? "
        "GROUP BY source ORDER BY c DESC", (cutoff,)).fetchall()
    return [(r["source"], r["c"]) for r in rows]


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def report(recent_days=7, baseline_days=45, buckets=None, label=None):
    """Build one report. buckets=None pools everything; pass a list to scope
    the analysis to one vertical. Mixing unrelated verticals in one corpus
    flattens the lift scores, so per-vertical reports are the default."""
    conn = db()
    now = datetime.now(timezone.utc)
    r_start, b_start = now - timedelta(days=recent_days), now - timedelta(days=baseline_days)

    rc, rdf, r_n = window_counts(conn, r_start, now, buckets)
    bc, bdf, b_n = window_counts(conn, b_start, r_start, buckets)

    os.makedirs(REPORT_DIR, exist_ok=True)
    slug = f"-{label}" if label else ""
    path = os.path.join(REPORT_DIR, f"{now:%Y-%m-%d}-topicwatch{slug}.md")

    heading = f" — {label}" if label else ""
    L = [f"# Topic watch{heading} — {now:%Y-%m-%d}", ""]
    L += [f"Window: last **{recent_days}d** ({r_n} posts) vs. prior "
          f"**{baseline_days - recent_days}d** ({b_n} posts).", ""]

    labels = {1: "Rising single terms", 2: "Rising phrases (2-word)",
              3: "Rising phrases (3-word)"}
    for n in (2, 3, 1):
        L += [f"## {labels[n]}", "",
              "`docs` = how many separate articles use it. Spread across several "
              "sources is the signal; a high count in one article is not.", "",
              "| term | now | before | docs | lift | score |",
              "|---|---:|---:|---:|---:|---:|"]
        for term, cnt, base, lift, score, dfq in rising(
                rc[n], bc[n], r_n, b_n, docfreq=rdf[n]):
            L.append(f"| {term} | {cnt} | {base} | {dfq} | {lift} | {score} |")
        L.append("")

    L += ["## Stable core (highest doc-frequency, last window)", "",
          "These are table stakes — if you don't cover them you look absent.", ""]
    for term, dfq in rdf[2].most_common(20):
        if dfq >= max(2, r_n * 0.1):
            L.append(f"- **{term}** — appears in {dfq}/{r_n} posts")
    L.append("")

    L += ["## Question headlines (last 30d)", "",
          "Each of these is a query someone decided was worth a whole page. "
          "This is your AI-SEO shortlist.", ""]
    for t, src, url in questions(conn, buckets=buckets):
        L.append(f"- {t}  \n  <sub>{src} — {url}</sub>")
    L.append("")

    L += ["## Publishing volume by source (30d)", ""]
    for src, c in source_share(conn):
        L.append(f"- {src}: {c}")

    open(path, "w").write("\n".join(L))
    print(f"[ok] wrote {path}")
    return path


def report_all(recent_days=7, baseline_days=45):
    """One report per vertical, plus a pooled one. Reading five focused
    reports beats reading one blurred report."""
    conn = db()
    buckets = [r[0] for r in conn.execute(
        "SELECT DISTINCT bucket FROM articles WHERE bucket IS NOT NULL")]
    groups = {}
    for b in buckets:
        groups.setdefault(VERTICAL_OF.get(b, b), []).append(b)

    paths = []
    for vertical, bs in sorted(groups.items()):
        n = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE bucket IN (%s)"
            % ",".join("?" * len(bs)), bs).fetchone()[0]
        if n < 15:
            print(f"[skip] {vertical}: only {n} docs — too thin to score")
            continue
        paths.append(report(recent_days, baseline_days, bs, vertical))
    paths.append(report(recent_days, baseline_days, None, "all"))
    return paths


def gaps(days=90):
    """What the space covers heavily that you barely touch."""
    conn = db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def terms_for(where, params):
        rows = conn.execute(
            f"SELECT title, body FROM articles WHERE published >= ? AND {where}",
            params).fetchall()
        c = Counter()
        for r in rows:
            c.update(set(doc_terms(r["body"], r["title"])[2]))
        return c, len(rows)

    theirs, tn = terms_for("bucket != 'own'", (cutoff,))
    mine, mn = terms_for("bucket = 'own'", (cutoff,))

    if mn == 0:
        print("[warn] No posts in the 'own' bucket. Add your blog feed to "
              "sources.json under \"own\" to get gap analysis.")
        return

    print(f"\nGaps — heavily covered by the space, thin on your blog "
          f"({tn} their posts vs {mn} yours):\n")
    rows = []
    for term, c in theirs.most_common(400):
        their_pct, my_pct = c / tn, mine.get(term, 0) / mn
        if their_pct > 0.08 and my_pct < their_pct / 3:
            rows.append((term, round(their_pct * 100), round(my_pct * 100)))
    for term, tp, mp in rows[:30]:
        print(f"  {term:<38} them {tp:>3}%   you {mp:>3}%")


# --------------------------------------------------------------------------
def init():
    if not os.path.exists(SOURCES_PATH):
        json.dump(DEFAULT_SOURCES, open(SOURCES_PATH, "w"), indent=2)
        print(f"[ok] wrote {SOURCES_PATH} — edit it before your first fetch.")
    else:
        print(f"[skip] {SOURCES_PATH} already exists.")
    db()
    print(f"[ok] db ready at {DB_PATH}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["init", "fetch", "report", "report-all",
                                   "run", "gaps", "discover"])
    p.add_argument("domain", nargs="?", help="domain for `discover`")
    p.add_argument("--recent", type=int, default=7)
    p.add_argument("--baseline", type=int, default=45)
    a = p.parse_args()

    if a.cmd == "init":
        init()
    elif a.cmd == "discover":
        if not a.domain:
            sys.exit("Usage: python topicwatch.py discover prophet.com")
        discover(a.domain)
    elif a.cmd == "fetch":
        fetch()
    elif a.cmd == "report":
        report(a.recent, a.baseline)
    elif a.cmd == "report-all":
        report_all(a.recent, a.baseline)
    elif a.cmd == "run":
        fetch()
        report_all(a.recent, a.baseline)
    elif a.cmd == "gaps":
        gaps()


if __name__ == "__main__":
    main()
