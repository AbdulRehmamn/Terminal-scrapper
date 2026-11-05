#!/usr/bin/env python3
"""
leadscraper.py - polite lead extraction for directories, company websites and PDFs.

Modes
  procore    Crawl a Procore Network listing (e.g. California) and every company profile.
  directory  Crawl any paginated directory: list pages -> detail pages -> one lead each.
  site       Pull emails, phones, socials and address from company websites (or PDF URLs).
  pdf        Pull contacts out of PDF files (member lists, bid lists, plan-holder lists).
  enrich     Fill in missing emails/socials in a leads CSV by visiting each lead's website.

Every mode writes the same CSV columns, appends as it goes, and can be stopped and
re-run: rows already in the output file are skipped.

The crawler is deliberately polite: it obeys robots.txt, waits between requests to the
same site, backs off on errors, and stops cleanly if a site starts blocking it or shows
a CAPTCHA. It does not log in, solve CAPTCHAs or disguise itself.
"""

import argparse
import csv
import io
import json
import os
import random
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlencode, parse_qsl, urlunparse
from urllib.robotparser import RobotFileParser

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing packages. Run:  pip install -r requirements.txt")

VERSION = "1.0"
DEFAULT_UA = f"Mozilla/5.0 (compatible; LeadScraper/{VERSION})"

COLUMNS = [
    "Company", "Company Type", "Trades", "Phone", "Email", "Website",
    "Address", "City", "State", "Zip", "LinkedIn", "Facebook", "Instagram",
    "X/Twitter", "YouTube", "Lead Score", "Signals", "Source URL", "Scraped At",
]

# Phrases that suggest a company is active, growing or buying right now. Tuned for
# selling services to contractors; override with --signals.
DEFAULT_SIGNALS = [
    "now hiring", "we're hiring", "we are hiring", "careers", "join our team",
    "estimator", "estimating", "bid", "bidding", "accepting bids", "invitation to bid",
    "rfp", "rfq", "request for proposal", "request a quote", "free estimate",
    "new project", "current projects", "under construction", "expanding", "growing",
]

PROCORE_COMPANY_TYPES = [
    "General Contractor", "Specialty Contractor", "Owner Real Estate Developer",
    "Architect", "Engineer", "Consultant", "Supplier", "Manufacturer",
    "Distributor", "Service Provider", "Equipment Rental",
]

SOCIAL_HOSTS = {
    "LinkedIn": ("linkedin.com",),
    "Facebook": ("facebook.com", "fb.com"),
    "Instagram": ("instagram.com",),
    "X/Twitter": ("twitter.com", "x.com"),
    "YouTube": ("youtube.com", "youtu.be"),
}

# Links that are never a company's own website.
NON_WEBSITE_HOSTS = (
    "procore.com", "google.", "goo.gl", "apple.com", "bing.com", "yelp.com",
    "mapquest.com", "waze.com", "gstatic.com", "googleapis.com", "cloudflare.com",
    "doubleclick.net", "hubspot.com", "wix.com", "squarespace.com", "w3.org",
    "schema.org", "adobe.com", "microsoft.com", "play.google", "bbb.org",
    "tiktok.com", "pinterest.com", "vimeo.com", "wa.me", "whatsapp.com",
) + tuple(h for hosts in SOCIAL_HOSTS.values() for h in hosts)

JUNK_EMAIL_PARTS = (
    "example.", "domain.com", "email.com", "yourdomain", "yourcompany", "sentry",
    "wixpress.com", "@2x", "godaddy.com", "no-reply", "noreply", "donotreply",
    "privacy@", "abuse@", "postmaster@", "webmaster@", "user@", "name@",
)
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico")

CONTACT_WORDS = ("contact", "about", "team", "staff", "people", "leadership",
                 "directory", "office", "location", "careers", "company")

CAPTCHA_MARKERS = (
    "cf-chl", "challenge-platform", "just a moment...", "attention required",
    "px-captcha", "please verify you are a human", "verify you are human",
    "are you a robot", "unusual traffic", "access denied", "datadome",
    "perimeterx", "captcha-delivery",
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
US_PHONE_RE = re.compile(
    r"(?<![\d\w])(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?([2-9]\d{2})[\s.\-]?(\d{4})(?!\d)"
)
US_ADDRESS_RE = re.compile(
    r"\b(\d{1,6}\s+[A-Za-z0-9 .'#\-]{3,50}?),\s*([A-Za-z .'\-]{2,40}),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b"
)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class Blocked(Exception):
    """The site is refusing us (CAPTCHA, 403, repeated 429). Stop and resume later."""


# --------------------------------------------------------------------------- fetching

class PoliteFetcher:
    """HTTP client that obeys robots.txt and spaces out requests per host."""

    def __init__(self, delay=3.0, user_agent=DEFAULT_UA, timeout=25, max_retries=3):
        self.delay = delay
        self.ua = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._robots = {}
        self._last_hit = defaultdict(float)
        self._locks = defaultdict(threading.Lock)
        self._global = threading.Lock()

    def _host_lock(self, host):
        with self._global:
            return self._locks[host]

    def allowed(self, url):
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=self.timeout)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self._robots[base] = rp
            delay = rp.crawl_delay(self.ua) or rp.crawl_delay("*")
            if delay and float(delay) > self.delay:
                log(f"{p.netloc} asks for {delay}s between requests; using that.")
                self._robots[base + "#delay"] = float(delay)
        return self._robots[base].can_fetch(self.ua, url)

    def _wait(self, host, base):
        delay = self._robots.get(base + "#delay", self.delay)
        gap = time.time() - self._last_hit[host]
        wait = delay * random.uniform(0.8, 1.4) - gap
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.time()

    def get(self, url):
        """Return a requests.Response, or None if disallowed/unreachable. Raises Blocked."""
        if not self.allowed(url):
            log(f"robots.txt disallows {url} - skipped")
            return None
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        backoff = 30
        for attempt in range(1, self.max_retries + 1):
            with self._host_lock(p.netloc):
                self._wait(p.netloc, base)
                try:
                    r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                except requests.RequestException as e:
                    log(f"  network error ({e.__class__.__name__}) on {url}")
                    r = None
            if r is None:
                time.sleep(5 * attempt)
                continue
            if r.status_code == 429 or r.status_code == 503:
                retry_after = r.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else backoff
                log(f"  {p.netloc} says slow down ({r.status_code}); waiting {wait}s")
                time.sleep(wait)
                backoff *= 3
                continue
            if r.status_code == 403 or looks_like_captcha(r):
                raise Blocked(f"{p.netloc} is showing a block page or CAPTCHA ({r.status_code}).")
            if r.status_code >= 400:
                return None
            return r
        if r is not None and r.status_code in (429, 503):
            raise Blocked(f"{p.netloc} keeps rate-limiting us.")
        return None


def looks_like_captcha(r):
    ctype = r.headers.get("Content-Type", "")
    if "html" not in ctype:
        return False
    head = r.text[:30000].lower()
    if any(m in head for m in CAPTCHA_MARKERS):
        # Real pages can mention "captcha" in a form; block pages are short.
        return len(r.text) < 60000 or "cf-chl" in head or "captcha-delivery" in head
    return False


# --------------------------------------------------------------------------- extraction

def norm_phone(raw):
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if 8 <= len(digits) <= 15 and raw.strip().startswith("+"):
        return "+" + digits
    return None


def clean_email(e):
    e = e.strip().strip(".,;:").lower()
    if e.endswith(IMAGE_EXT) or any(j in e for j in JUNK_EMAIL_PARTS):
        return None
    if not EMAIL_RE.fullmatch(e):
        return None
    return e


def deobfuscate(text):
    text = re.sub(r"\s*[\[\(\{<]\s*(?:at|AT)\s*[\]\)\}>]\s*", "@", text)
    text = re.sub(r"\s*[\[\(\{<]\s*(?:dot|DOT)\s*[\]\)\}>]\s*", ".", text)
    text = re.sub(r"(\w)\s+(?:at|AT)\s+(\w[\w\-]*)\s+(?:dot|DOT)\s+(\w{2,6})\b", r"\1@\2.\3", text)
    return text


def decode_cfemail(hexstr):
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return None


def host_of(url):
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def iter_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                if "@graph" in item:
                    stack.append(item["@graph"])


def walk_json(obj, depth=0):
    if depth > 25:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk_json(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_json(v, depth + 1)


def extract_contacts(html, url, main_only=True):
    """Pull every contact detail we can find on one HTML page."""
    soup = BeautifulSoup(html, "lxml")
    out = {"emails": [], "phones": [], "websites": [], "socials": {}, "name": "",
           "address": "", "city": "", "state": "", "zip": "", "text": ""}

    # Structured data first: it is the most reliable.
    for item in iter_jsonld(soup):
        types = item.get("@type", "")
        types = " ".join(types) if isinstance(types, list) else str(types)
        if any(t in types for t in ("Organization", "Business", "Contractor", "Corporation",
                                    "Store", "Service", "Place", "Person")):
            if not out["name"] and isinstance(item.get("name"), str):
                out["name"] = item["name"].strip()
            if item.get("telephone"):
                p = norm_phone(str(item["telephone"]))
                if p:
                    out["phones"].append(p)
            if item.get("email"):
                e = clean_email(str(item["email"]).replace("mailto:", ""))
                if e:
                    out["emails"].append(e)
            if isinstance(item.get("url"), str) and host_of(item["url"]) != host_of(url):
                out["websites"].append(item["url"])
            for s in (item.get("sameAs") or []) if isinstance(item.get("sameAs"), list) else []:
                if isinstance(s, str):
                    out["websites"].append(s)
            addr = item.get("address")
            if isinstance(addr, list) and addr:
                addr = addr[0]
            if isinstance(addr, dict) and not out["address"]:
                out["address"] = str(addr.get("streetAddress", "")).strip()
                out["city"] = str(addr.get("addressLocality", "")).strip()
                out["state"] = str(addr.get("addressRegion", "")).strip()
                out["zip"] = str(addr.get("postalCode", "")).strip()

    # Cloudflare-protected emails.
    for tag in soup.select("[data-cfemail]"):
        e = clean_email(decode_cfemail(tag["data-cfemail"]) or "")
        if e:
            out["emails"].append(e)

    title = ""
    og = soup.find("meta", property="og:title") or soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        title = og["content"]
    elif soup.find("h1"):
        title = soup.find("h1").get_text(" ", strip=True)
    elif soup.title:
        title = soup.title.get_text(" ", strip=True)
    if not out["name"]:
        out["name"] = re.split(r"\s+[|\-–—]\s+", title)[0].strip()

    # Drop site chrome so the directory's own phone/links are not picked up.
    if main_only:
        for tag in soup(["header", "footer", "nav", "script", "style", "noscript", "svg"]):
            tag.decompose()
        for tag in soup.select('[role="navigation"], [role="banner"], [role="contentinfo"], '
                               '[class*="footer"], [id*="footer"], [class*="cookie"]'):
            tag.decompose()
    else:
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("mailto:"):
            e = clean_email(href[7:].split("?")[0])
            if e:
                out["emails"].append(e)
        elif low.startswith("tel:"):
            p = norm_phone(href[4:])
            if p:
                out["phones"].append(p)
        elif low.startswith(("http://", "https://", "//")):
            full = urljoin(url, href)
            h = host_of(full)
            matched = False
            for net, hosts in SOCIAL_HOSTS.items():
                if any(h == x or h.endswith("." + x) for x in hosts):
                    out["socials"].setdefault(net, full)
                    matched = True
            if not matched and h and h != host_of(url) and not any(x in h for x in NON_WEBSITE_HOSTS):
                out["websites"].append(full)

    text = deobfuscate(soup.get_text(" ", strip=True))
    out["text"] = text
    for e in EMAIL_RE.findall(text):
        e = clean_email(e)
        if e:
            out["emails"].append(e)
    for m in US_PHONE_RE.finditer(text):
        p = norm_phone("".join(m.groups()))
        if p:
            out["phones"].append(p)
    if not out["address"]:
        m = US_ADDRESS_RE.search(US_PHONE_RE.sub(" | ", text))
        if m:
            out["address"], out["city"], out["state"], out["zip"] = (g.strip() for g in m.groups())

    for net, link in list(out["socials"].items()):
        if any(x in link.lower() for x in ("/sharer", "/share?", "intent/tweet", "/procore")):
            out["socials"].pop(net)
    for k in ("emails", "phones", "websites"):
        out[k] = list(dict.fromkeys(out[k]))
    return out, soup


def find_signals(text, signals):
    low = text.lower()
    return [s for s in signals if re.search(r"\b" + re.escape(s.lower()) + r"\b", low)]


def score_lead(row, trade_terms, city_terms, signal_hits):
    score = 0
    score += 30 if row.get("Email") else 0
    score += 20 if row.get("Phone") else 0
    score += 10 if row.get("Website") else 0
    score += 5 if row.get("Address") else 0
    blob = " ".join(str(v) for v in row.values()).lower()
    if trade_terms and any(t in blob for t in trade_terms):
        score += 10
    if city_terms and any(c in blob for c in city_terms):
        score += 5
    score += min(20, 5 * len(signal_hits))
    return min(score, 100)


# --------------------------------------------------------------------------- CSV output

class LeadWriter:
    def __init__(self, path):
        self.path = path
        self.seen = set()
        self.count = 0
        self._lock = threading.Lock()
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        if exists:
            with open(path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    self.seen.add(r.get("Source URL", ""))
                    self.count += 1
            log(f"Resuming: {self.count} leads already in {path}")
        self._f = open(path, "a", newline="", encoding="utf-8-sig")
        self._w = csv.DictWriter(self._f, fieldnames=COLUMNS, extrasaction="ignore")
        if not exists:
            self._w.writeheader()

    def has(self, source):
        return source in self.seen

    def write(self, row):
        with self._lock:
            if row["Source URL"] in self.seen:
                return
            row.setdefault("Scraped At", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
            self._w.writerow(row)
            self._f.flush()
            self.seen.add(row["Source URL"])
            self.count += 1

    def close(self):
        self._f.close()


def build_row(info, source, **extra):
    row = {c: "" for c in COLUMNS}
    row.update({
        "Company": info.get("name", ""),
        "Phone": "; ".join(info.get("phones", [])[:3]),
        "Email": "; ".join(info.get("emails", [])[:5]),
        "Website": info["websites"][0] if info.get("websites") else "",
        "Address": info.get("address", ""),
        "City": info.get("city", ""),
        "State": info.get("state", ""),
        "Zip": info.get("zip", ""),
        "Source URL": source,
    })
    for net, link in info.get("socials", {}).items():
        row[net] = link
    row.update({k: v for k, v in extra.items() if v})
    return row


def terms(s):
    return [t.strip().lower() for t in (s or "").split(",") if t.strip()]


def passes_filters(row, trade_terms, city_terms, require):
    blob = " ".join(str(v) for v in row.values()).lower()
    if trade_terms and not any(t in blob for t in trade_terms):
        return False
    if city_terms and not any(c in (row.get("City", "") + " " + row.get("Address", "")).lower()
                              for c in city_terms):
        return False
    for field in require:
        if not row.get(field):
            return False
    return True


# --------------------------------------------------------------------------- Procore

def procore_details(html, url, fallback_state):
    info, soup = extract_contacts(html, url, main_only=True)
    full_soup = BeautifulSoup(html, "lxml")
    text = info["text"]

    # Company type: Procore shows one or more of a fixed set of labels.
    types = [t for t in PROCORE_COMPANY_TYPES if t.lower() in text.lower()]

    # Trades: try embedded app data, then the "Trades"/"Services" section on the page.
    trades = []
    nd = full_soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        try:
            for k, v in walk_json(json.loads(nd.string)):
                if isinstance(k, str) and "trade" in k.lower() and isinstance(v, list):
                    for t in v:
                        name = t.get("name") if isinstance(t, dict) else t
                        if isinstance(name, str):
                            trades.append(name)
                if isinstance(k, str) and k.lower() in ("phone", "phonenumber", "business_phone"):
                    p = norm_phone(str(v)) if v else None
                    if p:
                        info["phones"].insert(0, p)
                if isinstance(k, str) and k.lower() in ("website", "websiteurl", "website_url") and v:
                    info["websites"].insert(0, str(v))
        except (json.JSONDecodeError, TypeError):
            pass
    if not trades:
        for label in soup.find_all(string=re.compile(r"^\s*(Trades|Services|Trades & Services|Specialties)\s*$", re.I)):
            heading = label.parent
            lst = heading.find_next(["ul", "ol", "dl"])
            container = lst if lst is not None else (heading.find_parent(["section", "div", "dl"]) or heading)
            items = [li.get_text(" ", strip=True) for li in container.find_all(["li", "dd"])] \
                or [x.get_text(" ", strip=True) for x in container.find_all(["a", "span"])]
            items = [i for i in items if 2 < len(i) < 60 and i.lower() not in ("trades", "services")]
            if items:
                trades.extend(items[:15])
                break

    # Procore lists the street and city but usually no ZIP.
    if not info["state"]:
        info["state"] = fallback_state.upper()

    info["phones"] = list(dict.fromkeys(info["phones"]))
    info["websites"] = [w for w in dict.fromkeys(info["websites"]) if "procore" not in w.lower()]
    info["name"] = re.sub(r"\s*\|\s*Procore.*$", "", info["name"], flags=re.I)
    return info, ", ".join(dict.fromkeys(types)), ", ".join(dict.fromkeys(trades))


def set_page(url, page):
    p = urlparse(url)
    q = dict(parse_qsl(p.query))
    if page > 1:
        q["page"] = str(page)
    else:
        q.pop("page", None)
    return urlunparse(p._replace(query=urlencode(q)))


def run_procore(args, fetcher):
    start = args.url or f"https://network.procore.com/us/{args.state.lower()}"
    writer = LeadWriter(args.out)
    trade_terms, city_terms, require = terms(args.trade), terms(args.city), terms_fields(args.require)
    signals = terms(args.signals) or DEFAULT_SIGNALS
    state_file = args.out + ".progress.json"
    progress = {}
    if os.path.exists(state_file):
        progress = json.load(open(state_file))
    page = max(args.start_page, progress.get("next_page", 1)) if not args.restart else args.start_page
    added = 0
    empty_pages = 0
    stale_pages = 0
    try:
        while page <= args.end_page and added < args.max:
            list_url = set_page(start, page)
            log(f"Listing page {page}: {list_url}")
            r = fetcher.get(list_url)
            if r is None:
                log("  could not load this page; moving on")
                page += 1
                continue
            slugs = list(dict.fromkeys(re.findall(r'["\'(]/p/([a-z0-9][a-z0-9\-]+)', r.text)))
            slugs += [s for s in re.findall(r"network\.procore\.com/p/([a-z0-9][a-z0-9\-]+)", r.text) if s not in slugs]
            if not slugs:
                empty_pages += 1
                log("  no company links on this page")
                if empty_pages >= 3:
                    log("Three empty pages in a row - reached the end of the listing.")
                    break
                page += 1
                continue
            new = [s for s in slugs if not writer.has(urljoin(start, f"/p/{s}"))]
            if not new:
                stale_pages += 1
                if stale_pages >= 5:
                    log("Five pages in a row with nothing new - reached the end of the listing.")
                    break
            else:
                stale_pages = 0
            empty_pages = 0
            for slug in slugs:
                if added >= args.max:
                    break
                prof = urljoin(start, f"/p/{slug}")
                if writer.has(prof):
                    continue
                pr = fetcher.get(prof)
                if pr is None:
                    continue
                info, ctype, trades = procore_details(pr.text, prof, args.state)
                hits = find_signals(info["text"], signals)
                row = build_row(info, prof, **{"Company Type": ctype, "Trades": trades,
                                                "Signals": ", ".join(hits)})
                row["Lead Score"] = score_lead(row, trade_terms, city_terms, hits)
                if not passes_filters(row, trade_terms, city_terms, require):
                    writer.seen.add(prof)  # don't refetch it on resume this run
                    continue
                writer.write(row)
                added += 1
                log(f"  + {row['Company'][:45]:45} {row['Phone'][:14]:14} {row['City']}  ({writer.count} total)")
            page += 1
            json.dump({"next_page": page, "start": start}, open(state_file, "w"))
    except Blocked as b:
        log(f"STOPPED: {b}")
        log("Progress is saved. Wait an hour or two (or raise --delay) and run the same command to continue.")
    except KeyboardInterrupt:
        log("Stopped by you. Run the same command to continue where it left off.")
    finally:
        json.dump({"next_page": page, "start": start}, open(state_file, "w"))
        writer.close()
    log(f"Done. {added} new leads this run, {writer.count} in {args.out}")


# --------------------------------------------------------------------------- generic directory

def run_directory(args, fetcher):
    writer = LeadWriter(args.out)
    follow = re.compile(args.follow)
    trade_terms, city_terms, require = terms(args.trade), terms(args.city), terms_fields(args.require)
    signals = terms(args.signals) or DEFAULT_SIGNALS
    added = 0
    try:
        for page in range(args.start_page, args.end_page + 1):
            if added >= args.max:
                break
            list_url = args.list_url.replace("{page}", str(page)) if "{page}" in args.list_url \
                else set_page(args.list_url, page)
            log(f"Listing page {page}: {list_url}")
            r = fetcher.get(list_url)
            if r is None:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            links = []
            for a in soup.find_all("a", href=True):
                full = urljoin(list_url, a["href"]).split("#")[0]
                if follow.search(full) and full not in links:
                    links.append(full)
            if not links:
                log("  no detail links matched --follow; stopping")
                break
            for link in links:
                if added >= args.max:
                    break
                if writer.has(link):
                    continue
                dr = fetcher.get(link)
                if dr is None:
                    continue
                info, _ = extract_contacts(dr.text, link, main_only=True)
                hits = find_signals(info["text"], signals)
                row = build_row(info, link, Signals=", ".join(hits))
                row["Lead Score"] = score_lead(row, trade_terms, city_terms, hits)
                if passes_filters(row, trade_terms, city_terms, require):
                    writer.write(row)
                    added += 1
                    log(f"  + {row['Company'][:45]:45} {row['Phone'][:14]:14} {row['Email'][:30]}")
    except Blocked as b:
        log(f"STOPPED: {b}  Progress saved; re-run later with --start-page {page}.")
    except KeyboardInterrupt:
        log(f"Stopped. Re-run with --start-page {page} to continue.")
    finally:
        writer.close()
    log(f"Done. {added} new leads this run, {writer.count} in {args.out}")


# --------------------------------------------------------------------------- websites

def crawl_site(fetcher, start, max_pages, signals):
    """Visit a company's homepage plus contact/about pages and merge what is found."""
    if not start.startswith(("http://", "https://")):
        start = "https://" + start.strip().strip("/")
    merged = {"emails": [], "phones": [], "websites": [], "socials": {}, "name": "",
              "address": "", "city": "", "state": "", "zip": "", "text": ""}
    queue, visited, hits = [start], set(), set()
    home_host = None
    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        r = fetcher.get(url)
        if r is None and home_host is None and url.startswith("https://"):
            r = fetcher.get("http://" + url[8:])  # some small-business sites have no HTTPS
        if r is None:
            continue
        if home_host is None:
            home_host = host_of(r.url)
        ctype = r.headers.get("Content-Type", "")
        if "pdf" in ctype or url.lower().endswith(".pdf"):
            for rec in pdf_records(r.content, url):
                merged["emails"] += rec["emails"]
                merged["phones"] += rec["phones"]
            continue
        info, soup = extract_contacts(r.text, r.url, main_only=False)
        for k in ("emails", "phones"):
            merged[k] += info[k]
        for k in ("name", "address", "city", "state", "zip"):
            merged[k] = merged[k] or info[k]
        for net, link in info["socials"].items():
            merged["socials"].setdefault(net, link)
        hits.update(find_signals(info["text"], signals))
        # Queue likely contact pages on the same site, most promising first.
        cands = []
        for a in BeautifulSoup(r.text, "lxml").find_all("a", href=True):
            full = urljoin(r.url, a["href"]).split("#")[0]
            if host_of(full) != home_host or full in visited:
                continue
            label = (a.get_text(" ", strip=True) + " " + urlparse(full).path).lower()
            rank = sum(w in label for w in CONTACT_WORDS)
            if rank:
                cands.append((-rank, full))
        for _, full in sorted(set(cands)):
            if full not in queue:
                queue.append(full)
    merged["websites"] = [start]
    for k in ("emails", "phones"):
        merged[k] = list(dict.fromkeys(merged[k]))
    # Prefer emails on the company's own domain.
    if home_host:
        merged["emails"].sort(key=lambda e: 0 if e.split("@")[-1].endswith(home_host) else 1)
    return merged, sorted(hits), len(visited)


def run_site(args, fetcher):
    targets = list(args.urls or [])
    if args.file:
        targets += [l.strip() for l in open(args.file, encoding="utf-8") if l.strip() and not l.startswith("#")]
    if not targets:
        sys.exit("Give one or more URLs/domains, or --file domains.txt")
    writer = LeadWriter(args.out)
    signals = terms(args.signals) or DEFAULT_SIGNALS
    trade_terms, city_terms = terms(args.trade), terms(args.city)

    def one(t):
        info, hits, n = crawl_site(fetcher, t, args.max_pages, signals)
        row = build_row(info, info["websites"][0], Signals=", ".join(hits))
        row["Lead Score"] = score_lead(row, trade_terms, city_terms, hits)
        return row, n

    todo = [t for t in targets if not writer.has(t if t.startswith("http") else "https://" + t.strip("/"))]
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(one, t): t for t in todo}
            for f in as_completed(futs):
                try:
                    row, n = f.result()
                except Blocked as b:
                    log(f"  skipped {futs[f]}: {b}")
                    continue
                writer.write(row)
                log(f"  {futs[f][:40]:40} {n} pages  {row['Email'][:40]:40} {row['Phone'][:14]}")
    finally:
        writer.close()
    log(f"Done. {writer.count} rows in {args.out}")


# --------------------------------------------------------------------------- PDFs

def pdf_text_pages(data):
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = []
            for p in pdf.pages:
                try:
                    pages.append(p.extract_text(layout=True) or "")
                except TypeError:
                    pages.append(p.extract_text() or "")
            return pages
    except ImportError:
        pass
    from pypdf import PdfReader
    return [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]


def pdf_records(data, source):
    """Split PDF text into one record per contact block (blank-line or company-line separated)."""
    records = []
    for pno, text in enumerate(pdf_text_pages(data), 1):
        text = deobfuscate(text)
        lines = [l.strip() for l in text.splitlines()]
        block = []
        blocks = []
        for line in lines + [""]:
            if not line:
                if block:
                    blocks.append(block)
                block = []
            else:
                block.append(line)
        # Blocks that still hold several companies (no blank lines between entries):
        # cut them into one chunk per contact line, keeping the lines just above it.
        split = []
        for b in blocks:
            hits = [i for i, l in enumerate(b) if EMAIL_RE.search(l) or US_PHONE_RE.search(l)]
            if len(hits) <= 2 or len(b) <= 4:
                split.append(b)
                continue
            prev = 0
            for i in hits:
                split.append(b[max(prev, i - 3): i + 1])
                prev = i + 1
        blocks = split
        for b in blocks:
            joined = " ".join(b)
            emails = [e for e in (clean_email(x) for x in EMAIL_RE.findall(joined)) if e]
            phones = [p for p in (norm_phone("".join(m.groups())) for m in US_PHONE_RE.finditer(joined)) if p]
            if not emails and not phones:
                continue
            webs = re.findall(r"\b(?:https?://)?(?:www\.)?[a-z0-9\-]+\.(?:com|net|org|us|biz|co|io|info)\b", joined, re.I)
            webs = [w for w in webs if "@" not in w and not any(w.lower() in e for e in emails)]
            name = next((l for l in b if not EMAIL_RE.search(l) and not US_PHONE_RE.search(l)
                         and len(l) > 2 and not l[0].isdigit()), "")
            addr = US_ADDRESS_RE.search(US_PHONE_RE.sub(" | ", joined))
            records.append({
                "name": name, "emails": list(dict.fromkeys(emails)), "phones": list(dict.fromkeys(phones)),
                "websites": webs[:1], "socials": {},
                "address": addr.group(1) if addr else "", "city": addr.group(2) if addr else "",
                "state": addr.group(3) if addr else "", "zip": addr.group(4) if addr else "",
                "source": f"{source}#page={pno}&rec={len(records) + 1}", "text": joined,
            })
    return records


def run_pdf(args, fetcher):
    writer = LeadWriter(args.out)
    signals = terms(args.signals) or DEFAULT_SIGNALS
    trade_terms, city_terms = terms(args.trade), terms(args.city)
    total = 0
    for src in args.files:
        if src.startswith(("http://", "https://")):
            r = fetcher.get(src)
            if r is None:
                log(f"Could not download {src}")
                continue
            data = r.content
        else:
            data = open(src, "rb").read()
        recs = pdf_records(data, os.path.basename(src) if not src.startswith("http") else src)
        log(f"{src}: {len(recs)} contact blocks")
        for rec in recs:
            hits = find_signals(rec["text"], signals)
            row = build_row(rec, rec["source"], Signals=", ".join(hits))
            row["Lead Score"] = score_lead(row, trade_terms, city_terms, hits)
            if passes_filters(row, trade_terms, city_terms, []):
                writer.write(row)
                total += 1
    writer.close()
    log(f"Done. {total} new leads, {writer.count} in {args.out}")


# --------------------------------------------------------------------------- enrich

def run_enrich(args, fetcher):
    with open(args.input, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    signals = terms(args.signals) or DEFAULT_SIGNALS
    todo = [i for i, r in enumerate(rows) if r.get("Website") and not r.get("Email")]
    log(f"{len(todo)} of {len(rows)} leads have a website but no email - visiting those.")
    done = 0

    def save():
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in COLUMNS})

    def one(i):
        info, hits, _ = crawl_site(fetcher, rows[i]["Website"], args.max_pages, signals)
        return i, info, hits

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for f in as_completed([pool.submit(one, i) for i in todo]):
                try:
                    i, info, hits = f.result()
                except Blocked as b:
                    log(f"  skipped: {b}")
                    continue
                r = rows[i]
                r["Email"] = "; ".join(info["emails"][:5])
                if not r.get("Phone") and info["phones"]:
                    r["Phone"] = "; ".join(info["phones"][:3])
                for net, link in info["socials"].items():
                    r[net] = r.get(net) or link
                old = set(filter(None, (r.get("Signals") or "").split(", ")))
                r["Signals"] = ", ".join(sorted(old | set(hits)))
                r["Lead Score"] = score_lead(r, [], [], r["Signals"].split(", ") if r["Signals"] else [])
                done += 1
                log(f"  {r.get('Company', '')[:40]:40} {r['Email'][:45] or '(no email found)'}")
                if done % 25 == 0:
                    save()
    finally:
        save()
    found = sum(1 for i in todo if rows[i].get("Email"))
    log(f"Done. Found emails for {found} of {len(todo)} websites. Saved {args.out}")


# --------------------------------------------------------------------------- CLI

def terms_fields(s):
    names = {"email": "Email", "phone": "Phone", "website": "Website", "address": "Address"}
    return [names[t] for t in terms(s) if t in names]


def main():
    ap = argparse.ArgumentParser(description="Polite lead scraper for directories, websites and PDFs.")
    sub = ap.add_subparsers(dest="mode", required=True)

    def common(p, out_default):
        p.add_argument("--out", default=out_default, help="CSV file to write/append (default %(default)s)")
        p.add_argument("--delay", type=float, default=3.0, help="seconds between requests to one site (default 3)")
        p.add_argument("--trade", help="only keep leads mentioning these words, comma-separated (e.g. 'concrete,electrical')")
        p.add_argument("--city", help="only keep leads in these cities, comma-separated")
        p.add_argument("--signals", help="comma-separated buying-signal phrases (replaces the defaults)")
        p.add_argument("--user-agent", default=DEFAULT_UA)

    p = sub.add_parser("procore", help="Procore Network listing + profiles")
    common(p, "procore_leads.csv")
    p.add_argument("--state", default="ca", help="two-letter state (default ca)")
    p.add_argument("--url", help="any Procore listing URL instead of the whole state")
    p.add_argument("--max", type=int, default=0, help="stop after this many new leads (default 0 = no limit, scrape everything)")
    p.add_argument("--start-page", type=int, default=1)
    p.add_argument("--end-page", type=int, default=100000)
    p.add_argument("--require", help="only keep leads that have these, e.g. 'phone' or 'phone,website'")
    p.add_argument("--restart", action="store_true", help="ignore saved page progress")

    p = sub.add_parser("directory", help="any paginated directory site")
    common(p, "directory_leads.csv")
    p.add_argument("--list-url", required=True, help="listing URL; put {page} where the page number goes")
    p.add_argument("--follow", required=True, help="regex that matches company detail links, e.g. '/company/'")
    p.add_argument("--start-page", type=int, default=1)
    p.add_argument("--end-page", type=int, default=50)
    p.add_argument("--max", type=int, default=0, help="stop after this many new leads (0 = no limit)")
    p.add_argument("--require", help="only keep leads that have these, e.g. 'email'")

    p = sub.add_parser("site", help="company websites (or PDF links)")
    common(p, "site_leads.csv")
    p.add_argument("urls", nargs="*", help="domains or URLs")
    p.add_argument("--file", help="text file with one domain/URL per line")
    p.add_argument("--max-pages", type=int, default=6, help="pages per site (default 6)")
    p.add_argument("--workers", type=int, default=4, help="sites crawled in parallel (default 4)")

    p = sub.add_parser("pdf", help="PDF files or PDF URLs")
    common(p, "pdf_leads.csv")
    p.add_argument("files", nargs="+")

    p = sub.add_parser("enrich", help="find emails for leads that have a website")
    common(p, "")
    p.add_argument("input", help="leads CSV made by this tool")
    p.add_argument("--max-pages", type=int, default=6)
    p.add_argument("--workers", type=int, default=4)

    args = ap.parse_args()
    if args.mode == "enrich" and not args.out:
        root, ext = os.path.splitext(args.input)
        args.out = f"{root}_enriched{ext or '.csv'}"

    if getattr(args, "max", None) is not None and args.max <= 0:
        args.max = float("inf")

    fetcher = PoliteFetcher(delay=args.delay, user_agent=args.user_agent)
    {"procore": run_procore, "directory": run_directory, "site": run_site,
     "pdf": run_pdf, "enrich": run_enrich}[args.mode](args, fetcher)


if __name__ == "__main__":
    main()
