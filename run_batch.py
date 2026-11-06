#!/usr/bin/env python3
"""Signalpost batch runner.

One command: a file of Norwegian organisation numbers in, one result per company out.

    python run_batch.py orgnrs.txt results.jsonl --budget 900

- Needs no API keys and no Ollama. Python standard library only.
- Always writes exactly one result per distinct input company, in input order,
  even when the budget runs out (those get status "timeout" or "partial").
- Slows down on HTTP 429 (honours Retry-After, backs off, throttles per host).
- Website, news and jobs are only reported when they pass the checks below.
"""
import argparse
import datetime as dt
import email.utils
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, wait
from html.parser import HTMLParser

BRREG = "https://data.brreg.no/enhetsregisteret/api"
UA = "SignalpostResearchAgent/2.1 (+https://github.com/2116240701394/signalpost-company-research-agent)"

Resp = namedtuple("Resp", "status url text headers")


class FetchError(Exception):
    pass


# --------------------------------------------------------------------------
# HTTP with per-host throttling and 429 handling
# --------------------------------------------------------------------------
class Throttle:
    """Spaces out requests to one host; widens the gap after 429/503."""

    def __init__(self, min_interval):
        self.lock = threading.Lock()
        self.next_ok = 0.0
        self.min_interval = min_interval
        self.penalty = 0.0

    def wait(self, deadline):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_ok)
            self.next_ok = start + self.min_interval + self.penalty
        delay = start - now
        if delay > 0:
            if time.monotonic() + delay > deadline:
                raise TimeoutError("run budget exhausted")
            time.sleep(delay)

    def slow_down(self, seconds):
        with self.lock:
            self.penalty = min(max(self.penalty * 2, 0.5), 5.0)
            self.next_ok = max(self.next_ok, time.monotonic() + seconds)

    def speed_up(self):
        with self.lock:
            self.penalty = self.penalty * 0.9 if self.penalty > 0.05 else 0.0


_throttles = {}
_throttles_lock = threading.Lock()


def throttle_for(host):
    with _throttles_lock:
        if host not in _throttles:
            _throttles[host] = Throttle(0.05 if host.endswith("brreg.no") else 0.3)
        return _throttles[host]


def _retry_after(headers, default):
    v = None
    for k, val in (headers or {}).items():
        if k.lower() == "retry-after":
            v = val
    if v:
        try:
            return min(float(v), 60.0)
        except ValueError:
            try:
                d = email.utils.parsedate_to_datetime(v)
                return max(0.0, min((d - dt.datetime.now(d.tzinfo)).total_seconds(), 60.0))
            except Exception:
                pass
    return default


def _decode(data, ctype):
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I)
    for enc in ([m.group(1)] if m else []) + ["utf-8"]:
        try:
            return data.decode(enc, "replace")
        except LookupError:
            continue
    return data.decode("utf-8", "replace")


def _sleep(seconds, deadline):
    if time.monotonic() + seconds > deadline - 0.5:
        raise TimeoutError("run budget exhausted")
    time.sleep(seconds)


def http_get(url, deadline, accept="text/html,application/json;q=0.9,*/*;q=0.5", max_bytes=1_500_000):
    host = urllib.parse.urlsplit(url).hostname or ""
    th = throttle_for(host)
    attempt = 0
    while True:
        attempt += 1
        th.wait(deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            raise TimeoutError("run budget exhausted")
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": accept, "Accept-Language": "nb,no;q=0.9,en;q=0.8"}
        )
        backoff = min(2 ** attempt + random.random(), 30)
        try:
            with urllib.request.urlopen(req, timeout=min(15, remaining)) as r:
                data = r.read(max_bytes)
                th.speed_up()
                return Resp(r.status, r.geturl(), _decode(data, r.headers.get("Content-Type", "")), dict(r.headers))
        except urllib.error.HTTPError as e:
            hdrs = dict(e.headers or {})
            if e.code == 429 or e.code == 503:
                wait_s = _retry_after(hdrs, backoff)
                th.slow_down(wait_s)
                if attempt >= 7:
                    return Resp(e.code, url, "", hdrs)
                _sleep(wait_s, deadline)
                continue
            if e.code >= 500 and attempt < 3:
                _sleep(backoff, deadline)
                continue
            try:
                body = _decode(e.read(100_000), hdrs.get("Content-Type", ""))
            except Exception:
                body = ""
            return Resp(e.code, url, body, hdrs)
        except (urllib.error.URLError, OSError, ValueError) as e:
            if attempt < 3:
                _sleep(min(backoff, 4), deadline)
                continue
            raise FetchError(str(e))


FETCH = http_get  # replaced in tests


# --------------------------------------------------------------------------
# HTML helpers
# --------------------------------------------------------------------------
class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.h1 = ""
        self.meta = {}
        self.links = []  # (href, text)
        self.jsonld = []
        self.times = []
        self.text = []
        self._title = False
        self._h1 = False
        self._skip = 0
        self._a = None
        self._ld = False
        self._ldbuf = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "title":
            self._title = True
        elif tag == "meta":
            k = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if k and "content" in a:
                self.meta.setdefault(k, a["content"])
        elif tag == "a" and a.get("href"):
            self._a = {"href": a["href"], "text": []}
        elif tag == "script":
            if a.get("type", "").lower() == "application/ld+json":
                self._ld = True
                self._ldbuf = []
            else:
                self._skip += 1
        elif tag == "style":
            self._skip += 1
        elif tag == "time" and a.get("datetime"):
            self.times.append(
                {"datetime": a["datetime"], "itemprop": a.get("itemprop", "").lower(), "class": a.get("class", "").lower()}
            )
        elif tag == "h1" and not self.h1:
            self._h1 = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._title = False
        elif tag == "a" and self._a is not None:
            self.links.append((self._a["href"], " ".join("".join(self._a["text"]).split())))
            self._a = None
        elif tag == "script":
            if self._ld:
                self.jsonld.append("".join(self._ldbuf))
                self._ld = False
            elif self._skip:
                self._skip -= 1
        elif tag == "style" and self._skip:
            self._skip -= 1
        elif tag == "h1":
            self._h1 = False

    def handle_data(self, data):
        if self._ld:
            self._ldbuf.append(data)
            return
        if self._skip:
            return
        if self._title:
            self.title += data
        if self._h1:
            self.h1 += data
        if self._a is not None:
            self._a["text"].append(data)
        self.text.append(data)


def parse_page(html):
    p = PageParser()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        pass
    p.h1 = " ".join(p.h1.split())
    p.title = " ".join(p.title.split())
    p.body = " ".join(" ".join(p.text).split())
    return p


def abs_links(page, base_url):
    out, seen = [], set()
    for href, text in page.links:
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        u = urllib.parse.urljoin(base_url, href)
        u, _ = urllib.parse.urldefrag(u)
        if not u.startswith(("http://", "https://")) or u in seen:
            continue
        seen.add(u)
        out.append((u, text))
    return out


def norm_host(h):
    h = (h or "").lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def host_of(url):
    return norm_host(urllib.parse.urlsplit(url).hostname)


def same_site(url, base):
    h = host_of(url)
    return h == base or h.endswith("." + base)


def ld_nodes(blocks):
    for b in blocks:
        try:
            data = json.loads(b, strict=False)
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                yield x
                if "@graph" in x:
                    stack.append(x["@graph"])


def ld_types(node):
    t = node.get("@type")
    return [s.lower() for s in (t if isinstance(t, list) else [t]) if isinstance(s, str)]


# --------------------------------------------------------------------------
# Identity matching
# --------------------------------------------------------------------------
LEGAL = {"as", "asa", "sa", "ans", "da", "ba", "enk", "nuf", "kf", "sf", "fkf", "iks", "stiftelse"}


def norm_text(s):
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


def name_forms(name):
    full = norm_text(name)
    toks = full.split()
    while toks and toks[-1] in LEGAL:
        toks.pop()
    stem = " ".join(toks)
    forms = {full}
    if len(stem) >= 4:
        forms.add(stem)
    return {f for f in forms if f}


def name_in(text, name):
    t = norm_text(text)
    return any(re.search(r"(?<!\w)" + re.escape(f) + r"(?!\w)", t) for f in name_forms(name))


def orgnr_in(text, orgnr):
    pat = r"(?<!\d)" + r"[\s.]?".join([orgnr[:3], orgnr[3:6], orgnr[6:]]) + r"(?!\d)"
    return re.search(pat, text or "") is not None


def evidence(text, orgnr, name):
    if orgnr_in(text, orgnr):
        return "orgnr"
    if name and name_in(text, name):
        return "name"
    return None


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------
def parse_date(s):
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", s)
    try:
        if m:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        else:
            m = re.match(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$", s)
            if not m:
                return None
            d = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None
    if d > dt.date.today() or d.year < 1995:
        return None
    return d.isoformat()


ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "report", "techarticle", "pressrelease"}
PUBLISHED_META = ("article:published_time", "og:article:published_time", "datepublished", "pubdate",
                  "dc.date.issued", "dc.date.created", "publish_date", "article.published")


def published_date(page):
    """Publish date only. dateModified, og:updated_time, feed 'updated' are ignored."""
    for node in ld_nodes(page.jsonld):
        if set(ld_types(node)) & ARTICLE_TYPES:
            d = parse_date(node.get("datePublished"))
            if d:
                return d, "jsonld"
    for k in PUBLISHED_META:
        d = parse_date(page.meta.get(k))
        if d:
            return d, "meta"
    for t in page.times:
        cls = t["class"]
        if re.search(r"modif|updat", cls):
            continue
        if t["itemprop"] == "datepublished" or re.search(r"publish|posted|entry-date|post-date", cls):
            d = parse_date(t["datetime"])
            if d:
                return d, "time_tag"
    return None, None


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
def valid_org_checksum(o):
    w = [3, 2, 7, 6, 5, 4, 3, 2]
    s = sum(int(c) * k for c, k in zip(o[:8], w))
    r = 11 - s % 11
    r = 0 if r == 11 else r
    return r != 10 and r == int(o[8])


def read_orgnrs(path):
    """Returns list of (orgnr_or_None, raw). Dedupes while keeping order."""
    out, seen = [], set()
    with open(path, encoding="utf-8-sig") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.search(r"(?<!\d)(\d{3}[\s.]?\d{3}[\s.]?\d{3})(?!\d)", line)
            if m:
                o = re.sub(r"\D", "", m.group(1))
            elif i == 0:
                continue  # header line
            else:
                o = None
            key = o or ("raw:" + line)
            if key in seen:
                continue
            seen.add(key)
            out.append((o, line))
    return out


def addr_of(j):
    a = j.get("forretningsadresse") or j.get("beliggenhetsadresse") or j.get("postadresse") or {}
    if not a:
        return None
    parts = list(a.get("adresse") or []) + [a.get("postnummer"), a.get("poststed")]
    return ", ".join(p for p in parts if p) or None


def registry_fields(j, kind):
    nk = j.get("naeringskode1") or {}
    of = j.get("organisasjonsform") or {}
    return {
        "name": j.get("navn"),
        "unit_type": kind,
        "org_form": of.get("kode"),
        "org_form_description": of.get("beskrivelse"),
        "address": addr_of(j),
        "industry_code": nk.get("kode"),
        "industry": nk.get("beskrivelse"),
        "employees": j.get("antallAnsatte"),
        "registered_date": j.get("registreringsdatoEnhetsregisteret") or j.get("oppstartsdato"),
        "bankrupt": j.get("konkurs"),
        "under_liquidation": bool(j.get("underAvvikling") or j.get("underTvangsavviklingEllerTvangsopplosning")),
        "deleted_date": j.get("slettedato"),
        "registry_website": j.get("hjemmeside"),
        "parent_orgnr": j.get("overordnetEnhet"),
    }


def prefetch_registry(orgnrs, deadline):
    found = {}
    for kind in ("enheter", "underenheter"):
        todo = [o for o in orgnrs if o not in found]
        for i in range(0, len(todo), 50):
            chunk = todo[i:i + 50]
            url = f"{BRREG}/{kind}?organisasjonsnummer={','.join(chunk)}&size=100"
            try:
                r = FETCH(url, deadline, "application/json")
                if r.status != 200:
                    continue
                for e in (json.loads(r.text).get("_embedded") or {}).get(kind, []):
                    found[e["organisasjonsnummer"]] = (kind[:-1].replace("enhet", "enhet"), e)
            except TimeoutError:
                return found
            except Exception:
                continue
    return found


def fetch_registry_single(orgnr, deadline):
    """Returns (kind, json, status) with status in ok/not_found/deleted."""
    for kind in ("enheter", "underenheter"):
        r = FETCH(f"{BRREG}/{kind}/{orgnr}", deadline, "application/json")
        if r.status == 200:
            return kind[:-1], json.loads(r.text), "ok"
        if r.status == 410:
            try:
                return kind[:-1], json.loads(r.text), "deleted"
            except Exception:
                return kind[:-1], {}, "deleted"
        if r.status in (404,):
            continue
        raise FetchError(f"registry HTTP {r.status}")
    return None, None, "not_found"


# --------------------------------------------------------------------------
# Website, social, news, jobs
# --------------------------------------------------------------------------
def candidate_site_url(raw):
    if not raw:
        return None
    tok = re.split(r"[\s,;]+", raw.strip())[0]
    if not tok:
        return None
    if not re.match(r"https?://", tok, re.I):
        tok = "https://" + tok
    return tok


def get_page(url, deadline):
    r = FETCH(url, deadline)
    if r.status != 200 or not r.text:
        return None, r
    return parse_page(r.text), r


SUPPORT_PATH = re.compile(r"kontakt|contact|om-oss|about|personvern|privacy|impressum|vilkar|terms|omoss", re.I)


def check_website(c, deadline, notes):
    """Returns dict with url/verified/evidence/reason plus pages for reuse."""
    raw = c["registry_website"]
    info = {"registry_value": raw, "url": None, "verified": False, "evidence": None, "reason": None}
    url = candidate_site_url(raw)
    if not url:
        info["reason"] = "no website in registry"
        return info, None, None
    page, r = None, None
    sp = urllib.parse.urlsplit(url)
    alt_host = sp.netloc[4:] if sp.netloc.lower().startswith("www.") else "www." + sp.netloc
    alt = urllib.parse.urlunsplit((sp.scheme, alt_host, sp.path, sp.query, ""))
    probe_deadline = min(deadline, time.monotonic() + 20)  # a dead site must not eat the whole company allowance
    for u in [url, url.replace("https://", "http://", 1), alt, alt.replace("https://", "http://", 1)]:
        if time.monotonic() >= probe_deadline - 0.5:
            break
        try:
            page, r = get_page(u, probe_deadline)
        except (FetchError, TimeoutError):
            if time.monotonic() >= deadline - 0.5:
                raise
            continue
        if page:
            break
    if not page:
        info["reason"] = "website not reachable"
        return info, None, None
    final = r.url
    base = host_of(final)
    info["url"] = final
    orgnr, name = c["orgnr"], c["name"]

    parent_host = c.get("_parent_host")
    group_site = bool(parent_host and parent_host == base)

    texts = [page.body + " " + page.title]
    ev = evidence(texts[0], orgnr, name)
    if ev != "orgnr":
        extra = [u for u, t in abs_links(page, final) if same_site(u, base) and (SUPPORT_PATH.search(urllib.parse.urlsplit(u).path) or SUPPORT_PATH.search(t))]
        for u in extra[:3]:
            try:
                p2, _ = get_page(u, deadline)
            except FetchError:
                continue
            if p2:
                ev2 = evidence(p2.body, orgnr, name)
                if ev2 == "orgnr" or (ev2 and not ev):
                    ev = ev2
                if ev == "orgnr":
                    break
    if group_site and ev != "orgnr":
        info["reason"] = "site belongs to parent group and does not show this company's org number"
        return info, page, final
    if ev:
        info["verified"] = True
        info["evidence"] = ev
        return info, page, final
    info["reason"] = "org number and exact name not found on site (possible group or brand site)"
    return info, page, final


SOCIAL = [
    ("linkedin", r"linkedin\.com/(company|school)/[^/?#]+"),
    ("facebook", r"facebook\.com/(?!sharer|share|plugins|dialog|tr\b)[^/?#]+"),
    ("instagram", r"instagram\.com/(?!p/|explore)[^/?#]+"),
    ("x", r"(?:x|twitter)\.com/(?!intent|share|home)[^/?#]+"),
    ("youtube", r"youtube\.com/(@|channel/|c/|user/)[^/?#]+"),
]


def extract_social(page, final):
    out = {}
    for u, _ in abs_links(page, final):
        for k, pat in SOCIAL:
            if k not in out and re.search(pat, u, re.I) and "share" not in u.lower():
                out[k] = u
    return out


NEWS_SEG = re.compile(r"(nyheter|nyhet|news|presse|pressemeldinger|pressemelding|press|aktuelt|blogg|blog|artikler|siste-nytt)", re.I)
PRODUCT_SEG = re.compile(r"(produkt|produkter|product|products|tjenester|tjeneste|services|service|losninger|solutions|priser|pricing|shop|butikk|kategori|category|tag|tags)", re.I)
NOT_SLUG = re.compile(r"(page|side|p|archive|arkiv|\d{1,4})", re.I)
LABEL_RE = re.compile(
    r"\b(see|view|read|show|load)\s+(more|all)\b|\b(les|vis|se)\s+(mer|flere|alle)\b|\bmore news\b|\bflere nyheter\b|\balle nyheter\b|\ball news\b|\barchive\b|\barkiv\b|^\s*(next|previous|neste|forrige)\b",
    re.I,
)
GENERIC_TITLES = {"news", "nyheter", "blog", "blogg", "press", "presse", "aktuelt", "home", "forside", "pressemeldinger"}


def segs(url):
    return [s for s in urllib.parse.urlsplit(url).path.split("/") if s]


def is_news_index(url):
    s = segs(url)
    return 0 < len(s) <= 2 and any(NEWS_SEG.fullmatch(x) for x in s) and not any(PRODUCT_SEG.fullmatch(x.lower()) for x in s)


def is_article_url(url):
    s = segs(url)
    if len(s) < 2 or "page=" in urllib.parse.urlsplit(url).query:
        return False
    if any(PRODUCT_SEG.fullmatch(x.lower()) for x in s):
        return False
    if not any(NEWS_SEG.fullmatch(x) for x in s[:-1]):
        return False
    return not NOT_SLUG.fullmatch(s[-1])


def clean_title(page):
    t = page.meta.get("og:title") or page.h1 or page.title
    parts = re.split(r"\s+[|–—-]\s+", t.strip())
    t = parts[0] if parts and len(parts[0]) >= 8 else t.strip()
    return " ".join(t.split())


def find_news(page, final, base, deadline, notes):
    index_urls, art_urls = [], []
    for u, text in abs_links(page, final):
        if not same_site(u, base):
            continue
        if LABEL_RE.search(text or ""):
            if is_news_index(u) and u not in index_urls:
                index_urls.append(u)
            continue
        if is_article_url(u):
            art_urls.append(u)
        elif is_news_index(u) and u not in index_urls:
            index_urls.append(u)
    for iu in sorted(index_urls, key=lambda x: len(segs(x)))[:2]:
        try:
            ip, ir = get_page(iu, deadline)
        except FetchError:
            continue
        if ip:
            for u, text in abs_links(ip, ir.url):
                if same_site(u, base) and not LABEL_RE.search(text or "") and is_article_url(u) and u != iu:
                    art_urls.append(u)
    items, seen, rejected = [], set(), 0
    for u in art_urls:
        if u in seen:
            continue
        seen.add(u)
        if len(seen) > 8:
            break
        try:
            ap, _ = get_page(u, deadline)
        except FetchError:
            continue
        if not ap:
            continue
        title = clean_title(ap)
        d, how = published_date(ap)
        article_signal = bool(set(t for n in ld_nodes(ap.jsonld) for t in ld_types(n)) & ARTICLE_TYPES) or ap.meta.get("og:type", "").lower() == "article"
        is_product = any("product" in ld_types(n) for n in ld_nodes(ap.jsonld)) and not article_signal
        if (not title or title.lower() in GENERIC_TITLES or LABEL_RE.search(title) or not d or is_product
                or not (article_signal or is_article_url(u))):
            rejected += 1
            continue
        items.append({"title": title, "url": u, "published": d, "date_source": how})
    if rejected:
        notes.append(f"news: rejected {rejected} candidate(s) without title or explicit publish date")
    items.sort(key=lambda i: i["published"], reverse=True)
    return items[:5]


JOB_PATH = re.compile(r"karriere|career|(^|/)jobs?(/|$)|stilling|ledige|rekrutter|vacanc|work-with|join-us|bli-med", re.I)
JOB_AD_PATH = re.compile(r"/(jobs?|job|stilling|stillinger|position|positions|vacancy|vacancies|career|careers|karriere|ledig-stilling)/[^/?#]+", re.I)
ATS_HOSTS = ("webcruiter", "teamtailor", "easycruit", "recman", "varbi", "hrmanager", "lever.co", "greenhouse.io", "workable", "jobylon",
             "personio", "smartrecruiters", "jobbnorge", "reachmee", "cvpartner", "finn.no", "arbeidsplassen.nav.no")
JOB_LABEL = re.compile(r"alle stillinger|se alle|see all|open positions|ledige stillinger|search|søk|vis alle|all jobs|all vacancies", re.I)
NAV_AD = re.compile(r"arbeidsplassen\.nav\.no/stillinger/stilling/[0-9a-f-]{8,}", re.I)
SEARCHY = re.compile(r"(^|&)(q|query|search|sok|s|keyword|keywords|page|filter)=", re.I)


AD_ID = re.compile(r"\d{5,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
APPLY_CUE = re.compile(r"\b(apply now|apply here|apply for this|application deadline|søknadsfrist|søk på stillingen|send søknad)\b", re.I)
LANDING_TITLE = re.compile(
    r"careers?|karriere|life at|graduates?|students?|trainee|internship|apprentice|vacancies|job opportunit|"
    r"ledige stillinger|meet our|recruitment|rekruttering|why work|working at|jobs at", re.I)


def looks_like_single_ad(url, page):
    path = urllib.parse.urlsplit(url).path
    return bool(AD_ID.search(path)) and bool(APPLY_CUE.search(page.body)) and not LANDING_TITLE.search(page.h1 or "")


def is_ats(url):
    h = urllib.parse.urlsplit(url).hostname or ""
    return any(a in h for a in ATS_HOSTS)


def looks_like_ad(url, landing_urls, base):
    sp = urllib.parse.urlsplit(url)
    h = norm_host(sp.hostname)
    if h.endswith("nav.no") or h == "finn.no":
        return bool(NAV_AD.search(url)) or (h == "finn.no" and "/job/ad/" in sp.path)
    if SEARCHY.search(sp.query or ""):
        return False
    if not (same_site(url, base) or is_ats(url)):
        return False
    for lu in landing_urls:
        lp = urllib.parse.urlsplit(lu).path.rstrip("/")
        if lp and sp.path.startswith(lp + "/") and len(sp.path.rstrip("/")) > len(lp) + 1 and host_of(lu) == h:
            return True
    return bool(JOB_AD_PATH.search(sp.path))


def find_jobs(c, page, final, base, deadline, notes):
    landing, ats = [], []
    for u, text in abs_links(page, final):
        if same_site(u, base) and (JOB_PATH.search(urllib.parse.urlsplit(u).path) or JOB_PATH.search(text or "")):
            if not SEARCHY.search(urllib.parse.urlsplit(u).query or ""):
                landing.append(u)
        elif is_ats(u) and not host_of(u).endswith("nav.no"):
            ats.append(u)
    landing = sorted(set(landing), key=lambda x: len(segs(x)))[:2]
    ats = list(dict.fromkeys(ats))[:2]
    cands = []
    pages = [(final, page)]
    for lu in landing + ats[:1]:
        try:
            lp, lr = get_page(lu, deadline)
        except FetchError:
            continue
        if lp:
            pages.append((lr.url, lp))
    for pu, pg in pages:
        for u, text in abs_links(pg, pu):
            if JOB_LABEL.search(text or ""):
                continue
            if u not in landing and u != pu and looks_like_ad(u, landing + ats, base):
                cands.append(u)
    cands = list(dict.fromkeys(cands))[:10]
    own_name, orgnr = c["name"], c["orgnr"]
    parent_name = c.get("_parent_name")
    jobs, rejected = [], 0
    for u in cands:
        try:
            ap, _ = get_page(u, deadline)
        except FetchError:
            continue
        if not ap:
            continue
        postings = [n for n in ld_nodes(ap.jsonld) if "jobposting" in ld_types(n)]
        if len(postings) > 1:
            rejected += 1
            continue
        title, ok = None, False
        if postings:
            n = postings[0]
            title = n.get("title") or n.get("name")
            org = n.get("hiringOrganization")
            org_name = org.get("name") if isinstance(org, dict) else org
            if isinstance(org_name, str) and org_name.strip():
                ok = name_in(org_name, own_name) or name_in(own_name, org_name) or orgnr_in(json.dumps(org) if isinstance(org, dict) else "", orgnr)
            else:
                ok = evidence(ap.body, orgnr, own_name) is not None
        else:
            # Without JobPosting data, only accept a page that clearly is one ad:
            # an id in the URL, an apply/deadline cue, a non-landing title, and the company named.
            title = ap.h1
            ok = looks_like_single_ad(u, ap) and evidence(ap.body, orgnr, own_name) is not None
        if ok and parent_name and not evidence(ap.body, orgnr, own_name) and name_in(ap.body, parent_name) and not postings:
            ok = False
        if not ok or not title or str(title).strip().lower() in GENERIC_TITLES:
            rejected += 1
            continue
        jobs.append({"title": " ".join(str(title).split()), "url": u})
    if rejected:
        notes.append(f"jobs: rejected {rejected} candidate(s) (listing page, other organisation or no evidence it is this company's ad)")
    seen, uniq = set(), []
    for j in jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            uniq.append(j)
    return uniq[:5]


# --------------------------------------------------------------------------
# Per company
# --------------------------------------------------------------------------
def empty_result(orgnr, raw=None):
    return {
        "orgnr": orgnr, "input": raw, "status": "error", "name": None, "unit_type": None, "org_form": None,
        "address": None, "industry_code": None, "industry": None, "employees": None, "registered_date": None,
        "bankrupt": None, "under_liquidation": None,
        "website": {"registry_value": None, "url": None, "verified": False, "evidence": None, "reason": None},
        "social": {}, "news": [], "jobs": [], "notes": [], "source_url": None,
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def research(orgnr, raw, prefetched, deadline, opts):
    res = empty_result(orgnr, raw)
    notes = res["notes"]
    if orgnr is None or not re.fullmatch(r"\d{9}", orgnr):
        res["status"] = "invalid_orgnr"
        notes.append("not a 9-digit organisation number")
        return res
    if not valid_org_checksum(orgnr):
        notes.append("organisation number fails the mod-11 checksum")
    try:
        if orgnr in prefetched:
            kind, j = prefetched[orgnr]
            st = "ok"
        else:
            kind, j, st = fetch_registry_single(orgnr, deadline)
    except TimeoutError:
        res["status"] = "timeout"
        notes.append("run budget exhausted before registry lookup")
        return res
    except Exception as e:
        res["status"] = "error"
        notes.append(f"registry lookup failed: {e}")
        return res
    if st == "not_found":
        res["status"] = "not_found"
        notes.append("organisation number not found in Enhetsregisteret")
        return res
    res["source_url"] = f"{BRREG}/{kind}er/{orgnr}"
    c = registry_fields(j or {}, kind)
    c["orgnr"] = orgnr
    res.update({k: v for k, v in c.items() if k in res})
    res["website"]["registry_value"] = c["registry_website"]
    if st == "deleted":
        res["status"] = "deleted"
        notes.append("unit is deleted in the registry")
        return res
    res["status"] = "ok"
    if opts.get("no_web"):
        return res
    try:
        if c.get("parent_orgnr"):
            try:
                _, pj, pst = fetch_registry_single(str(c["parent_orgnr"]), deadline)
                if pst == "ok" and pj:
                    c["_parent_name"] = pj.get("navn")
                    pu = candidate_site_url(pj.get("hjemmeside"))
                    c["_parent_host"] = host_of(pu) if pu else None
            except FetchError:
                pass
        info, page, final = check_website(c, deadline, notes)
        res["website"].update(info)
        if info["verified"] and page:
            base = host_of(final)
            res["social"] = extract_social(page, final)
            res["news"] = find_news(page, final, base, deadline, notes)
            res["jobs"] = find_jobs(c, page, final, base, deadline, notes)
        elif info["url"]:
            notes.append("website not verified as this company's own: social links, news and jobs not collected")
    except TimeoutError:
        res["status"] = "partial"
        notes.append("run budget exhausted during website, news or jobs collection")
    except Exception as e:
        res["status"] = "partial"
        notes.append(f"website step failed: {e}")
    return res


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def write_results(path, results, fmt):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if fmt == "json":
            json.dump(results, f, ensure_ascii=False, indent=2)
        else:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def run(input_path, output_path, budget=600.0, workers=6, fmt="jsonl", no_web=False):
    start = time.monotonic()
    global_deadline = start + budget - 3
    items = read_orgnrs(input_path)
    valid = [o for o, _ in items if o]
    prefetched = prefetch_registry(valid, min(global_deadline, start + budget * 0.25)) if valid else {}
    results = {}
    lock = threading.Lock()
    state = {"left": len(items)}
    partial_path = output_path + ".partial"
    pf = open(partial_path, "w", encoding="utf-8")

    def task(idx, o, raw):
        with lock:
            n_left = max(1, state["left"])
        now = time.monotonic()
        share = (global_deadline - now) * workers / n_left
        dl = min(global_deadline, now + max(8.0, min(60.0, share)))
        try:
            r = research(o, raw, prefetched, dl, {"no_web": no_web})
        except Exception as e:  # never lose a company
            r = empty_result(o, raw)
            r["notes"].append(f"unexpected error: {e}")
        with lock:
            results[idx] = r
            state["left"] -= 1
            pf.write(json.dumps(r, ensure_ascii=False) + "\n")
            pf.flush()
        return r

    ex = ThreadPoolExecutor(max_workers=workers)
    futs = [ex.submit(task, i, o, raw) for i, (o, raw) in enumerate(items)]
    wait(futs, timeout=max(1.0, global_deadline + 20 - time.monotonic()))
    with lock:
        ordered = []
        for i, (o, raw) in enumerate(items):
            r = results.get(i)
            if r is None:
                r = empty_result(o, raw)
                r["status"] = "timeout"
                r["notes"].append("run budget exhausted before this company was processed")
            ordered.append(r)
    write_results(output_path, ordered, fmt)
    pf.close()
    try:
        os.remove(partial_path)
    except OSError:
        pass
    ex.shutdown(wait=False, cancel_futures=True)
    return ordered


def main(argv=None):
    ap = argparse.ArgumentParser(description="Signalpost batch runner: org numbers in, one result per company out.")
    ap.add_argument("input", help="text/CSV file with one Norwegian organisation number per line")
    ap.add_argument("output", nargs="?", default="results.jsonl")
    ap.add_argument("--budget", type=float, default=600.0, help="total run budget in seconds (default 600)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--format", choices=["jsonl", "json"], default="jsonl")
    ap.add_argument("--no-web", action="store_true", help="registry data only; skip website, news and jobs")
    a = ap.parse_args(argv)
    t0 = time.monotonic()
    out = run(a.input, a.output, a.budget, a.workers, a.format, a.no_web)
    counts = {}
    for r in out:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"{len(out)} companies -> {a.output} in {time.monotonic() - t0:.1f}s  {counts}")
    sys.stdout.flush()
    os._exit(0)  # do not wait for any straggler threads


if __name__ == "__main__":
    main()
