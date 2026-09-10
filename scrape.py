import argparse
import base64
import csv
import io
import json
import mimetypes
import os
import re
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ============================================================
# GENERIC (last-resort) SCORING — used only when a county has
# no specific profile below, or a new/unknown county is added
# ============================================================
FILE_EXT_RE = re.compile(r"\.(pdf|xls|xlsx|csv|doc|docx)(\?.*)?$", re.IGNORECASE)
GENERIC_BAD_WORDS = ["claim", "application", "exemption", "request form", "official claim"]

CHALLENGE_MARKERS = [
    "just a moment",
    "checking your browser",
    "verify you are human",
    "cf-browser-verification",
    "challenges.cloudflare.com",
    "attention required! | cloudflare",
    "_cf_chl_opt",
]

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Chrome PDF Plugin' })),
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters)
);
"""

# ============================================================
# PER-COUNTY KNOWLEDGE BASE
# Every fact you sent about each site's HTML/behaviour lives
# here. n8n only ever has to send {url, county}; the script
# figures out the rest by itself.
# ============================================================
COUNTY_CONFIGS = {
    "brantley": {
        "match_mode": "sentence_context",
        "anchor_text": "here",
        "context_phrase": "excess funds list",
        "skip_static": True,
        "label": "Brantley County",
    },
    "chattooga": {
        "match_mode": "sentence_context",
        "anchor_text": "here",
        "context_phrase": "excess funds",
        "skip_static": True,
        "label": "Chattooga County",
    },
    "dawson": {
        "match_mode": "exact_text",
        "text": "excess funds list",
        "skip_static": True,
        "label": "Dawson County",
    },
    "hall": {
        "match_mode": "exact_text",
        "text": "tax sale excess funds",
        "exclude_text": ["claim form"],
        "label": "Hall County",
    },
    "pierce": {
        "match_mode": "sentence_context",
        "anchor_text": "here",
        "context_phrase": "excess funds list",
        "skip_static": True,
        "label": "Pierce County",
    },
    "union": {
        "match_mode": "exact_text",
        "text": "excess tax funds listing",
        "skip_static": True,
        "label": "Union County",
    },
    "walker": {
        "match_mode": "nav_preferred_text",
        "text": "excess funds from prior tax sales",
        "label": "Walker County",
    },
    "forsyth": {
        "match_mode": "table_scrape",
        "label": "Forsyth County",
    },
    "liberty": {
        "match_mode": "nested_menu_text",
        "text": "excess funds list",
        "skip_static": True,
        "label": "Liberty County",
    },
    "pickens": {
        "match_mode": "exact_text",
        "text": "excess fund list",
        "label": "Pickens County",
    },
    "quitman": {
        "match_mode": "exact_text",
        "text": "excess funds",
        "label": "Quitman County",
    },
    "chatham": {
        "match_mode": "prefix_text",
        "prefix": "excess funds report (updated",
        "label": "Chatham County",
    },
    "carroll": {
        "match_mode": "exact_text",
        "text": "excess funds list",
        "exclude_text": ["claim form", "information", "delinquent", "legals"],
        "label": "Carroll County",
    },
    "clayton": {
        "match_mode": "exact_text",
        "text": "excess funds listing",
        "exclude_text": ["official claim"],
        "label": "Clayton County",
    },
    "dekalb": {
        "match_mode": "exact_text",
        "text": "view excess funds list",
        "exclude_text": ["claim form"],
        "label": "DeKalb County",
    },
}

# Lets the script recognise a county from its URL even if n8n
# sends the county name spelled slightly differently.
DOMAIN_HINTS = {
    "brantleytax.com": "brantley",
    "chattoogatax.com": "chattooga",
    "dawsoncountytax.com": "dawson",
    "hallcountytax.org": "hall",
    "piercegatax.com": "pierce",
    "uniongatax.com": "union",
    "walkercountytax.com": "walker",
    "forsythcountytax.com": "forsyth",
    "libertycountygatax.com": "liberty",
    "pickensgatax.com": "pickens",
    "quitmantax.org": "quitman",
    "chathamcountyga.gov": "chatham",
    "claytoncountyga.gov": "clayton",
    "carrollcountygatax.com": "carroll",
    "dekalbtaxga.gov": "dekalb",
    "dekalbtax.org": "dekalb",
}

DEFAULT_CONFIG = {"match_mode": "generic", "label": "Unrecognized county (adaptive generic matcher)"}


def normalize_key(name):
    return re.sub(r"[^a-z]", "", (name or "").lower().replace("county", ""))


def get_config(county, url):
    key = normalize_key(county)
    if key in COUNTY_CONFIGS:
        return key, COUNTY_CONFIGS[key]
    host = urlparse(url).netloc.lower()
    for snippet, k in DOMAIN_HINTS.items():
        if snippet in host:
            return k, COUNTY_CONFIGS[k]
    return None, DEFAULT_CONFIG


def norm_text(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ").strip()).lower()


# ============================================================
# CHALLENGE / JS-REQUIRED DETECTION
# ============================================================
def looks_like_challenge(status_code, html):
    if status_code == 403:
        return True
    blob = (html or "").lower()
    return any(marker in blob for marker in CHALLENGE_MARKERS)


def is_challenge_page(title, html):
    blob = f"{title} {html}".lower()
    return any(marker in blob for marker in CHALLENGE_MARKERS)


def wait_out_challenge(page, max_wait_ms=25000, poll_ms=1000):
    """
    Poll the page until any Cloudflare-style JS challenge clears, or time out.
    Cloudflare challenge pages often auto-reload/navigate mid-poll, which can
    destroy Playwright's execution context right as we call page.title()/
    page.content(). That's expected on these sites, not a real failure — so
    we swallow it and just retry on the next poll tick instead of crashing.
    """
    waited = 0
    while waited < max_wait_ms:
        try:
            title = page.title()
            html = page.content()
        except Exception:
            page.wait_for_timeout(poll_ms)
            waited += poll_ms
            continue
        if not is_challenge_page(title, html):
            return True, html
        page.wait_for_timeout(poll_ms)
        waited += poll_ms
    try:
        return False, page.content()
    except Exception:
        return False, ""


def solve_with_flaresolverr(url, timeout_ms=60000):
    payload = json.dumps({"cmd": "request.get", "url": url, "maxTimeout": timeout_ms}).encode("utf-8")
    try:
        r = requests.post(FLARESOLVERR_URL, data=payload,
                           headers={"Content-Type": "application/json"},
                           timeout=(timeout_ms / 1000) + 10)
        body = r.json()
    except Exception:
        return None, None

    if body.get("status") != "ok":
        return None, None

    solution = body.get("solution", {})
    html = solution.get("response", "")
    cookies = []
    for c in solution.get("cookies", []):
        cookie = {"name": c.get("name"), "value": c.get("value"),
                  "domain": c.get("domain"), "path": c.get("path", "/")}
        if cookie["name"] is not None and cookie["domain"]:
            cookies.append(cookie)
    return html, cookies


# ============================================================
# MATCHING ENGINE — one function, many strategies
# ============================================================
def find_match(cfg, soup, base_url):
    mode = cfg.get("match_mode", "generic")
    exclude = [e.lower() for e in cfg.get("exclude_text", [])]

    if mode == "table_scrape":
        table = soup.select_one("figure.wp-block-table table") or soup.find("table")
        if table and table.find_all("tr"):
            return {"mode": "table", "table": table}
        return None

    candidates = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = norm_text(a.get_text())
        if not href or href.lower().startswith("javascript:"):
            continue
        if href.startswith("#") and not text:
            continue
        if exclude and any(bad in text for bad in exclude):
            continue
        candidates.append((a, href, text))

    if mode == "exact_text":
        target = cfg["text"].lower()
        for a, href, text in candidates:
            if text == target or target in text:
                return {"mode": "link", "href": urljoin(base_url, href), "text": text}
        return None

    if mode == "prefix_text":
        prefix = cfg["prefix"].lower()
        for a, href, text in candidates:
            if text.startswith(prefix):
                return {"mode": "link", "href": urljoin(base_url, href), "text": text}
        return None

    if mode in ("sentence_context",):
        anchor_word = cfg.get("anchor_text", "here")
        phrase = cfg["context_phrase"]
        for a, href, text in candidates:
            if anchor_word not in text:
                continue
            parent = a.find_parent(["p", "li", "div", "td", "span"]) or a.parent
            context = norm_text(parent.get_text()) if parent else ""
            if phrase in context:
                return {"mode": "link", "href": urljoin(base_url, href), "text": text}
        return None

    if mode == "nested_menu_text":
        target = cfg["text"].lower()
        for a, href, text in candidates:
            if text == target or target in text:
                return {"mode": "link", "href": urljoin(base_url, href), "text": text}
        return None

    if mode == "nav_preferred_text":
        target = cfg["text"].lower()
        matches = [(a, href, text) for a, href, text in candidates if text == target or target in text]
        if not matches:
            return None

        def score(item):
            a, href, text = item
            s = 0
            if "/wp-content/uploads/" in href:
                s += 10
            classes = " ".join(a.get("class", []) or [])
            if "sub-item" in classes or "dropdown" in classes or "menu" in classes:
                s += 5
            if re.search(r"/20\d{2}/\d{2}/", href):
                s += 5
            return s

        matches.sort(key=score, reverse=True)
        a, href, text = matches[0]
        return {"mode": "link", "href": urljoin(base_url, href), "text": text}

    # ---- generic fallback scorer for unknown/new counties ----
    best, best_score = None, 0
    for a, href, text in candidates:
        s = 0
        href_l = href.lower()
        if FILE_EXT_RE.search(href):
            s += 10
        if "excess" in href_l or "excess" in text:
            s += 10
        if "fund" in href_l or "fund" in text:
            s += 5
        if "report" in href_l or "report" in text:
            s += 8
        if "list" in href_l or "list" in text:
            s += 4
        if text == "here" or "here" in text:
            s += 1
        if any(b in href_l or b in text for b in GENERIC_BAD_WORDS):
            s -= 20
        if s > best_score:
            best_score, best = s, (a, href, text)
    if best and best_score > 0:
        a, href, text = best
        return {"mode": "link", "href": urljoin(base_url, href), "text": text}
    return None


def table_to_csv_bytes(table):
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        row = [re.sub(r"\s+", " ", c.get_text(separator=" ").replace("\xa0", " ")).strip() for c in cells]
        if any(row):
            rows.append(row)
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode("utf-8")


# ============================================================
# FILE DOWNLOAD HELPERS (always produce base64 for n8n)
# ============================================================
def _guess_filename(file_url, content_type, content_disposition):
    if content_disposition and "filename=" in content_disposition:
        return content_disposition.split("filename=")[-1].strip('"; ')
    name = os.path.basename(urlparse(file_url).path) or "excess_funds"
    if "." not in name:
        ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ".pdf"
        name += ext
    return name


def download_via_requests(file_url, cookies=None, timeout=45):
    try:
        s = requests.Session()
        if cookies:
            for c in cookies:
                s.cookies.set(c["name"], c["value"], domain=c.get("domain"))
        r = s.get(file_url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400:
            return None, None, None, f"Download HTTP {r.status_code}"
        ct = r.headers.get("content-type", "application/octet-stream")
        fn = _guess_filename(file_url, ct, r.headers.get("content-disposition", ""))
        return base64.b64encode(r.content).decode("utf-8"), ct, fn, None
    except Exception as e:
        return None, None, None, f"Download error: {e}"


def download_via_playwright(context, file_url, timeout_ms=45000):
    try:
        resp = context.request.get(file_url, timeout=timeout_ms)
        if not resp.ok:
            return None, None, None, f"Download HTTP {resp.status}"
        body_bytes = resp.body()
        ct = resp.headers.get("content-type", "application/octet-stream")
        fn = _guess_filename(file_url, ct, resp.headers.get("content-disposition", ""))
        return base64.b64encode(body_bytes).decode("utf-8"), ct, fn, None
    except Exception as e:
        return None, None, None, f"Download error: {e}"


# ============================================================
# RESULT BUILDERS
# ============================================================
def build_result(url, county):
    return {
        "url": url,
        "county": county,
        "status": "no_file_found",
        "fileUrl": "",
        "linkText": "",
        "fileBase64": "",
        "fileContentType": "",
        "fileName": "",
        "note": "",
    }


def add_note(result, text):
    result["note"] = (result["note"] + " | " + text).strip(" |") if result["note"] else text


def finalize_link(result, match, b64, ct, fn, err):
    result["fileUrl"] = match["href"]
    result["linkText"] = match["text"]
    if b64:
        result["status"] = "file_downloaded"
        result["fileBase64"] = b64
        result["fileContentType"] = ct
        result["fileName"] = fn
    else:
        result["status"] = "file_found"
        add_note(result, f"Link matched but download failed: {err}")
    return result


def finalize_table(result, table, county):
    csv_bytes = table_to_csv_bytes(table)
    safe_county = re.sub(r"[^a-z0-9]+", "_", (county or "county").lower()).strip("_")
    result["status"] = "file_downloaded"
    result["fileBase64"] = base64.b64encode(csv_bytes).decode("utf-8")
    result["fileContentType"] = "text/csv"
    result["fileName"] = f"{safe_county}_excess_funds.csv"
    result["linkText"] = "(embedded HTML table, no separate file on this site)"
    add_note(result, "Extracted directly from in-page HTML table")
    return result


# ============================================================
# MAIN SCRAPE PIPELINE
# static fetch -> (JS render if needed) -> (FlareSolverr if
# Cloudflare-style challenge is still blocking) -> match -> download
# ============================================================
def scrape(url, county, timeout_ms=45000):
    result = build_result(url, county)
    key, cfg = get_config(county, url)
    add_note(result, f"Profile: {cfg.get('label', key or 'generic')}")

    # ---------------- Stage 1: fast static fetch ----------------
    if not cfg.get("skip_static"):
        try:
            r = requests.get(url, headers=DEFAULT_HEADERS, timeout=20, allow_redirects=True)
            if looks_like_challenge(r.status_code, r.text):
                add_note(result, "Static fetch hit a bot-challenge page — escalating to headless browser")
            else:
                soup = BeautifulSoup(r.text, "lxml")
                match = find_match(cfg, soup, r.url)
                if match:
                    if match["mode"] == "table":
                        return finalize_table(result, match["table"], county)
                    b64, ct, fn, err = download_via_requests(match["href"])
                    add_note(result, "Resolved via fast static fetch (no browser needed)")
                    return finalize_link(result, match, b64, ct, fn, err)
                add_note(result, "Static HTML fetched OK but target link not present — likely JS-rendered; escalating")
        except Exception as e:
            add_note(result, f"Static fetch error: {e}")
    else:
        add_note(result, "Known JS-rendered site — skipping static fetch, going straight to browser")

    # ---------------- Stage 2/3: Playwright (+ FlareSolverr) ----------------
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=DEFAULT_HEADERS["User-Agent"],
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9", "Upgrade-Insecure-Requests": "1"},
        )
        context.add_init_script(STEALTH_INIT_SCRIPT)
        page = context.new_page()

        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)  # let Angular/AJAX views settle
        except Exception as e:
            result["status"] = "load_failed"
            add_note(result, f"Page load error: {e}")
            browser.close()
            return result

        cleared, html = wait_out_challenge(page, max_wait_ms=25000, poll_ms=1000)
        if not cleared:
            try:
                page.reload(timeout=timeout_ms, wait_until="domcontentloaded")
                cleared, html = wait_out_challenge(page, max_wait_ms=15000, poll_ms=1000)
            except Exception as e:
                add_note(result, f"Reload error: {e}")

        if not cleared:
            fs_html, fs_cookies = solve_with_flaresolverr(url)
            if fs_cookies:
                try:
                    context.add_cookies(fs_cookies)
                    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)
                    cleared, html = wait_out_challenge(page, max_wait_ms=15000, poll_ms=1000)
                    if cleared:
                        add_note(result, "Cloudflare-style challenge cleared via FlareSolverr")
                except Exception as e:
                    add_note(result, f"FlareSolverr cookie-replay error: {e}")
            else:
                add_note(result, "FlareSolverr fallback unavailable or failed to solve")

        if not cleared:
            result["status"] = "blocked"
            add_note(result, "Still on bot-challenge page after wait + reload + FlareSolverr fallback")
            browser.close()
            return result

        try:
            final_html = page.content()
        except Exception:
            final_html = html

        soup = BeautifulSoup(final_html, "lxml")
        match = find_match(cfg, soup, page.url)

        if not match:
            add_note(result, "No matching link/table found even after full JS render")
            browser.close()
            return result

        if match["mode"] == "table":
            out = finalize_table(result, match["table"], county)
            browser.close()
            return out

        b64, ct, fn, err = download_via_playwright(context, match["href"])
        add_note(result, "Resolved via headless-browser render")
        out = finalize_link(result, match, b64, ct, fn, err)
        browser.close()
        return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="County page URL to scrape")
    parser.add_argument("--county", default="", help="County name, passed through to output")
    parser.add_argument("--out", default="result.json", help="Path to write JSON result")
    args = parser.parse_args()

    data = scrape(args.url, args.county)

    with open(args.out, "w") as f:
        json.dump(data, f, indent=2)

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
