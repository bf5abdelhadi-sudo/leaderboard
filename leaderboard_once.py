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
  const findId = el => {
    if (!el) return null;
    const h = el.outerHTML;
    const m = h.match(/avatars\\/(\\d{17,20})\\//) || h.match(/(?:users|profile|user|member)s?\\/(\\d{17,20})/);
    return m ? m[1] : null;
  };
  const h1 = document.querySelector('h1');
  const body = document.body.innerText;
  const mm = body.match(/Members:\\s*(\\d+)/);

  const rows = [...document.querySelectorAll('table tbody tr')].map(tr => ({
    cells: [...tr.querySelectorAll('td')].map(td => fix(td.innerText)),
    uid: findId(tr)}));

  const levelEls = [...document.querySelectorAll('body *')].filter(e =>
    e.children.length === 0 && /^Lvl\\s*\\d+$/.test(e.textContent.trim()) && !e.closest('table'));
  const podium = levelEls.map(e => {
    const row = e.parentElement;
    const xp = parseInt(row.textContent.replace(e.textContent, '').replace(/[^\\d]/g, '')) || 0;
    const nameEl = row.previousElementSibling;
    let card = row;
    for (let i = 0; i < 4 && card.parentElement && !/%%/.test(card.textContent); i++) card = card.parentElement;
    const pm = card.textContent.match(/(\\d+)%%/);
    let c = e, uid = null;
    for (let i = 0; i < 8 && c.parentElement; i++) {
      c = c.parentElement;
      if ((c.textContent.match(/Lvl\\s*\\d+/g) || []).length > 1) break;
      uid = findId(c) || uid;
    }
    return {level: e.textContent.replace(/\\D/g, ''), xp, name: fix(nameEl ? nameEl.innerText : ''), pct: pm ? +pm[1] : 0, uid};
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


LRM = "‎"  # invisible left-to-right mark: keeps Arabic names from flipping the line


def ltr(text: str) -> str:
    return LRM + text + LRM


def bar(pct: int, size: int) -> str:
    n = max(0, min(size, round(pct / 100 * size)))
    return "▰" * n + "▱" * (size - n)


def to_int(s: str) -> int:
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else 0


ROWS_PER_FIELD = 8
USE_MENTIONS = True   # True: clickable @mentions (needs the member's Discord ID); False: plain names


def who(name: str, uid) -> str:
    """Clickable mention if we have the Discord ID, otherwise the plain name."""
    if USE_MENTIONS and uid:
        return f"<@{uid}>"
    return f"**{ltr(esc(name[:28]))}**"


def build_embed(data: dict) -> dict:
    podium = sorted(data["podium"], key=lambda x: x["xp"], reverse=True)[:3]
    places = ["1st", "2nd", "3rd"]

    # Podium as three side-by-side columns: 2nd - 1st - 3rd, like the website.
    # (Mentions only render inside field VALUES, so the name goes in the value.)
    fields = []
    order = [1, 0, 2] if len(podium) == 3 else list(range(len(podium)))
    for i in order:
        m = podium[i]
        fields.append({
            "name": f"{MEDALS[i]}  {places[i]} place",
            "value": (
                f"{LRM}{who(m['name'] or '?', m.get('uid'))}\n"
                f"**{m['xp']:,} XP**\n"
                f"Level {m['level']}\n"
                f"`{bar(m['pct'], 8)}` {m['pct']}%\n"
                "\u200b"
            ),
            "inline": True,
        })

    rows = []
    for item in data["rows"]:
        cells = item["cells"]
        if len(cells) < 4:
            continue
        rank = to_int(cells[0])
        level = to_int(cells[2])
        xp = to_int(cells[3])
        line = (f"{LRM}`{rank:02d}`   {who(cells[1], item.get('uid'))}   \u00b7   "
                f"Lvl {level}   \u00b7   `{xp:,} XP`{LRM}")
        rows.append((rank, line))

    for n in range(0, len(rows), ROWS_PER_FIELD):
        chunk = rows[n:n + ROWS_PER_FIELD]
        first, last = chunk[0][0], chunk[-1][0]
        fields.append({
            "name": f"\U0001F4CB  Ranks {first} \u2013 {last}",
            "value": ("\n".join(line for _, line in chunk) + "\n\u200b")[:1024],
            "inline": False,
        })

    members = data["members"] or "?"
    embed = {
        "title": f"\U0001F3C6  {data['title'] or 'Leaderboard'}",
        "url": LEADERBOARD_URL,
        "description": f"\U0001F465  **{members}** members   \u00b7   Ranked by experience\n\u200b",
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
    ids = sum(1 for r in data["rows"] if r.get("uid")) + sum(1 for m in data["podium"] if m.get("uid"))
    print("Discord IDs found:", ids, "of", len(data["rows"]) + len(data["podium"]))
    payload = {"content": "", "embeds": [build_embed(data)], "attachments": [],
               "allowed_mentions": {"parse": []}}

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
