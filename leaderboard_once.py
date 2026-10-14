"""Runs ONCE: screenshots the leaderboard and posts/edits a Discord webhook message.
Designed for GitHub Actions (WEBHOOK_URL secret, MESSAGE_ID variable)."""
import io
import json
import os
import time

import requests
from playwright.sync_api import sync_playwright

LEADERBOARD_URL = "https://raccoon-bot.com/leaderboard/387708754814304260"
WEBHOOK_URL = os.environ["WEBHOOK_URL"].split("?")[0].rstrip("/")
MESSAGE_ID = os.environ.get("MESSAGE_ID", "").strip()
PER_PAGE = "25"  # 10, 25 or 50


def take_screenshot() -> bytes:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(LEADERBOARD_URL, wait_until="networkidle", timeout=60000)
        try:
            page.get_by_role("button", name=PER_PAGE, exact=True).click(timeout=3000)
            page.wait_for_load_state("networkidle")
        except Exception:
            pass
        page.wait_for_selector("table", timeout=30000)
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
