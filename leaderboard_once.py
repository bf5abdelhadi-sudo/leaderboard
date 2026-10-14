"""Runs ONCE: screenshots the leaderboard (cropped, no cookie banner) and
posts/edits a Discord webhook message. Designed for GitHub Actions
(WEBHOOK_URL secret, MESSAGE_ID variable)."""
import io
import json
import os
import time

import requests
from playwright.sync_api import sync_playwright

LEADERBOARD_URL = "https://raccoon-bot.com/leaderboard/387708754814304260"
WEBHOOK_URL = os.environ["WEBHOOK_URL"].split("?")[0].rstrip("/")
MESSAGE_ID = os.environ.get("MESSAGE_ID", "").strip()
PER_PAGE = "10"  # 10 = almost square, 25 = taller, 50 = very tall

FIX_ENTITIES_JS = """
() => {
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    n.textContent = n.textContent
      .replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');
  }
}
"""

HIDE_COOKIE_CSS = """
[class*="cookie" i], [id*="cookie" i], [class*="consent" i] { display: none !important; }
"""


def take_screenshot() -> bytes:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(LEADERBOARD_URL, wait_until="networkidle", timeout=60000)

        # Accept cookies, then hide any leftover banner
        try:
            page.get_by_role("button", name="Accept").click(timeout=4000)
        except Exception:
            pass
        page.add_style_tag(content=HIDE_COOKIE_CSS)

        # Choose rows per page
        try:
            page.get_by_role("button", name=PER_PAGE, exact=True).click(timeout=3000)
            page.wait_for_load_state("networkidle")
        except Exception:
            pass

        page.wait_for_selector("table", timeout=30000)
        page.evaluate(FIX_ENTITIES_JS)
        page.wait_for_timeout(1000)

        # Crop: from the title down to just below the table (no menu, no footer)
        try:
            h1 = page.locator("h1").first.bounding_box()
            tbl = page.locator("table").first.bounding_box()
            top = max(h1["y"] - 30, 0)
            bottom = tbl["y"] + tbl["height"] + 80  # includes page numbers
            clip = {"x": 70, "y": top, "width": 1260, "height": bottom - top}
            png = page.screenshot(full_page=True, clip=clip)
        except Exception:
            png = page.screenshot(full_page=True)

        browser.close()
        return png


def files(png):
    return {"files[0]": ("leaderboard.png", io.BytesIO(png), "image/png")}


png = take_screenshot()

if MESSAGE_ID:
    payload = {"content": f"Last update: <t:{int(time.time())}:R>", "attachments": []}
    r = requests.patch(
        f"{WEBHOOK_URL}/messages/{MESSAGE_ID}",
        files=files(png),
        data={"payload_json": json.dumps(payload)},
        timeout=60,
    )
    if r.status_code != 404:
        r.raise_for_status()
        print("Message updated")
        raise SystemExit(0)
    print("Old message not found, posting a new one")

r = requests.post(
    WEBHOOK_URL + "?wait=true",
    files=files(png),
    data={"payload_json": json.dumps({"content": ""})},
    timeout=60,
)
r.raise_for_status()
print("NEW MESSAGE ID (save this as the MESSAGE_ID variable):", r.json()["id"])
