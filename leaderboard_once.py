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
BAR_SIZE = 8          # length of the XP progress bar (number of blocks)
PLAIN_NAME_IDS = set()  # Discord IDs that should show a plain name instead of a @mention
USE_MENTIONS = True   # True: clickable @mentions (needs the member's Discord ID); False: plain names


def who(name: str, uid) -> str:
    """Clickable mention if we have the Discord ID, otherwise the plain name."""
    # IDs ending in "00" are usually rounded by the website (not real) -> plain name
    if USE_MENTIONS and uid and uid not in PLAIN_NAME_IDS and not uid.endswith("00"):
        return f"<@{uid}>"
    return f"**{ltr(esc(name[:28]))}**"


V2_FLAG = 1 << 15     # Discord "components v2" message flag (one card, free layout)
MAX_TEXT = 3800       # Discord allows 4000 characters of text per v2 message


def row_lines(data: dict):
    out = []
    for item in data["rows"]:
        cells = item["cells"]
        if len(cells) < 4:
            continue
        rank, level, xp = to_int(cells[0]), to_int(cells[2]), to_int(cells[3])
        pm = re.search(r"(\d+)\s*%", cells[-1])
        pct = int(pm.group(1)) if pm else 0
        line = (f"{LRM}`{rank:02d}`  {who(cells[1], item.get('uid'))}  ·  "
                f"Lvl {level}  ·  `{xp:,} XP`  `{bar(pct, BAR_SIZE)} {pct:>3}%`{LRM}")
        out.append((rank, line))
    return out


def top3_lines(podium):
    return [f"{LRM}{MEDALS[i]}  {who(m['name'] or '?', m.get('uid'))}  ·  "
            f"Lvl {m['level']}  ·  **{m['xp']:,} XP**  `{bar(m['pct'], BAR_SIZE)} {m['pct']:>3}%`{LRM}"
            for i, m in enumerate(podium)]


def sorted_podium(data):
    return sorted(data["podium"], key=lambda x: x["xp"], reverse=True)[:3]


def attachments_for(image_name):
    return [{"id": 0, "filename": image_name}] if image_name else []


def build_v2(data: dict, image_name=None) -> dict:
    """ONE card: title, podium picture, top 3, the rest of the ranking, footer."""
    podium = sorted_podium(data)
    members = data["members"] or "?"
    title = (data["title"] or "Leaderboard").strip()
    rows = row_lines(data)
    footer = f"-# Updates automatically · raccoon-bot.com · last update <t:{int(time.time())}:R>"
    head = f"# \U0001F3C6  {title}\n-# \U0001F465  **{members}** members · Ranked by experience"
    top = "### \U0001F3C5  Top 3\n" + "\n".join(top3_lines(podium))

    def ranks_text(rs):
        return f"### \U0001F4CB  Ranks {rs[0][0]} – {rs[-1][0]}\n" + "\n".join(l for _, l in rs)

    while rows and len(head) + len(top) + len(footer) + len(ranks_text(rows)) > MAX_TEXT:
        rows.pop()

    def txt(c):
        return {"type": 10, "content": c}

    def sep(divider=True):
        return {"type": 14, "divider": divider, "spacing": 1}

    comps = [txt(head)]
    if image_name:
        comps.append({"type": 12, "items": [
            {"media": {"url": f"attachment://{image_name}"}, "description": "Top 3 podium"}]})
    comps += [sep(), txt(top)]
    if rows:
        comps += [sep(), txt(ranks_text(rows))]
    comps += [sep(False), txt(footer)]

    return {
        "flags": V2_FLAG,
        "components": [{"type": 17, "accent_color": COLOR, "components": comps}],
        "attachments": attachments_for(image_name),
        "allowed_mentions": {"parse": []},
    }


def build_classic(data: dict, image_name=None) -> dict:
    """Fallback: ONE normal embed (the picture can only sit at its bottom)."""
    podium = sorted_podium(data)
    rows = row_lines(data)
    fields = [{"name": "\U0001F3C6  Top 3", "value": "\n".join(top3_lines(podium)) + "\n​",
               "inline": False}]
    for n in range(0, len(rows), ROWS_PER_FIELD):
        chunk = rows[n:n + ROWS_PER_FIELD]
        fields.append({
            "name": f"\U0001F4CB  Ranks {chunk[0][0]} – {chunk[-1][0]}",
            "value": ("\n".join(l for _, l in chunk) + "\n​")[:1024],
            "inline": False,
        })
    embed = {
        "title": f"\U0001F3C6  {data['title'] or 'Leaderboard'}",
        "url": LEADERBOARD_URL,
        "description": f"\U0001F465  **{data['members'] or '?'}** members   ·   Ranked by experience\n​",
        "color": COLOR,
        "fields": fields,
        "footer": {"text": "Updates automatically · raccoon-bot.com"},
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if image_name:
        embed["image"] = {"url": f"attachment://{image_name}"}
    elif data.get("avatar"):
        embed["thumbnail"] = {"url": data["avatar"]}
    return {"content": "", "embeds": [embed], "attachments": attachments_for(image_name),
            "allowed_mentions": {"parse": []}}


def call(method, url, payload, png, image_name):
    if png:
        return requests.request(method, url, timeout=90,
                                data={"payload_json": json.dumps(payload)},
                                files={"files[0]": (image_name, png, "image/png")})
    return requests.request(method, url, timeout=90, json=payload)


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
    v2 = build_v2(data, image_name)
    classic = build_classic(data, image_name)

    if message_id:
        edit_url = f"{webhook}/messages/{message_id}"
        r = call("PATCH", edit_url + "?with_components=true",
                 {**v2, "content": None, "embeds": []}, png, image_name)
        if r.ok:
            print("Message updated (one-card layout)")
            return
        if r.status_code != 404:
            print("One-card layout refused on edit:", r.status_code, r.text[:300])
            r = call("PATCH", edit_url, classic, png, image_name)
            if r.ok:
                print("Message updated (single-embed layout)")
                print("TIP: to get the one-card layout, delete the MESSAGE_ID variable, "
                      "run once, then save the new ID printed below/at the end of that run.")
                return
            r.raise_for_status()
        print("Old message not found, posting a new one")

    r = call("POST", f"{webhook}?wait=true&with_components=true", v2, png, image_name)
    if not r.ok:
        print("One-card layout refused:", r.status_code, r.text[:300])
        r = call("POST", f"{webhook}?wait=true", classic, png, image_name)
    r.raise_for_status()
    print("NEW MESSAGE ID (save this as the MESSAGE_ID variable):", r.json()["id"])


if __name__ == "__main__":
    main()
