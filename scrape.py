import argparse
import json
import re
from playwright.sync_api import sync_playwright

FILE_EXT_RE = re.compile(r"\.(pdf|xls|xlsx|csv|doc|docx)(\?.*)?$", re.IGNORECASE)

# Words that make a link a bad match even if it otherwise scores high
BAD_WORDS = ["claim", "application", "exemption"]


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


def scrape(url, timeout_ms=45000):
    result = {
        "url": url,
        "status": "no_file_found",
        "fileUrl": "",
        "linkText": "",
        "note": "",
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        )
        try:
            page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            # give SPA frameworks (AngularJS etc.) a little extra time to paint
            page.wait_for_timeout(2000)
        except Exception as e:
            result["status"] = "load_failed"
            result["note"] = f"Page load error: {e}"
            browser.close()
            return result

        try:
            anchors = page.eval_on_selector_all(
                "a", "els => els.map(e => [e.href, e.innerText])"
            )
        except Exception as e:
            anchors = []
            result["note"] = f"Anchor extraction error: {e}"

        browser.close()

    best, best_score = score_links(anchors)
    if best and best_score > 0:
        result["fileUrl"] = best[0]
        result["linkText"] = best[1]
        result["status"] = "file_found"
        result["note"] = f"Found via JS-rendered scrape (score {best_score})"
    else:
        result["note"] = result["note"] or "No matching link found after JS render"

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
