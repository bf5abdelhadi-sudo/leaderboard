"""Runs ONCE: reads the Raccoon leaderboard and posts/edits a native Discord
embed through a webhook. Designed for GitHub Actions
(WEBHOOK_URL secret, MESSAGE_ID variable)."""
import datetime
import json
import os
import re

import requests

LEADERBOARD_URL = "https://raccoon-bot.com/leaderboard/387708754814304260"
PER_PAGE = "25"          # 10, 25 or 50 rows shown after the podium
COLOR = 0x0EA5E9         # embed side-bar color (blue)
MEDALS = ["🥇", "🥈", "🥉"]

FIX = "s => (s||'').replaceAll('&lt;','<').replaceAll('&gt;','>').replaceAll('&amp;','&').trim()"

SCRAPE_JS = """
() => {
  const fix = %s;
  const h1 = document.querySelector('h1');
  const body = document.body.innerText;
  const mm = body.match(/Members:\\s*(\\d+)/);

  const rows = [...document.querySelectorAll('table tbody tr')].map(tr =>
    [...tr.querySelectorAll('td')].map(td => fix(td.innerText)));

  const levelEls = [...document.querySelectorAll('body *')].filter(e =>
    e.children.length === 0 && /^Lvl\\s*\\d+$/.test(e.textContent.trim()) && !e.closest('table'));
  const podium = levelEls.map(e => {
    const row = e.parentElement;
    const xp = parseInt(row.textContent.replace(e.textContent, '').replace(/[^\\d]/g, '')) || 0;
    const nameEl = row.previousElementSibling;
    let card = row;
    for (let i = 0; i < 4 && card.parentElement && !/%%/.test(card.textContent); i++) card = card.parentElement;
    const pm = card.textContent.match(/(\\d+)%%/);
    return {level: e.textContent.replace(/\\D/g, ''), xp, name: fix(nameEl ? nameEl.innerText : ''), pct: pm ? +pm[1] : 0};
  });

  const imgs = [...document.querySelectorAll('img')]
    .map(i => ({src: i.src, w: i.getBoundingClientRect().width}))
    .filter(i => i.w >= 90 && i.src.startsWith('http'))
    .sort((a, b) => b.w - a.w);

  return {title: fix(h1 ? h1.innerText : ''), members: mm ? +mm[1] : null,
          rows, podium, avatar: imgs.length ? imgs[0].src : null};
}
""" % FIX


def scrape():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(LEADERBOARD_URL, wait_until="networkidle", timeout=60000)
        try:
            page.get_by_role("button", name="Accept").click(timeout=4000)
        except Exception:
            pass
        try:
            page.get_by_role("button", name=PER_PAGE, exact=True).click(timeout=3000)
            page.wait_for_load_state("networkidle")
        except Exception:
            pass
        page.wait_for_selector("table", timeout=30000)
        page.wait_for_timeout(1000)
        data = page.evaluate(SCRAPE_JS)
        browser.close()
    return data


def esc(text: str) -> str:
    return re.sub(r"([*_`~|>\\])", r"\\\1", text)


def isolate(text: str) -> str:
    """Keep right-to-left names (Arabic) from scrambling the rest of the line."""
    return "\u2066" + text + "\u2069"


def bar(pct: int, size: int) -> str:
    n = max(0, min(size, round(pct / 100 * size)))
    return "\u25b0" * n + "\u25b1" * (size - n)


def to_int(s: str) -> int:
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else 0


ROWS_PER_FIELD = 8


def build_embed(data: dict) -> dict:
    podium = sorted(data["podium"], key=lambda x: x["xp"], reverse=True)[:3]

    # Podium as three side-by-side fields, 2nd - 1st - 3rd, like the website
    fields = []
    order = [1, 0, 2] if len(podium) == 3 else list(range(len(podium)))
    for i in order:
        m = podium[i]
        fields.append({
            "name": f"{MEDALS[i]} {isolate(m['name'] or '?')}"[:256],
            "value": f"**{m['xp']:,} XP**\nLevel {m['level']}\n`{bar(m['pct'], 8)}` {m['pct']}%",
            "inline": True,
        })

    # Everyone else, grouped into tidy blocks
    rows = []
    for cells in data["rows"]:
        if len(cells) < 4:
            continue
        rank = to_int(cells[0])
        name = isolate(esc(cells[1][:28]))
        level = to_int(cells[2])
        xp = to_int(cells[3])
        rows.append(f"`{rank:02d}` **{name}** \u00b7 Lvl {level} \u00b7 `{xp:,} XP`")

    for n in range(0, len(rows), ROWS_PER_FIELD):
        chunk = rows[n:n + ROWS_PER_FIELD]
        fields.append({
            "name": "\U0001F4CB Other members" if n == 0 else "\u200b",
            "value": "\n".join(chunk)[:1024],
            "inline": False,
        })

    members = data["members"] or "?"
    embed = {
        "title": f"\U0001F3C6 {data['title'] or 'Leaderboard'}",
        "url": LEADERBOARD_URL,
        "description": f"\U0001F465 **{members}** members \u00b7 Ranked by experience",
        "color": COLOR,
        "fields": fields,
        "footer": {"text": "Updates automatically \u00b7 raccoon-bot.com"},
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if data.get("avatar"):
        embed["thumbnail"] = {"url": data["avatar"]}
    return embed


def main():
    webhook = os.environ["WEBHOOK_URL"].split("?")[0].rstrip("/")
    message_id = os.environ.get("MESSAGE_ID", "").strip()

    data = scrape()
    print("Scraped podium:", data["podium"])
    print("Scraped rows:", len(data["rows"]))
    payload = {"content": "", "embeds": [build_embed(data)], "attachments": []}

    if message_id:
        r = requests.patch(f"{webhook}/messages/{message_id}", json=payload, timeout=60)
        if r.status_code != 404:
            r.raise_for_status()
            print("Message updated")
            return
        print("Old message not found, posting a new one")

    r = requests.post(webhook + "?wait=true", json=payload, timeout=60)
    r.raise_for_status()
    print("NEW MESSAGE ID (save this as the MESSAGE_ID variable):", r.json()["id"])


if __name__ == "__main__":
    main()
