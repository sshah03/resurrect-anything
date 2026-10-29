#!/usr/bin/env python3
"""Screenshots and then/now comparisons.

Subcommands
  capture URL OUT.png [--width 1024 --height 768 --full]
        Render a page headlessly. For Wayback captures use the `if_` form so assets load
        without the toolbar:  https://web.archive.org/web/<TS>if_/<original-url>
        Works for local rebuilds too (http://localhost:5173/...). Flash won't render, so
        for Flash-era pages use video stills or community screenshots instead.
        --mask SELECTOR covers user-generated/personal regions (guestbooks, comments) in grey.
  wayback ORIGINAL_URL TS OUT.png       Shortcut for capture with the if_ replay URL. A replay that shows nothing
                                        (one flat colour, or an error with no title) is saved as OUT.FAILED.png
                                        and exits 3. page_issues names Ruffle error panels and dead plugin embeds.
                                        far_resources lists images served from captures > 6 months off the page.
  fetch-image URL OUT [--page URL]      Save an image found online as evidence (images only).
  play URL --steps steps.json|'[...]'   Play a preserved original (Wayback-emulated Flash) like a player
                                        would, with clicks, holds, keys and timed bursts. Measure the bursts with probe.
                                        Steps: '[{"do": "click", "x": 400, "y": 300}, {"do": "screenshot",
                                        "name": "title"}]'. Exit 0 when every step ran.
  swf FILE.swf|DIR [--steps ...] [--out DIR]
                                        See and drive an archived Flash movie locally. Runs a pinned, self-hosted
                                        Ruffle (SHA-256 checked) in muted headless Chromium and serves the SWF's
                                        folder on a free local port so it can load its sibling files
                                        (vids/clip_N.swf). Same steps as play. The movie is untrusted, so it runs
                                        only in the browser sandbox with no network beyond that folder. Anything it
                                        asked for and didn't get is listed.
  redact IMG --box x,y,w,h [--box ...]  Pixelate regions holding user content or personal data.
  crop IMG --box x,y,w,h                Cut a region (e.g. the game stage out of a page shot).
  probe IMG... [--at x,y] [--row y] [--find #hex]
        Measure exact colours, colour runs along a row, or where a colour sits. On a frame burst,
        --find gives an object's position per frame, so you get speed and gravity as numbers.
  sheet IMG...|DIR [--out sheet.png]    Contact sheet with timestamps, to read a whole burst at once (default:
                                        sheet.png next to the frames). Non-image files are skipped.
  track FRAMES... --find #hex --fps N   Object position per (unique) frame, plus a gravity/launch-speed fit.
  compare THEN.png NOW.png [--out pair.png]
        Visual similarity (0-1) from layout structure, light/dark placement and palette, plus a labelled
        side-by-side image for the museum page. Needs Pillow.

Requires: playwright (pip install playwright && playwright install chromium) for capture;
Pillow (pip install pillow) for compare. Both are optional. The skill still works without them, with less.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import atomic_write  # noqa: E402

MUTE = "--mute-audio"  # every browser here is muted
# software WebGL (headless has no GPU), no user gesture needed
EMULATOR_ARGS = [MUTE, "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
                 "--autoplay-policy=no-user-gesture-required"]

# Flash embeds in page coords, maybe blank in the replay
FLASH_JS = r"""() => {
  const out = [];
  for (const e of document.querySelectorAll('embed, object, ruffle-embed, ruffle-object, ruffle-player')) {
    if (e.tagName === 'OBJECT' && e.querySelector('embed')) continue;
    if (e.closest('ruffle-object, ruffle-embed') && e.closest('ruffle-object, ruffle-embed') !== e) continue;
    const mv = e.querySelector ? e.querySelector('param[name=movie i], param[name=src i]') : null;
    const src = e.getAttribute('src') || e.getAttribute('data') || (mv ? mv.getAttribute('value') : '') || '';
    const type = e.getAttribute('type') || e.getAttribute('classid') || '';
    if (!/\.swf(\?|$)/i.test(src) && !/flash|D27CDB6E/i.test(type) && !/^RUFFLE/.test(e.tagName)) continue;
    const b = e.getBoundingClientRect();
    out.push({src: src, x: Math.round(b.left + scrollX), y: Math.round(b.top + scrollY), w: Math.round(b.width),
              h: Math.round(b.height), ruffle: /^RUFFLE/.test(e.tagName)});
  }
  return out;
}"""

# Ruffle error panels and dead embeds (not the page's look)
PAGE_ISSUES_JS = r"""() => {
  const out = [];
  for (const r of document.querySelectorAll('ruffle-player, ruffle-embed, ruffle-object')) {
    const root = r.shadowRoot;
    // visible text only: the shadow root's stylesheet names #panic and friends even when nothing failed
    const shown = n => n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
    const leaves = root ? Array.from(root.querySelectorAll('*')).filter(n => !/^(STYLE|SCRIPT)$/.test(n.tagName) && !n.children.length && shown(n)) : [];
    const t = (leaves.map(n => n.textContent).join(' ') + ' ' + r.textContent).replace(/\s+/g, ' ');
    const m = t.match(/[^.]*(encountered a major issue|panic|failed to load|couldn.t load|could not load|error loading|something went wrong|not supported|unsupported)[^.]*/i);
    const pn = root ? root.querySelector('#panic, .panic') : null;
    if (m || (pn && shown(pn))) out.push('Ruffle error panel: ' + (m ? m[0].trim().slice(0, 100) : 'panic'));
  }
  for (const e of document.querySelectorAll('embed, object, applet')) {
    if (e.closest('ruffle-object, ruffle-embed, ruffle-player')) continue;
    if (e.tagName === 'OBJECT' && e.querySelector('embed')) continue;  // the nested <embed> is reported instead
    const mv = e.querySelector ? e.querySelector('param[name=movie i], param[name=src i]') : null;
    const src = e.getAttribute('src') || e.getAttribute('data') || e.getAttribute('code') || (mv ? mv.getAttribute('value') : '') || '';
    const type = e.getAttribute('type') || e.getAttribute('classid') || '';
    if (e.tagName === 'APPLET' || /\.(swf|dcr|dir|dxr|class|jar|mid|midi|wav)(\?|$)/i.test(src) || /shockwave|flash|java|director|D27CDB6E/i.test(type)) {
      const b = e.getBoundingClientRect();
      out.push('dead embed: <' + e.tagName.toLowerCase() + '> ' + src.split('/').pop().slice(0, 60) + ' (' + Math.round(b.width) + 'x' + Math.round(b.height) + ') renders as a blank or placeholder, not the original');
    }
  }
  return out;
}"""


def flat_image(path) -> str | None:
    """Why this shot is blank (one colour, bar a few hundred pixels), or None."""
    try:
        from PIL import Image
    except ImportError:
        return None
    g = Image.open(path).convert("L")
    lo, hi = g.getextrema()
    if hi - lo < 8:
        return "the image is a single flat colour"
    hist = g.histogram()
    top = max(hist)
    if sum(hist) - top < 0.001 * sum(hist):
        return f"the image is one colour except {sum(hist) - top} pixels"
    return None


def far_resources(responses, page_ts: str, months: int = 6) -> list[dict]:
    """Resources Wayback served from captures over `months` off the page's (e.g. a hit counter).

    Takes the replay's (url, status) pairs. Returns "YYYY-MM (+N mo) url" lines, farthest first."""
    def m_(ts):
        return int(ts[:4]) * 12 + int(ts[4:6])
    out, seen = [], set()
    for u, status in responses:
        m = re.match(r"https?://web\.archive\.org/web/(\d{14})[a-z]{0,2}_?/(.+)", u)
        if not m or status != 200 or not page_ts or m.group(2) in seen:
            continue
        seen.add(m.group(2))
        off = m_(m.group(1)) - m_(page_ts.ljust(14, "0"))
        if abs(off) > months:
            out.append((-abs(off), f"{m.group(1)[:4]}-{m.group(1)[4:6]} ({off:+d} mo) {m.group(2)[:140]}"))
    return [x for _, x in sorted(out)]


def cmd_capture(a, url=None, strict=False):
    """strict (wayback): a blank shot or untitled failed replay goes to <out>.FAILED.png, exit 3."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright not installed: pip install playwright && playwright install chromium")
    url = url or a.url
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    nav_error, responses, issues = None, [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[MUTE])
        page = browser.new_page(viewport={"width": a.width, "height": a.height})
        page.on("response", lambda r: responses.append((r.url, r.status)))
        try:
            page.goto(url, wait_until="networkidle", timeout=a.timeout * 1000)
        except Exception as e:  # archived pages often never reach networkidle
            nav_error = e.__class__.__name__
            print(f"warning: {nav_error}; capturing what loaded", file=sys.stderr)
        page.wait_for_timeout(a.settle)
        masks = [page.locator(sel) for sel in (a.mask or [])]
        if a.mask_images:  # avatars and photos show real people
            masks.append(page.locator("img"))
        if a.selector:  # just one element, e.g. the game stage
            page.locator(a.selector).first.screenshot(path=a.out, mask=masks, mask_color="#808080")
        else:
            clip = dict(zip(("x", "y", "width", "height"), (float(v) for v in a.clip.split(",")))) if a.clip else None
            page.screenshot(path=a.out, full_page=a.full and not clip, clip=clip, mask=masks,
                            mask_color="#808080")
        try:
            title = page.title()
        except Exception:
            title = ""
        for f in page.frames:
            try:
                issues += [x for x in f.evaluate(PAGE_ISSUES_JS) if x not in issues]
            except Exception:
                pass
        try:
            flash = page.main_frame.evaluate(FLASH_JS)
        except Exception:
            flash = []
        browser.close()
    flat = None
    try:
        flat = flat_image(a.out)
    except Exception:
        pass
    m = re.search(r"web\.archive\.org/web/(\d{8,14})", url)
    far = far_resources(responses, m.group(1)) if m else []
    page_orig = re.search(r"web\.archive\.org/web/\d+[a-z_]*/(.+)", url)
    flash_note = flash_blank_note(a.out, flash, None if (a.selector or a.clip) else (a.width, a.height, a.full),
                                  m.group(1) if m else None, page_orig.group(1) if page_orig else url)
    res = {"url": url, "out": a.out, "title": title, "viewport": [a.width, a.height],
           "masked": (a.mask or []) + (["img"] if getattr(a, "mask_images", False) else []),
           **({"page_issues": issues[:8]} if issues else {}),
           **({"flash": flash_note} if flash_note else {}),
           **({"far_resources": far[:12], **({"far_more": len(far) - 12} if len(far) > 12 else {}),
               "far_note": "served from captures more than 6 months from the page: possibly another era's image "
                           "(a hit counter, a later banner); check before copying them as this page's look"}
              if far else {})}
    failed = flat or (f"the replay failed ({nav_error}) and the page has no title" if nav_error and not title.strip()
                      else "Wayback's own error page (not archived at this address/time)"
                      if title.strip() == "Wayback Machine" else None)
    if strict and failed:
        bad = Path(a.out).with_suffix(".FAILED.png")
        Path(a.out).replace(bad)
        res.update(out=str(bad), failed=failed
                   + ": nothing usable rendered. Try another timestamp (`wayback.py cdx <url>`), `wayback.py text`, "
                   "video stills or community screenshots")
        print(json.dumps(res))
        sys.exit(3)
    if flat:
        print("warning: the capture is a single flat colour. The page probably did not render (a SWF that "
              "needs its host client, a blocked script, a login wall). Try `shots.py play`, video frames "
              "or community screenshots instead.", file=sys.stderr)
        res["flat"] = True
    if issues:
        print("warning: " + "; ".join(issues[:3]) + " (see page_issues)", file=sys.stderr)
    if flash_note and flash_note.get("blank"):
        print("note: " + flash_note["note"], file=sys.stderr)
    print(json.dumps(res))


def flash_blank_note(shot, embeds, view, ts=None, page_url="") -> dict | None:
    """Which Flash embeds came out blank, and how to see the movie instead.

    view is (width, height, full) for a plain screenshot, or None to skip the pixel check."""
    embeds = [e for e in embeds or [] if e.get("w", 0) >= 20 and e.get("h", 0) >= 20]
    if not embeds:
        return None
    blank = []
    if view:
        try:
            from PIL import Image
            img = Image.open(shot).convert("L")
            for e in embeds:
                box = (max(0, e["x"]), max(0, e["y"]), min(img.width, e["x"] + e["w"]), min(img.height, e["y"] + e["h"]))
                if box[2] - box[0] < 20 or box[3] - box[1] < 20:
                    continue
                lo, hi = img.crop(box).getextrema()
                if hi - lo < 8:
                    blank.append(e)
        except Exception:
            pass
    def orig(src):  # the movie's real URL, out of the replay's rewritten one
        import urllib.parse
        m = re.match(r"https?://web\.archive\.org/web/(\d{4,14})[a-z_]*/(.+)", src or "")
        return (m.group(2), m.group(1)) if m else (urllib.parse.urljoin(page_url, src or ""), ts)
    names = sorted({orig(e["src"])[0].rsplit("/", 1)[-1][:60] for e in (blank or embeds) if e.get("src")})
    out = {"embeds": [f"{orig(e['src'])[0][:120]} {e['w']}x{e['h']} at {e['x']},{e['y']}" for e in embeds[:4]]}
    src, ts_ = orig((blank or embeds)[0].get("src"))
    get = (f"fetch the SWF (`wayback.py fetch {src or '<swf url>'} {ts_ or '<ts>'} --original`, plus the files it loads: "
           "`inventory`, then `mirror --from-inventory`) and see or drive it with `shots.py swf <file.swf>`")
    if blank:
        out["blank"] = len(blank)
        out["note"] = (f"the page holds Flash ({', '.join(names) or 'an embed'}) that this replay did not render "
                       f"(a flat block where the movie should be). Do not copy the blank as the look: {get}")
    else:
        out["note"] = (f"the page holds Flash ({', '.join(names) or 'an embed'}); a replay often shows it stuck or partial "
                       f"(a preloader: it cannot load the files the movie fetches). To see it work, {get}")
    return out


def cmd_play(a):
    """Play a preserved original (e.g. a Wayback SWF) and record the screen. Never downloads it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright not installed: pip install playwright && playwright install chromium")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from verify import do_step
    import re
    steps = _steps(a.steps)
    # normal replay emulates a bare .swf, if_ just downloads it
    a.url = re.sub(r"(web\.archive\.org/web/\d+)(?:[a-z]{2}_)/", r"\1/", a.url)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    res = {"shots": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=EMULATOR_ARGS)
        page = browser.new_page(viewport={"width": a.width, "height": a.height})
        page.set_default_timeout(a.timeout * 1000)
        try:
            page.goto(a.url, wait_until="load", timeout=a.timeout * 1000)
        except Exception as e:
            print(f"warning: {e.__class__.__name__}; continuing with what loaded", file=sys.stderr)
        page.wait_for_timeout(a.settle)
        # Wayback replays in an iframe. Act in the player's frame,
        # but mouse, keys and timing use page coordinates.
        sel = "ruffle-player, ruffle-embed, embed, object, canvas"
        frame = next((f for f in page.frames if f.locator(sel).count()), page.main_frame)

        class View:
            def __getattr__(self, name):
                if name in ("locator", "click", "fill", "hover", "press", "evaluate", "inner_text",
                            "wait_for_selector", "wait_for_function"):
                    return getattr(frame, name)
                return getattr(page, name)
        view = View()
        # Ruffle needs a real click before it takes input
        player = frame.locator(sel).first
        stage = None
        if player.count():
            box = player.bounding_box()
            stage = box
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.wait_for_timeout(500)
        t0 = time.time()
        shot_times = []
        orig_shot = page.screenshot

        def timed(*args, **kw):  # real shot times, bursts aren't evenly spaced
            shot_times.append(round((time.time() - t0) * 1000, 1))
            return orig_shot(*args, **kw)
        page.screenshot = timed
        failed = []
        for i, st in enumerate(steps):
            try:
                do_step(view, a.url, st, {"id": "play"}, i, res, out, a.timeout * 1000)
            except Exception as e:
                failed.append({"step": i, "do": st.get("do"), "error": str(e).splitlines()[0][:200] if str(e) else
                               type(e).__name__})
                print(f"step {i} ({st.get('do')}) failed: {e}", file=sys.stderr)
        browser.close()
    atomic_write(out / "play-times.json", json.dumps({"times": shot_times, "shots": res["shots"]}))
    print(json.dumps({"url": a.url, "out": str(out.resolve()), "shots": res["shots"],
                      "times_file": str((out / "play-times.json").resolve()), "stage_rect": stage,
                      "steps_run": len(steps) - len(failed), "steps_failed": failed,
                      "note": "selector screenshots crop to the stage; element screenshots are "
                      "not timed: use full-page shots with --clip at the stage_rect for timed bursts"}), flush=True)
    sys.exit(1 if failed else 0)


STEPS_HELP = ("a JSON file, or the JSON itself, holding a list of verify.py steps (click, type, press or key, hold, wait, "
              "screenshot or shot, burst; coordinates are page pixels), e.g. '[{\"do\": \"click\", \"x\": 400, \"y\": "
              "300}, {\"do\": \"type\", \"text\": \"hello\"}, {\"do\": \"key\", \"key\": \"Enter\"}, {\"do\": "
              "\"wait\", \"ms\": 500}, {\"do\": \"shot\", \"name\": \"after\"}]'. Pauses are wait steps "
              "(page.wait_for_timeout): in your own Playwright scripts never time.sleep, it starves the event loop and "
              "the movie stops")
STEP_ALIASES = {"key": "press", "shot": "screenshot", "keys": "type"}


def _steps(arg: str) -> list:
    """Read --steps, given as an inline JSON list or as a file holding one."""
    text = arg if arg.lstrip()[:1] in ("[", "{") else None
    if text is None:
        try:
            text = Path(arg).read_text(encoding="utf-8")
        except (OSError, ValueError) as e:
            sys.exit(f"--steps: no such file {arg!r} and not inline JSON ({e.__class__.__name__}); give a JSON list "
                     'such as \'[{"do": "click", "x": 400, "y": 300}, {"do": "screenshot", "name": "start"}]\'')
    try:
        steps = json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"--steps is not valid JSON: {e}")
    steps = [steps] if isinstance(steps, dict) else steps
    if not isinstance(steps, list) or not all(isinstance(x, dict) and x.get("do") for x in steps):
        sys.exit('--steps must be a list of objects with a "do" key, e.g. [{"do": "press", "key": "Space"}]')
    return [dict(x, do=STEP_ALIASES.get(x["do"], x["do"])) for x in steps]


# pinned Ruffle, hashes as jsDelivr and unpkg publish them,
# so a swapped or corrupted download is refused.
RUFFLE_VERSION = "0.7.1"
RUFFLE_URL = "https://cdn.jsdelivr.net/npm/@ruffle-rs/ruffle@" + RUFFLE_VERSION + "/{}"
RUFFLE_SHA256 = {
    "ruffle.js": "26036a94567088dfb3de88a759e8ed10a4cea07b29500af36f345eef42420098",
    "core.ruffle.16d61072ac7fdc4d5013.js": "d27c6bdce2a34afb32e8dfa40970bc7e86cbcb912f92db7eb3e1ac70e943bd82",
    "core.ruffle.994eeef0a471a88cd0e3.js": "d89e507ee60d550629e9229aa1e4dd4f9e75ec7db271e0135cbc87dab042ea51",
    "172de42e6619eac8f371.wasm": "1ed33b9b71a6b01b9aafafed660be343ffeb2d246c5d79e6425a32bf93e38207",
    "878d548199cddef07035.wasm": "af9b6a13a8f5e8d305bc3ce28a4edf3f6242df1183e8576ad4ca086e9f21eaea",
}


def ruffle_dir() -> Path:
    """The cached Ruffle build (~29 MB, downloaded once, SHA-256 checked each use)."""
    import hashlib
    import urllib.request
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _common import USER_AGENT, cache_dir
    d = cache_dir() / f"ruffle-{RUFFLE_VERSION}"
    d.mkdir(parents=True, exist_ok=True)
    got = []
    for name, want in RUFFLE_SHA256.items():
        f = d / name
        if f.exists() and hashlib.sha256(f.read_bytes()).hexdigest() == want:
            continue
        print(f"downloading Ruffle {RUFFLE_VERSION}: {name}", file=sys.stderr)
        req = urllib.request.Request(RUFFLE_URL.format(name), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
        sha = hashlib.sha256(body).hexdigest()
        if sha != want:
            sys.exit(f"refusing the Ruffle download {name}: its SHA-256 is {sha}, not the pinned {want}. Nothing was "
                     "saved. Retry later; if it persists, the published file changed and must be checked by hand")
        f.write_bytes(body)
        got.append(name)
    if got:
        print(f"Ruffle {RUFFLE_VERSION} web build saved in {d} ({len(got)} file(s) from {RUFFLE_URL.format('')}, "
              "SHA-256 checked)", file=sys.stderr)
    return d


def swf_header(path) -> dict:
    """Stage size, frame rate and version from the SWF header. Never runs it."""
    import zlib
    raw = Path(path).read_bytes()[:4096]
    sig = raw[:3]
    if sig not in (b"FWS", b"CWS", b"ZWS"):
        raise ValueError(f"{path} is not a SWF (starts {raw[:3]!r})")
    out = {"version": raw[3], "compressed": {b"FWS": None, b"CWS": "zlib", b"ZWS": "lzma"}[sig]}
    if sig == b"ZWS":
        return out  # stage size not read for LZMA
    body = raw[8:] if sig == b"FWS" else zlib.decompressobj().decompress(raw[8:], 64)
    nbits = body[0] >> 3
    nbytes = (5 + 4 * nbits + 7) // 8
    bits, total = int.from_bytes(body[:nbytes], "big"), nbytes * 8
    vals = []
    for i in range(4):
        v = (bits >> (total - 5 - (i + 1) * nbits)) & ((1 << nbits) - 1)
        vals.append(v - (1 << nbits) if nbits and v >> (nbits - 1) else v)
    out.update(width=round((vals[1] - vals[0]) / 20), height=round((vals[3] - vals[2]) / 20),
               fps=round(body[nbytes + 1] + body[nbytes] / 256, 2))
    return out


SWF_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>swf</title>
<style>html,body{margin:0;background:#000;overflow:hidden}</style>
<script>window.RufflePlayer=window.RufflePlayer||{};window.RufflePlayer.config=%(config)s;</script>
<script src="/__ruffle__/ruffle.js"></script></head><body><div id="stage"></div>
<script>
const pl = window.RufflePlayer.newest().createPlayer();
pl.style.width = "%(w)dpx"; pl.style.height = "%(h)dpx"; pl.style.display = "block";
document.getElementById("stage").appendChild(pl);
(pl.ruffle ? pl.ruffle() : pl).load({url: %(movie)s, parameters: %(params)s});
</script></body></html>"""


def _swf_target(a):
    """Given a .swf, or a folder with one in it, return (folder to serve, movie path inside it)."""
    t = Path(a.target).resolve()
    if t.is_dir():
        swfs = sorted(t.glob("*.swf"), key=lambda f: -f.stat().st_size)
        if a.movie:
            movie = (t / a.movie).resolve()
        elif swfs:
            movie = swfs[0]
            if len(swfs) > 1:
                print(f"note: {len(swfs)} SWFs in {t}; playing the largest, {movie.name} (--movie picks another)",
                      file=sys.stderr)
        else:
            sys.exit(f"no .swf in {t} (give the movie's file, or --movie NAME)")
    elif t.is_file():
        movie = t
    else:
        sys.exit(f"no such file or folder: {a.target}")
    root = Path(a.root).resolve() if a.root else movie.parent
    try:
        rel = movie.relative_to(root).as_posix()
    except ValueError:
        sys.exit(f"--root {root} does not contain {movie}")
    return root, rel


def cmd_swf(a):
    """Drive an archived SWF in a pinned, muted Ruffle, with its folder served locally.

    Requests outside the folder (http://site/vids/x.swf) are served from it if the file is there,
    else blocked. Both are reported."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright not installed: pip install playwright && playwright install chromium")
    import functools
    import http.server
    import threading
    import urllib.parse
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from verify import do_step
    root, movie = _swf_target(a)
    try:
        head = swf_header(root / movie)
    except (ValueError, OSError, IndexError) as e:
        sys.exit(str(e))
    w = a.width or head.get("width") or 800
    h = a.height or head.get("height") or 600
    steps = _steps(a.steps) if a.steps else [{"do": "screenshot", "name": "start"}]
    rdir = ruffle_dir()
    base = "/" + (movie.rsplit("/", 1)[0] + "/" if "/" in movie else "")
    config = {"publicPath": "/__ruffle__/", "autoplay": "on", "unmuteOverlay": "hidden", "splashScreen": False,
              "letterbox": "off", "allowScriptAccess": False, "openUrlMode": "deny", "contextMenu": "off",
              "warnOnUnsupportedContent": False, "polyfills": False, "base": base, "volume": 0}
    params = dict(urllib.parse.parse_qsl(a.flashvars or "", keep_blank_values=True))
    page_html = SWF_PAGE % {"config": json.dumps(config), "w": w, "h": h, "movie": json.dumps("/" + movie),
                            "params": json.dumps(params)}
    served, absent = [], []

    class Handler(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".wasm": "application/wasm",
                          ".swf": "application/x-shockwave-flash", ".js": "text/javascript"}

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/__swf__.html":
                body = page_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/__ruffle__/"):
                name = path.rsplit("/", 1)[-1]
                f = rdir / name
                if name in RUFFLE_SHA256 and f.is_file():
                    body = f.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", self.extensions_map.get(f.suffix, "application/octet-stream"))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)
                return
            return super().do_GET()

        def log_request(self, code="-", size="-"):  # record what the movie asked for, quietly
            try:
                c = int(code)
            except (TypeError, ValueError):
                return
            path = urllib.parse.unquote(urllib.parse.urlsplit(getattr(self, "path", "") or "").path)
            if path and not path.startswith(("/__ruffle__/", "/__swf__")):
                (served if c < 400 else absent).append(path)

        def log_message(self, fmt, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(root)))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    res, failed, console, offsite = {"shots": []}, [], [], []
    url = f"http://127.0.0.1:{port}/__swf__.html"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=EMULATOR_ARGS)  # muted, software WebGL
            page = browser.new_page(viewport={"width": w, "height": h})
            page.set_default_timeout(a.timeout * 1000)
            page.on("console", lambda m: console.append(m.text[:160]) if m.type == "error" and
                    not m.text.startswith("Failed to load resource") else None)  # missing files get their own list

            def route(r):  # nothing leaves the machine. Site URLs come from the folder
                u = urllib.parse.urlsplit(r.request.url)
                if u.hostname == "127.0.0.1" and u.port == port:
                    return r.continue_()
                local = (root / urllib.parse.unquote(u.path).lstrip("/")).resolve()
                if u.scheme in ("http", "https") and local.is_file() and root in local.parents:
                    offsite.append(f"{r.request.url[:100]} -> {local.relative_to(root)}")
                    return r.fulfill(path=str(local))
                offsite.append(f"{r.request.url[:100]} (blocked: not in the folder)")
                return r.abort()
            page.route("**/*", route)
            page.goto(url, wait_until="load", timeout=a.timeout * 1000)
            try:
                page.wait_for_function("() => { const p = document.querySelector('ruffle-player'); "
                                       "return p && p.shadowRoot && p.shadowRoot.querySelector('canvas'); }",
                                       timeout=a.timeout * 1000)
            except Exception:
                print("warning: Ruffle drew no canvas in time; continuing", file=sys.stderr)
            page.wait_for_timeout(a.settle)
            if a.focus != "none":  # Ruffle only takes keys after a click
                fx, fy = (float(v) for v in a.focus.split(","))
                page.mouse.click(fx, fy)
                page.wait_for_timeout(300)
            t0 = time.time()
            shot_times, orig_shot = [], page.screenshot

            def timed(*args, **kw):
                shot_times.append(round((time.time() - t0) * 1000, 1))
                return orig_shot(*args, **kw)
            page.screenshot = timed
            for i, st in enumerate(steps):
                try:
                    do_step(page, url, st, {"id": "swf"}, i, res, out, a.timeout * 1000)
                except Exception as e:
                    failed.append({"step": i, "do": st.get("do"),
                                   "error": str(e).splitlines()[0][:200] if str(e) else type(e).__name__})
                    print(f"step {i} ({st.get('do')}) failed: {e}", file=sys.stderr)
            try:
                issues = page.evaluate(PAGE_ISSUES_JS)
            except Exception:
                issues = []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
    atomic_write(out / "play-times.json", json.dumps({"times": shot_times, "shots": res["shots"]}))
    blank = [Path(x).name for x in res["shots"] if flat_image(x)]
    gridded = [_grid_copy(x, a.grid) for x in res["shots"]] if a.grid else []
    missing = sorted(set(absent))
    print(json.dumps({
        "movie": str(root / movie), "served_from": str(root), "stage": [w, h], "swf": head,
        "ruffle": f"{RUFFLE_VERSION} (self-hosted, SHA-256 checked)", "out": str(out.resolve()), "shots": res["shots"],
        "steps_run": len(steps) - len(failed), "steps_failed": failed,
        "files_loaded": sorted(set(served) - {"/" + movie})[:20],
        **({"not_in_folder": missing[:30], "not_in_folder_note": "the movie asked for these and the folder lacks them: "
            "fetch them (`wayback.py mirror --from-inventory <list> --match ...` or `wayback.py fetch <url> <ts> "
            "--original`) into the same layout and run again"} if missing else {}),
        **({"outside_requests": offsite[:12]} if offsite else {}),
        **({"page_issues": issues[:5]} if issues else {}),
        **({"console_errors": console[:5]} if console else {}),
        **({"blank_shots": blank} if blank else {}),
        **({"grid_copies": gridded} if gridded else {}),
        "note": "coordinates are stage pixels (the viewport is the stage size unless --width/--height). The movie ran "
                "only in the browser sandbox, muted, with no network beyond its folder."}), flush=True)
    sys.exit(1 if failed else 0)


def _grid_copy(shot: str, step: int) -> str:
    """Copy of a shot with a labelled grid every `step` px, for reading click targets."""
    from PIL import Image, ImageDraw
    im = Image.open(shot).convert("RGB")
    d = ImageDraw.Draw(im)
    for x in range(step, im.width, step):
        d.line([(x, 0), (x, im.height)], fill=(255, 0, 255), width=1)
        d.text((x + 2, 2), str(x), fill=(255, 0, 255))
    for y in range(step, im.height, step):
        d.line([(0, y), (im.width, y)], fill=(0, 200, 255), width=1)
        d.text((2, y + 2), str(y), fill=(0, 200, 255))
    out = Path(shot).with_name(Path(shot).stem + "-grid.png")
    im.save(out)
    return str(out)


def cmd_wayback(a):
    import urllib.parse
    p = urllib.parse.urlsplit(a.original)
    original = urllib.parse.urlunsplit(p._replace(query=urllib.parse.quote(p.query, safe="=&+%")))
    url = f"https://web.archive.org/web/{a.ts}if_/{original}"
    cmd_capture(a, url, strict=True)


def _load(path, size):
    from PIL import Image
    img = Image.open(path).convert("RGB")
    w, h = img.size
    # first screen only, top-anchored
    target_h = int(w * size[1] / size[0])
    img = img.crop((0, 0, w, min(h, target_h)))
    return img.resize(size)


def _pearson(xs, ys) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx == 0 or sy == 0:  # a blank screen has no structure to match
        return 0.0
    return sxy / (sx * sy)


def similarity(then_path, now_path) -> dict:
    """Similarity (0-1) of the first screen, tolerant of text and small shifts.

    layout:    correlation of blurred edge maps (where boundaries and blocks of content sit)
    tonal:     correlation of a coarse luminance map (where light and dark areas sit)
    palette:   overlap of quantised colour histograms (same colours in similar amounts)
    A blank or flat page scores ~0 on layout and tonal, so it can't fake a match.
    """
    from PIL import ImageFilter, ImageOps
    size = (256, 192)
    a, b = _load(then_path, size), _load(now_path, size)

    def edges(img):
        e = ImageOps.grayscale(img).filter(ImageFilter.GaussianBlur(2)).filter(ImageFilter.FIND_EDGES)
        return list(e.filter(ImageFilter.GaussianBlur(3)).resize((48, 36)).tobytes())

    def tone(img):
        return list(ImageOps.grayscale(img).resize((16, 12)).tobytes())

    def pal(img):
        q = img.quantize(colors=12).convert("RGB").resize((64, 48)).tobytes()
        hist = {}
        for i in range(0, len(q), 3):
            key = (q[i] // 32, q[i + 1] // 32, q[i + 2] // 32)
            hist[key] = hist.get(key, 0) + 1
        return hist

    layout = max(0.0, _pearson(edges(a), edges(b)))
    tonal = max(0.0, _pearson(tone(a), tone(b)))
    pa_, pb_ = pal(a), pal(b)
    n = 64 * 48
    overlap = sum(min(pa_.get(k, 0), pb_.get(k, 0)) for k in set(pa_) | set(pb_)) / n
    # a flat page in the main colour scores this already,
    # so only credit the excess over it.
    floor = max(pa_.values()) / n if pa_ else 0.0
    palette = max(0.0, (overlap - floor) / (1 - floor)) if floor < 1 else (1.0 if overlap >= 1 else 0.0)
    score = round(0.4 * layout + 0.35 * tonal + 0.25 * palette, 3)
    return {"score": score, "layout": round(layout, 3), "tonal": round(tonal, 3),
            "palette": round(palette, 3), "palette_raw": round(overlap, 3)}


def look_sheet(then_paths, now_paths, out, bands=3, width=1000, code=None) -> dict:
    """THEN/NOW in full-width bands (sky, ground, HUD...) from each side's sharpest frame, for eyes.

    Numbers aren't enough. On a lost Flash game, thumbnail similarity, tile scores and band texture
    stats all passed a smeared hedge and a HUD that lost its bricks."""
    from PIL import Image, ImageDraw, ImageFilter, ImageOps

    def sharpest(paths):
        def energy(p):
            g = ImageOps.grayscale(Image.open(p)).resize((320, 200)).filter(ImageFilter.FIND_EDGES)
            return sum(g.getdata())
        return max(paths, key=energy)
    a_p, b_p = sharpest(then_paths), sharpest(now_paths)
    a = Image.open(a_p).convert("RGB")
    b = Image.open(b_p).convert("RGB").resize(a.size, Image.LANCZOS)
    k = width / a.width
    bh = round(a.height * k / bands)
    img = Image.new("RGB", (width, bands * (2 * bh + 44)), "white")
    d = ImageDraw.Draw(img)
    measured = []
    if code:  # big, since LOOK.md quotes it
        try:
            from PIL import ImageFont
            big = ImageFont.load_default(size=26)
        except (TypeError, OSError):
            big = None
        head = Image.new("RGB", (width, 40), (255, 245, 200))
        ImageDraw.Draw(head).text((10, 6), f"LOOK CODE {code}  (quote it in codex/LOOK.md)", fill="black", font=big)
        full = Image.new("RGB", (width, img.height + 40), "white")
        full.paste(head, (0, 0))
        full.paste(img, (0, 40))
        img = full
        d = ImageDraw.Draw(img)
        y_off = 40
    else:
        y_off = 0
    for i in range(bands):
        y0, y1 = round(i * a.height / bands), round((i + 1) * a.height / bands)
        top = i * (2 * bh + 44) + y_off
        d.text((6, top + 3), f"band {i + 1}: y {y0}-{y1}   THEN (above) / NOW (below)"
               + (f"   code {code}" if code else ""), fill="black")
        ca, cb = a.crop((0, y0, a.width, y1)), b.crop((0, y0, b.width, y1))
        img.paste(ca.resize((width, bh), Image.LANCZOS), (0, top + 18))
        img.paste(cb.resize((width, bh), Image.LANCZOS), (0, top + 20 + bh))
        # our own per-band measure, to check `matches` verdicts.
        # 8 tiles across, blurred, per channel. Worst tile counts,
        # so one missing sprite still shows.
        ga, gb = ImageOps.grayscale(ca), ImageOps.grayscale(cb)
        ea = sum(ga.filter(ImageFilter.FIND_EDGES).getdata()) / max(1, ga.width * ga.height)
        eb = sum(gb.filter(ImageFilter.FIND_EDGES).getdata()) / max(1, gb.width * gb.height)
        ba, bb_ = ca.filter(ImageFilter.GaussianBlur(1.5)), cb.filter(ImageFilter.GaussianBlur(1.5))
        dmap = ImageChops_difference(ba, bb_).split()
        strong = ImageChops_lighter(ImageChops_lighter(dmap[0], dmap[1]), dmap[2]).point(lambda v: 255 if v > 60 else 0)
        tw = max(1, ca.width // 8)
        worst, mean_all = 0.0, 0.0
        for tx in range(0, ca.width - tw + 1, tw):
            box = (tx, 0, tx + tw, ca.height)
            m = max(sum(ch.crop(box).getdata()) / max(1, tw * ca.height) for ch in dmap)
            share = sum(strong.crop(box).getdata()) / 255 / max(1, tw * ca.height) * 100
            worst = max(worst, share)  # % of the tile's pixels that differ strongly
            mean_all += m
        measured.append({"band": i + 1, "y": [y0, y1], "worst_tile": round(worst, 1),
                         "difference": round(mean_all / max(1, ca.width // tw), 1),
                         "sharpness": round(eb / ea, 2) if ea > 1 else None})
    img.save(out)
    return {"sheet": str(out), "then_frame": str(a_p), "now_frame": str(b_p), "bands": measured, "code": code}


def ImageChops_lighter(a, b):
    from PIL import ImageChops
    return ImageChops.lighter(a, b)


def ImageChops_difference(a, b):
    from PIL import ImageChops
    return ImageChops.difference(a, b)


def side_by_side(then_path, now_path, out, labels=("THEN", "NOW")):
    from PIL import Image, ImageDraw
    a, b = Image.open(then_path).convert("RGB"), Image.open(now_path).convert("RGB")
    w = 800
    a = a.resize((w, int(a.height * w / a.width)))
    b = b.resize((w, int(b.height * w / b.width)))
    h = min(max(a.height, b.height), 1400)
    pad, band = 24, 44
    canvas = Image.new("RGB", (w * 2 + pad * 3, h + band + pad), (18, 18, 20))
    canvas.paste(a.crop((0, 0, w, min(h, a.height))), (pad, band))
    canvas.paste(b.crop((0, 0, w, min(h, b.height))), (w + pad * 2, band))
    d = ImageDraw.Draw(canvas)
    d.text((pad, 14), labels[0], fill=(200, 200, 200))
    d.text((w + pad * 2, 14), labels[1], fill=(200, 200, 200))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)


IMAGE_MAGIC = {b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg", b"GIF8": ".gif", b"RIFF": ".webp", b"BM": ".bmp"}


def cmd_fetch_image(a):
    """Save an image found online as evidence."""
    import hashlib
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _common import fetch
    body = fetch(a.url, max_bytes=20 * 1024 * 1024)
    ext = next((e for m, e in IMAGE_MAGIC.items() if body.startswith(m)), None)
    if not ext:
        sys.exit("not an image (png/jpg/gif/webp/bmp); nothing saved")
    out = Path(a.out)
    if out.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        out = out.with_suffix(ext)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    meta = {"source_url": a.url, "page": a.page or "", "path": str(out), "bytes": len(body),
            "sha1": hashlib.sha1(body).hexdigest()}
    atomic_write(Path(str(out) + ".meta.json"), json.dumps(meta, indent=2))
    print(json.dumps(meta))


def cmd_redact(a):
    """Pixelate rectangles in an existing image, mostly screenshots found online."""
    from PIL import Image
    img = Image.open(a.image).convert("RGB")
    for box in a.box:
        x, y, w, h = (int(v) for v in box.split(","))
        if a.fill:  # pixelated faces can stay recognisable
            img.paste(tuple(int(a.fill.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)), (x, y, x + w, y + h))
            continue
        region = img.crop((x, y, x + w, y + h))
        small = region.resize((max(1, w // 16), max(1, h // 16)))
        img.paste(small.resize((w, h), Image.NEAREST), (x, y))
    out = a.out or a.image
    img.save(out)
    print(json.dumps({"out": out, "redacted_boxes": len(a.box)}))


def cmd_crop(a):
    from PIL import Image
    x, y, w, h = (int(v) for v in a.box.split(","))
    img = Image.open(a.image)
    out = a.out or str(Path(a.image).with_name(Path(a.image).stem + "-crop.png"))
    img.crop((x, y, x + w, y + h)).save(out)
    print(json.dumps({"out": out, "box": [x, y, w, h]}))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb[:3])


def cmd_probe(a):
    """Colours at points, a row profile, or where a colour sits."""
    from PIL import Image
    out = []
    for path in a.images:
        img = Image.open(path).convert("RGB")
        w, h = img.size
        px = img.load()
        res = {"image": path, "size": [w, h]}
        if a.at:
            res["at"] = {}
            for pt in a.at:
                x_, y_ = (int(v) for v in pt.split(","))
                res["at"][pt] = _hex(px[x_, y_]) if 0 <= x_ < w and 0 <= y_ < h else f"outside the {w}x{h} image"
        if a.row is not None:  # colour runs along a row (edges, bars, thresholds)
            runs, start, cur = [], 0, px[0, a.row]
            for x in range(1, w + 1):
                c = px[x, a.row] if x < w else None
                if c is None or sum(abs(c[i] - cur[i]) for i in range(3)) > a.tol * 3:
                    if x - start >= 2:
                        runs.append({"x0": start, "x1": x - 1, "color": _hex(cur)})
                    if c is not None:
                        start, cur = x, c
            res["row"] = runs[:60]
        if a.find:
            t = tuple(int(a.find.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
            xs, ys = [], []
            for yy in range(0, h, a.step):
                for xx in range(0, w, a.step):
                    c = px[xx, yy]
                    if all(abs(c[i] - t[i]) <= a.tol for i in range(3)):
                        xs.append(xx)
                        ys.append(yy)
            res["find"] = ({"color": a.find, "pixels": len(xs), "box": [min(xs), min(ys), max(xs), max(ys)],
                            "center": [round(sum(xs) / len(xs), 1), round(sum(ys) / len(ys), 1)]}
                           if xs else {"color": a.find, "pixels": 0})
        out.append(res)
    for r in out:
        print(json.dumps(r))


def cmd_track(a):
    """Track a coloured object through a burst and fit y = y0 + v0 t + g t^2 / 2.

    Drops repeated frames, which videos have and which wreck the timing."""
    import hashlib
    from PIL import Image
    t_target = tuple(int(a.find.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    rows, last_hash = [], None
    paths = list(a.images)  # already in time order
    times = json.loads(Path(a.times).read_text(encoding="utf-8")) if a.times else None  # real shot times, from `play`
    if isinstance(times, dict):
        times = times.get("times") or times.get("shot_times")
    rx = [int(v) for v in a.region.split(",")] if a.region else None
    for k, path in enumerate(paths):
        img = Image.open(path).convert("RGB")
        if rx:
            img = img.crop((rx[0], rx[1], rx[0] + rx[2], rx[1] + rx[3]))
        h = hashlib.md5(img.tobytes()).hexdigest()
        if h == last_hash:
            continue  # repeated frame, not a new moment
        last_hash = h
        px, (w, hgt) = img.load(), img.size
        hits = set()
        for yy in range(0, hgt, a.step):
            for xx in range(0, w, a.step):
                c = px[xx, yy]
                if all(abs(c[i] - t_target[i]) <= a.tol for i in range(3)):
                    hits.add((xx, yy))
        best = []  # biggest blob is the sprite, not same-coloured strays
        seen = set()
        for start in hits:
            if start in seen:
                continue
            blob, stack = [], [start]
            seen.add(start)
            while stack:
                cx, cy = stack.pop()
                blob.append((cx, cy))
                for nb in ((cx + a.step, cy), (cx - a.step, cy), (cx, cy + a.step), (cx, cy - a.step)):
                    if nb in hits and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            if len(blob) > len(best):
                best = blob
        if best:
            xs, ys = [p[0] for p in best], [p[1] for p in best]
            t = times[k] / 1000 if times and k < len(times) else k / a.fps
            rows.append({"frame": Path(path).name, "t": round(t, 4),
                         "x": round(sum(xs) / len(xs), 1), "y": round(sum(ys) / len(ys), 1),
                         "top": min(ys), "bottom": max(ys), "pixels": len(best)})
    out = {"frames": len(paths), "unique": len(set(r["frame"] for r in rows)), "points": rows}
    seg = rows[a.first:a.last] if (a.first is not None or a.last is not None) else rows
    if len(seg) >= 3:  # least-squares parabola fit
        ts, ys = [r["t"] - seg[0]["t"] for r in seg], [r["y"] for r in seg]
        n = len(ts)
        s1, s2, s3, s4 = sum(ts), sum(t * t for t in ts), sum(t ** 3 for t in ts), sum(t ** 4 for t in ts)
        sy, sty, st2y = sum(ys), sum(t * y for t, y in zip(ts, ys)), sum(t * t * y for t, y in zip(ts, ys))
        m = [[n, s1, s2, sy], [s1, s2, s3, sty], [s2, s3, s4, st2y]]
        for i in range(3):  # Gaussian elimination
            piv = m[i][i] or 1e-12
            for j in range(i + 1, 3):
                f = m[j][i] / piv
                m[j] = [x - f * y for x, y in zip(m[j], m[i])]
        c2 = m[2][3] / (m[2][2] or 1e-12)
        c1 = (m[1][3] - m[1][2] * c2) / (m[1][1] or 1e-12)
        c0 = (m[0][3] - m[0][1] * c1 - m[0][2] * c2) / (m[0][0] or 1e-12)
        resid = (sum((c0 + c1 * t + c2 * t * t - y) ** 2 for t, y in zip(ts, ys)) / n) ** 0.5
        xs_ = [r["x"] for r in seg]
        if resid > 4:
            print(f"warning: rms {resid:.1f} px is high: the target may be animating or another object shares its "
                  "colour. Narrow --region, raise --tol precision, or fit the `top`/`bottom` edge instead.", file=sys.stderr)
        out["fit"] = {"segment": [seg[0]["frame"], seg[-1]["frame"]], "gravity_px_s2": round(2 * c2, 1),
                      "initial_vy_px_s": round(c1, 1), "rms_px": round(resid, 2),
                      "vx_px_s": round((xs_[-1] - xs_[0]) / ((seg[-1]["t"] - seg[0]["t"]) or 1), 1),
                      "note": "screen y grows downward: positive gravity means falling. Convert px to game units "
                              "with a known size in the frame (pipe width, screen height)."}
    print(json.dumps(out, indent=1))


SHEET_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")


def _label(p) -> str:
    """The timestamp after '@' (scene004@01m02_50s), else the name's start."""
    stem = Path(p).stem
    tail = stem.rsplit("@", 1)[-1]
    if "@" in stem and re.fullmatch(r"\d+m[\d_.]+s?|[\d_.:]+s|t?[\d_.]+(ms|s)", tail):
        return tail[:24]
    return stem if len(stem) <= 24 else stem[:23] + "~"


def cmd_sheet(a):
    """Many frames in one image, so one Read shows a burst."""
    from PIL import Image, ImageDraw
    paths = []
    for x in a.images:  # only folder contents get sorted
        p = Path(x)
        if p.is_dir():  # an older sheet isn't a frame
            paths += sorted(str(f) for f in p.iterdir() if f.suffix.lower() in SHEET_EXT
                            and not re.fullmatch(r"sheet(-\d+)?", f.stem))
        else:
            paths.append(x)

    def flat(p):  # show transparency on mid grey rather than black
        im = Image.open(p)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (128, 128, 128, 255))
            bg.alpha_composite(im)
            return bg.convert("RGB")
        return im.convert("RGB")
    thumbs, kept, skipped = [], [], []
    for p in paths:
        if len(kept) >= a.max:
            break
        try:
            thumbs.append(flat(p))
            kept.append(p)
        except Exception as e:  # a .json or .txt caught by a glob
            skipped.append(p)
            print(f"skipped (not an image: {e.__class__.__name__}): {p}", file=sys.stderr)
    paths = kept
    if not paths:
        sys.exit("no images" + (f" ({len(skipped)} non-image files skipped)" if skipped else ""))
    if not a.out:
        first = Path(a.images[0])
        a.out = str((first if first.is_dir() else first.parent) / "sheet.png")
    out = Path(a.out)
    if not out.suffix:
        out = out / "sheet.png"  # a folder was given
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _common import out_note
    full = out_note(out)
    full.parent.mkdir(parents=True, exist_ok=True)
    cols = a.cols
    tw = min(a.width, 4000 // max(1, cols))  # max 4000 px wide, readable and small
    hs = sorted(int(im.height * tw / im.width) for im in thumbs)
    # one tall strip (mixed bitmaps) mustn't stretch every row
    th = max(8, min(hs[-1], max(hs[len(hs) // 2], tw * 3 // 4), tw * 5 // 4))
    sizes = {im.size for im in thumbs}
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * (th + 16)), (20, 20, 20))
    d = ImageDraw.Draw(sheet)
    for i, (p, im) in enumerate(zip(paths, thumbs)):
        x, y = (i % cols) * tw, (i // cols) * (th + 16)
        k = min(tw / im.width, th / im.height)  # fit, keeping the aspect ratio
        sheet.paste(im.resize((max(1, int(im.width * k)), max(1, int(im.height * k)))), (x, y + 16))
        # real size in labels, cell coordinates are scaled
        d.text((x + 3, y + 2), _label(p) + ("" if len(sizes) == 1 else f" {im.width}x{im.height}"), fill=(230, 230, 230))
    sheet.save(full)
    one = next(iter(sizes)) if len(sizes) == 1 else None
    print(json.dumps({"out": str(full), "frames": len(paths), "grid": [cols, rows], "cell": [tw, th],
                      **({"frame_size": list(one), "note": f"each cell shows a {one[0]}x{one[1]} frame at "
                          f"{min(tw / one[0], th / one[1]):.2f}x: divide cell coordinates by that to click or "
                          "measure in the frame"} if one else {}),
                      **({"skipped_not_images": skipped} if skipped else {})}))


def cmd_compare(a):
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("Pillow not installed: pip install pillow")
    res = similarity(a.then, a.now)
    if a.out:
        side_by_side(a.then, a.now, a.out, (a.then_label, a.now_label))
        res["pair_image"] = a.out
    res.update({"then": a.then, "now": a.now})
    print(json.dumps(res, indent=2))


def enter_ws(ws=None):
    from _common import enter_ws as _enter
    return _enter(ws)


def pop_ws_arg():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _common import pop_ws_arg as _pop
    return _pop()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("capture", cmd_capture), ("wayback", cmd_wayback)):
        s = sub.add_parser(name)
        if name == "capture":
            s.add_argument("url")
        else:
            s.add_argument("original")
            s.add_argument("ts")
        s.add_argument("out")
        s.add_argument("--width", type=int, default=1024)
        s.add_argument("--height", type=int, default=768)
        s.add_argument("--full", "--full-page", dest="full", action="store_true", help="full-page screenshot")
        s.add_argument("--timeout", type=int, default=45)
        s.add_argument("--settle", type=int, default=1500, help="ms to wait after load")
        s.add_argument("--selector", help="screenshot only this element (e.g. embed, #stage)")
        s.add_argument("--clip", help="x,y,w,h in CSS pixels: screenshot only this region")
        s.add_argument("--mask-images", action="store_true", help="grey out every <img> (avatars, photos)")
        s.add_argument("--mask", action="append",
                       help="CSS selector to cover with a grey box (user content, personal data); repeatable")
        s.set_defaults(fn=fn)
    s = sub.add_parser("play", help="drive a preserved original (e.g. emulated Flash) and record bursts")
    s.add_argument("url", help="e.g. https://web.archive.org/web/<ts>/<url-of-the.swf> (the bare SWF often "
                               "emulates more reliably than the page that embedded it)")
    s.add_argument("--steps", required=True, help=STEPS_HELP)
    s.add_argument("--out", default="dossier/screens/play")
    s.add_argument("--width", type=int, default=800)
    s.add_argument("--height", type=int, default=600)
    s.add_argument("--timeout", type=int, default=45)
    s.add_argument("--settle", type=int, default=4000, help="ms for the emulator to boot")
    s.set_defaults(fn=cmd_play)
    s = sub.add_parser("swf", help="see and drive an archived SWF locally (pinned self-hosted Ruffle, muted, sandboxed)")
    s.add_argument("target", help="the .swf file, or the folder holding it (its folder is served, so the movie can "
                                  "load sibling files such as vids/clip_N.swf)")
    s.add_argument("--steps", help=STEPS_HELP + ". Default: one screenshot after the movie loads")
    s.add_argument("--out", default="dossier/screens/swf")
    s.add_argument("--movie", help="with a folder: the SWF in it to play (default: the largest)")
    s.add_argument("--root", help="serve this folder instead of the SWF's own (when it loads ../files)")
    s.add_argument("--flashvars", help="FlashVars the page passed, as a query string: 'lang=en&level=2'")
    s.add_argument("--focus", default="1,1", help="x,y of the first click that gives the movie keyboard focus, or "
                                                  "'none' (default: the stage's top-left corner, away from buttons)")
    s.add_argument("--width", type=int, help="viewport width (default: the SWF's stage width)")
    s.add_argument("--height", type=int, help="viewport height (default: the stage height)")
    s.add_argument("--timeout", type=int, default=45)
    s.add_argument("--settle", type=int, default=3000, help="ms for the movie to start after Ruffle loads")
    s.add_argument("--grid", type=int, metavar="PX", help="also save a copy of each shot with a labelled grid every PX "
                   "stage pixels (e.g. 50), to read click coordinates off it")
    s.set_defaults(fn=cmd_swf)
    s = sub.add_parser("fetch-image")
    s.add_argument("url")
    s.add_argument("out", help="e.g. dossier/screens/editor-2007.png")
    s.add_argument("--page", help="the page the image was found on (for citation)")
    s.set_defaults(fn=cmd_fetch_image)
    s = sub.add_parser("redact")
    s.add_argument("image")
    s.add_argument("--box", action="append", required=True, help="x,y,w,h in pixels; repeatable")
    s.add_argument("--fill", help="#rrggbb solid fill instead of pixelation (use for faces)")
    s.add_argument("--out", help="default: overwrite the input")
    s.set_defaults(fn=cmd_redact)
    s = sub.add_parser("crop")
    s.add_argument("image")
    s.add_argument("--box", "--clip", dest="box", required=True, help="x,y,w,h in pixels")
    s.add_argument("--out", help="default: <name>-crop.png next to the input (evidence is never overwritten)")
    s.set_defaults(fn=cmd_crop)
    s = sub.add_parser("probe", help="colours at points, a row profile, or where a colour sits")
    s.add_argument("images", nargs="+")
    s.add_argument("--at", action="append", help="x,y (repeatable)")
    s.add_argument("--row", type=int, help="y of a row to profile into colour runs")
    s.add_argument("--find", help="#rrggbb: bounding box and centre of matching pixels")
    s.add_argument("--tol", type=int, default=24, help="per-channel tolerance")
    s.add_argument("--step", type=int, default=2, help="sampling step for --find")
    s.set_defaults(fn=cmd_probe)
    s = sub.add_parser("track", help="follow a coloured object through a burst; fit gravity and launch speed")
    s.add_argument("images", nargs="+", help="burst frames in time order (from community.py frames --fps)")
    s.add_argument("--find", required=True, help="#rrggbb of the object")
    s.add_argument("--fps", type=float, default=30, help="the burst's frame rate (ignored with --times)")
    s.add_argument("--times", help="JSON of real shot times in ms (written by `play` as play-times.json)")
    s.add_argument("--region", help="x,y,w,h: only look here (the stage, not the HUD)")
    s.add_argument("--tol", type=int, default=28)
    s.add_argument("--step", type=int, default=2)
    s.add_argument("--first", type=int, help="first point index of the segment to fit (e.g. just after a tap)")
    s.add_argument("--last", type=int, help="end index (exclusive)")
    s.set_defaults(fn=cmd_track)
    s = sub.add_parser("sheet", help="contact sheet of frames (one Read instead of many)")
    s.add_argument("images", nargs="+", help="image files or a folder of frames")
    s.add_argument("--out", help="sheet file (absolute, or relative to the workspace when there is one; parent "
                                 "folders are made). Default: sheet.png next to the frames")
    s.add_argument("--cols", type=int, default=6)
    s.add_argument("--width", type=int, default=240, help="width of each frame in the sheet (px)")
    s.add_argument("--max", type=int, default=48)
    s.set_defaults(fn=cmd_sheet)
    s = sub.add_parser("compare")
    s.add_argument("then")
    s.add_argument("now")
    s.add_argument("--out", help="write a labelled side-by-side PNG")
    s.add_argument("--then-label", default="THEN")
    s.add_argument("--now-label", default="NOW")
    s.set_defaults(fn=cmd_compare)
    ws_arg = pop_ws_arg() if argv is None else None  # --ws may come before or after the subcommand
    a = ap.parse_args(argv)
    enter_ws(ws_arg)  # relative paths are workspace paths
    a.fn(a)


if __name__ == "__main__":
    from _common import use_heavy
    use_heavy("PIL", "playwright")
    main()
