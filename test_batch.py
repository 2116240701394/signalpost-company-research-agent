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
TREEM = "916208631"   # Treemballasje AS: registry lists a sister company's site
OTIS1 = "916276389"   # Otis AS: registry lists the parent's global site
OTIS2 = "916312345"   # Otis AS: registry lists a site that really is Otis AS

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
        <a href="https://group.com/careers/job-77">Group job</a>
        <a href="/karriere/livet-hos-alpha">Livet hos Alpha</a><a href="/karriere/studenter">Studenter</a>
        <a href="/karriere/selger-55555">Selger</a></body></html>"""),
    # landing-style subpages: company named in footer, but no JobPosting data and no ad cues -> must be rejected
    "https://alpha.no/karriere/livet-hos-alpha": (200, """<html><body><h1>Livet hos Alpha</h1><footer>Alpha AS 923 609 016</footer></body></html>"""),
    "https://alpha.no/karriere/studenter": (200, """<html><body><h1>Students and graduates</h1><p>Apply now to our programme</p><footer>Alpha AS</footer></body></html>"""),
    # ad without JSON-LD but with id, apply cue and company name -> accepted
    "https://alpha.no/karriere/selger-55555": (200, """<html><body><h1>Selger</h1><p>Søknadsfrist 1. desember. Alpha AS søker selger.</p></body></html>"""),
    "https://alpha.no/karriere/utvikler-123": (200, """<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Utvikler","hiringOrganization":{"@type":"Organization","name":"Alpha AS"}}</script></head><body><h1>Utvikler</h1></body></html>"""),
    "https://alpha.no/karriere/annen-1": (200, """<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Sjef","hiringOrganization":{"name":"Group Holding ASA"}}</script></head><body><h1>Sjef</h1></body></html>"""),
    # sister company's site: lists Treemballasje AS in the footer, but the domain is not Treemballasje's
    "https://rekonstrukt.no": (200, """<html><head><title>Rekonstrukt</title></head><body><h1>Rekonstrukt</h1>
        <a href="https://www.linkedin.com/company/rekonstrukt">in</a><a href="/nyheter/ny-sak-2025">Ny sak</a>
        <footer>© 2026 Rekonstrukt AS. Våre selskaper: Treemballasje AS, Emineo Gruppen</footer></body></html>"""),
    # parent's global site: brand word "Otis" everywhere, but never the full name "Otis AS"
    "https://otis.com": (200, """<html><head><title>Otis</title></head><body><h1>Otis</h1>
        <a href="https://www.youtube.com/user/OtisElevatorCompany">YouTube</a>
        <footer>© 2026 Otis Worldwide Corporation</footer></body></html>"""),
    # the real Otis AS site: full name in the footer, matching domain, but links to global social accounts
    "https://otis.no": (200, """<html><head><title>Otis Norge</title></head><body><h1>Otis</h1>
        <a href="https://www.youtube.com/user/OtisElevatorCompany">YouTube</a>
        <a href="https://www.linkedin.com/company/otis">Otis global</a>
        <a href="https://www.linkedin.com/company/otis-norge">Otis Norge</a>
        <footer>© 2026 Otis AS</footer></body></html>"""),
    # brand/group site: no org number, no exact name
    "https://group.com": (200, """<html><title>Group Holding</title><body><a href="/news">News</a><a href="/careers">Careers</a>
        <a href="https://linkedin.com/company/group">in</a><p>Welcome to Group Holding</p></body></html>"""),
}


def fake_fetch(url, deadline, accept="", max_bytes=0):
    u = url.rstrip("/") if url.count("/") > 2 else url
    if "organisasjonsnummer=" in url:
        return rb.Resp(200, url, json.dumps({"_embedded": {"enheter": [
            enhet(ALPHA, "Alpha AS", "www.alpha.no"),
            enhet(BRAND, "Brand Norge AS", "group.com", parent=PARENT),
            enhet(TREEM, "TREEMBALLASJE AS", "rekonstrukt.no"),
            enhet(OTIS1, "OTIS AS", "otis.com"),
            enhet(OTIS2, "OTIS AS", "www.otis.no")]}}), {})
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
        # parent's ad, NAV search, pagination and landing-style subpages are rejected
        self.assertEqual(sorted(j["url"] for j in r["jobs"]),
                         ["https://alpha.no/karriere/selger-55555", "https://alpha.no/karriere/utvikler-123"])
        self.assertEqual(r["social"], {"linkedin": "https://www.linkedin.com/company/alpha-as"})

    def test_group_site_not_used(self):
        r = self.run_batch([BRAND], budget=60)[0]
        self.assertFalse(r["website"]["verified"])
        self.assertEqual((r["social"], r["news"], r["jobs"]), ({}, [], []))

    def test_claims_about_another_company_are_left_out(self):
        treem, otis_global, otis_no = self.run_batch([TREEM, OTIS1, OTIS2], budget=60)
        # sister company's site: name only listed in a group footer, domain is not Treemballasje's
        self.assertFalse(treem["website"]["verified"])
        self.assertEqual((treem["social"], treem["news"], treem["jobs"]), ({}, [], []))
        # parent's global site: no "Otis AS" anywhere, so nothing is collected, not even its YouTube channel
        self.assertFalse(otis_global["website"]["verified"])
        self.assertEqual(otis_global["social"], {})
        # the real Otis AS site is accepted by name, but the global parent's handles are not
        self.assertTrue(otis_no["website"]["verified"])
        self.assertEqual(otis_no["website"]["evidence"], "name")
        self.assertEqual(otis_no["social"], {"linkedin": "https://www.linkedin.com/company/otis-norge"})

    def test_same_company_and_social_ties(self):
        self.assertTrue(rb.same_company("Otis Norge AS", "OTIS AS"))
        self.assertTrue(rb.same_company("Alpha AS", "ALPHA AS"))
        self.assertFalse(rb.same_company("Otis Elevator Company", "OTIS AS"))
        self.assertFalse(rb.same_company("Equinor Energy AS", "EQUINOR ASA"))
        c = {"name": "OTIS AS", "orgnr": OTIS2}
        tie = rb.social_ties_to_company
        self.assertFalse(tie("https://www.youtube.com/user/OtisElevatorCompany", c, "orgnr"))
        self.assertFalse(tie("https://www.linkedin.com/company/otis", c, "name"))   # bare brand, weak site proof
        self.assertTrue(tie("https://www.linkedin.com/company/otis", c, "orgnr"))   # bare brand, org number on site
        self.assertTrue(tie("https://www.linkedin.com/company/otis-norge", c, "name"))
        self.assertTrue(tie("https://www.facebook.com/otisas", c, "name"))

    def test_menu_labels_are_not_news(self):
        for label in ["See more news", "VIEW ALL", "Read more", "Les mer", "Alle nyheter", "Next page", "News archive", "Load more"]:
            self.assertTrue(rb.LABEL_RE.search(label), label)
        for title in ["Alpha vinner ny kunde", "Equinor third quarter 2026 results", "Hydro opens new plant"]:
            self.assertFalse(rb.LABEL_RE.search(title), title)

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
    def test_throttle_rejected_uses_retry_after_then_backoff(self):
        th = rb.Throttle(0.0)
        self.assertEqual(th.rejected({"Retry-After": "7"}, 1), 7.0)
        self.assertEqual(th.rejected({"retry-after": "500"}, 1), 60.0)  # capped
        w = th.rejected({}, 3)  # no header: 2**3 plus jitter
        self.assertTrue(8.0 <= w < 9.0)
        self.assertGreater(th.penalty, 0)  # later requests to this host are spaced out
        rb.Throttle(0.0).wait()  # works without a deadline (main.py has none)

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
