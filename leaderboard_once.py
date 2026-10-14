"""Runs ONCE: reads the Raccoon leaderboard and posts/edits a native Discord
embed through a webhook. Designed for GitHub Actions
(WEBHOOK_URL secret, MESSAGE_ID variable)."""
import datetime
import json
import os
import re
import time

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


PODIUM_JS = r"""
() => {
  const lv = [...document.querySelectorAll('body *')].filter(e =>
    e.children.length === 0 && /^Lvl\s*\d+$/.test(e.textContent.trim()) && !e.closest('table'));
  if (lv.length < 3) return null;
  let c = lv[0];
  while (c && !lv.every(x => c.contains(x))) c = c.parentElement;
  return c;
}
"""

HIDE_COOKIE_CSS = '[class*="cookie" i], [id*="cookie" i], [class*="consent" i] { display: none !important; }'


def scrape():
    """Returns (data, podium_png_bytes_or_None)."""
    from playwright.sync_api import sync_playwright
    png = None
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 1200}, device_scale_factor=2)
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
        try:
            page.wait_for_function("[...document.images].every(i => i.complete)", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(1000)
        data = page.evaluate(SCRAPE_JS)

        # Picture of the podium (the 3 cards with the profile pictures)
        try:
            page.add_style_tag(content=HIDE_COOKIE_CSS)
            page.evaluate("window.scrollTo(0, 0)")
            el = page.evaluate_handle(PODIUM_JS).as_element()
            box = el.bounding_box()
            pad = 16
            png = page.screenshot(full_page=True, clip={
                "x": max(box["x"] - pad, 0), "y": max(box["y"] - pad, 0),
                "width": box["width"] + 2 * pad, "height": box["height"] + 2 * pad})
        except Exception as e:
            print("Podium image failed, using text podium:", e)
        browser.close()
    return data, png


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


def podium_text_fields(podium):
    """Fallback if the podium picture could not be made: 3 columns of text."""
    places = ["1st", "2nd", "3rd"]
    order = [1, 0, 2] if len(podium) == 3 else list(range(len(podium)))
    out = []
    for i in order:
        m = podium[i]
        out.append({
            "name": f"{MEDALS[i]}  {places[i]} place",
            "value": (f"{LRM}{who(m['name'] or '?', m.get('uid'))}\n"
                      f"**{m['xp']:,} XP**\nLevel {m['level']}\n"
                      f"`{bar(m['pct'], 8)}` {m['pct']}%\n​"),
            "inline": True,
        })
    return out


def top3_field(podium):
    """Clickable names of the top 3 (the picture can't have clickable names)."""
    lines = [f"{LRM}{MEDALS[i]}   {who(m['name'] or '?', m.get('uid'))}   ·   "
             f"Lvl {m['level']}   ·   `{m['xp']:,} XP`{LRM}" for i, m in enumerate(podium)]
    return {"name": "\U0001F3C6  Top 3", "value": "\n".join(lines) + "\n​", "inline": False}


def build_embeds(data: dict, image_name=None) -> list:
    podium = sorted(data["podium"], key=lambda x: x["xp"], reverse=True)[:3]
    members = data["members"] or "?"

    # Embed 1: title + the podium picture
    head = {
        "title": f"\U0001F3C6  {data['title'] or 'Leaderboard'}",
        "url": LEADERBOARD_URL,
        "description": f"\U0001F465  **{members}** members   ·   Ranked by experience\n​",
        "color": COLOR,
    }
    fields = []
    if image_name:
        head["image"] = {"url": f"attachment://{image_name}"}
        fields.append(top3_field(podium))
    else:
        if data.get("avatar"):
            head["thumbnail"] = {"url": data["avatar"]}
        head["fields"] = podium_text_fields(podium)

    # Embed 2: everyone else
    rows = []
    for item in data["rows"]:
        cells = item["cells"]
        if len(cells) < 4:
            continue
        rank = to_int(cells[0])
        level = to_int(cells[2])
        xp = to_int(cells[3])
        line = (f"{LRM}`{rank:02d}`   {who(cells[1], item.get('uid'))}   ·   "
                f"Lvl {level}   ·   `{xp:,} XP`{LRM}")
        rows.append((rank, line))

    for n in range(0, len(rows), ROWS_PER_FIELD):
        chunk = rows[n:n + ROWS_PER_FIELD]
        fields.append({
            "name": f"\U0001F4CB  Ranks {chunk[0][0]} – {chunk[-1][0]}",
            "value": ("\n".join(line for _, line in chunk) + "\n​")[:1024],
            "inline": False,
        })

    body = {
        "color": COLOR,
        "fields": fields,
        "footer": {"text": "Updates automatically · raccoon-bot.com"},
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    return [head, body]


def main():
    webhook = os.environ["WEBHOOK_URL"].split("?")[0].rstrip("/")
    message_id = os.environ.get("MESSAGE_ID", "").strip()

    data, png = scrape()
    print("Scraped podium:", data["podium"])
    print("Scraped rows:", len(data["rows"]))
    ids = sum(1 for r in data["rows"] if r.get("uid")) + sum(1 for m in data["podium"] if m.get("uid"))
    print("Discord IDs found:", ids, "of", len(data["rows"]) + len(data["podium"]))
    print("Podium picture:", f"{len(png)} bytes" if png else "none")

    image_name = f"podium-{int(time.time())}.png" if png else None
    payload = {"content": "", "embeds": build_embeds(data, image_name), "attachments": [],
               "allowed_mentions": {"parse": []}}
    kwargs = {"json": payload}
    if png:
        payload["attachments"] = [{"id": 0, "filename": image_name}]
        kwargs = {"data": {"payload_json": json.dumps(payload)},
                  "files": {"files[0]": (image_name, png, "image/png")}}

    if message_id:
        r = requests.patch(f"{webhook}/messages/{message_id}", timeout=90, **kwargs)
        if r.status_code != 404:
            r.raise_for_status()
            print("Message updated")
            return
        print("Old message not found, posting a new one")

    r = requests.post(webhook + "?wait=true", timeout=90, **kwargs)
    r.raise_for_status()
    print("NEW MESSAGE ID (save this as the MESSAGE_ID variable):", r.json()["id"])


if __name__ == "__main__":
    main()
