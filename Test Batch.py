import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ["no_proxy"] = "127.0.0.1,localhost"
import run_batch as rb

TODAY_OK = "2025-03-04"


def enhet(org, name, site, parent=None):
    j = {"organisasjonsnummer": org, "navn": name, "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
         "hjemmeside": site, "antallAnsatte": 12, "forretningsadresse": {"adresse": ["Gate 1"], "postnummer": "0150", "poststed": "OSLO"},
         "naeringskode1": {"kode": "62.010", "beskrivelse": "Programmering"}}
    if parent:
        j["overordnetEnhet"] = parent
    return j


ALPHA = "923609016"
BRAND = "914778271"
PARENT = "981276957"
GONE = "999999999"

PAGES = {
    "https://alpha.no": (200, """<html><title>Alpha</title><body>
        <a href="/nyheter">See more news</a><a href="/nyheter/ny-kunde-2025">Ny kunde</a>
        <a href="/produkter/alpha-pro">Alpha Pro</a><a href="/karriere">Karriere</a>
        <a href="https://www.linkedin.com/company/alpha-as">LinkedIn</a>
        <a href="https://www.linkedin.com/sharer?u=x">share</a>
        <footer>Alpha AS org.nr 923 609 016</footer></body></html>"""),
    "https://alpha.no/nyheter": (200, """<html><body><a href="/nyheter/ny-kunde-2025">Ny kunde</a>
        <a href="/nyheter/uten-dato">Uten dato</a><a href="/nyheter/endret">Endret</a><a href="/nyheter/page/2">Les mer</a></body></html>"""),
    "https://alpha.no/nyheter/ny-kunde-2025": (200, """<html><head><meta property="og:title" content="Alpha vinner ny kunde"><meta property="og:type" content="article">
        <meta property="article:published_time" content="2025-03-04T09:00:00+01:00"><meta property="article:modified_time" content="2026-01-01"></head><body></body></html>"""),
    "https://alpha.no/nyheter/uten-dato": (200, """<html><head><meta property="og:title" content="Artikkel uten dato"><meta property="og:type" content="article"></head></html>"""),
    "https://alpha.no/nyheter/endret": (200, """<html><head><meta property="og:title" content="Kun endret dato"><meta property="og:type" content="article">
        <meta property="og:updated_time" content="2025-05-05"></head></html>"""),
    "https://alpha.no/karriere": (200, """<html><body><a href="/karriere/utvikler-123">Utvikler</a><a href="/karriere/annen-1">Annen</a>
        <a href="https://arbeidsplassen.nav.no/stillinger?q=alpha">NAV-søk</a><a href="/karriere?page=2">Neste</a>
        <a href="https://group.com/careers/job-77">Group job</a></body></html>"""),
    "https://alpha.no/karriere/utvikler-123": (200, """<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Utvikler","hiringOrganization":{"@type":"Organization","name":"Alpha AS"}}</script></head><body><h1>Utvikler</h1></body></html>"""),
    "https://alpha.no/karriere/annen-1": (200, """<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Sjef","hiringOrganization":{"name":"Group Holding ASA"}}</script></head><body><h1>Sjef</h1></body></html>"""),
    # brand/group site: no org number, no exact name
    "https://group.com": (200, """<html><title>Group Holding</title><body><a href="/news">News</a><a href="/careers">Careers</a>
        <a href="https://linkedin.com/company/group">in</a><p>Welcome to Group Holding</p></body></html>"""),
}


def fake_fetch(url, deadline, accept="", max_bytes=0):
    u = url.rstrip("/") if url.count("/") > 2 else url
    if "organisasjonsnummer=" in url:
        return rb.Resp(200, url, json.dumps({"_embedded": {"enheter": [
            enhet(ALPHA, "Alpha AS", "www.alpha.no"),
            enhet(BRAND, "Brand Norge AS", "group.com", parent=PARENT)]}}), {})
    if url.endswith("/enheter/" + PARENT):
        return rb.Resp(200, url, json.dumps(enhet(PARENT, "Group Holding ASA", "group.com")), {})
    if url.endswith("/enheter/" + GONE) or url.endswith("/underenheter/" + GONE):
        return rb.Resp(404, url, "", {})
    key = u.replace("http://", "https://").replace("https://www.", "https://")
    if key in PAGES:
        s, b = PAGES[key]
        return rb.Resp(s, key, b, {})
    return rb.Resp(404, url, "", {})


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.orig = rb.FETCH
        rb.FETCH = fake_fetch

    def tearDown(self):
        rb.FETCH = self.orig

    def run_batch(self, lines, **kw):
        d = tempfile.mkdtemp()
        inp, out = os.path.join(d, "in.txt"), os.path.join(d, "out.jsonl")
        open(inp, "w").write("\n".join(lines))
        rb.run(inp, out, **kw)
        return [json.loads(l) for l in open(out)]

    def test_one_result_per_company_in_order(self):
        res = self.run_batch(["orgnr", ALPHA, BRAND, GONE, "not-a-number", ALPHA], budget=60, workers=3)
        self.assertEqual([r["status"] for r in res], ["ok", "ok", "not_found", "invalid_orgnr"])
        self.assertEqual(res[0]["orgnr"], ALPHA)

    def test_own_site_verified_news_jobs_social(self):
        r = self.run_batch([ALPHA], budget=60)[0]
        self.assertTrue(r["website"]["verified"])
        self.assertEqual(r["website"]["evidence"], "orgnr")
        self.assertEqual([n["url"] for n in r["news"]], ["https://alpha.no/nyheter/ny-kunde-2025"])
        self.assertEqual(r["news"][0]["published"], TODAY_OK)  # modified date ignored
        self.assertEqual([j["url"] for j in r["jobs"]], ["https://alpha.no/karriere/utvikler-123"])  # parent's ad, NAV search, pagination rejected
        self.assertEqual(r["social"], {"linkedin": "https://www.linkedin.com/company/alpha-as"})

    def test_group_site_not_used(self):
        r = self.run_batch([BRAND], budget=60)[0]
        self.assertFalse(r["website"]["verified"])
        self.assertEqual((r["social"], r["news"], r["jobs"]), ({}, [], []))

    def test_budget_exhausted_still_one_result_each(self):
        res = self.run_batch([ALPHA, BRAND], budget=3.2, workers=1)
        self.assertEqual(len(res), 2)


class Flaky(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        Flaky.hits += 1
        if Flaky.hits <= 2:
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class RateLimitTest(unittest.TestCase):
    def test_429_is_retried_with_delay(self):
        srv = HTTPServer(("127.0.0.1", 0), Flaky)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        t = time.monotonic()
        r = rb.http_get(f"http://127.0.0.1:{srv.server_port}/x", time.monotonic() + 30)
        srv.shutdown()
        self.assertEqual(r.status, 200)
        self.assertEqual(Flaky.hits, 3)
        self.assertGreaterEqual(time.monotonic() - t, 2.0)


if __name__ == "__main__":
    unittest.main()