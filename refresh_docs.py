"""
refresh_docs.py - keep the local Microsoft Learn index current (Power BI ships monthly).

Crawls Learn pages under the Power BI-related prefixes, extracts the article body (headings,
paragraphs, lists, tables and code blocks) plus its `ms.date`, and upserts it into the DocsIndex
used by pbi_retrieval.HybridRetriever. Unchanged pages are detected by content hash; pages fetched
within --max-age-days are skipped, so a weekly run only touches what moved.

    python refresh_docs.py                       # default prefixes, up to 3000 pages
    python refresh_docs.py --max-pages 200 --prefix https://learn.microsoft.com/en-us/dax/
    python refresh_docs.py --urls my-pages.txt   # just these URLs (one per line)
    python refresh_docs.py --stats

Only outbound traffic: GET requests for public documentation pages. No model or user data is sent.
Honours robots.txt and HTTPS_PROXY. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from collections import deque
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

from pbi_retrieval import DEFAULT_DOCS_DB, DocsIndex

DEFAULT_PREFIXES = [
    "https://learn.microsoft.com/en-us/power-bi/",
    "https://learn.microsoft.com/en-us/dax/",
    "https://learn.microsoft.com/en-us/power-query/",
    "https://learn.microsoft.com/en-us/analysis-services/tmdl/",
    "https://learn.microsoft.com/en-us/rest/api/power-bi/",
    "https://learn.microsoft.com/en-us/fabric/fundamentals/direct-lake-overview",
]
USER_AGENT = "powerbi-llm-docs-refresh/1.0 (+local documentation index)"

_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form", "button", "svg", "noscript"}
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "table", "ul", "ol", "dl", "dt", "dd", "br",
               "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}


class LearnPage(HTMLParser):
    """Extract title, ms.date, links and the readable text of <main> (falls back to <body>)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.ms_date, self.links = "", "", []
        self._in_title = False
        self._skip = 0
        self._main_depth = 0      # >0 while inside <main>
        self._saw_main = False
        self._pre = 0
        self._main_parts, self._body_parts = [], []

    def _out(self, s: str):
        (self._main_parts if self._main_depth else self._body_parts).append(s)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("name") in ("ms.date", "updated_at") and not self.ms_date:
            self.ms_date = (a.get("content") or "").strip()
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag == "title":
            self._in_title = True
        if tag == "main":
            self._main_depth += 1
            self._saw_main = True
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre += 1
            self._out("\n\n```\n")
        elif tag in ("h1", "h2", "h3", "h4"):
            self._out("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._out("\n- ")
        elif tag in ("td", "th"):
            self._out(" | ")
        elif tag in _BLOCK_TAGS:
            self._out("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag == "pre" and self._pre:
            self._pre -= 1
            self._out("\n```\n\n")
        elif tag in ("p", "h1", "h2", "h3", "h4", "table", "ul", "ol"):
            self._out("\n")
        if tag == "main" and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self._out(data if self._pre else re.sub(r"\s+", " ", data))

    @property
    def text(self) -> str:
        raw = "".join(self._main_parts if self._saw_main else self._body_parts)
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def normalise_date(s: str) -> str:
    """Learn uses MM/DD/YYYY in ms.date; store ISO YYYY-MM-DD."""
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def canonical(url: str) -> str:
    url, _ = urldefrag(url)
    u = urlparse(url)
    return u._replace(query="").geturl().rstrip("/")


class Crawler:
    def __init__(self, index: DocsIndex, prefixes: list, max_pages: int = 3000, delay: float = 0.5,
                 max_age_days: float = 7.0, timeout: int = 30, respect_robots: bool = True, log=print):
        self.index, self.prefixes = index, [p.rstrip("/") for p in prefixes]
        self.max_pages, self.delay, self.timeout = max_pages, delay, timeout
        self.max_age = timedelta(days=max_age_days)
        self.respect_robots = respect_robots
        self.log = log
        self._robots = {}
        self.stats = {"fetched": 0, "updated": 0, "unchanged": 0, "fresh": 0, "failed": 0}

    def in_scope(self, url: str) -> bool:
        return any(url == p or url.startswith(p + "/") for p in self.prefixes)

    def _allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        u = urlparse(url)
        base = f"{u.scheme}://{u.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    rp.parse(r.read().decode("utf-8", "ignore").splitlines())
            except Exception:
                rp.parse([])  # unreachable robots.txt: allow, as most crawlers do
            self._robots[base] = rp
        return self._robots[base].can_fetch(USER_AGENT, url)

    def _is_fresh(self, url: str) -> bool:
        page = self.index.page(url)
        if not page or not page.get("fetched"):
            return False
        try:
            fetched = datetime.fromisoformat(page["fetched"])
        except ValueError:
            return False
        return datetime.now(timezone.utc) - fetched < self.max_age

    def fetch(self, url: str) -> LearnPage:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype:
                raise ValueError(f"not HTML ({ctype})")
            html = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
        page = LearnPage()
        page.feed(html)
        return page

    def run(self, seeds: list, follow: bool = True) -> dict:
        queue, seen = deque(canonical(s) for s in seeds), set()
        while queue and self.stats["fetched"] + self.stats["fresh"] < self.max_pages:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            if not self._allowed(url):
                continue
            if self._is_fresh(url):           # recently fetched: reuse its stored links, no request
                self.stats["fresh"] += 1
                if follow:
                    queue.extend(n for n in self.index.links(url) if self.in_scope(n) and n not in seen)
                continue
            try:
                page = self.fetch(url)
            except (urllib.error.URLError, ValueError, TimeoutError, OSError) as e:
                self.stats["failed"] += 1
                self.log(f"  fail  {url}: {e}")
                continue
            self.stats["fetched"] += 1
            text = page.text
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            links = sorted({n for n in (canonical(urljoin(url, h)) for h in page.links)
                            if n.startswith(("http://", "https://")) and self.in_scope(n)})
            old = self.index.page(url)
            if old and old.get("sha") == sha:
                self.index.touch(url, links)
                self.stats["unchanged"] += 1
            elif len(text) > 200:
                title = re.sub(r"\s*\|\s*Microsoft Learn\s*$", "", page.title.strip())
                n = self.index.upsert_page(url, title, text, normalise_date(page.ms_date), sha, links)
                self.stats["updated"] += 1
                self.log(f"  {n:>3}  {url}")
            if follow:
                queue.extend(n for n in links if n not in seen)
            if self.delay:
                time.sleep(self.delay)
        return self.stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Refresh the local Microsoft Learn index used for grounding.")
    ap.add_argument("--db", default=str(DEFAULT_DOCS_DB))
    ap.add_argument("--prefix", action="append", help="URL prefix to crawl (repeatable; default: Power BI set)")
    ap.add_argument("--urls", help="file with one URL per line: fetch these only, do not follow links")
    ap.add_argument("--max-pages", type=int, default=3000)
    ap.add_argument("--max-age-days", type=float, default=7.0, help="skip pages fetched more recently than this")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between requests")
    ap.add_argument("--no-robots", action="store_true")
    ap.add_argument("--stats", action="store_true", help="print index statistics and exit")
    a = ap.parse_args(argv)

    index = DocsIndex(a.db)
    if a.stats:
        print(index.stats())
        return 0
    if a.urls:
        seeds = [ln.strip() for ln in Path(a.urls).read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.startswith("#")]
        crawler = Crawler(index, seeds, a.max_pages, a.delay, a.max_age_days, respect_robots=not a.no_robots)
        stats = crawler.run(seeds, follow=False)
    else:
        prefixes = a.prefix or DEFAULT_PREFIXES
        crawler = Crawler(index, prefixes, a.max_pages, a.delay, a.max_age_days, respect_robots=not a.no_robots)
        stats = crawler.run(prefixes, follow=True)
    print(f"done: {stats}  index: {index.stats()}")
    return 0 if stats["failed"] < max(1, stats["fetched"]) else 1


if __name__ == "__main__":
    sys.exit(main())
