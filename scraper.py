"""
Heidelberg startup-events aggregator.

Auto-collects upcoming events from five server-rendered sources
(BioRN, hei_INNOVATION, Technologiepark Heidelberg, H3 Health Hub, Up2B) and
builds a single mobile-friendly index.html plus data/events.json (used to flag
NEW events).

H3 Health Hub and Up2B both list events across all of Germany / BW, so their
parsers keep only events that are near Heidelberg, hybrid, or online (see
NEAR_HD / ONLINE_KEYWORDS + near_or_online()).

Sources that block bots, are JavaScript-only, or only publish a past-dated news
feed (hip, KI-Garage, DeepTechHub, Eventbrite, Meetup, ...) can't be turned into
a clean "upcoming events" list, so they appear as one-tap links in an
"Also check" section instead. Add or remove sources in SOURCES / ALSO_CHECK.

Runs daily in GitHub Actions. One bad source never breaks the build.
"""

import json
import re
import sys
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --- sources that get scraped ----------------------------------------------

SOURCES = [
    {"name": "BioRN", "color": "#0a7d5a", "parser": "biorn",
     "url": "https://biorn.org/news-events/events/"},
    {"name": "hei_INNOVATION", "color": "#b3282d", "parser": "hei",
     "url": "https://www.uni-heidelberg.de/en/transfer/heiinnovation/events"},
    {"name": "Technologiepark", "color": "#004a77", "parser": "tphd",
     "url": "https://www.technologiepark-heidelberg.de/aktuelles/events/"},
    {"name": "H3 Health Hub", "color": "#0d8a8a", "parser": "h3hub",
     "url": "https://www.helmholtz-h3.de/events"},
    {"name": "Up2B", "color": "#e06a1b", "parser": "up2b",
     "url": "https://www.up2b.io/events"},
]

# sources we can't scrape into a clean upcoming-events list -> tap-through links
#   - hip:        /news-en/ is a past-dated news feed, not an events calendar
#   - KI-Garage:  server-rendered, but "Aktuelle Events" is currently empty
#                 (only past "Eventrückblick" recaps are listed)
#   - DeepTechHub: events load from a pitchload.net JS embed; the page itself
#                 server-renders "Keine Ergebnisse"
ALSO_CHECK = [
    ("Heidelberg Startup Partners (Eventbrite)",
     "https://www.eventbrite.com/o/heidelberg-startup-partners-ev-9766883937"),
    ("Heidelberg Innovation Park (hip) - News",
     "https://www.hip-heidelberg.com/en/news-en/"),
    ("KI-Garage - Events", "https://www.ki-garage.de/de/ueber-uns/ki-events"),
    ("DeepTechHub - Events", "https://deep-tech-hub.de/events/"),
    ("Life Science Accelerator BW", "https://www.lifescience-bw.de/"),
]

BIORN_EXCLUDE = {
    "life-the-biomedical-convention-2026", "life-the-biomedical-convention-2027",
    "smartlabs-summit-2025", "bac-life-science-week", "presse-news",
}
# section pages on Technologiepark that live under /aktuelles/ but aren't events
TPHD_EXCLUDE = {"news", "events", "presse", "pressekit", "found"}

# --- location filter (H3 Health Hub + Up2B) --------------------------------
# Keep events that are online/hybrid, or physically within ~roughly Rhine-Neckar
# + a few close BW neighbours. Trim NEAR_HD (e.g. drop karlsruhe/heilbronn/
# pforzheim) for a tighter radius, or add cities as needed.
ONLINE_KEYWORDS = ("online", "hybrid", "remote", "virtual", "digital",
                   "webinar", "zoom")
NEAR_HD = (
    "heidelberg", "mannheim", "ludwigshafen", "walldorf", "speyer", "weinheim",
    "schwetzingen", "wiesloch", "eppelheim", "leimen", "sinsheim", "sandhausen",
    "nussloch", "hockenheim", "dossenheim", "rhein-neckar", "rhine-neckar",
    "karlsruhe", "heilbronn", "pforzheim", "bruchsal", "bretten",
)

HEADERS = {
    "User-Agent": "HD-Events-Aggregator/1.0 (personal project)"
}

DATA_FILE = Path("data/events.json")
OUT_FILE = Path("index.html")
TODAY = date.today()
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --- helpers ----------------------------------------------------------------

def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def slug_to_title(slug):
    return re.sub(r"[-_]+", " ", slug).strip().title()


def fmt_iso(iso):
    try:
        d = datetime.strptime(iso, "%Y-%m-%d").date()
        return f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    except Exception:
        return iso


def date_from_ddmmyyyy(text):
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if not m:
        return ""
    dd, mm, yy = (int(x) for x in m.groups())
    try:
        return date(yy, mm, dd).isoformat()
    except ValueError:
        return ""


def near_or_online(location):
    """True if a location string looks online/hybrid or near Heidelberg.
    Unknown/empty locations are kept (better to show than silently hide)."""
    if not location:
        return True
    t = location.lower()
    return (any(k in t for k in ONLINE_KEYWORDS)
            or any(c in t for c in NEAR_HD))


# --- parsers ----------------------------------------------------------------

def parse_hei(html, base):
    """hei_INNOVATION event links end in an ISO date, read directly."""
    soup = BeautifulSoup(html, "html.parser")
    events, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/veranstaltungen/" not in href:
            continue
        m = re.search(r"(\d{4}-\d{2}-\d{2})/?$", href)
        if not m:
            continue
        url = urljoin(base, href)
        if url in seen:
            continue
        seen.add(url)
        iso = m.group(1)
        text = a.get_text(" ", strip=True)
        text = re.sub(r"^\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M\s*", "", text)
        text = re.sub(r"\s*-\s*$", "", text).strip(" -\u2013")
        if not text:
            text = slug_to_title(re.sub(r"-\d{4}-\d{2}-\d{2}/?$", "",
                                        href.rstrip("/").split("/")[-1]))
        events.append({"title": text, "url": url, "source": "hei_INNOVATION",
                       "date_iso": iso, "date_text": fmt_iso(iso)})
    return events


def parse_biorn(html, base):
    """BioRN 'read more' links point to /news-events/events/<slug>/."""
    soup = BeautifulSoup(html, "html.parser")
    events, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/news-events/events/" not in href or "#" in href:
            continue
        slug = href.split("/news-events/events/")[-1].strip("/")
        if not slug or slug in BIORN_EXCLUDE:
            continue
        url = urljoin(base, href)
        if url in seen:
            continue
        seen.add(url)
        title = None
        h = a.find_previous(["h5", "h4", "h3", "h2"])
        if h:
            t = h.get_text(" ", strip=True)
            if t and t.lower() != "read more" and len(t) > 4:
                title = t
        if not title:
            title = slug_to_title(slug)
        date_iso = ""
        for hd in a.find_all_previous(["h4", "h5", "h6", "h3"]):
            date_iso = date_from_ddmmyyyy(hd.get_text())
            if date_iso:
                break
        events.append({"title": title, "url": url, "source": "BioRN",
                       "date_iso": date_iso,
                       "date_text": fmt_iso(date_iso) if date_iso else "See details"})
    return events


def parse_tphd(html, base):
    """Technologiepark events link to /aktuelles/<slug>/ with a clean title=attr."""
    soup = BeautifulSoup(html, "html.parser")
    events, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/aktuelles/" not in href or "#" in href:
            continue
        slug = href.split("/aktuelles/")[-1].strip("/")
        if not slug or "/" in slug or slug in TPHD_EXCLUDE:
            continue
        url = urljoin(base, href)
        if url in seen:
            continue
        seen.add(url)
        title = (a.get("title") or "").strip()
        title = re.sub(r"^News:\s*", "", title)
        if not title or len(title) < 4:
            title = slug_to_title(slug)
        date_iso = date_from_ddmmyyyy(a.get_text(" ", strip=True))
        events.append({"title": title, "url": url, "source": "Technologiepark",
                       "date_iso": date_iso,
                       "date_text": fmt_iso(date_iso) if date_iso else "See details"})
    return events


def parse_h3hub(html, base):
    """H3 Health Hub lists each event as a heading linking to
    /events/event/<slug>/, immediately followed by its date (dd.mm.yyyy) and a
    location. We read the text between consecutive event links and keep only
    events that are near Heidelberg, hybrid, or online."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = [a for a in soup.find_all("a", href=True)
               if "/events/event/" in a["href"]]
    events, seen = [], set()
    for idx, a in enumerate(anchors):
        url = urljoin(base, a["href"])
        if url in seen:
            continue
        seen.add(url)
        title = (a.get("title") or a.get_text(" ", strip=True)).strip()
        if not title:
            title = slug_to_title(a["href"].rstrip("/").split("/")[-1])

        # collect the text after this event link, up to the next event link
        stop = anchors[idx + 1] if idx + 1 < len(anchors) else None
        parts = []
        for el in a.next_elements:
            if stop is not None and el is stop:
                break
            if getattr(el, "name", None) is None:          # NavigableString
                s = str(el).strip()
                if s:
                    parts.append(s)
        blk = " ".join(parts)
        blk = blk.replace(title, " ", 1)                   # drop the title echo

        date_iso = date_from_ddmmyyyy(blk)
        m = re.search(r"\d{1,2}\.\d{1,2}\.\d{4}\s*(.+)", blk)
        loc = (m.group(1) if m else blk)[:60]

        if not near_or_online(loc):
            continue
        events.append({"title": title, "url": url, "source": "H3 Health Hub",
                       "date_iso": date_iso,
                       "date_text": fmt_iso(date_iso) if date_iso else "See details"})
    return events


def parse_up2b(html, base):
    """Up2B (Wix) server-renders its event cards. Each card is a heading
    followed by a description and emoji-tagged meta lines (📅 date, 📍 location).
    Group by heading blocks, keep near-Heidelberg / hybrid / online events, and
    use the card's external 'More info' link as the event URL."""
    soup = BeautifulSoup(html, "html.parser")
    heads = soup.find_all(["h2", "h3", "h4", "h5", "h6"])
    events, seen = [], set()
    for idx, h in enumerate(heads):
        title = re.sub(r"^[^\w(]+", "", h.get_text(" ", strip=True)).strip()
        if len(title) < 6:
            continue

        stop = heads[idx + 1] if idx + 1 < len(heads) else None
        texts, hrefs = [], []
        for el in h.next_elements:
            if stop is not None and el is stop:
                break
            name = getattr(el, "name", None)
            if name == "a" and el.get("href"):
                hrefs.append(el["href"])
            elif name is None:                             # NavigableString
                s = str(el).strip()
                if s:
                    texts.append(s)
        block = " ".join(texts)

        # only treat blocks that actually look like an event card
        if "📍" not in block and not re.search(r"\d{1,2}\.\d{1,2}\.\d{4}", block):
            continue

        # date: prefer the 📅 line, else the first dd.mm.yyyy in the block
        date_iso = ""
        mcal = re.search(r"📅[^\d]*(\d{1,2}\.\d{1,2}\.\d{4})", block)
        if mcal:
            date_iso = date_from_ddmmyyyy(mcal.group(1))
        if not date_iso:
            date_iso = date_from_ddmmyyyy(block)

        # location: the text window after 📍 (used only for the filter)
        loc_src = block.split("📍", 1)[1][:80] if "📍" in block else block
        if not near_or_online(loc_src):
            continue

        # link: first real external "More info" / "Apply" href in the card
        url = next((hr for hr in hrefs if hr.startswith("http")), "")
        if not url:
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            url = base + "#" + slug
        if url in seen:
            continue
        seen.add(url)

        events.append({"title": title, "url": url, "source": "Up2B",
                       "date_iso": date_iso,
                       "date_text": fmt_iso(date_iso) if date_iso else "See details"})
    return events


PARSERS = {"biorn": parse_biorn, "hei": parse_hei, "tphd": parse_tphd,
           "h3hub": parse_h3hub, "up2b": parse_up2b}


# --- build ------------------------------------------------------------------

def collect():
    all_events = []
    for src in SOURCES:
        try:
            html = fetch(src["url"])
            found = PARSERS[src["parser"]](html, src["url"])
            print(f"{src['name']}: {len(found)} events")
            all_events.extend(found)
        except Exception as e:
            print(f"WARNING: {src['name']} failed: {e}", file=sys.stderr)
    return all_events


def apply_first_seen(events):
    store = {}
    if DATA_FILE.exists():
        try:
            store = json.loads(DATA_FILE.read_text())
        except Exception:
            store = {}
    today = TODAY.isoformat()
    for ev in events:
        prev = store.get(ev["url"])
        ev["first_seen"] = prev["first_seen"] if prev else today
        ev["is_new"] = (TODAY - date.fromisoformat(ev["first_seen"])).days <= 7
        store[ev["url"]] = {"title": ev["title"], "source": ev["source"],
                            "date_iso": ev["date_iso"], "first_seen": ev["first_seen"]}
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(store, indent=2, ensure_ascii=False))
    return events


def upcoming_sorted(events):
    out = [e for e in events
           if not (e["date_iso"] and e["date_iso"] < TODAY.isoformat())]
    out.sort(key=lambda e: (e["date_iso"] == "", e["date_iso"]))
    return out


def render(events):
    updated = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    colors = {s["name"]: s["color"] for s in SOURCES}
    filters = "".join(
        f'<button onclick="flt(this,\'{escape(s["name"])}\')">{escape(s["name"])}</button>'
        for s in SOURCES)
    rows = []
    for ev in events:
        c = colors.get(ev["source"], "#555")
        new = '<span class="new">NEW</span>' if ev.get("is_new") else ""
        rows.append(f"""
      <li class="card" data-id="{escape(ev['url'])}" data-source="{escape(ev['source'])}">
        <div class="date">{escape(ev['date_text'])}</div>
        <a class="title" href="{escape(ev['url'])}" target="_blank" rel="noopener">{escape(ev['title'])}</a>
        <div class="meta"><span class="tag" style="background:{c}">{escape(ev['source'])}</span>{new}<span class="going">★ Going</span></div>
      </li>""")
    items = "\n".join(rows) or '<li class="card"><div class="title">No upcoming events found right now.</div></li>'
    links = "".join(
        f'<a class="xlink" href="{escape(u)}" target="_blank" rel="noopener">{escape(n)}</a>'
        for n, u in ALSO_CHECK)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Heidelberg Startup Events</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, Segoe UI, Roboto, sans-serif;
         margin: 0; background: #f6f7f9; color: #16181d; }}
  header {{ padding: 22px 18px 10px; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 4px; }}
  .sub {{ color: #6b7280; font-size: .82rem; }}
  .filters {{ padding: 8px 18px 4px; display: flex; gap: 8px; flex-wrap: wrap; }}
  .filters button {{ border: 1px solid #d1d5db; background: #fff; color: #16181d;
         padding: 7px 13px; border-radius: 999px; font-size: .85rem; cursor: pointer; }}
  .filters button.active {{ background: #16181d; color: #fff; border-color: #16181d; }}
  ul {{ list-style: none; margin: 0; padding: 10px 14px 8px; }}
  .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 14px;
         padding: 14px 15px; margin: 10px 0; cursor: pointer; }}
  .card.marked {{ background: #e8f1ff; border-color: #9cc2ff; }}
  .going {{ display: none; font-size: .68rem; font-weight: 700; color: #0a7d5a;
         background: #d9f2e6; padding: 3px 8px; border-radius: 999px; }}
  .card.marked .going {{ display: inline-block; }}
  .date {{ font-size: .78rem; color: #6b7280; margin-bottom: 4px; }}
  .title {{ display: block; font-size: 1.02rem; font-weight: 600;
         color: #0b57d0; text-decoration: none; line-height: 1.3; }}
  .meta {{ margin-top: 9px; display: flex; align-items: center; gap: 8px; }}
  .tag {{ color: #fff; font-size: .72rem; padding: 3px 9px; border-radius: 999px; }}
  .new {{ font-size: .68rem; font-weight: 700; color: #b45309;
         background: #fef3c7; padding: 3px 8px; border-radius: 999px; }}
  .also {{ padding: 6px 18px 40px; }}
  .also h2 {{ font-size: .95rem; margin: 14px 0 4px; }}
  .also p {{ color: #6b7280; font-size: .78rem; margin: 0 0 10px; }}
  .xlink {{ display: block; background: #fff; border: 1px solid #e5e7eb;
         border-radius: 12px; padding: 12px 14px; margin: 8px 0; color: #0b57d0;
         text-decoration: none; font-size: .92rem; font-weight: 500; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0f1115; color: #e5e7eb; }}
    .card, .xlink {{ background: #181b22; border-color: #262a33; }}
    .filters button {{ background: #181b22; color: #e5e7eb; border-color: #333; }}
    .filters button.active {{ background: #e5e7eb; color: #0f1115; }}
    .title, .xlink {{ color: #7cb0ff; }}
    .card.marked {{ background: #1c2c46; border-color: #3b5b8c; }}
    .going {{ color: #7ee0b0; background: #123024; }}
  }}
</style>
</head>
<body>
  <header>
    <h1>Heidelberg Startup Events</h1>
    <div class="sub">{len(events)} upcoming &middot; updated {updated}</div>
  </header>
  <div class="filters">
    <button class="active" onclick="flt(this,'all')">All</button>
    {filters}
  </div>
  <ul id="list">{items}
  </ul>
  <div class="also">
    <h2>Also check (can't be auto-pulled)</h2>
    <p>These block scrapers, need JavaScript, or only post past news, so tap through to their pages.</p>
    {links}
  </div>
<script>
  // --- mark events as "Going" (persists per-device in localStorage) ---
  var MARK_KEY = 'hd_events_marked';
  function loadMarks() {{
    try {{ return new Set(JSON.parse(localStorage.getItem(MARK_KEY) || '[]')); }}
    catch (e) {{ return new Set(); }}
  }}
  function saveMarks(set) {{
    try {{ localStorage.setItem(MARK_KEY, JSON.stringify(Array.from(set))); }}
    catch (e) {{}}
  }}
  var marked = loadMarks();
  document.querySelectorAll('#list .card').forEach(function(card){{
    if (card.dataset.id && marked.has(card.dataset.id)) card.classList.add('marked');
  }});
  document.getElementById('list').addEventListener('click', function(e){{
    if (e.target.closest('a')) return;          // let the title link open the event
    var card = e.target.closest('.card');
    if (!card || !card.dataset.id) return;
    var id = card.dataset.id;
    if (marked.has(id)) {{ marked.delete(id); card.classList.remove('marked'); }}
    else {{ marked.add(id); card.classList.add('marked'); }}
    saveMarks(marked);
  }});

  function flt(btn, src) {{
    document.querySelectorAll('.filters button').forEach(function(b){{b.classList.remove('active');}});
    btn.classList.add('active');
    document.querySelectorAll('#list .card').forEach(function(c){{
      c.style.display = (src === 'all' || c.dataset.source === src) ? '' : 'none';
    }});
  }}
</script>
</body>
</html>
"""


def main():
    events = apply_first_seen(collect())
    events = upcoming_sorted(events)
    OUT_FILE.write_text(render(events), encoding="utf-8")
    print(f"Wrote {OUT_FILE} with {len(events)} upcoming events")


if __name__ == "__main__":
    main()
