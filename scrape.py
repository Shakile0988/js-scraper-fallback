import argparse
import json
import os
import re
import base64
import mimetypes
import urllib.request
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

FILE_EXT_RE = re.compile(r"\.(pdf|xls|xlsx|csv|doc|docx)(\?.*)?$", re.IGNORECASE)

# Words that make a link a bad match even if it otherwise scores high
BAD_WORDS = ["claim", "application", "exemption"]

# Markers that indicate we're still looking at a Cloudflare (or similar) challenge page
CHALLENGE_MARKERS = [
    "just a moment",
    "checking your browser",
    "verify you are human",
    "cf-browser-verification",
    "challenges.cloudflare.com",
    "attention required! | cloudflare",
]

# Domains n8n Cloud's server cannot reach itself (DNS/IP blocked at their end).
# ONLY for these, this script downloads the file itself and sends it back as
# base64. For every other county, nothing changes — fileUrl is passed to n8n
# exactly like before and n8n fetches it on its own.
N8N_BLOCKED_DOMAINS = [
    "chathamcountyga.gov",
]

# Base URL of the FlareSolverr container. Defaults to the standard local port —
# in the GitHub Action this is reachable at localhost because FlareSolverr runs
# as a `services:` container on the same runner, with its port mapped out.
FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")

STEALTH_INIT_SCRIPT = """
// Patch navigator.webdriver
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Fake a realistic plugins array
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Chrome PDF Plugin' })),
});

// Fake languages
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// Fake window.chrome
window.chrome = { runtime: {} };

// Patch permissions.query for notifications (common headless tell)
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters)
);

// Hide the automation-controlled hint some sites check for
Object.defineProperty(navigator, 'webdriver', { get: () => false });
"""


def is_blocked_domain(url):
    """True only for domains n8n Cloud's own server can't reach."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(blocked in host for blocked in N8N_BLOCKED_DOMAINS)


def score_links(anchors):
    """Score every anchor found on the rendered page and return the best match."""
    best = None
    best_score = 0
    for href, text in anchors:
        if not href:
            continue
        href_l = href.lower()
        text_l = (text or "").lower().strip()

        score = 0
        if FILE_EXT_RE.search(href):
            score += 10
        if "excess" in href_l or "excess" in text_l:
            score += 10
        if "fund" in href_l or "fund" in text_l:
            score += 5
        if text_l == "here" or "here" in text_l:
            score += 3
        if any(bad in href_l or bad in text_l for bad in BAD_WORDS):
            score -= 20

        if score > best_score:
            best_score = score
            best = (href, text_l)

    return best, best_score


def is_challenge_page(title, html):
    blob = f"{title} {html}".lower()
    return any(marker in blob for marker in CHALLENGE_MARKERS)


def wait_out_challenge(page, max_wait_ms=25000, poll_ms=1000):
    """Poll the page until any Cloudflare-style JS challenge clears, or time out."""
    waited = 0
    while waited < max_wait_ms:
        title = page.title()
        html = page.content()
        if not is_challenge_page(title, html):
            return True, html
        page.wait_for_timeout(poll_ms)
        waited += poll_ms
    return False, page.content()


def solve_with_flaresolverr(url, timeout_ms=60000):
    """
    Ask the FlareSolverr container to solve the challenge for `url`.
    Returns (html, cookies) on success, or (None, None) on any failure.
    `cookies` is a list of dicts already shaped for Playwright's
    context.add_cookies().
    """
    payload = json.dumps({
        "cmd": "request.get",
        "url": url,
        "maxTimeout": timeout_ms,
    }).encode("utf-8")

    req = urllib.request.Request(
        FLARESOLVERR_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=(timeout_ms / 1000) + 10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None, None

    if body.get("status") != "ok":
        return None, None

    solution = body.get("solution", {})
    html = solution.get("response", "")

    cookies = []
    for c in solution.get("cookies", []):
        cookie = {
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
        }
        if cookie["name"] is not None and cookie["domain"]:
            cookies.append(cookie)

    return html, cookies


def download_file_as_base64(context, file_url):
    """
    Download the found file using the SAME Playwright browser context
    (so any Cloudflare/challenge cookies already set are reused). Only
    called for domains n8n Cloud itself can't reach.
    """
    try:
        resp = context.request.get(file_url, timeout=45000)
        if not resp.ok:
            return None, None, None, f"Download HTTP {resp.status}"

        body_bytes = resp.body()
        content_type = resp.headers.get("content-type", "application/octet-stream")

        cd = resp.headers.get("content-disposition", "")
        filename = None
        if "filename=" in cd:
            filename = cd.split("filename=")[-1].strip('"; ')
        if not filename:
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".pdf"
            filename = f"excess_funds{ext}"

        b64 = base64.b64encode(body_bytes).decode("utf-8")
        return b64, content_type, filename, None
    except Exception as e:
        return None, None, None, f"Download error: {e}"


def scrape(url, timeout_ms=45000):
    result = {
        "url": url,
        "status": "no_file_found",
        "fileUrl": "",
        "linkText": "",
        "note": "",
    }

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
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Upgrade-Insecure-Requests": "1",
            },
        )
        context.add_init_script(STEALTH_INIT_SCRIPT)
        page = context.new_page()

        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except Exception as e:
            result["status"] = "load_failed"
            result["note"] = f"Page load error: {e}"
            browser.close()
            return result

        cleared = True
        try:
            cleared, html = wait_out_challenge(page, max_wait_ms=25000, poll_ms=1000)
            if not cleared:
                # One more shot: reload now that any challenge cookies have been set
                page.reload(timeout=timeout_ms, wait_until="domcontentloaded")
                cleared, html = wait_out_challenge(page, max_wait_ms=15000, poll_ms=1000)

            if not cleared:
                # Manual stealth wasn't enough on its own — hand the challenge to
                # FlareSolverr, then replay its solved cookies into this SAME
                # Playwright context so everything downstream (anchor extraction,
                # scoring, JSON output) runs exactly as before.
                fs_html, fs_cookies = solve_with_flaresolverr(url)
                if fs_cookies:
                    try:
                        context.add_cookies(fs_cookies)
                        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                        page.wait_for_timeout(2000)
                        cleared, html = wait_out_challenge(page, max_wait_ms=15000, poll_ms=1000)
                        if cleared:
                            result["note"] = "Challenge cleared via FlareSolverr fallback"
                    except Exception as e:
                        result["note"] = f"FlareSolverr cookie-replay error: {e}"
                else:
                    result["note"] = "FlareSolverr fallback unavailable or failed to solve"

            if not cleared:
                result["status"] = "blocked"
                result["note"] = (
                    result["note"]
                    or "Still on bot-challenge page after wait + reload + FlareSolverr fallback"
                )
                browser.close()
                return result
        except Exception as e:
            result["note"] = f"Challenge-wait error: {e}"

        try:
            anchors = page.eval_on_selector_all(
                "a", "els => els.map(e => [e.href, e.innerText])"
            )
        except Exception as e:
            anchors = []
            result["note"] = f"Anchor extraction error: {e}"

        best, best_score = score_links(anchors)
        if best and best_score > 0:
            result["fileUrl"] = best[0]
            result["linkText"] = best[1]
            result["status"] = "file_found"
            if not result["note"]:
                result["note"] = f"Found via JS-rendered scrape (score {best_score})"

            # ONLY for domains n8n itself can't reach — every other county keeps
            # working exactly as before (n8n fetches fileUrl on its own).
            if is_blocked_domain(best[0]):
                b64, content_type, filename, dl_err = download_file_as_base64(context, best[0])
                if b64:
                    result["fileBase64"] = b64
                    result["fileContentType"] = content_type
                    result["fileName"] = filename
                    result["status"] = "file_downloaded"
                else:
                    result["note"] += f" | Blocked-domain download failed: {dl_err}"
        else:
            result["note"] = result["note"] or "No matching link found after JS render"

        browser.close()

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="County page URL to render and scrape")
    parser.add_argument("--county", default="", help="County name, passed through to output")
    parser.add_argument("--out", default="result.json", help="Path to write JSON result")
    args = parser.parse_args()

    data = scrape(args.url)
    data["county"] = args.county

    with open(args.out, "w") as f:
        json.dump(data, f, indent=2)

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
