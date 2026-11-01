"""Crawler against a local fake of Microsoft Learn."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import refresh_docs as rd
from pbi_retrieval import DocsIndex

PAGES = {
    "/en-us/power-bi/": ("Power BI docs", "01/15/2026",
                         '<p>Welcome to Power BI documentation hub with many articles about reports and models. It applies to Power BI Desktop and the Power BI service, and the article explains prerequisites, configuration steps, limitations and troubleshooting for administrators and report authors.</p>'
                         '<a href="/en-us/power-bi/incremental">inc</a> <a href="/en-us/power-bi/rls#x">rls</a>'
                         '<a href="https://example.com/out">out</a> <a href="/en-us/azure/other">azure</a>'),
    "/en-us/power-bi/incremental": ("Incremental refresh | Microsoft Learn", "09/12/2026",
                                    "<h2>Define RangeStart</h2><p>Incremental refresh uses RangeStart and RangeEnd "
                                    "parameters that must be DateTime. Filter with &gt;= RangeStart and &lt; RangeEnd "
                                    "so rows are not duplicated across partitions.</p>"
                                    "<pre><code>Table.SelectRows(Source, each [d] &gt;= RangeStart)</code></pre>"),
    "/en-us/power-bi/rls": ("Row-level security", "03/01/2025",
                            "<p>Row-level security restricts data access for given users with DAX filters "
                            "evaluated per role. USERPRINCIPALNAME returns the signed-in user principal name. It applies to Power BI Desktop and the Power BI service, and the article explains prerequisites, configuration steps, limitations and troubleshooting for administrators and report authors.</p>"),
    "/en-us/azure/other": ("Azure", "01/01/2026", "<p>not in scope " + "x " * 200 + "</p>"),
}


def page_html(title, date, body):
    return (f"<html><head><title>{title}</title><meta name='ms.date' content='{date}'></head><body>"
            f"<nav>NAVIGATION JUNK</nav><header>top</header><main id='main'>{body}</main>"
            f"<footer>FOOTER JUNK</footer><script>var x = 1;</script></body></html>")


@pytest.fixture
def learn():
    hits = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            path = self.path.split("?")[0]
            key = path if path in PAGES else path + "/"
            if path == "/robots.txt":
                body, ctype = b"User-agent: *\nDisallow: /en-us/power-bi/private\n", "text/plain"
            elif key in PAGES:
                body, ctype = page_html(*PAGES[key]).encode(), "text/html; charset=utf-8"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", hits
    srv.shutdown()


def test_learn_page_extraction():
    p = rd.LearnPage()
    p.feed(page_html(*PAGES["/en-us/power-bi/incremental"]))
    assert p.ms_date == "09/12/2026" and rd.normalise_date(p.ms_date) == "2026-09-12"
    assert "## Define RangeStart" in p.text and "```\nTable.SelectRows(Source, each [d] >= RangeStart)\n```" in p.text
    assert "JUNK" not in p.text and "var x" not in p.text and "top" not in p.text


def test_crawl_scope_incremental_and_fresh(tmp_path, learn):
    base, hits = learn
    db = DocsIndex(tmp_path / "docs.sqlite")
    c = rd.Crawler(db, [base + "/en-us/power-bi/"], max_pages=50, delay=0, max_age_days=0, log=lambda *a: None)
    stats = c.run([base + "/en-us/power-bi/"])
    assert stats["updated"] == 3 and stats["failed"] == 0
    assert not any("/azure/" in h for h in hits)                    # out of scope never fetched
    top = db.search("incremental refresh RangeStart RangeEnd duplicated")[0]
    assert top["source"].endswith("/en-us/power-bi/incremental") and top["date"] == "2026-09-12"
    assert db.page(base + "/en-us/power-bi/incremental")["title"] == "Incremental refresh"

    # second run with max_age 0: everything refetched, nothing changed
    stats2 = rd.Crawler(db, [base + "/en-us/power-bi/"], delay=0, max_age_days=0, log=lambda *a: None).run(
        [base + "/en-us/power-bi/"])
    assert stats2["unchanged"] == 3 and stats2["updated"] == 0

    # third run within max_age: no page requests at all, links reused from the index
    hits.clear()
    stats3 = rd.Crawler(db, [base + "/en-us/power-bi/"], delay=0, max_age_days=7, log=lambda *a: None).run(
        [base + "/en-us/power-bi/"])
    assert stats3["fresh"] == 3 and stats3["fetched"] == 0
    assert [h for h in hits if h != "/robots.txt"] == []


def test_robots_respected(tmp_path, learn):
    base, hits = learn
    db = DocsIndex(tmp_path / "docs.sqlite")
    c = rd.Crawler(db, [base + "/en-us/power-bi/"], delay=0, log=lambda *a: None)
    c.run([base + "/en-us/power-bi/private"], follow=False)
    assert "/en-us/power-bi/private" not in hits


def test_cli_urls_and_stats(tmp_path, learn, capsys):
    base, _ = learn
    urls = tmp_path / "urls.txt"
    urls.write_text(f"# comment\n{base}/en-us/power-bi/rls\n", encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert rd.main(["--db", db, "--urls", str(urls), "--delay", "0"]) == 0
    assert rd.main(["--db", db, "--stats"]) == 0
    assert "'pages': 1" in capsys.readouterr().out
