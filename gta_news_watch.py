"""GTA 6 news watcher. Checks official + media + Reddit sources, scores each story, toasts the spicy ones.

  python gta_news_watch.py            normal run (what Task Scheduler calls)
  python gta_news_watch.py --dry-run  print what would alert, change nothing
  python gta_news_watch.py --test     send a test toast
Stdlib only. State + log live next to this file.
"""
import html, json, os, re, subprocess, sys, time, urllib.request, urllib.parse, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
LOG = os.path.join(HERE, "news_log.md")
UA = "Mozilla/5.0 gta-news-watch/1.0"
MAX_AGE_H = 12          # ignore anything older than this

GN = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
FEEDS = [
    # (kind, source name, url)
    ("official", "Rockstar YouTube", "https://www.youtube.com/feeds/videos.xml?user=RockstarGames"),
    ("official", "Rockstar Newswire", GN + urllib.parse.quote('site:rockstargames.com (VI OR "GTA 6" OR "Grand Theft Auto VI") when:2d')),
    ("news", "Google News", GN + urllib.parse.quote('("GTA 6" OR "GTA VI" OR "Grand Theft Auto VI") when:1d')),
    ("reddit", "r/GTA6 top today", "https://www.reddit.com/r/GTA6/top/.rss?t=day"),
    ("reddit", "r/GTA6 new", "https://www.reddit.com/r/GTA6/new/.rss"),
]
RELEVANT = re.compile(r"gta\s*6|gta\s*vi\b|grand theft auto\s*(6|vi)\b|vice city|rockstar|take-?two", re.I)
RUMOR = re.compile(r"leak|rumou?r|insider|allegedly|claims?\b|reportedly|sources? (say|says)|unconfirmed|datamine|speculat", re.I)
STRICT = re.compile(r"gta\s*6|gta\s*vi\b|grand theft auto\s*(6|vi)\b", re.I)
THRESHOLD = 5           # news/reddit stories need this much "spice" (official sources always alert)
WEIGHTS = [  # (regex, weight)
    (r"delay|postpone", 6), (r"cancel", 6), (r"release date|launch date|release window", 5), (r"trailer|gameplay|footage", 3),
    (r"pre-?order|price|\$\s?\d{2,3}", 3), (r"leak", 3), (r"confirm", 2), (r"insider|rumou?r|reportedly", 2),
    (r"pc (version|release|port)|steam", 3), (r"take-?two|zelnick|earnings|investor", 2), (r"dlc|expansion|online", 2),
    (r"rockstar (says|announces|reveals|confirms|drops|statement)", 3), (r"map|vice city|character|jason|lucia", 1),
    (r"new (screenshots?|images?)|screenshot", 2),
]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def strip(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def parse(kind, name, url):
    """returns list of dicts: id,title,link,ts(utc or None),pub"""
    root = ET.fromstring(fetch(url))
    out = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in root.iter("{http://www.w3.org/2005/Atom}entry"):
        link = e.find("a:link", ns)
        link = link.get("href") if link is not None else ""
        ts = e.findtext("a:published", default=None, namespaces=ns) or e.findtext("a:updated", default=None, namespaces=ns)
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            ts = None
        out.append({"id": e.findtext("a:id", default=link, namespaces=ns), "title": strip(e.findtext("a:title", default="", namespaces=ns)), "link": link, "ts": ts, "pub": name})
    for it in root.iter("item"):
        ts = it.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(ts).astimezone(timezone.utc)
        except Exception:
            ts = None
        src = it.findtext("source") or name
        title = strip(it.findtext("title", ""))
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        out.append({"id": it.findtext("guid") or it.findtext("link"), "title": title, "link": it.findtext("link") or "", "ts": ts, "pub": src})
    return out


EXPLAINER = re.compile(r"when does|when is|how many days|how to|everything we know|what we know|guide|best |tips|countdown|deal|discount|trick|should you", re.I)


def score(title):
    s = sum(w for rx, w in WEIGHTS if re.search(rx, title, re.I))
    return max(0, s - 4) if EXPLAINER.search(title) else s


def toks(t):
    return {w for w in re.findall(r"[a-z0-9$]+", t.lower()) if len(w) > 3 and w not in {"gta", "grand", "theft", "auto", "rockstar", "games", "game"}}


def toast(title, body, url):
    ps = r"""
$ErrorActionPreference='Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$t = [Security.SecurityElement]::Escape($env:W_TITLE); $b = [Security.SecurityElement]::Escape($env:W_BODY); $u = [Security.SecurityElement]::Escape($env:W_URL)
$x.LoadXml("<toast activationType='protocol' launch='$u' scenario='reminder'><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual><audio src='ms-winsoundevent:Notification.Default'/></toast>")
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""
    env = dict(os.environ, W_TITLE=title, W_BODY=body, W_URL=url)
    subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], env=env, creationflags=0x08000000, timeout=30)


def phone_push(title, body, url):
    """Push to the phone via ntfy.sh. Topic comes from the NTFY_TOPIC secret."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    payload = json.dumps({"topic": topic, "title": title, "message": body, "click": url, "priority": 4, "tags": ["video_game", "rotating_light"]}).encode("utf-8")
    req = urllib.request.Request("https://ntfy.sh/", data=payload, headers={"Content-Type": "application/json", "User-Agent": UA})
    urllib.request.urlopen(req, timeout=20).read()


def main():
    for s in (sys.stdout, sys.stderr):  # pythonw has no console; cp1252 consoles choke on curly quotes
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    dry = "--dry-run" in sys.argv
    if "--test" in sys.argv:
        toast("GTA news watcher", "Test alert - this is how breaking news will look.", "https://www.rockstargames.com/newswire")
        try:
            phone_push("GTA news watcher", "Test alert - phone notifications are working.", "https://www.rockstargames.com/newswire")
        except Exception as ex:
            print(f"[warn] phone push failed: {ex}", file=sys.stderr)
        return
    state = {"seen": {}, "primed": False}
    if os.path.exists(STATE):
        state = json.load(open(STATE, encoding="utf-8"))
    seen = state["seen"]
    now = datetime.now(timezone.utc)
    items = []
    last = state.setdefault("last", {})
    for kind, name, url in FEEDS:
        if kind == "reddit" and time.time() - last.get(name, 0) < 600:
            continue  # Reddit RSS rate-limits hard: at most every 10 min per feed
        last[name] = time.time()
        try:
            for it in parse(kind, name, url):
                it["kind"] = kind
                if it["ts"] and (now - it["ts"]).total_seconds() > MAX_AGE_H * 3600:
                    continue
                if not (STRICT if kind == "official" else RELEVANT).search(it["title"]):
                    continue
                items.append(it)
            time.sleep(2.5)  # be polite to Reddit / Google (avoids HTTP 429)
        except Exception as ex:
            print(f"[warn] {name}: {ex}", file=sys.stderr)
    # coverage clusters: how many distinct publishers carry (nearly) the same story
    for it in items:
        it["w"] = score(it["title"]); it["rumor"] = bool(RUMOR.search(it["title"])); it["tk"] = toks(it["title"])
    for it in items:
        pubs = {o["pub"] for o in items if o["kind"] == "news" and len(it["tk"] & o["tk"]) >= 3 and len(it["tk"] & o["tk"]) / max(1, len(it["tk"] | o["tk"])) >= 0.35}
        it["cover"] = len(pubs)
    alerts = []
    for it in items:
        key = it["id"] or it["link"]
        if key in seen:
            continue
        top_reddit = it["kind"] == "reddit" and "top today" in it["pub"]
        if it["kind"] == "official":
            label = "OFFICIAL"
        elif it["w"] >= THRESHOLD or (it["cover"] >= 3 and it["w"] >= 3) or (top_reddit and it["rumor"] and it["w"] >= 3):
            label = "RUMOR/LEAK" if it["rumor"] else "BIG NEWS"
        else:
            seen[key] = now.isoformat()
            continue
        seen[key] = now.isoformat()
        it["label"] = label
        alerts.append(it)
    # collapse duplicates of one story in this run (keep the highest-weight / official one)
    alerts.sort(key=lambda a: (a["label"] != "OFFICIAL", -a["w"], -a["cover"]))
    kept = []
    for a in alerts:
        if not any(len(a["tk"] & k["tk"]) >= 3 and len(a["tk"] & k["tk"]) / max(1, len(a["tk"] | k["tk"])) >= 0.35 for k in kept):
            kept.append(a)
    first_run = not state.get("primed")
    for a in kept:
        line = f"[{a['label']}] {a['title']}  ({a['pub']}, spice {a['w']}, {a['cover']} outlets)  {a['link']}"
        print(("(seeding, no toast) " if first_run else "") + line)
        if dry or first_run:
            continue
        try:
            phone_push(f"GTA 6 {a['label']}", f"{a['title']}\n{a['pub']}", a["link"])
        except Exception as ex:
            print(f"[warn] phone push failed: {ex}", file=sys.stderr)
    if not dry:
        cutoff = time.time() - 7 * 86400
        state["seen"] = {k: v for k, v in seen.items() if datetime.fromisoformat(v).timestamp() > cutoff}
        state["primed"] = True
        json.dump(state, open(STATE, "w", encoding="utf-8"))
    print(f"checked {len(items)} items, {len(kept)} alert(s)")


if __name__ == "__main__":
    main()
