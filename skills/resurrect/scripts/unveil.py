#!/usr/bin/env python3
"""Build the museum page: a single self-contained HTML file people can share.

Reads  museum/exhibit.json (written by the agent; schema below), codex/parity.json,
       evidence.jsonl, state.json
Writes museum/index.html (all images inlined, no external requests)

exhibit.json
{
  "original_name": "Google Reader",        # used descriptively only
  "codename": "Riverbed",                  # share-safe mode only, a restoration keeps the original name
  "tagline": "The feed reader the internet still misses.",
  "born": "2005-10-07", "died": "2013-07-01", "cause_of_death": "Shut down by its owner",
  "story": ["Short paragraph...", "..."],  # 1-4 paragraphs, plain text, evidence-based, no E-ids
  "pairs": [{"title": "Home", "then": "dossier/screens/home-2011.png",
             "now": "museum/shots/home.png", "caption": "...", "evidence": ["E0012"],
             "then_credit": "Internet Archive capture, 2011-03-02",
             "privacy_checked": true}],  # once checked: no faces, names, emails, usernames (crop/redact first)
  "text_pairs": [{"title": "Forecast feed", "then": "dossier/raw/<archived>.xml", "now": "museum/now-feed.xml"}],
  "quotes": [{"text": "...", "who": "HN commenter", "url": "...", "evidence": "E0040"}],
  "play_url": "https://...", "repo_url": "https://...",
  # media (exhibit only):
  "fragments": [{"title": "2021 news clip", "kind": "video excerpt", "official": true, "url": "https://..."},
                {"title": "audio upload", "kind": "audio", "official": false, "where": "archive.org (described, not linked)"}],
  "hunt": [{"what": "full episode", "searched": "archive.org, YouTube, museum catalogue", "lead": "studio archive"}],
  "known_scenes": [{"scene": "Snuffy knocks over a block house", "quote": "<short verbatim line quoted by the press>",
                    "source": "Time, 1992", "url": "https://...", "note": "reported by test-screening staff"}],
  "videos": [{"file": "codex/footdiff/side-by-side.mp4", "caption": "Original capture (left) beside the rebuild",
              "privacy_checked": true}],  # same check as pairs, the footage shows nothing personal
  "unknowns": ["What the settings page looked like before 2008",
               {"what": "Launch date", "evidence": "E0015"}]  # if it repeats a ledger unknown, it's only shown once
}

Trace counts leave out ledger items of kind "verify" (footdiff or verify.py results about the rebuild),
since those measure the restoration, not the original.
"""
from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import html
import json
import difflib
import mimetypes
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ledger import EXHIBIT_ONLY, find_ws, load_state, read_jsonl  # noqa: E402
from verify import compute  # noqa: E402
from _common import markdown_html  # noqa: E402

E = html.escape
KNOWN_KEYS = {"original_name", "codename", "tagline", "born", "died", "cause_of_death", "story", "pairs",
              "text_pairs", "quotes", "play_url", "repo_url", "fragments", "hunt", "known_scenes", "videos",
              "unknowns"}
COLUMN = 1000  # page column in CSS px (.wrap max-width minus padding)
VIDEO_INLINE_MAX = 15 * 1024 * 1024
OWN_DIRS = ("codex/", "museum/", "app/")  # our own outputs, never traces of the original


def img_size(ws: Path, rel: str, clip=None):
    """Natural pixel size of an image or its clip, or None without Pillow."""
    if clip:
        return int(clip[2]), int(clip[3])
    try:
        from PIL import Image
        with Image.open((ws / rel) if not Path(rel).is_absolute() else Path(rel)) as im:
            return im.size
    except Exception:
        return None


def shown_width(w: int, room: int) -> tuple[int, int]:
    """(CSS width, scale) at a whole multiple of the natural width, never a fractional upscale.
    The page's script redoes this for the real column width."""
    k = max(1, room // w)
    return w * k, k


def source_key(src) -> str | None:
    """One distinct source: a web host (the archived host for Wayback) or a local file."""
    src = str(src or "").strip()
    if not src:
        return None
    if src.startswith(("http://", "https://")):
        u = urllib.parse.urlparse(src)
        host = u.netloc.lower()
        m = re.match(r"/web/[0-9a-z_*]+/(.+)", u.path) if host.endswith("web.archive.org") else None
        if m:
            inner = m.group(1) if "://" in m.group(1) else "http://" + m.group(1)
            host = urllib.parse.urlparse(inner).netloc.lower() or host
        host = host.split("@")[-1].split(":")[0]
        return host[4:] if host.startswith("www.") else host
    rel = src.split("#")[0].split("?")[0].lstrip("./")
    if rel.startswith(OWN_DIRS):
        return None
    return "file:" + rel


def _norm(t: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(t).lower()).split())


def _same_text(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or (min(len(a), len(b)) >= 16 and (a in b or b in a)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def video_src(ws: Path, rel: str) -> str | None:
    p = (ws / rel) if not Path(rel).is_absolute() else Path(rel)
    if not p.exists():
        print(f"warning: video not found: {rel}", file=sys.stderr)
        return None
    mime = mimetypes.guess_type(p.name)[0] or "video/mp4"
    if p.stat().st_size <= VIDEO_INLINE_MAX:  # inline, so the page stays one file
        return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    museum = (ws / "museum").resolve()
    if museum not in p.resolve().parents:
        dest = museum / "media" / p.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
        p = dest
    print(f"note: {rel} is over {VIDEO_INLINE_MAX // 2**20} MB, so it is linked, not inlined: share "
          f"{p.resolve().relative_to(museum)} with index.html", file=sys.stderr)
    return urllib.parse.quote(p.resolve().relative_to(museum).as_posix())


def pair_html(a: str, b: str, sa, sb, then_alt: str, now_alt: str) -> str:
    """Then/now at a whole-number pixel scale. Same-shaped pairs get a reveal slider, others sit side by side."""
    def lcm(x, y):
        import math
        return x * y // math.gcd(x, y)
    if sa and sb and sa[0] * sb[1] == sb[0] * sa[1]:  # same shape, so overlay with a slider
        lw = lcm(sa[0], sb[0])
        if lw <= COLUMN:
            width = lw * (COLUMN // lw)
        elif min(sa[0], sb[0]) >= COLUMN:
            width = min(sa[0], sb[0])  # both shrink to fit, no upscaling at all
        else:
            width = None
        if width:
            px = " px" if width > min(sa[0], sb[0]) else ""
            unit = lw if lw <= COLUMN else width
            return (f"<div class='cmpwrap' data-w='{unit}' style='width:{width}px'><div class='lbls'><span>◀ THEN</span>"
                    f"<span>NOW ▶</span></div><div class='cmp{px}'><img src='{a}' alt='{E(then_alt)}'>"
                    f"<img class='now' src='{b}' alt='{E(now_alt)}'></div><input type='range' min='0' max='100' "
                    f"value='50' aria-label='Reveal then or now'></div>")
    cells = []
    for lbl, src, size, alt in (("THEN", a, sa, then_alt), ("NOW", b, sb, now_alt)):
        if size:
            w, k = shown_width(size[0], (COLUMN - 16) // 2)
            style, cls = f" data-w='{size[0]}' data-half='1' style='width:{w}px'", " class='px'" if k > 1 else ""
        else:
            style, cls = "", ""  # natural size, never upscaled
        cells.append(f"<figure><div class='lbl2'>{lbl}</div><img{cls}{style} src='{src}' alt='{E(alt)}'></figure>")
    return "<div class='sbs'>" + "".join(cells) + "</div>"


def img_data(ws: Path, rel: str | None, clip=None) -> str | None:
    if not rel:
        return None
    p = (ws / rel) if not Path(rel).is_absolute() else Path(rel)
    if not p.exists():
        print(f"warning: image not found: {rel}", file=sys.stderr)
        return None
    if clip:  # same region the visual score compared
        from io import BytesIO
        from PIL import Image
        x, y, w, h = clip
        buf = BytesIO()
        Image.open(p).convert("RGB").crop((x, y, x + w, y + h)).save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def year(d: str | None) -> str:
    import re
    d = (d or "").strip()
    m = re.match(r"(?:c\.\s*|~|by\s+)?(\d{4})", d)
    return (d[:m.end()] if m else d[:14]) or "?"


def pct(x):
    return "—" if x is None else f"{round(x * 100)}%"


CSS = """
:root{--bg:#f6f3ee;--ink:#1d1b18;--muted:#6d665c;--line:#ddd5c8;--card:#fffdf9;--accent:#a4471c;--ok:#2f6b3a}
@media (prefers-color-scheme:dark){:root{--bg:#141311;--ink:#efe9df;--muted:#a59c8f;--line:#2e2b27;--card:#1c1a17;--accent:#e08a5a;--ok:#7fbf8a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 Georgia,'Iowan Old Style',serif}
.wrap{max-width:1040px;margin:0 auto;padding:56px 20px 80px}
.eyebrow{font:600 12px/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:.18em;text-transform:uppercase;color:var(--accent)}
h1{font-size:clamp(40px,7vw,76px);line-height:1;margin:14px 0 10px;font-weight:400;letter-spacing:-.02em}
.tag{font-size:21px;color:var(--muted);margin:0 0 28px;max-width:720px}
.dates{display:flex;gap:0;align-items:center;font:500 14px ui-sans-serif,system-ui,sans-serif;color:var(--muted);flex-wrap:wrap;row-gap:8px}
.dates b{color:var(--ink);font-size:22px;font-weight:600;margin-right:6px}.dates .sep{width:48px;height:1px;background:var(--line);margin:0 14px}
.score{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--line);border:1px solid var(--line);margin:40px 0;border-radius:6px;overflow:hidden}
.score div{background:var(--card);padding:18px 18px 16px}.score .n{font:600 34px/1.1 ui-sans-serif,system-ui,sans-serif}
.score .l{font:500 12px ui-sans-serif,system-ui,sans-serif;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.score .main .n{color:var(--accent)}
.tp{display:grid;grid-template-columns:1fr 1fr;gap:12px}.tp pre{font:12px/1.45 ui-monospace,Menlo,monospace;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:10px;max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-all}
.lbl2{font:600 11px ui-sans-serif,system-ui;letter-spacing:.12em;color:var(--muted);margin-bottom:4px}
@media (max-width:640px){.tp{grid-template-columns:1fr}}
.known li{margin:0 0 14px}.known blockquote{margin:6px 0 0}
.scope{font:14px/1.5 ui-sans-serif,system-ui,sans-serif;color:var(--muted);margin:-28px 0 32px}
.dates span{max-width:240px}
.story p{font-size:19px;max-width:720px}
h2{font-weight:400;font-size:28px;margin:56px 0 18px;border-top:1px solid var(--line);padding-top:28px}
.pair{margin:0 0 40px}
.cmpwrap{max-width:100%}.cmp{position:relative;background:#000;box-shadow:0 0 0 1px var(--line);line-height:0}
.cmp img{display:block;width:100%;height:auto}.cmp img.now{position:absolute;inset:0;height:100%;clip-path:inset(0 0 0 50%)}
.px img,img.px{image-rendering:pixelated}
.lbls{display:flex;justify-content:space-between;font:600 11px ui-sans-serif,system-ui,sans-serif;letter-spacing:.14em;color:var(--muted);margin:0 0 6px}
.cmpwrap input{display:block;width:100%;margin:10px 0 0}
.sbs{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start}.sbs figure{margin:0;max-width:100%}.sbs img{display:block;max-width:100%;height:auto;box-shadow:0 0 0 1px var(--line)}
.vid{margin:0 0 32px}.vid video{display:block;max-width:100%;height:auto;background:#000}
.cap{font:14px/1.5 ui-sans-serif,system-ui,sans-serif;color:var(--muted);margin-top:8px}
.quotes{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}
blockquote{margin:0;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:18px;font-size:17px}
blockquote cite{display:block;margin-top:10px;font:13px ui-sans-serif,system-ui,sans-serif;color:var(--muted);font-style:normal}
.facts{font:14px/1.7 ui-sans-serif,system-ui,sans-serif;color:var(--muted)}
.facts table{border-collapse:collapse;width:100%}.facts td{border-bottom:1px solid var(--line);padding:6px 0}.facts td:last-child{text-align:right;color:var(--ink)}
.headline{font:600 15px ui-sans-serif,system-ui,sans-serif;letter-spacing:.02em;margin:18px 0 0;opacity:.85}
.card{margin:18px 0 6px}.card td:first-child{width:180px;opacity:.7}.open{margin:0;padding-left:22px}.open li{margin:8px 0}
.open .cap{display:block;margin-top:2px}.open+.scope{margin:20px 0 0}
.recipe{margin:36px 0;padding:24px;border:1px solid var(--line, #ddd);border-radius:8px;background:var(--card, transparent)}
.recipe table{border-collapse:collapse;width:100%;margin:10px 0 18px;font-size:15px}
.recipe td,.recipe th{border-bottom:1px solid var(--line, #ddd);padding:6px 8px;text-align:left;vertical-align:top}
.recipe h3{margin:22px 0 6px}.recipe li{margin:4px 0}
@media print{.score,.cta,.pair,.quotes,.facts,.story,.eyebrow,.dates,.tag,footer,h2:not(.recipe h2){display:none}.recipe{border:0;padding:0}}
.cta{display:inline-block;margin:28px 12px 0 0;padding:12px 20px;border-radius:4px;background:var(--ink);color:var(--bg);text-decoration:none;font:600 15px ui-sans-serif,system-ui,sans-serif}
.cta.alt{background:transparent;color:var(--ink);border:1px solid var(--line)}
footer{margin-top:64px;font:13px/1.6 ui-sans-serif,system-ui,sans-serif;color:var(--muted)}
a{color:inherit}
@media (max-width:700px){.score{grid-template-columns:repeat(2,1fr)}.dates .sep{width:20px;margin:0 8px}}
"""

JS = """function fit(){document.querySelectorAll('[data-w]').forEach(el=>{const w=+el.dataset.w;
let room=el.closest('.pair').clientWidth;if(el.dataset.half)room=Math.max(w,(room-16)/2);
el.style.width=w*Math.max(1,Math.floor(room/w))+'px'})}fit();addEventListener('resize',fit);
document.querySelectorAll('.cmpwrap input').forEach(r=>{const n=r.parentElement.querySelector('.now');
r.addEventListener('input',()=>{n.style.clipPath=`inset(0 0 0 ${r.value}%)`})});"""


def build(ws: Path) -> str:
    st = load_state(ws)
    ex_path = ws / "museum" / "exhibit.json"
    if not ex_path.exists():
        sys.exit(f"write {ex_path} first (see this script's docstring for the schema)")
    ex = json.loads(ex_path.read_text(encoding="utf-8"))
    pp = ws / "codex" / "parity.json"
    parity = json.loads(pp.read_text(encoding="utf-8")) if pp.exists() else {}
    s = compute(parity, ws) if parity and st.get("type") not in EXHIBIT_ONLY else {}
    unknown_keys = sorted(k for k in ex if k not in KNOWN_KEYS and not str(k).startswith("_"))
    if unknown_keys:  # otherwise a misspelt key silently vanishes
        print("warning: exhibit.json keys not used by the museum: " + ", ".join(unknown_keys)
              + (' (videos go in "videos": [{"file": ..., "caption": ...}])'
                 if any("side" in k or "video" in k for k in unknown_keys) else "")
              + "; see unveil.py's docstring for the schema", file=sys.stderr)
    all_ev = read_jsonl(ws / "evidence.jsonl")
    ev = [e for e in all_ev if e.get("kind") != "verify"]  # results about the rebuild aren't traces of the original
    tags = collections.Counter(e["tag"] for e in ev)
    sources = {k for k in (source_key(e.get("source")) for e in ev) if k}
    domains = collections.Counter(k for k in (source_key(e.get("source")) for e in ev)
                                  if k and not k.startswith("file:"))
    vr_path = ws / "codex" / "verify-results.json"
    try:
        vr = json.loads(vr_path.read_text(encoding="utf-8")) if vr_path.exists() else {}
    except ValueError:
        vr = {}
    name = ex.get("codename") or st.get("codename") or st["name"]
    original = ex.get("original_name") or st["name"]
    today = dt.date.today().isoformat()

    exhibit = st.get("type") in EXHIBIT_ONLY
    share_safe = st.get("share_safe")
    eyebrow = ("An exhibit · what is known about " if exhibit else
               "A clean-room facsimile of " if share_safe else "A restoration of ")
    parts = [f"<div class='wrap'><div class='eyebrow'>{eyebrow}{E(original)}</div>",
             f"<h1>{E(name)}</h1><p class='tag'>{E(ex.get('tagline', ''))}</p>",
             "<div class='dates'>"
             f"<span><b>{E(year(ex.get('born')))}</b>born</span><span class='sep'></span>"
             + (f"<span><b>?</b>end unknown</span>"
                if str(ex.get('died') or '').lower().startswith('unknown') and
                str(ex.get('cause_of_death') or 'unknown').lower().startswith('unknown')
                else f"<span><b>{E(year(ex.get('died')))}</b>{E(ex.get('cause_of_death') or 'lost')}</span>")
             + f"<span class='sep'></span><span><b>{today[:4]}</b>{'assembled' if exhibit else 'resurrected'}</span></div>"]
    # headline: small, provable numbers
    head = [f"{len(ev)} traces", f"{tags.get('seen', 0)} seen / {tags.get('inferred', 0)} inferred / "
            f"{tags.get('unknown', 0)} unknown"]
    if s and s.get("features"):
        head.append(f"{s.get('verified', 0)} of {s['features']} features verified")
    parts.append(f"<p class='headline'>{E(' · '.join(head))}</p>")
    # curation card, after Flashpoint's
    vers = st.get("versions") or []
    tv = (st.get("target_version") or {}).get("label")
    tvd = next((v for v in vers if v["label"] == tv), None)
    tiers = (s or {}).get("tiers") or {}
    status = ("Exhibit" if exhibit else "Playable" if s and not s.get("missing") and not tiers.get("exhibit")
              else "Partial" if s else "")
    card = [("Status", status),
            ("Version", f"{tv} ({tvd['from']} to {tvd.get('to') or 'the end'})" if tvd else (tv or "")),
            ("Versions known", ", ".join(v["label"] for v in vers)),
            ("Original files", f"{s['original_files']} of {s['shipped_files']} shipped files" if s and s.get("shipped_files") else ""),
            ("Recovered from traces", f"{s['recovered_files']} files" if s and s.get("recovered_files") else ""),
            ("Tiers", f"{tiers.get('playable', 0)} playable, {tiers.get('exhibit', 0)} exhibit, {tiers.get('catalogue', 0)} catalogue" if tiers else ""),
            ("Sources", f"{len(sources)} sources")]
    card = [(k, v) for k, v in card if v]
    if card:
        parts.append("<div class='facts card'><table>" + "".join(f"<tr><td>{E(k)}</td><td>{E(str(v))}</td></tr>"
                                                                for k, v in card) + "</table></div>")
    if s:
        tiles = [(pct(s.get("fidelity_behaviour")), f"Verified, over {s.get('features', 0)} features"),
                 (pct(s.get("soul")), "Most-missed features" if s.get("sentiment_items", 1) else "Core rules"),
                 (pct(s.get("visual")), "Visual match"),
                 (str(len(ev)), "Pieces of evidence")]
        if s.get("label_match") is not None:  # recipes: the label is the yardstick until tasted
            tiles[0] = (pct(s["label_match"]), "Label match")
            tiles[1] = (str(len(sources)), "Sources")
            tiles[2] = (f"{s.get('manual_recorded', 0)}/{s.get('manual_total', 0)}", "Taste checks done")
        elif s.get("manual_pending") and not s.get("verified"):
            tiles[0] = ("pending", f"{s['manual_pending']} hands-on checks to do")
        parts.append("<div class='score'>" + "".join(
            f"<div{' class=main' if i == 0 else ''}><div class='n'>{E(n)}</div><div class='l'>{E(l)}</div></div>"
            for i, (n, l) in enumerate(tiles)) + "</div>")
        notes = []
        if s.get("grounding") is not None:
            notes.append(f"{pct(s['grounding'])} of the spec rests on evidence; the rest are marked design choices")
        if s.get("left_out"):
            notes.append("not rebuilt yet: " + ", ".join(s["left_out"][:6]))
        if notes:
            parts.append(f"<p class='scope'>{E('. '.join(notes).capitalize())}.</p>")
    elif st.get("type") in EXHIBIT_ONLY:
        parts.append(
            "<div class='score'>"
            f"<div class='main'><div class='n'>{len(ev)}</div><div class='l'>Pieces of evidence</div></div>"
            f"<div><div class='n'>{tags.get('seen', 0)}</div><div class='l'>Seen first-hand</div></div>"
            f"<div><div class='n'>{tags.get('unknown', 0)}</div><div class='l'>Still missing</div></div>"
            f"<div><div class='n'>{len(sources)}</div><div class='l'>Sources</div></div></div>")
    if ex.get("story"):
        parts.append("<div class='story'>" + "".join(f"<p>{E(p)}</p>" for p in ex["story"]) + "</div>")
    recipe = ws / "app" / "RECIPE.md"
    has_recipe = recipe.exists() and st.get("type") == "recipe"
    links = ""
    if has_recipe:  # for food the recipe IS the resurrection
        links += "<a class='cta' href='#recipe'>The recipe ↓</a>"
    if ex.get("play_url") and not exhibit:
        links += f"<a class='cta' href='{E(ex['play_url'])}'>Use it now</a>"
    if ex.get("repo_url"):
        links += f"<a class='cta alt' href='{E(ex['repo_url'])}'>Source &amp; evidence</a>"
    parts.append(links)
    if has_recipe:
        parts.append("<section id='recipe' class='recipe'><h2>The recipe</h2>"
                     + markdown_html(recipe.read_text(errors="replace", encoding="utf-8"))
                     + "<p class='scope'>Numbers come from the evidence (IDs like E0012 point to the ledger); "
                       "[DESIGN] marks choices the evidence could not settle. Print this page for the kitchen.</p>"
                       "</section>")

    if ex.get("known_scenes"):  # media: documented moments only, in order
        parts.append("<h2>What we know happens</h2><ol class='known'>" + "".join(
            f"<li><strong>{E(k.get('scene', ''))}</strong>"
            + (f"<blockquote>“{E(k['quote'][:240])}”<cite>— " +
               (f"<a href='{E(k['url'])}' rel='noopener noreferrer'>{E(k.get('source', 'source'))}</a>" if k.get("url")
                else E(k.get("source", ""))) + "</cite></blockquote>" if k.get("quote") else "")
            + (f"<div class='cap'>{E(k['note'])}</div>" if k.get("note") else "")
            + "</li>" for k in ex["known_scenes"][:30]) + "</ol>"
            "<p class='scope'>Only documented moments are listed; gaps between them are unknown, not invented.</p>")
    if ex.get("fragments"):  # media: where pieces survive, on whose authority
        parts.append("<h2>Where pieces survive</h2><div class='facts'><table>" + "".join(
            f"<tr><td>{E(f.get('title', ''))}</td><td>{E(f.get('kind', ''))}"
            f"{' · official' if f.get('official') else ' · unofficial'}</td><td>"
            + (f"<a href='{E(f['url'])}' rel='noopener noreferrer'>link</a>" if f.get("url") and f.get("official") else
               E(f.get("where", "")))
            + f"</td><td>{E(f.get('note', ''))}</td></tr>" for f in ex["fragments"][:20]) + "</table></div>")
    if ex.get("hunt"):  # media: what's still missing, for lost-media hunters
        parts.append("<h2>The hunt</h2><div class='facts'><table><tr><td><b>Missing</b></td><td><b>Searched</b></td>"
                     "<td><b>Best lead</b></td></tr>" + "".join(
                         f"<tr><td>{E(h.get('what', ''))}</td><td>{E(h.get('searched', ''))}</td>"
                         f"<td>{E(h.get('lead', ''))}</td></tr>" for h in ex["hunt"][:20]) + "</table></div>")
    pairs = [p for p in ex.get("pairs", []) if p.get("then") and p.get("now")]
    if not pairs and not ex.get("text_pairs") and st.get("type") not in EXHIBIT_ONLY:
        print("note: no then/now pairs: the museum's strongest moment is missing. Recipes: a period product photo "
              "(credited) next to your batch photo; APIs: text_pairs.", file=sys.stderr)
    unchecked = [p.get("title") or p["then"] for p in pairs if p.get("privacy_checked") is not True]
    if unchecked:  # law 3: nothing personal on the public page
        sys.exit("open each then-image and confirm it shows no faces, names, emails or usernames "
                 "(crop with shots.py crop, pixelate with shots.py redact), then set "
                 '"privacy_checked": true on: ' + ", ".join(unchecked))
    if pairs:
        parts.append("<h2>Then and now</h2>")
        for p in pairs:
            vp = next((v for v in parity.get("visual", []) if v.get("then") == p["then"]), {}) if parity else {}
            a = img_data(ws, p["then"], p.get("then_clip") or vp.get("then_clip"))
            b = img_data(ws, p["now"], p.get("now_clip") or vp.get("now_clip"))
            if not (a and b):
                sys.exit(f"pair {p.get('title') or p['then']}: image missing ({p['then'] if not a else p['now']}). "
                         "Fix the path or drop the pair.")
            ev_ids = ", ".join(p.get("evidence", []))
            sa = img_size(ws, p["then"], p.get("then_clip") or vp.get("then_clip"))
            sb = img_size(ws, p["now"], p.get("now_clip") or vp.get("now_clip"))
            parts.append(
                f"<div class='pair'>{pair_html(a, b, sa, sb, f'{original}, original', f'{name}, rebuilt')}"
                f"<div class='cap'><strong>{E(p.get('title', ''))}</strong> "
                f"{E(p.get('caption', ''))}{' · Then: ' + E(p['then_credit']) if p.get('then_credit') else ''}"
                f"{' · evidence ' + E(ev_ids) if ev_ids else ''}</div></div>")
    vids = [v for v in ex.get("videos", []) if isinstance(v, dict) and v.get("file")]
    unchecked = [v.get("caption") or v["file"] for v in vids if v.get("privacy_checked") is not True]
    if unchecked:  # footage gets the same rule as then-images
        sys.exit("watch each video and confirm it shows no faces, names, emails or usernames, then set "
                 '"privacy_checked": true on: ' + ", ".join(unchecked))
    shown = [(v, video_src(ws, v["file"])) for v in vids]
    shown = [(v, src) for v, src in shown if src]
    if shown:
        parts.append("<h2>In motion</h2>" + "".join(
            f"<figure class='vid'><video controls preload='metadata' playsinline src='{src}'></video>"
            + (f"<figcaption class='cap'>{E(v['caption'])}</figcaption>" if v.get("caption") else "")
            + "</figure>" for v, src in shown))
    texts = [p for p in ex.get("text_pairs", []) if p.get("then") and p.get("now")]
    if texts:  # APIs, feeds, file formats: archived response vs rebuild's
        parts.append("<h2>Then and now, as data</h2>")
        for p in texts:
            def snip(rel):
                f = (ws / rel)
                return E(f.read_text(errors="replace", encoding="utf-8")[:4000]) if f.exists() else "(missing)"
            parts.append(f"<div class='pair'><div class='tp'><div><div class='lbl2'>THEN</div><pre>{snip(p['then'])}</pre></div>"
                         f"<div><div class='lbl2'>NOW</div><pre>{snip(p['now'])}</pre></div></div>"
                         f"<div class='cap'><strong>{E(p.get('title', ''))}</strong> {E(p.get('caption', ''))}</div></div>")
    if ex.get("quotes"):
        parts.append("<h2>What people remembered</h2><div class='quotes'>")
        for q in ex["quotes"][:9]:
            who = E(q.get("who", "someone"))
            src = f"<a href='{E(q['url'])}'>{who}</a>" if q.get("url") else who
            parts.append(f"<blockquote>“{E(q['text'][:280])}”<cite>— {src}</cite></blockquote>")
        parts.append("</div>")

    rows = [("Evidence items", len(ev)), ("Seen directly", tags.get("seen", 0)),
            ("Inferred", tags.get("inferred", 0)), ("Confirmed unknown", tags.get("unknown", 0)),
            ("Distinct sources", len(sources)), ("Dig rounds", len(st.get("rounds", [])))]
    if len(ev) != len(all_ev):
        rows.append(("Verification results (not counted)", len(all_ev) - len(ev)))
    if s:
        rows += [("Features rebuilt", f"{s['features'] - s['missing']} of {s['features']}"),
                 ("Features verified", f"{s.get('verified', 0)} of {s['features']}")]
    if vr.get("total"):
        rows.append(("Journeys passed in last run", f"{vr.get('passed', 0)} of {vr['total']}"))
    parts.append("<h2>How it was reconstructed</h2><div class='facts'><table>" +
                 "".join(f"<tr><td>{E(k)}</td><td>{E(str(v))}</td></tr>" for k, v in rows) + "</table>")
    if domains:
        parts.append("<p>Top sources: " + ", ".join(E(d) for d, _ in domains.most_common(8)) + ".</p>")
    open_items = []
    for u in ex.get("unknowns", []):
        u = u if isinstance(u, dict) else {"what": str(u)}
        ids = u.get("evidence") or []
        item = {"what": u.get("what", ""), "searched": u.get("searched", ""),
                "ids": set([ids] if isinstance(ids, str) else ids)}
        if item["what"] and not any(_same_text(item["what"], o["what"]) for o in open_items) and len(open_items) < 8:
            open_items.append(item)
    for e in ev:  # bounded: "not found in X, Y", never "does not exist"
        if e.get("tag") != "unknown" or e.get("superseded_by"):
            continue
        dup = next((o for o in open_items if e.get("id") in o["ids"] or _same_text(e.get("claim", ""), o["what"])), None)
        if dup:  # exhibit already says it, keep wording, add the search
            dup["searched"] = dup["searched"] or e.get("searched", "")
            dup["ids"].add(e.get("id"))
        elif len(open_items) < 14:
            open_items.append({"what": e.get("claim", ""), "searched": e.get("searched", ""), "ids": {e.get("id")}})
    if open_items:
        parts.append("<h2>Still open</h2><ul class='open'>" + "".join(
            f"<li>{E(o['what'])}" + (f" <span class='cap'>Searched: {E(str(o['searched']))}</span>" if o["searched"] else "")
            + "</li>" for o in open_items) + "</ul><p class='scope'>" +
            ("Not found is not the same as never existed: these are where the search stopped." if exhibit else
             "Not found is not the same as never existed. Where the rebuild needed an answer, it made a marked "
             "design choice.") + "</p>")
    parts.append("</div>")
    parts.append(
        (f"<footer>{E(name)} is an independent exhibit assembled from public sources. It does not reproduce the "
         f"work itself. It is not affiliated with or endorsed by the owners of {E(original)}; all names and marks "
         f"belong to their owners. Made with the resurrect skill for Claude Code · {today}</footer></div>")
        if exhibit else
        (f"<footer>{E(name)} is an independent, fan-made facsimile built from public archives, screenshots "
         f"and community memories. It contains no original code or assets. It is not affiliated with or "
         f"endorsed by the owners of {E(original)}; all names and marks belong to their owners. "
         f"Made with the resurrect skill for Claude Code · {today}</footer></div>") if share_safe else
        (f"<footer>A restoration of {E(original)} from archived originals and public traces, for preservation "
         f"and personal use. Original material belongs to its owners; reconstructed parts are marked in the "
         f"README. Not affiliated with or endorsed by the owners. Made with the resurrect skill for Claude Code "
         f"· {today}</footer></div>"))
    title = f"{name}: {'an exhibit' if exhibit else 'restored'}" if name == original else \
        f"{name}: {original}, {'an exhibit' if exhibit else 'resurrected'}"
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'><title>{E(title)}</title>"
            f"<meta name='description' content='{E(ex.get('tagline', ''))}'>"
            f"<style>{CSS}</style></head><body>{''.join(parts)}<script>{JS}</script></body></html>")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ws")
    ap.add_argument("--out", help="default: <ws>/museum/index.html")
    a = ap.parse_args(argv)
    ws = find_ws(a.ws)
    out = Path(a.out) if a.out else ws / "museum" / "index.html"
    out.write_text(build(ws), encoding="utf-8")
    print(json.dumps({"museum": str(out), "bytes": out.stat().st_size}))


if __name__ == "__main__":
    from _common import use_heavy
    use_heavy("PIL")
    main()
