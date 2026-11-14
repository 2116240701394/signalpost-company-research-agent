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
import html
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
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

    def wait(self, deadline=None):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_ok)
            self.next_ok = start + self.min_interval + self.penalty
        delay = start - now
        if delay > 0:
            if deadline is not None and time.monotonic() + delay > deadline:
                raise TimeoutError("run budget exhausted")
            time.sleep(delay)

    def rejected(self, headers, attempt):
        """The server said 'too many requests' (429/503). Returns how many seconds to wait
        before trying again: its Retry-After if given, else 2, 4, 8... plus jitter.
        Also widens the gap for every later request to this host."""
        default = min(2 ** attempt + random.random(), 30)
        wait_s = _retry_after(headers, default)
        self.slow_down(wait_s)
        return wait_s

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
                wait_s = th.rejected(hdrs, attempt)
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
        self.footer = []
        self._footer = 0
        self.link_tags = []  # (rel, type, href) from <link> in the head: feeds, WordPress API
        self.cards = []      # <article>/<li> blocks holding both a link and a <time datetime>
        self._cards = []
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
            t = {"datetime": a["datetime"], "itemprop": a.get("itemprop", "").lower(), "class": a.get("class", "").lower()}
            self.times.append(t)
            if self._cards:
                self._cards[-1]["times"].append(t)
        elif tag == "link":
            self.link_tags.append((a.get("rel", "").lower(), a.get("type", "").lower(), a.get("href", "")))
        elif tag in ("article", "li"):
            self._cards.append({"links": [], "times": []})
        elif tag == "footer":
            self._footer += 1
        elif tag == "h1" and not self.h1:
            self._h1 = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._title = False
        elif tag == "a" and self._a is not None:
            self.links.append((self._a["href"], " ".join("".join(self._a["text"]).split())))
            if self._cards:
                self._cards[-1]["links"].append(self._a["href"])
            self._a = None
        elif tag in ("article", "li") and self._cards:
            card = self._cards.pop()
            if card["links"] and card["times"]:
                self.cards.append(card)
        elif tag == "script":
            if self._ld:
                self.jsonld.append("".join(self._ldbuf))
                self._ld = False
            elif self._skip:
                self._skip -= 1
        elif tag == "style" and self._skip:
            self._skip -= 1
        elif tag == "footer" and self._footer:
            self._footer -= 1
        elif tag == "h1":
            self._h1 = False

    def handle_data(self, data):
        if self._ld:
            self._ldbuf.append(data)
            return
        if self._skip:
            return
        if self._footer:
            self.footer.append(data)
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
    p.footer_text = " ".join(" ".join(p.footer).split())
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


def ascii_fold(s):
    s = (s or "").lower().replace("æ", "ae").replace("ø", "o").replace("å", "a")
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


LOCAL_WORDS = {"norge", "norway", "no"}
GENERIC_TOKENS = LEGAL | LOCAL_WORDS | {
    "norsk", "group", "gruppen", "holding", "holdings", "invest", "eiendom", "international", "nordic",
    "services", "service", "solutions", "consulting", "trading", "the", "and", "og", "for", "den", "det",
}


def name_stem(name):
    toks = norm_text(name).split()
    while toks and toks[-1] in LEGAL:
        toks.pop()
    return " ".join(toks)


def company_key(name):
    """Name without legal suffix or a trailing country word, as one lowercase ASCII string:
    'Otis Norge AS' -> 'otis'. Two names are the same company only if their keys are equal."""
    toks = name_stem(name).split()
    while toks and toks[-1] in LOCAL_WORDS:
        toks.pop()
    return "".join(ascii_fold(t) for t in toks)


def same_company(a, b):
    ka, kb = company_key(a), company_key(b)
    return bool(ka) and ka == kb


def full_name_in(text, name):
    """The complete registered name, legal suffix included ('Otis AS'), as a whole phrase.
    A brand word alone ('Otis') does not count: parents and sister companies share it."""
    full = norm_text(name)
    return bool(full) and re.search(r"(?<!\w)" + re.escape(full) + r"(?!\w)", norm_text(text)) is not None


def brand_in_domain(host, name):
    dom = re.sub(r"[^a-z0-9]", "", ascii_fold(host))
    toks = [ascii_fold(t) for t in name_stem(name).split()]
    toks = [t for t in toks if len(t) >= 3 and t not in GENERIC_TOKENS]
    return any(t in dom for t in toks)


def identity_text(page):
    """Where a site says who it belongs to: title, site name, main heading, footer and copyright lines.
    A name that only appears in body text (a list of group companies, a news item) is not identity."""
    parts = [page.title, page.meta.get("og:site_name", ""), page.h1, getattr(page, "footer_text", "")]
    parts += re.findall(r"(?:©|&copy;|\(c\)|copyright)[^\n]{0,100}", page.body, re.I)
    return " ".join(parts)


def evidence(text, orgnr, name):
    if orgnr_in(text, orgnr):
        return "orgnr"
    if name and full_name_in(text, name):
        return "name"
    return None


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------
MONTHS = {
    "januar": 1, "jan": 1, "january": 1, "februar": 2, "feb": 2, "february": 2, "mars": 3, "mar": 3, "march": 3,
    "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "jun": 6, "june": 6, "juli": 7, "jul": 7, "july": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "desember": 12, "des": 12, "december": 12, "dec": 12,
}
DATE_TEXT = (r"\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}\.?\s+[A-Za-zæøåÆØÅ]+\.?,?\s+\d{4}"
             r"|[A-Za-zæøåÆØÅ]+\.?\s+\d{1,2},?\s+\d{4}")


def _date(y, m, d):
    try:
        return dt.date(int(y), int(m), int(d))
    except ValueError:
        return None


def parse_any_date(s):
    """2026-10-29, 29.10.2026, 29. oktober 2026, 29 October 2026 or October 29, 2026 (text after it is ignored)."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return _date(*m.groups())
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)", s)
    if m:
        return _date(m.group(3), m.group(2), m.group(1))
    m = re.match(r"(\d{1,2})\.?\s+([A-Za-zæøåÆØÅ]+)\.?,?\s+(\d{4})(?!\d)", s)
    if m and m.group(2).lower() in MONTHS:
        return _date(m.group(3), MONTHS[m.group(2).lower()], m.group(1))
    m = re.match(r"([A-Za-zæøåÆØÅ]+)\.?\s+(\d{1,2}),?\s+(\d{4})(?!\d)", s)
    if m and m.group(1).lower() in MONTHS:
        return _date(m.group(3), MONTHS[m.group(1).lower()], m.group(2))
    return None


def parse_date(s):
    """A publish date: a real date, not in the future, not before 1995. Returns ISO text or None."""
    d = parse_any_date(s)
    if d is None or d > dt.date.today() or d.year < 1995:
        return None
    return d.isoformat()


# "Publisert: 3. november 2025", "Published October 29, 2026". 'Oppdatert' / 'Updated' never count.
PUBLISHED_TEXT = re.compile(r"\b(?:publisert|published|posted|lagt ut)\b\s*(?:den|on)?\s*:?\s*(" + DATE_TEXT + r")", re.I)


def published_from_text(body):
    m = PUBLISHED_TEXT.search(body or "")
    return parse_date(m.group(1)) if m else None


ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "report", "techarticle", "pressrelease"}
PUBLISHED_META = ("article:published_time", "og:article:published_time", "datepublished", "pubdate",
                  "dc.date.issued", "dc.date.created", "publish_date", "article.published")


def _is_modified_time(t):
    return bool(re.search(r"modif|updat", t["class"])) or t["itemprop"] == "datemodified"


def published_date(page, allow_bare_time=False):
    """Publish date only. dateModified, og:updated_time, feed 'updated' and modified <time> tags are ignored.
    A lone, unmarked <time datetime> is used only when the caller says the page is a news post."""
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
        if _is_modified_time(t):
            continue
        if t["itemprop"] == "datepublished" or re.search(r"publish|posted|entry-date|post-date", t["class"]):
            d = parse_date(t["datetime"])
            if d:
                return d, "time_tag"
    if allow_bare_time and len(page.times) == 1 and not _is_modified_time(page.times[0]):
        d = parse_date(page.times[0]["datetime"])
        if d:
            return d, "time_tag"
    d = published_from_text(page.body)
    if d:
        return d, "text"
    return None, None


def card_dates(page, base_url):
    """{absolute url: ISO date} for links sitting in the same <article>/<li> as one <time datetime>
    (a dated teaser on a news page). Cards with several dates or many links are ignored."""
    out = {}
    for card in page.cards:
        ds = {parse_date(t["datetime"]) for t in card["times"] if not _is_modified_time(t)}
        ds.discard(None)
        urls = set()
        for h in card["links"]:
            h = h.strip()
            if h and not h.startswith(("#", "mailto:", "tel:", "javascript:")):
                urls.add(urllib.parse.urldefrag(urllib.parse.urljoin(base_url, h))[0])
        if len(ds) == 1 and 0 < len(urls) <= 2:
            for u in urls:
                out.setdefault(u, next(iter(ds)))
    return out


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

    # Strongest proof: this company's own organisation number on the site (home, contact, about, privacy).
    ev = "orgnr" if orgnr_in(page.body + " " + page.title, orgnr) else None
    if not ev:
        extra = [u for u, t in abs_links(page, final) if same_site(u, base) and (SUPPORT_PATH.search(urllib.parse.urlsplit(u).path) or SUPPORT_PATH.search(t))]
        for u in extra[:3]:
            try:
                p2, _ = get_page(u, deadline)
            except FetchError:
                continue
            if p2 and orgnr_in(p2.body, orgnr):
                ev = "orgnr"
                break
    # Weaker proof, only for a site that is not the parent's: the full registered name (with AS/ASA) is
    # the site's own identity (title, footer, copyright) AND the domain contains the company's brand word.
    # A group site that merely lists this company, or a parent's global site, fails one of the two.
    if not ev and not group_site and full_name_in(identity_text(page), name) and brand_in_domain(base, name):
        ev = "name"
    if ev:
        info["verified"] = True
        info["evidence"] = ev
        return info, page, final
    if group_site:
        info["reason"] = "site belongs to parent group and does not show this company's org number"
    else:
        info["reason"] = ("org number not found, and the full registered name is not shown as the owner of a "
                          "domain matching it (possible group, parent or sister-company site)")
    return info, page, final


SOCIAL = [
    ("linkedin", r"linkedin\.com/(company|school)/[^/?#]+"),
    ("facebook", r"facebook\.com/(?!sharer|share|plugins|dialog|tr\b)[^/?#]+"),
    ("instagram", r"instagram\.com/(?!p/|explore)[^/?#]+"),
    ("x", r"(?:x|twitter)\.com/(?!intent|share|home)[^/?#]+"),
    ("youtube", r"youtube\.com/(@|channel/|c/|user/)[^/?#]+"),
]


def social_slug(url):
    parts = [s for s in urllib.parse.urlsplit(url).path.split("/") if s]
    while parts and parts[0].lower() in ("company", "school", "channel", "user", "c", "in", "pages"):
        parts.pop(0)
    return parts[0].lstrip("@") if parts else ""


def social_ties_to_company(url, c, site_evidence):
    """A profile found on the company's own site is kept only if its handle names this company.
    'OtisElevatorCompany' is the global parent of Otis AS, so it fails; 'otis-norge' passes.
    A bare brand handle ('equinor') is kept only when the site itself showed the org number,
    because a bare brand handle is often the parent's or the global account."""
    core = re.sub(r"[^a-z0-9]", "", ascii_fold(urllib.parse.unquote(social_slug(url))))
    stem = company_key(c["name"])
    if not core or not stem:
        return False
    if c["orgnr"] in core:
        return True
    if core == stem:
        return site_evidence == "orgnr"
    stripped = core
    for _ in range(3):
        for w in ("asa", "as", "norge", "norway", "no"):
            if stripped.endswith(w) and len(stripped) > len(w):
                stripped = stripped[: -len(w)]
                break
    return stripped == stem and stripped != core


def extract_social(page, final, c, site_evidence, notes=None):
    out, dropped = {}, set()
    for u, _ in abs_links(page, final):
        for k, pat in SOCIAL:
            if re.search(pat, u, re.I) and "share" not in u.lower():
                if social_ties_to_company(u, c, site_evidence):
                    out.setdefault(k, u)
                else:
                    dropped.add(u)
    if dropped and notes is not None:
        notes.append(f"social: left out {len(dropped)} link(s) that cannot be tied to this company's name or org number")
    return out


NEWS_SEG = re.compile(r"(nyheter|nyhet|news|presse|pressemeldinger|pressemelding|press|aktuelt|blogg|blog|artikler|siste-nytt)", re.I)
PRODUCT_SEG = re.compile(r"(produkt|produkter|product|products|tjenester|tjeneste|services|service|losninger|solutions|priser|pricing|shop|butikk|kategori|category|tag|tags)", re.I)
NOT_SLUG = re.compile(r"(page|side|p|archive|arkiv|\d{1,4})", re.I)
# Menu or button labels that are not news titles. Add a phrase here to reject it (matched as whole words, any case).
LABEL_PHRASES = [
    "see more", "see all", "view more", "view all", "read more", "show more", "load more",
    "more news", "all news", "older posts", "newer posts", "news archive", "go to news", "visit our newsroom",
    "les mer", "vis mer", "vis flere", "vis alle", "se alle", "se mer", "flere nyheter", "alle nyheter",
    "eldre nyheter", "nyhetsarkiv", "archive", "arkiv", "next page", "previous page", "neste side", "forrige side",
]
LABEL_RE = re.compile(
    "|".join(r"\b" + re.escape(p) + r"\b" for p in LABEL_PHRASES) + r"|^\s*(next|previous|neste|forrige)\b",
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


def title_ok(title):
    return bool(title) and title.strip().lower() not in GENERIC_TITLES and not LABEL_RE.search(title)


def news_url_ok(url, base):
    return same_site(url, base) and not is_news_index(url) and not any(PRODUCT_SEG.fullmatch(x.lower()) for x in segs(url))


def feed_urls(page, final, base):
    out = []
    for rel, typ, href in page.link_tags:
        if "alternate" in rel and ("rss" in typ or "atom" in typ) and href:
            u = urllib.parse.urljoin(final, href)
            if same_site(u, base) and u not in out:
                out.append(u)
    return out[:2]


def declares_wordpress(page):
    return any("api.w.org" in rel for rel, _, _ in page.link_tags) or "wordpress" in page.meta.get("generator", "").lower()


def _feed_date(s):
    if not s:
        return None
    try:
        return parse_date(email.utils.parsedate_to_datetime(s.strip()).date().isoformat())
    except (TypeError, ValueError, IndexError):
        return parse_date(s)


def parse_feed(text):
    """RSS or Atom -> [(title, link, publish date)]. Uses RSS pubDate, dc:date and Atom <published> only.
    Atom <updated> is not a publish date and is ignored."""
    up = (text or "").upper()
    if not text or "<!ENTITY" in up or "<!DOCTYPE" in up[:3000]:
        return []
    try:
        root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text))
    except ET.ParseError:
        return []
    out = []
    for e in root.iter():
        if not isinstance(e.tag, str) or e.tag.rsplit("}", 1)[-1].lower() not in ("item", "entry"):
            continue
        title = link = pub = None
        for ch in e:
            if not isinstance(ch.tag, str):
                continue
            n = ch.tag.rsplit("}", 1)[-1].lower()
            if n == "title" and title is None:
                title = " ".join("".join(ch.itertext()).split())
            elif n == "link" and link is None:
                if ch.get("href"):
                    if ch.get("rel", "alternate") == "alternate":
                        link = ch.get("href")
                elif (ch.text or "").strip():
                    link = ch.text.strip()
            elif n in ("pubdate", "published", "date") and pub is None:
                pub = (ch.text or "").strip()
        d = _feed_date(pub)
        if title and link and d:
            out.append((title, link, d))
    return out[:20]


def wp_posts(origin, deadline):
    """WordPress REST API: the site's own list of posts. 'date' is the post's publish date ('modified' is ignored)."""
    r = FETCH(origin + "/wp-json/wp/v2/posts?per_page=6&_fields=date,link,title,type", deadline, "application/json")
    if r.status != 200:
        return []
    try:
        data = json.loads(r.text)
    except ValueError:
        return []
    out = []
    for p in data if isinstance(data, list) else []:
        if not isinstance(p, dict) or p.get("type", "post") != "post":
            continue
        t = p.get("title")
        title = html.unescape(re.sub(r"<[^>]+>", "", t.get("rendered", ""))) if isinstance(t, dict) else ""
        d = parse_date(p.get("date"))
        if title.strip() and p.get("link") and d:
            out.append((" ".join(title.split()), p["link"], d))
    return out


def find_news(page, final, base, deadline, notes):
    """Dated news posts on the company's own site. Every item has a title, a URL on the verified site, a publish
    date (never a modified/updated date) and 'linked_from', the page or feed of the site that points to it."""
    items, seen = [], set()
    st = {"rejected": 0, "stopped": False}

    def add(title, url, published, source, linked_from):
        key = url.rstrip("/")
        if key not in seen:
            seen.add(key)
            items.append({"title": title, "url": url, "published": published, "date_source": source, "linked_from": linked_from})

    try:  # 1. crawl: news links on the home page and on a news index page, each article checked on its own page
        listing_dates = dict(card_dates(page, final))
        index_urls, cands = [], []
        for u, text in abs_links(page, final):
            if not same_site(u, base):
                continue
            if LABEL_RE.search(text or ""):
                if is_news_index(u) and u not in index_urls:
                    index_urls.append(u)
                continue
            if is_article_url(u):
                cands.append((u, False, final))
            elif is_news_index(u) and u not in index_urls:
                index_urls.append(u)
        for iu in sorted(index_urls, key=lambda x: len(segs(x)))[:2]:
            try:
                ip, ir = get_page(iu, deadline)
            except FetchError:
                continue
            if not ip:
                continue
            cd = card_dates(ip, ir.url)
            listing_dates.update(cd)
            for u, text in abs_links(ip, ir.url):
                if not news_url_ok(u, base) or LABEL_RE.search(text or "") or u == iu:
                    continue
                if is_article_url(u):
                    cands.append((u, False, iu))
                elif u in cd:  # a dated teaser card on a news page
                    cands.append((u, True, iu))
        tried = set()
        for u, from_card, src in cands:
            key = u.rstrip("/")
            if key in tried:
                continue
            tried.add(key)
            if len(tried) > 8:
                break
            try:
                ap, _ = get_page(u, deadline)
            except FetchError:
                continue
            if not ap:
                continue
            title = clean_title(ap)
            nodes = list(ld_nodes(ap.jsonld))
            article_signal = bool(set(t for n in nodes for t in ld_types(n)) & ARTICLE_TYPES) or ap.meta.get("og:type", "").lower() == "article"
            is_product = any("product" in ld_types(n) for n in nodes) and not article_signal
            looks_post = article_signal or is_article_url(u) or from_card
            d, how = published_date(ap, allow_bare_time=looks_post)
            if not d and u in listing_dates:
                d, how = listing_dates[u], "listing"
            if not title_ok(title) or not d or is_product or not looks_post:
                st["rejected"] += 1
                continue
            add(title, u, d, how, src)
    except TimeoutError:
        st["stopped"] = True

    if not st["stopped"] and len(items) < 3:
        try:  # 2. the site's own feeds, then 3. its WordPress post list
            for fu in feed_urls(page, final, base):
                try:
                    r = FETCH(fu, deadline, "application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.5")
                except FetchError:
                    continue
                if r.status != 200:
                    continue
                for title, link, d in parse_feed(r.text):
                    link = urllib.parse.urljoin(fu, link)
                    if news_url_ok(link, base) and title_ok(title):
                        add(title, link, d, "feed", fu)
                    else:
                        st["rejected"] += 1
            if len(items) < 3 and declares_wordpress(page):
                sp = urllib.parse.urlsplit(final)
                origin = f"{sp.scheme}://{sp.netloc}"
                for title, link, d in wp_posts(origin, deadline):
                    if news_url_ok(link, base) and title_ok(title):
                        add(title, link, d, "wp_rest", origin + "/wp-json/wp/v2/posts")
                    else:
                        st["rejected"] += 1
        except TimeoutError:
            st["stopped"] = True
        except FetchError:
            pass
    if st["rejected"]:
        notes.append(f"news: rejected {st['rejected']} candidate(s) without title, explicit publish date or own-site link")
    if st["stopped"]:
        notes.append("news: stopped early, run budget exhausted")
    items.sort(key=lambda i: i["published"], reverse=True)
    return items[:5]


JOB_PATH = re.compile(r"karriere|career|(^|/)jobs?(/|$)|stilling|ledige|rekrutter|vacanc|work-with|join-us|bli-med", re.I)
JOB_AD_PATH = re.compile(r"/(jobs?|job|stilling|stillinger|position|positions|vacancy|vacancies|career|careers|karriere|ledig-stilling)/[^/?#]+", re.I)
ATS_HOSTS = ("webcruiter", "teamtailor", "easycruit", "recman", "varbi", "hrmanager", "lever.co", "greenhouse.io", "workable", "jobylon",
             "personio", "smartrecruiters", "jobbnorge", "reachmee", "cvpartner", "finn.no", "arbeidsplassen.nav.no")
JOB_LABEL = re.compile(r"alle stillinger|se alle|see all|open positions|ledige stillinger|search|søk|vis alle|all jobs|all vacancies", re.I)
NAV_AD = re.compile(r"arbeidsplassen\.nav\.no/stillinger/stilling/[0-9a-f-]{8,}", re.I)
SEARCHY = re.compile(r"(^|&)(q|query|search|sok|s|keyword|keywords|page|filter)=", re.I)


PROBE_PATHS = ("/karriere", "/jobb", "/ledige-stillinger", "/careers")
AD_ID = re.compile(r"\d{5,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
APPLY_CUE = re.compile(
    r"\b(apply now|apply here|apply for this|apply today|how to apply|application deadline|søknadsfrist|søk på stillingen|"
    r"søk nå|send (?:inn )?søknad|send (?:in )?(?:your )?application|vi søker|we are (?:looking|hiring)|ledig stilling)\b", re.I)
LANDING_TITLE = re.compile(
    r"careers?|karriere|life at|graduates?|students?|trainee|internship|apprentice|vacancies|job opportunit|"
    r"ledige stillinger|meet our|recruitment|rekruttering|why work|working at|jobs at", re.I)
DEADLINE_TEXT = re.compile(r"\b(?:søknadsfrist|frist|apply by|application deadline|deadline)\b\s*:?\s*(" + DATE_TEXT + r")", re.I)


def is_child_of(url, landing_urls):
    sp = urllib.parse.urlsplit(url)
    for lu in landing_urls:
        lp = urllib.parse.urlsplit(lu).path.rstrip("/")
        if lp and sp.path.startswith(lp + "/") and len(sp.path.rstrip("/")) > len(lp) + 1 and host_of(lu) == host_of(url):
            return True
    return False


def looks_like_single_ad(url, page, landing_urls=()):
    """One ad: an id in the URL or a page under the careers page, an apply/deadline cue, and a title that is not a landing title."""
    anchored = bool(AD_ID.search(urllib.parse.urlsplit(url).path)) or is_child_of(url, landing_urls)
    return anchored and bool(APPLY_CUE.search(page.body)) and not LANDING_TITLE.search(page.h1 or "")


def ad_deadline(page, posting):
    """Last application date from JSON-LD validThrough, else a labelled 'Søknadsfrist: <date>' in the text."""
    if posting:
        d = parse_any_date(str(posting.get("validThrough") or ""))
        if d:
            return d
    m = DEADLINE_TEXT.search(page.body)
    return parse_any_date(m.group(1)) if m else None


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
    if is_child_of(url, landing_urls):
        return True
    return bool(JOB_AD_PATH.search(sp.path))


def find_jobs(c, page, final, base, deadline, notes):
    """Open single job ads for this company, linked from its own site (directly, from its careers page, or from
    the job board its careers page points to). Each has 'linked_from'. Expired ads are left out."""
    landing, ats = [], []
    for u, text in abs_links(page, final):
        if same_site(u, base) and (JOB_PATH.search(urllib.parse.urlsplit(u).path) or JOB_PATH.search(text or "")):
            if not SEARCHY.search(urllib.parse.urlsplit(u).query or ""):
                landing.append(u)
        elif is_ats(u) and not host_of(u).endswith("nav.no"):
            ats.append(u)
    landing = sorted(set(landing), key=lambda x: len(segs(x)))[:2]
    ats = list(dict.fromkeys(ats))[:2]
    stopped = False
    pages, have, cands = [(final, page)], {final}, {}
    try:
        sp = urllib.parse.urlsplit(final)
        origin = f"{sp.scheme}://{sp.netloc}"
        if not landing and not ats:  # no careers link on the home page: try the usual addresses
            for p in PROBE_PATHS:
                try:
                    pp, pr = get_page(origin + p, deadline)
                except FetchError:
                    continue
                if pp and same_site(pr.url, base) and JOB_PATH.search(urllib.parse.urlsplit(pr.url).path):
                    landing.append(pr.url)
                    pages.append((pr.url, pp))
                    have.add(pr.url)
                    break
        for lu in landing + ats[:1]:
            if lu in have:
                continue
            try:
                lp, lr = get_page(lu, deadline)
            except FetchError:
                continue
            have.add(lu)
            if lp:
                pages.append((lr.url, lp))
        for pu, pg in pages:
            for u, text in abs_links(pg, pu):
                if JOB_LABEL.search(text or ""):
                    continue
                if u not in landing and u != pu and u not in cands and looks_like_ad(u, landing + ats, base):
                    cands[u] = pu
    except TimeoutError:
        stopped = True
    cands = dict(list(cands.items())[:10])
    own_name, orgnr = c["name"], c["orgnr"]
    parent_name = c.get("_parent_name")
    today = dt.date.today()
    jobs, rejected, expired = [], 0, 0
    try:
        for u, src in cands.items():
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
            posting = postings[0] if postings else None
            on_site = same_site(u, base)
            # On the verified site the company's name is enough; on a job board the full name or org number is needed.
            named = evidence(ap.body, orgnr, own_name) is not None or (on_site and name_in(ap.body, own_name))
            if posting:
                title = posting.get("title") or posting.get("name")
                org = posting.get("hiringOrganization")
                org_name = org.get("name") if isinstance(org, dict) else org
                if isinstance(org_name, str) and org_name.strip():
                    ok = same_company(org_name, own_name) or orgnr_in(json.dumps(org) if isinstance(org, dict) else "", orgnr)
                else:
                    ok = named
            else:
                title = ap.h1
                ok = looks_like_single_ad(u, ap, landing + ats) and named
            if ok and parent_name and not posting and not evidence(ap.body, orgnr, own_name) and name_in(ap.body, parent_name):
                ok = False
            if not ok or not title or str(title).strip().lower() in GENERIC_TITLES:
                rejected += 1
                continue
            until = ad_deadline(ap, posting)
            if until and until < today:
                expired += 1
                continue
            job = {"title": " ".join(str(title).split()), "url": u, "linked_from": src}
            posted = parse_date(posting.get("datePosted")) if posting else None
            if posted:
                job["posted"] = posted
            if until:
                job["valid_through"] = until.isoformat()
            jobs.append(job)
    except TimeoutError:
        stopped = True
    if rejected:
        notes.append(f"jobs: rejected {rejected} candidate(s) (listing page, other organisation or no evidence it is this company's ad)")
    if expired:
        notes.append(f"jobs: left out {expired} expired ad(s)")
    if stopped:
        notes.append("jobs: stopped early, run budget exhausted")
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
            res["social"] = extract_social(page, final, c, info["evidence"], notes)
            t0 = time.monotonic()
            news_deadline = min(deadline, t0 + max(5.0, (deadline - t0) * 0.55))  # keep time for the jobs step
            try:
                res["news"] = find_news(page, final, base, news_deadline, notes)
            except TimeoutError:
                notes.append("news: stopped early, run budget exhausted")
            try:
                res["jobs"] = find_jobs(c, page, final, base, deadline, notes)
            except TimeoutError:
                notes.append("jobs: stopped early, run budget exhausted")
            if any("stopped early" in n for n in notes):
                res["status"] = "partial"
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
