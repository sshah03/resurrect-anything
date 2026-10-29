#!/usr/bin/env python3
"""Verification loop + fidelity scoreboard.

Subcommands
  run --base URL [--journeys codex/journeys.json]
        Drive the running rebuild through scripted user journeys (Playwright). Writes
        codex/verify-results.json, screenshots to museum/shots/, and updates codex/parity.json.
        A feature becomes `verified` only when ALL its journeys pass in THIS run. A feature with a
        failing journey becomes `broken`.
  visual   Pair every screen in codex/screens.json (plus extra pairs in parity.json `visual`) with its
           reference, score each, and write a look sheet per pair for codex/LOOK.md (needs Pillow).
  score    Print the scoreboard and write codex/SCOREBOARD.md. Only counts verifications from
           the latest run (stale verifications are ignored).

Journey step vocabulary (one dict per step):
  {"do": "goto", "url": "/path"}             {"do": "click", "selector": "text=Subscribe"}
  {"do": "fill", "selector": "#q", "value": "x"}   {"do": "press", "key": "j"}
  {"do": "hover", "selector": ".menu"}       {"do": "select", "selector": "#suburb", "value": "Nebula"}
  {"do": "type", "selector": "#editor", "text": "hello", "ms": 40} (focuses, then types)  {"do": "keys", "text": "cat"} (keystrokes only)   {"do": "expect_hidden", "selector": ".popup"}       {"do": "wait", "ms": 300}
  {"do": "expect_visible", "selector": ".item.selected"}
  {"do": "expect_text", "selector": "h1", "contains": "Inbox"}
  {"do": "expect_count", "selector": ".item", "min": 3}
  {"do": "eval", "js": "window.__RESURRECT__.score", "equals": 100}   # games: expose test hooks
      eval checks: equals | truthy | gt | gte | lt | lte | approx + tol | contains
  {"do": "wait_until", "js": "window.__RESURRECT__.state === 'over'", "ms": 5000}
  {"do": "mouse_down"} / {"do": "mouse_up"} / {"do": "hold", "ms": 800}   (optionally "selector" or "x","y")
  {"do": "long_press", "selector": ".note", "ms": 600}   # = hold; apps: lift-to-reorder, context menus
  Mid-gesture checks: mouse_down, mouse_move (dx past the threshold), expect_*, mouse_up. Test every
  gesture threshold twice, just past it (it commits) and just short of it (it springs back).
  {"do": "key_down", "key": "Space"} / {"do": "key_up", "key": "Space"} / {"do": "hold", "key": "ArrowUp", "ms": 500}
  {"do": "drag", "selector": ".row:first-child", "dx": 120, "dy": 0, "steps": 12}   # swipes; real mouse events
  {"do": "mouse_move", "x": 200, "y": 300, "steps": 5}
  {"do": "arc", "selector": "#wheel", "from": 0, "to": 90, "steps": 12}   # circular drag (degrees, clockwise from 12)
  {"do": "advance", "ms": 300000}   # fast-forward timers; the journey needs "clock": true
  {"do": "upload", "selector": "input[type=file]", "files": ["app/test/tone.wav"]}
  {"do": "request", "method": "GET", "url": "/v1/short?u=x", "status": 200,
   "content_type": "json", "json": {"data.id": "*", "kind": "short"}, "contains": "..."}   # APIs
  {"do": "request", "url": "/forecastrss?w=1", "xml": {"./channel/item/yweather:forecast": 5,
   "./channel/title": "*"}}   # XML: count, exists, or exact text; namespaces from the document
APIs also get `verify.py shape --base URL`: parity.json "shape": [{"name": "feed", "then":
"dossier/raw/<archived response>", "url": "/same/request"}] scores structure against real responses.
  {"do": "screenshot", "name": "home", "full": false, "selector": "#stage", "clip": [x, y, w, h]}
  {"do": "burst", "name": "fall", "frames": 20, "ms": 100, "selector": "#stage"}   # numbered shots
expect_text retries until the step timeout. It reads the first VISIBLE match.

Manual journeys (taste, feel, a printed playtest, anything a browser cannot check):
  {"id": "J9", "feature": "F3", "name": "shatter crunch", "manual": true,
   "check": "Bite a chip: it snaps cleanly rather than bending (E0031)"}
  `verify.py manual` lists them and the USER does the check. `verify.py manual --record J9=pass
  --note "..."` records the result. Never record a manual result on the user's behalf.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import atomic_write  # noqa: E402

STATUSES = ("missing", "built", "verified", "broken")
WS_ROOT: list = []  # set by `run` so steps can resolve workspace paths
PRIORITY_WEIGHT = {"soul": 3, "core": 2, "extra": 1}


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def ws_path(arg):
    from ledger import find_ws
    return find_ws(arg)


def load(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _abs(base: str, url: str) -> str:
    return url if url.startswith("http") else base.rstrip("/") + "/" + url.lstrip("/")


def _dig(obj, path: str):
    for part in path.split("."):
        if isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)]
        elif isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            raise KeyError(path)
    return obj


def check_value(label: str, val, st: dict) -> None:
    def fail(want):
        raise AssertionError(f"{label} = {val!r}, expected {want}")
    if "equals" in st and val != st["equals"]:
        fail(repr(st["equals"]))
    if "truthy" in st and bool(val) != bool(st["truthy"]):
        fail(f"truthy={st['truthy']}")
    for op, cmp in (("gt", lambda a, b: a > b), ("gte", lambda a, b: a >= b),
                    ("lt", lambda a, b: a < b), ("lte", lambda a, b: a <= b)):
        if op in st and not (isinstance(val, (int, float)) and cmp(val, st[op])):
            fail(f"{op} {st[op]}")
    if "approx" in st:
        tol = st.get("tol", abs(st["approx"]) * 0.1 or 0.1)
        if not (isinstance(val, (int, float)) and abs(val - st["approx"]) <= tol):
            fail(f"{st['approx']} ± {tol}")
    if "contains" in st and str(st["contains"]).lower() not in str(val).lower():
        fail(f"to contain {st['contains']!r}")


def _point(page, st):
    if st.get("selector"):
        box = page.locator(st["selector"]).first.bounding_box()
        if not box:
            raise AssertionError(f"{st['selector']!r} not visible")
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    if "x" in st and "y" in st:
        return st["x"], st["y"]
    return None


def _js(code: str) -> str:
    """Allow statement-style snippets (`pause(); return x`) as well as expressions."""
    c = code.strip()
    if "return" in c and not c.startswith(("(", "function", "async")) and "=>" not in c.split("\n", 1)[0]:
        return f"(() => {{ {c} }})()"
    parts = [p for p in c.rstrip(";").split(";") if p.strip()]
    if len(parts) > 1 and not c.startswith(("(", "function", "async")):  # `a(); b()` -> the value of the last one
        return f"(() => {{ {';'.join(parts[:-1])}; return ({parts[-1]}); }})()"
    return c


ALLOW_CONSOLE = [""]  # set by `run --allow-console REGEX`


def do_step(page, base, st, j, i, res, shots_dir, timeout_ms):
    if st.get("do") == "long_press":  # a hold (lift-to-reorder, context menus)
        st = {**st, "do": "hold", "ms": st.get("ms", 600)}
    do = st.get("do")
    if st.get("frame") and do not in ("goto", "screenshot", "burst", "request", "wait", "press", "key_down", "key_up"):
        # iframe steps: DOM in the frame, input on the page
        fr = page.locator(st["frame"]).first.element_handle().content_frame()
        if fr is None:
            raise AssertionError(f"frame {st['frame']!r} not found")

        class InFrame:
            def __getattr__(self, name):
                return getattr(fr if hasattr(fr, name) and name not in ("mouse", "keyboard", "wait_for_timeout",
                                                                        "screenshot", "request") else page, name)
        st = {k: v for k, v in st.items() if k != "frame"}
        return do_step(InFrame(), base, st, j, i, res, shots_dir, timeout_ms)
    if do == "goto":
        page.goto(_abs(base, st["url"]), wait_until="load")
    elif do == "click":
        pt = (st["x"], st["y"]) if "x" in st and "y" in st else None
        if pt and st.get("selector"):  # x/y relative to the element
            box = page.locator(st["selector"]).first.bounding_box()
            if not box:
                raise AssertionError(f"{st['selector']!r} not visible")
            pt = (box["x"] + st["x"], box["y"] + st["y"])
        if pt:
            page.mouse.click(*pt)
        else:
            try:
                page.click(st["selector"], timeout=min(timeout_ms, 3000))
            except Exception as e:  # animated buttons never become "stable"
                if "stable" not in str(e) and "intercept" not in str(e):
                    raise
                page.click(st["selector"], force=True)
    elif do == "fill":
        page.fill(st["selector"], st.get("value", ""))
    elif do in ("type", "keys"):  # real keystrokes, one at a time
        if st.get("selector") and do == "type":
            page.focus(st["selector"])  # focus, since a click hits whatever's at the centre
        page.keyboard.type(st.get("text", ""), delay=int(st.get("ms", 30)))
    elif do == "expect_hidden":
        page.wait_for_selector(st["selector"], state="hidden")
    elif do == "press":
        if st.get("selector"):
            page.press(st["selector"], st["key"])
        else:
            page.keyboard.press(st["key"])
    elif do == "hover":
        page.hover(st["selector"])
    elif do == "advance":  # needs "clock": true on the journey
        page.clock.run_for(int(st.get("ms", 60000)))  # fires every timer due in the window, repeats included
    elif do == "dblclick":
        page.dblclick(st["selector"])
    elif do == "select":
        page.locator(st["selector"]).first.select_option(st.get("value") or st.get("label"))
    elif do == "wait":
        page.wait_for_timeout(int(st.get("ms", 250)))
    elif do in ("mouse_down", "mouse_up", "hold"):
        pt = _point(page, st)
        if pt:
            page.mouse.move(*pt)
        if do == "hold" and st.get("key"):
            page.keyboard.down(st["key"])
            page.wait_for_timeout(int(st.get("ms", 500)))
            page.keyboard.up(st["key"])
        elif do == "hold":
            page.mouse.down()
            page.wait_for_timeout(int(st.get("ms", 500)))
            page.mouse.up()
        elif do == "mouse_down":
            page.mouse.down()
        else:
            page.mouse.up()
    elif do == "mouse_move":
        pt = _point(page, st)
        if pt:
            page.mouse.move(*pt, steps=int(st.get("steps", 1)))
    elif do == "drag":
        start = _point(page, st)
        if not start:
            raise AssertionError("drag needs a selector or x,y to start from")
        page.mouse.move(*start)
        page.mouse.down()
        page.mouse.move(start[0] + st.get("dx", 0), start[1] + st.get("dy", 0), steps=int(st.get("steps", 12)))
        if st.get("hold_ms"):
            page.wait_for_timeout(int(st["hold_ms"]))
        page.mouse.up()
    elif do == "arc":  # circular drag for click wheels, dials, knobs
        import math
        box = page.locator(st["selector"]).first.bounding_box()
        if not box:
            raise AssertionError(f"{st['selector']!r} not visible")
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        r = st.get("radius", min(box["width"], box["height"]) * 0.38)
        a0, a1 = math.radians(st.get("from", 0)), math.radians(st.get("to", 90))
        n = int(st.get("steps", max(8, abs(st.get("to", 90) - st.get("from", 0)) // 3)))
        pts = [(cx + r * math.sin(a0 + (a1 - a0) * k / n), cy - r * math.cos(a0 + (a1 - a0) * k / n)) for k in range(n + 1)]
        page.mouse.move(*pts[0])
        page.mouse.down()
        for pt in pts[1:]:
            page.mouse.move(*pt)
            page.wait_for_timeout(int(st.get("ms_per_step", 8)))
        page.mouse.up()
    elif do == "upload":  # file inputs (your own music, photos)
        files = st["files"] if isinstance(st["files"], list) else [st["files"]]
        page.locator(st["selector"]).first.set_input_files(  # workspace-relative, like every other path
            [str(Path(f) if Path(f).is_absolute() else (WS_ROOT[0] / f if WS_ROOT else Path(f))) for f in files])
    elif do == "key_down":
        page.keyboard.down(st["key"])
    elif do == "key_up":
        page.keyboard.up(st["key"])
    elif do == "wait_until":
        page.wait_for_function(_js(st["js"]), timeout=int(st.get("ms", timeout_ms)))
    elif do == "expect_visible":
        page.wait_for_selector(st["selector"], state="visible")
    elif do == "expect_text":
        loc = page.locator(f"{st['selector']} >> visible=true").first
        deadline = time.time() + timeout_ms / 1000
        txt = ""
        while True:
            try:
                txt = loc.inner_text(timeout=500) or (loc.text_content(timeout=500) or "")
            except Exception:
                txt = ""
            if st["contains"].lower() in txt.lower():
                break
            if time.time() > deadline:
                raise AssertionError(f"{st['selector']!r} text {txt[:80]!r} lacks {st['contains']!r}")
            page.wait_for_timeout(100)
    elif do == "expect_count":
        n = page.locator(st["selector"]).count()
        lo = st.get("min", 0 if "max" in st else 1)
        if n < lo or ("max" in st and n > st["max"]):
            raise AssertionError(f"{st['selector']!r} count {n} outside "
                                 f"[{st.get('min', 1)}, {st.get('max', '∞')}]")
    elif do == "eval":
        check_value(st["js"], page.evaluate(_js(st["js"])), st)
    elif do == "request":
        r = page.request.fetch(_abs(base, st["url"]), method=st.get("method", "GET"),
                               data=st.get("body"), headers=st.get("headers"), fail_on_status_code=False)
        if "status" in st and r.status != st["status"]:
            raise AssertionError(f"{st['url']} status {r.status}, expected {st['status']}")
        ctype = r.headers.get("content-type", "")
        if "content_type" in st and st["content_type"] not in ctype:
            raise AssertionError(f"{st['url']} content-type {ctype!r} lacks {st['content_type']!r}")
        if "contains" in st and st["contains"] not in r.text():
            raise AssertionError(f"{st['url']} body lacks {st['contains']!r}")
        if st.get("xml"):  # {"xpath": "*" (exists) | 5 (count) | "text"}, namespaces taken from the document
            import io
            import xml.etree.ElementTree as ET
            raw = r.body()
            ns = {k: v for _, (k, v) in ET.iterparse(io.BytesIO(raw), events=["start-ns"]) if k}
            ns.update(st.get("ns", {}))
            root = ET.fromstring(raw)
            for path, want in st["xml"].items():
                hits = root.findall(path, ns) if not path.startswith("@") else []
                if want == "*" and not hits:
                    raise AssertionError(f"{st['url']} XML has no {path}")
                if isinstance(want, int) and not isinstance(want, bool) and len(hits) != want:
                    raise AssertionError(f"{st['url']} XML {path}: {len(hits)} found, expected {want}")
                if isinstance(want, str) and want != "*" and (not hits or (hits[0].text or "").strip() != want):
                    raise AssertionError(f"{st['url']} XML {path} text {(hits[0].text if hits else None)!r} != {want!r}")
        if st.get("json"):
            body = r.json()
            for path, want in st["json"].items():
                try:
                    got = _dig(body, path)
                except (KeyError, IndexError):
                    raise AssertionError(f"{st['url']} JSON has no {path}")
                if want != "*" and got != want:
                    raise AssertionError(f"{st['url']} {path} = {got!r}, expected {want!r}")
    elif do == "burst":  # timed shots for measuring motion (shots.py probe --find)
        target = page.locator(st["selector"]).first if st.get("selector") else page
        for k in range(int(st.get("frames", 10))):
            out = shots_dir / f"{st.get('name', 'burst')}-{k:03d}.png"
            target.screenshot(path=str(out))
            res["shots"].append(str(out))
            page.wait_for_timeout(int(st.get("ms", 100)))
    elif do == "screenshot":
        out = shots_dir / f"{st.get('name', j['id'] + '-' + str(i))}.png"
        if st.get("selector"):
            page.locator(st["selector"]).first.screenshot(path=str(out))
        else:
            kw = {"clip": dict(zip(("x", "y", "width", "height"), st["clip"]))} if st.get("clip") else {}
            page.screenshot(path=str(out), full_page=bool(st.get("full")), **kw)
        res["shots"].append(str(out))
    else:
        raise ValueError(f"unknown step {do!r}")


def run_journeys(base: str, journeys: list[dict], shots_dir: Path, width: int, height: int,
                 timeout_ms: int, fake_media: bool = False) -> list[dict]:
    from playwright.sync_api import sync_playwright
    results = []
    shots_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # Chromium's fake camera and mic, permission pre-granted
        args = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] if fake_media else []
        browser = p.chromium.launch(args=args)
        for j in journeys:
            ctx = browser.new_context(viewport={"width": width, "height": height},
                                      permissions=["camera", "microphone"] if fake_media else [])
            page = ctx.new_page()
            console_errors = []
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append(str(e)))
            failed_urls = []  # "Failed to load resource" doesn't name the file
            page.on("response", lambda r: failed_urls.append(f"{r.status} {r.url}") if r.status >= 400 else None)
            page.set_default_timeout(timeout_ms)
            if j.get("clock"):  # controllable clock for timers, see `advance`
                page.clock.install()
            res = {"id": j["id"], "feature": j.get("feature"), "name": j.get("name", ""),
                   "passed": True, "failed_step": None, "error": None, "shots": []}
            for i, st in enumerate(j.get("steps", [])):
                do = st.get("do")
                try:
                    do_step(page, base, st, j, i, res, shots_dir, timeout_ms)
                except Exception as e:
                    res.update({"passed": False, "failed_step": i, "error": f"{do}: {e}"[:400]})
                    try:
                        out = shots_dir / f"FAIL-{j['id']}.png"
                        page.screenshot(path=str(out))
                        res["shots"].append(str(out))
                    except Exception:
                        pass
                    break
            import re as _re
            allow = "|".join(x for x in (j.get("allow_console"), ALLOW_CONSOLE[0]) if x)
            if allow:  # harmless noise like "favicon|404"
                console_errors = [m for m in console_errors if not _re.search(allow, m)]
            if failed_urls:
                res["failed_requests"] = failed_urls[:10]
                console_errors = [m + (f" ({'; '.join(failed_urls[:3])})" if "Failed to load resource" in m else "")
                                  for m in console_errors]
            if console_errors and j.get("fail_on_console_error", True):
                res["console_errors"] = console_errors[:10]
                if res["passed"]:
                    res.update({"passed": False, "error": "console errors: " + console_errors[0][:200]})
            results.append(res)
            ctx.close()
        browser.close()
    return results


STEP_KEYS = {
    "goto": {"url"}, "click": set(), "dblclick": {"selector"}, "advance": {"ms"}, "type": {"text"}, "keys": {"text"},
    "expect_hidden": {"selector"}, "fill": {"selector", "value"}, "press": {"key"},
    "hover": {"selector"}, "select": {"selector"}, "wait": set(), "wait_until": {"js"},
    "mouse_down": set(), "mouse_up": set(), "mouse_move": set(), "hold": set(), "long_press": set(), "drag": set(),
    "key_down": {"key"}, "key_up": {"key"}, "expect_visible": {"selector"},
    "expect_text": {"selector", "contains"}, "expect_count": {"selector"}, "eval": {"js"},
    "request": {"url"}, "screenshot": set(), "burst": set(), "arc": {"selector"}, "upload": {"selector", "files"}}
CHECKS = {"equals", "truthy", "gt", "gte", "lt", "lte", "approx", "contains"}


def lint(journeys: list[dict], parity: dict) -> list[str]:
    """Catch journey mistakes before a run."""
    errs, ids = [], set()
    feats = {f["id"] for f in parity.get("features", [])}
    for j in journeys:
        jid = j.get("id", "?")
        if jid in ids:
            errs.append(f"{jid}: duplicate id")
        ids.add(jid)
        if j.get("feature") and feats and j["feature"] not in feats:
            errs.append(f"{jid}: feature {j['feature']} is not in parity.json")
        if j.get("manual"):
            if not j.get("check"):
                errs.append(f"{jid}: manual journeys need a `check` sentence")
            continue
        if j.get("kind") == "label":
            continue
        if not j.get("steps"):
            errs.append(f"{jid}: no steps")
        for i, st in enumerate(j.get("steps", [])):
            do = st.get("do")
            if do not in STEP_KEYS:
                errs.append(f"{jid} step {i}: unknown step {do!r} (see verify.py --help)")
                continue
            missing = STEP_KEYS[do] - set(st)
            if do == "click" and not ({"selector"} & set(st) or {"x", "y"} <= set(st)):
                missing.add("selector or x,y")
            known = STEP_KEYS[do] | {"do", "selector", "x", "y", "ms", "steps", "dx", "dy", "hold_ms", "name", "full",
                                     "clip", "frames", "key", "value", "label", "min", "max", "contains", "status", "frame", "radius", "from",
        "to", "ms_per_step", "files", "text",
                                     "content_type", "json", "xml", "ns", "method", "body", "headers", "note"} | CHECKS | {"tol"}
            extra = set(st) - known
            if extra:
                errs.append(f"{jid} step {i} ({do}): unknown keys {', '.join(sorted(extra))} would be ignored")
            if do == "select" and not ({"value", "label"} & set(st)):
                missing.add("value")
            if missing:
                errs.append(f"{jid} step {i} ({do}): missing {', '.join(sorted(missing))}")
            if do == "eval" and not (CHECKS & set(st)):
                errs.append(f"warning: {jid} step {i} (eval) checks nothing (fine for an action such as pause(); "
                            "otherwise add equals/gt/lt/approx/contains/truthy)")
            if do == "expect_text" and "text" in st and "contains" not in st:
                errs.append(f"{jid} step {i}: use `contains`, not `text`")
    return errs


def cmd_feature(a):
    """Set a feature's status or tags without hand-editing parity.json."""
    ws = ws_path(a.ws)
    ppath = ws / "codex" / "parity.json"
    parity = load(ppath, {"features": [], "visual": []})
    f = next((x for x in parity.get("features", []) if x["id"] == a.id), None)
    if not f:
        sys.exit(f"no feature {a.id} in parity.json")
    if a.status:
        f["status"] = a.status
    if a.tag:
        f["tags"] = sorted(set(f.get("tags", [])) | set(a.tag))
    atomic_write(ppath, json.dumps(parity, indent=2))
    print(json.dumps(f))


def cmd_lint(a):
    ws = ws_path(a.ws)
    errs = lint(load(ws / "codex" / "journeys.json", []), load(ws / "codex" / "parity.json", {}))
    print("\n".join(errs) or "journeys look fine")
    sys.exit(1 if any(not e.startswith("warning:") for e in errs) else 0)


def cmd_run(a):
    ws = ws_path(a.ws)
    jpath = Path(a.journeys) if a.journeys else ws / "codex" / "journeys.json"
    st_ = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    appr = st_.get("approval") or {}
    codex = ws / "codex" / "CODEX.md"
    if appr.get("codex_sha1") and codex.exists() and \
            __import__("hashlib").sha1(codex.read_bytes()).hexdigest()[:12] != appr["codex_sha1"]:
        sys.exit("CODEX.md changed after approval: show the user the change and `ledger.py approve` again before "
                 "verifying (scores must describe the approved spec).")
    all_journeys = load(jpath, [])
    WS_ROOT[:] = [ws.resolve()]
    found = lint(all_journeys, load(ws / "codex" / "parity.json", {}))
    for w in (e for e in found if e.startswith("warning:")):
        print(w, file=sys.stderr)
    errs = [e for e in found if not e.startswith("warning:")]
    if errs:
        sys.exit("journeys.json has problems (fix them first):\n" + "\n".join(errs))
    journeys = [j for j in all_journeys if not j.get("manual") and not j.get("kind")]
    if not journeys:
        sys.exit(f"no automatic journeys in {jpath}" + (" (only manual ones: use `verify.py manual`)"
                                                        if all_journeys else ""))
    try:
        import playwright  # noqa: F401
    except ImportError:
        sys.exit("playwright not installed: pip install playwright && playwright install chromium")
    server = None
    if a.serve:  # serve it ourselves, nothing to start or kill by hand
        import socket
        import subprocess
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        root = Path(a.serve) if Path(a.serve).is_dir() else ws / a.serve  # cwd-relative or workspace-relative
        if not root.is_dir():
            sys.exit(f"--serve: no folder {a.serve} (relative to here or to the workspace {ws})")
        server = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "serve.py"), str(port),
                                   str(root.resolve())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        a.base = f"http://127.0.0.1:{port}"
    elif not a.base:
        sys.exit("give --base URL, or --serve app to have verify.py serve the folder itself")
    run_id = uuid.uuid4().hex[:8]
    for old in (ws / "museum" / "shots").glob("FAIL-*.png"):
        old.unlink()  # failures from earlier runs must not leak into the museum
    try:
        vp = st_.get("viewport")
        if vp and a.width is None:
            log(f"viewport {vp[0]}x{vp[1]} (from state.json)")
        width, height = (a.width or (vp[0] if vp else 1024)), (a.height or (vp[1] if vp else 768))
        ALLOW_CONSOLE[0] = getattr(a, "allow_console", None) or ""
        results = run_journeys(a.base, journeys, ws / "museum" / "shots", width, height, a.timeout * 1000,
                               a.fake_media)
    finally:
        if server:
            server.terminate()
    passed = sum(r["passed"] for r in results)
    report = {"run_id": run_id, "at": now(), "base": a.base, "passed": passed,
              "total": len(results), "results": results}
    atomic_write(ws / "codex" / "verify-results.json", json.dumps(report, indent=2))

    ppath = ws / "codex" / "parity.json"
    parity = load(ppath, {"features": [], "visual": []})
    parity["last_run"] = run_id
    parity["journey_results"] = {r["id"]: {"feature": r.get("feature"), "passed": r["passed"]}
                                 for r in results}
    settle(parity, report["at"], all_journeys)
    atomic_write(ppath, json.dumps(parity, indent=2))
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"{mark} {r['id']} [{r.get('feature')}] {r['name']}" + ("" if r["passed"] else f"  -> {r['error']}"))
    print(f"\n{passed}/{len(results)} journeys passed (run {run_id})")
    sys.exit(0 if passed == len(results) else 1)


def settle(parity: dict, at: str, journeys: list | None = None) -> None:
    """Feature status from the latest run plus manual checks. Any failure means broken."""
    listed = {}  # a journey counts for every feature that lists it
    for f in parity.get("features", []):
        for jid in f.get("journeys", []):
            listed.setdefault(jid, set()).add(f["id"])
    per = {}
    auto_feats = set()
    for source, auto in ((parity.get("journey_results", {}), True), (parity.get("manual", {}), False),
                         (parity.get("checks", {}), False)):
        for jid, r in source.items():
            for fid in ({r["feature"]} if r.get("feature") else set()) | listed.get(jid, set()):
                per.setdefault(fid, []).append(r["passed"])
                if auto:
                    auto_feats.add(fid)
    done = set(parity.get("manual", {}))
    awaiting = {j.get("feature") for j in (journeys or []) if j.get("manual") and j["id"] not in done}
    for f in parity.get("features", []):
        oks = per.get(f["id"])
        if oks and all(oks) and f["id"] in awaiting:  # a pending hands-on check means not verified yet
            f["status"], f["verified_run"], f["awaiting"] = "built", None, "manual check"
            continue
        f.pop("awaiting", None)
        if oks is None:
            if f.get("status") == "verified":  # verified earlier, not re-checked, so stale
                f["status"] = "built"
            continue
        if all(oks):
            f["status"] = "verified"
            f["verified_run"] = parity.get("last_run") if f["id"] in auto_feats else "manual"
            if f["verified_run"] == "manual" and any(r.get("feature") == f["id"] for r in parity.get("checks", {}).values()) \
                    and not any(r.get("feature") == f["id"] for r in parity.get("manual", {}).values()):
                f["verified_run"] = "check"
        elif f.get("status", "missing") != "missing":  # never-built features stay missing
            f["status"], f["verified_run"] = "broken", None
        f["checked_at"] = at


def cmd_manual(a):
    ws = ws_path(a.ws)
    journeys = [j for j in load(ws / "codex" / "journeys.json", []) if j.get("manual")]
    ppath = ws / "codex" / "parity.json"
    parity = load(ppath, {"features": [], "visual": []})
    manual = parity.setdefault("manual", {})
    if not a.record:
        lines = ["# Manual checks", "", "Do each check yourself, then tell Claude the results.", ""]
        for j in journeys:
            r = manual.get(j["id"])
            mark = "pending" if not r else ("PASS" if r["passed"] else "FAIL")
            lines.append(f"- [{'x' if r else ' '}] **{j['id']}** ({j.get('feature')}) {j.get('name', '')}: "
                         f"{j.get('check', '')} — {mark}")
        md = "\n".join(lines) + "\n"
        (ws / "codex" / "MANUAL-CHECKS.md").write_text(md, encoding="utf-8")
        print(md)
        return
    ids = {j["id"]: j for j in journeys}
    for item in a.record:
        jid, _, verdict = item.partition("=")
        if jid not in ids or verdict not in ("pass", "fail"):
            sys.exit(f"bad record {item!r}: use J<n>=pass|fail for a manual journey ({', '.join(ids)})")
        manual[jid] = {"feature": ids[jid].get("feature"), "passed": verdict == "pass",
                       "note": a.note or "", "at": now(), "by": "user"}
    settle(parity, now(), load(ws / "codex" / "journeys.json", []))
    atomic_write(ppath, json.dumps(parity, indent=2))
    print(json.dumps({k: v["passed"] for k, v in manual.items()}))


def _clipped(ws: Path, img: Path, clip, tag: str) -> Path:
    """Crop an image to the product's screen, so like is compared with like."""
    if not clip:
        return img
    from PIL import Image
    x, y, w, h = clip  # [x, y, width, height], not two corners
    im = Image.open(img)
    if x + w > im.width or y + h > im.height:
        log(f"warning: {tag}_clip {clip} runs past {img.name} ({im.width}x{im.height}); clips are [x, y, width, height]")
    out = ws / "museum" / "shots" / f".{tag}-{img.stem}-clip.png"
    im.crop((x, y, x + w, y + h)).save(out)
    return out


def _unbarred(ws: Path, img: Path, target_aspect: float, tag: str) -> Path:
    """Trim uniform bars around a canvas stage when that gets closer to the original's shape."""
    from PIL import Image, ImageChops
    im = Image.open(img).convert("RGB")
    corner = im.getpixel((0, 0))
    diff = ImageChops.difference(im, Image.new("RGB", im.size, corner)).convert("L").point(lambda v: 255 if v > 12 else 0)
    box = diff.getbbox()
    if not box or (box[2] - box[0]) * (box[3] - box[1]) > 0.97 * im.width * im.height:
        return img
    w, h = box[2] - box[0], box[3] - box[1]
    before, after = abs(im.width / im.height - target_aspect), abs(w / h - target_aspect)
    # trim if it keeps the shape (4:3 in 4:3) or gets closer
    if not (after <= 0.02 * target_aspect or after < 0.6 * before):
        return img
    out = ws / "museum" / "shots" / f".{tag}-{img.stem}-unbarred.png"
    im.crop(box).save(out)
    log(f"note: trimmed bars around {img.name} to {w}x{h} (the stage); pass now_clip to choose the region yourself")
    return out


def _masked(ws: Path, img: Path, boxes, tag: str) -> Path:
    from PIL import Image, ImageDraw
    im = Image.open(img).convert("RGB")
    d = ImageDraw.Draw(im)
    for x, y, w, h in boxes:  # boxes in the (clipped) image's own pixels
        d.rectangle([x, y, x + w, y + h], fill=(128, 128, 128))
    out = ws / "museum" / "shots" / f".{tag}-{img.stem}-masked.png"
    im.save(out)
    return out


def _screen_pairs(ws: Path, parity: dict) -> list[str]:
    """Pair every screen in codex/screens.json with its reference. Returns unpaired screens with reasons.
    A screen with `then: null` and a `why` is excused."""
    screens = load(ws / "codex" / "screens.json", [])
    vis = parity.setdefault("visual", [])
    names = {screen_pair_name(sc) for sc in screens}
    # drop pairs under an old screen name, they'd double-count
    vis[:] = [v for v in vis if not v.get("screen") or v.get("name") in names]
    have = {v.get("name") for v in vis}
    gaps = []
    for sc in screens:
        name = screen_pair_name(sc)
        if sc.get("then") is None and sc.get("why") and not sc.get("then_frames"):
            continue  # excused, counted separately on the scoreboard
        then = sc.get("then")
        if not then and sc.get("then_frames"):
            import glob as _g
            got = sorted(_g.glob(str(ws / sc["then_frames"])))
            then = str(Path(got[0]).relative_to(ws)) if got else None
        now_ = sc.get("now") or (f"museum/shots/{sc['shot']}.png" if sc.get("shot") else None)
        if not then:
            gaps.append(f"{name}: no reference frame (`then`): find one, or set `then: null` with a `why`")
            continue
        if not now_ or not (ws / now_).exists():
            gaps.append(f"{name}: no rebuild screenshot ({now_ or 'set `shot` to a journey screenshot name'}): "
                        "add a `screenshot` step with that name to the screen's journey and run verify.py run")
            continue
        entry = {"name": name, "then": then, "now": now_, "screen": sc.get("id"), "evidence": sc.get("evidence", [])}
        for k in ("then_frames", "now_frames", "then_clip", "now_clip", "mask"):
            if sc.get(k):
                entry[k] = sc[k]
        if name in have:
            for v in vis:
                if v.get("name") == name:
                    v.update(entry)
        else:
            vis.append(entry)
    return gaps


def screen_pair_name(sc: dict) -> str:
    from ledger import slugify
    return f"{sc.get('id', 'S')}-{slugify(sc.get('name', 'screen'))}"


def cmd_visual(a):
    from shots import similarity, side_by_side
    ws = ws_path(a.ws)
    ppath = ws / "codex" / "parity.json"
    parity = load(ppath, {"features": [], "visual": []})
    gaps = _screen_pairs(ws, parity)
    for g in gaps:
        print(f"unpaired screen {g}")
    for v in parity.get("visual", []):
        then, now_ = ws / v["then"], ws / v["now"]
        if not (then.exists() and now_.exists()):
            print(f"skip {v.get('name')}: missing {'then' if not then.exists() else 'now'} image")
            v["score"] = None
            continue
        then, now_ = _clipped(ws, then, v.get("then_clip"), "then"), _clipped(ws, now_, v.get("now_clip"), "now")
        if not v.get("now_clip"):
            from PIL import Image as _I
            tw_, th_ = _I.open(then).size
            now_ = _unbarred(ws, now_, tw_ / th_, "now")
        if v.get("mask"):  # regions meant to differ (a sample photo, a user's canvas)
            then, now_ = _masked(ws, then, v["mask"], "then"), _masked(ws, now_, v["mask"], "now")
        from PIL import Image
        (tw, th), (nw, nh) = Image.open(then).size, Image.open(now_).size
        if not v.get("privacy_checked"):
            print(f"note {v.get('name')}: pair images land in museum/shots; open the then-image and check it for "
                  "faces, names and usernames (crop or `shots.py redact --fill`) before unveiling")
        if abs(tw / th - nw / nh) > 0.15 * (tw / th) or max(tw, nw) > 2.5 * min(tw, nw):
            print(f"warning {v.get('name')}: then {tw}x{th} vs now {nw}x{nh}; different framing or scale makes "
                  "the score meaningless. Use then_clip/now_clip or a screenshot `selector`.")
        res = similarity(then, now_)
        import hashlib as _h
        then_sha = _h.sha1(Path(v["then"] if Path(v["then"]).is_absolute() else ws / v["then"]).read_bytes()).hexdigest() \
            if (Path(v["then"]) if Path(v["then"]).is_absolute() else ws / v["then"]).exists() else None
        shipped = {_h.sha1(f.read_bytes()).hexdigest() for f in (ws / "app").rglob("*")
                   if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")} if then_sha else set()
        if then_sha in shipped:
            res["self_referential"] = True
            print(f"warning {v.get('name')}: the then-image is itself shipped in app/ (the rebuild shows the recovered "
                  "frame), so this score measures the copy, not the rebuild. Compare a screen the app draws itself, "
                  "or mark the pair \"reference\": true.")
        pair = ws / "museum" / "shots" / f"pair-{v.get('name', 'view')}.png"
        side_by_side(then, now_, pair)
        # up-close look, thumbnails miss smeared or textureless parts
        from shots import look_sheet
        import glob as _g
        tf = sorted(_g.glob(str(ws / v["then_frames"]))) if v.get("then_frames") else [str(then)]
        nf = sorted(_g.glob(str(ws / v["now_frames"]))) if v.get("now_frames") else [str(now_)]
        look = ws / "museum" / "shots" / f"look-{v.get('name', 'view')}.png"
        import hashlib as _hl  # LOOK.md quotes this code to prove the sheet was opened.
        # Hash of the pictures, so it holds until they change.
        code = _hl.sha1(b"".join(Path(x).read_bytes() for x in (tf or [str(then)]) + (nf or [str(now_)])
                                 if Path(x).is_file())).hexdigest()[:4].upper()
        ls = look_sheet(tf or [str(then)], nf or [str(now_)], look, code=code)
        v["look"] = str(look.relative_to(ws))
        v["look_code"], v["look_bands"] = code, ls["bands"]
        v.update({"score": res["score"], "detail": res, "pair": str(pair.relative_to(ws)),
                  "checked_at": now()})
        print(f"{v.get('name')}: {res['score']}")
    looks = [v["look"] for v in parity.get("visual", []) if v.get("look")]
    if looks:
        print("\nNow LOOK (the score above cannot see detail): open each of " + ", ".join(looks) + " with Read. For every "
              "band, compare the rebuild with the original element by element (background layers, ground, HUD, "
              "clouds, each sprite, text): shape, texture, sharpness, colour, and anything present on one side only. "
              "Write codex/LOOK.md, per pair:\n  ## <pair name> (code <the code printed on its sheet>)\n"
              "  - band 1: matches | other moment: <what> | differs: <what> | differs, no trace: <what is missing, "
              "where you searched> | matches (stand-in content) | matches (photo: <why>)\n"
              "  - band 2: ...\n"
              "A `differs` is a to-do, not a finding: fix it and run visual again (new sheets get new codes) until each "
              "band matches, is the same thing at `other moment` (an animation or scroll phase, a cursor), or is "
              "`differs, no trace` because nothing that survives shows it. Lettering, digits and transitions seen in "
              "frames are traces. Where a "
              "tool's version of an element looks worse than a plain crop of the clearest frame, ship the plain crop "
              "(floor rule). `score` checks each `matches` against its own measurement of that band.")
    atomic_write(ppath, json.dumps(parity, indent=2))


def honesty(ws: Path, parity: dict) -> dict:
    """What the headline numbers leave out: unlisted features, Codex grounding, label match."""
    from ledger import feature_table, read_jsonl, slugify, spec_report
    covered, cited = set(), set()
    for f in parity.get("features", []):
        covered.add(slugify(f.get("name", "")))
        covered.update(slugify(t) for t in f.get("tags", []))
        cited.update(f.get("evidence", []))
    ev_all = read_jsonl(ws / "evidence.jsonl")
    untagged = [e["id"] for e in ev_all if e.get("kind") in ("feature", "behavior", "flow") and not e.get("feature")]
    not_features = {"reference", "references", "research", "data", "source", "sources", "background", "context",
                    "usda", "nutrition-data", "identity", "timeline", "death", "history"}
    rows = [r for r in feature_table(read_jsonl(ws / "evidence.jsonl"))  # a rulebook or the file itself is enough
            if (r["missed_by_sources"] >= 1 or r["documented_by_sources"] >= 2 or r.get("primary_sources", 0) >= 1)
            and r["feature"] not in not_features]  # reference data is not a feature to rebuild
    # covered if a feature names the tag or cites its evidence
    left_out = [r["feature"] for r in rows
                if r["feature"] not in covered and not cited.intersection(r["evidence"])]
    out = {"evidence_features": len(rows), "counted_tags": [r["feature"] for r in rows], "left_out": left_out,
           "untagged_feature_items": untagged,
           "scope": round(1 - len(left_out) / len(rows), 3) if rows else None,
           "sentiment_items": sum(1 for e in read_jsonl(ws / "evidence.jsonl") if e.get("kind") == "sentiment")}
    codex = ws / "codex" / "CODEX.md"
    if codex.exists():
        rep, _ = spec_report(ws, str(codex))
        out["grounding"] = rep["grounding"]
    journeys_all = load(ws / "codex" / "journeys.json", [])
    out["via_hooks"] = sum(1 for j in journeys_all if j.get("via") == "hooks")
    out["auto_journeys"] = sum(1 for j in journeys_all if not j.get("manual"))
    out["not_built"] = [f.get("name", f["id"]) for f in parity.get("features", []) if f.get("status", "missing") == "missing"]
    manual = [j for j in journeys_all if j.get("manual")]
    done = parity.get("manual", {})
    out.update({"manual_total": len(manual), "manual_recorded": sum(1 for j in manual if j["id"] in done),
                "manual_pending": sum(1 for j in manual if j["id"] not in done)})
    # original share: bytes of the app that are the original
    app = ws / "app"
    orig_files, rec_files = set(), set()
    for mf in app.rglob("MANIFEST.json"):
        try:
            mj = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log(f"warning: {mf} is not valid JSON; its files are not counted")
            continue
        files = mj.get("files")
        if files is None and isinstance(mj, dict) and any(isinstance(v, dict) and "origin" in v for v in mj.values()):
            files = {k: v for k, v in mj.items() if isinstance(v, dict) and "origin" in v}  # flat form accepted
        files = files or {}
        if not isinstance(files, dict):
            log(f"warning: {mf}: \"files\" must be an object {{\"<path relative to the manifest>\": {{\"origin\": "
                "\"original\"|\"recovered\", ...}}}}; this one is not counted")
            continue
        for rel, meta in (files.items() if isinstance(files, dict) else []):
            if meta.get("origin") in ("original", "original-edited"):  # edited = privacy redactions only
                orig_files.add((mf.parent / rel).resolve())
            elif meta.get("origin") == "recovered":  # cut from footage or fan rips
                rec_files.add((mf.parent / rel).resolve())
    shipped = [f for f in app.rglob("*") if f.is_file() and f.name != "MANIFEST.json"
               and not f.name.endswith((".md", ".meta.json")) and "test" not in f.parts]
    if shipped:
        ob = sum(f.stat().st_size for f in shipped if f.resolve() in orig_files)
        tb = sum(f.stat().st_size for f in shipped) or 1
        def role(f):
            e = f.suffix.lower()
            return ("art" if e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg") else
                    "sound" if e in (".mp3", ".ogg", ".wav", ".m4a", ".aac") else
                    "font" if e in (".ttf", ".otf", ".woff", ".woff2") else
                    "program" if e in (".swf", ".js", ".wasm") else "other")
        by_role = {}
        for f in shipped:
            r_ = by_role.setdefault(role(f), [0, 0, 0])
            r_[0] += 1
            r_[1] += f.resolve() in orig_files
            r_[2] += f.resolve() in rec_files
        out["by_role"] = {k: {"files": v[0], "original": v[1], "recovered": v[2]} for k, v in sorted(by_role.items())}
        edited = set()
        for mf in app.rglob("MANIFEST.json"):
            try:
                fj = json.loads(mf.read_text(encoding="utf-8")).get("files") or {}
            except (OSError, ValueError):
                continue
            edited |= {(mf.parent / r).resolve() for r, m in fj.items() if isinstance(m, dict) and m.get("origin") == "original-edited"}
        out["edited_files"] = sum(1 for f in shipped if f.resolve() in edited)
        out.update({"original_share": round(ob / tb, 3),
                    "original_files": sum(1 for f in shipped if f.resolve() in orig_files),
                    "shipped_files": len(shipped),
                    "recovered_files": sum(1 for f in shipped if f.resolve() in rec_files),
                    "recovered_share": round(sum(f.stat().st_size for f in shipped if f.resolve() in rec_files) / tb, 3)})
    for mf in app.rglob("MANIFEST.json"):
        try:
            out["missing_originals"] = out.get("missing_originals", 0) + len(
                json.loads(mf.read_text(encoding="utf-8")).get("missing", []))
        except (OSError, ValueError):
            pass
    # what each verified feature rests on. REA: a check needs
    # evidence of comparable authority to close a claim
    from ledger import authority, AUTHORITY
    ev_by_id = {e["id"]: e for e in ev_all}
    rest = collections.Counter()
    for f in parity.get("features", []):
        if f.get("status") != "verified":
            continue
        items = [ev_by_id[i] for i in f.get("evidence", []) if i in ev_by_id] or \
            [e for e in ev_all if e.get("feature") and (e["feature"] in f.get("tags", []) or e["feature"] == f.get("id"))]
        best = max((AUTHORITY.index(authority(e)) for e in items), default=0)
        rest[AUTHORITY[best]] += 1
    if rest:
        out["verified_rest_on"] = {k: rest[k] for k in reversed(AUTHORITY) if rest.get(k)}
    journeys_cases = collections.defaultdict(set)
    for j in journeys_all:
        if j.get("feature"):
            journeys_cases[j["feature"]].add(j.get("case", "normal"))
    soul_ids = [f["id"] for f in parity.get("features", []) if f.get("priority") == "soul"]
    if soul_ids:
        none_shown = {f["id"] for f in parity.get("features", []) if f.get("failure_case") == "none in the evidence"}
        out["soul_with_failure_cases"] = sum(1 for fid in soul_ids if journeys_cases[fid] - {"normal"})
        out["soul_features"] = len(soul_ids) - len(none_shown & set(soul_ids))
    # share of copy-deck lines that are the original's words
    codex = ws / "codex" / "CODEX.md"
    if codex.exists():
        in_copy, verb, rec = False, 0, 0
        for line in codex.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                in_copy = bool(re.search(r"(?i)\bcopy\b|messages|text deck|wording", line))
                continue
            if in_copy and line.strip().startswith("|") and not re.match(r"^\|[\s:|-]+\|?$", line.strip()):
                if re.search(r"\bE\d{4}\b", line) and not re.search(r"(?i)\[design\]", line):
                    verb += 1
                elif re.search(r"(?i)\[design\]|\[unknown\]", line):
                    rec += 1
        if verb or rec:
            out["copy_verbatim"], out["copy_reconstructed"] = verb, rec
    lm = ws / "codex" / "label-match.json"
    if lm.exists():
        lmj = json.loads(lm.read_text(encoding="utf-8"))
        out["label_match"] = lmj.get("label_match")
        out["label_source"] = lmj.get("target_source", "")
        out["fitted_share"] = lmj.get("fitted_share")
        out["label_underdetermined"] = any("underdetermined" in w for w in lmj.get("warnings", []))
        out["panel_numbers"] = lmj.get("panel_numbers")
    return out


def _shape(raw: bytes) -> list[str]:
    """A response's structure (paths, attribute names, value types), ignoring values."""
    text = raw.lstrip()
    if text[:1] in (b"{", b"["):
        out = []

        def walk(o, path):
            if isinstance(o, dict):
                for k in o:
                    walk(o[k], f"{path}.{k}")
            elif isinstance(o, list):
                for x in o[:3]:
                    walk(x, path + "[]")
            else:
                out.append(f"{path}:{type(o).__name__}")
        walk(json.loads(text), "$")
        return out
    import xml.etree.ElementTree as ET
    out = []

    def walk_x(el, path):
        p = f"{path}/{el.tag}"
        out.append(p + ("[" + ",".join(sorted(el.attrib)) + "]" if el.attrib else ""))
        for c in el:
            walk_x(c, p)
    walk_x(ET.fromstring(text), "")
    return out


def cmd_shape(a):
    """Score archived API responses in parity.json `shape` against the rebuild's."""
    import difflib
    import socket
    import subprocess
    import urllib.request
    ws = ws_path(a.ws)
    server = None
    if a.serve:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        root = Path(a.serve) if Path(a.serve).is_dir() else ws / a.serve
        server = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "serve.py"), str(port),
                                   str(root.resolve())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        a.base = f"http://127.0.0.1:{port}"
    if not a.base:
        sys.exit("give --base URL or --serve <folder>")
    ppath = ws / "codex" / "parity.json"
    parity = load(ppath, {"features": [], "visual": []})
    for sp in parity.get("shape", []):
        then = (ws / sp["then"]).read_bytes()
        now_ = urllib.request.urlopen(a.base.rstrip("/") + "/" + sp["url"].lstrip("/"), timeout=20).read()
        t, n = _shape(then), _shape(now_)
        ratio = difflib.SequenceMatcher(a=t, b=n, autojunk=False).ratio()
        sp.update({"score": round(ratio, 3), "missing": [x for x in t if x not in n][:20],
                   "extra": [x for x in n if x not in t][:20], "checked_at": now()})
        print(f"{sp.get('name')} ({sp.get('kind', 'archived-response')}): {sp['score']}")
        for x in sp["missing"][:15]:
            print(f"   - missing: {x}")
        for x in sp["extra"][:15]:
            print(f"   + extra:   {x}")
    if server:
        server.terminate()
    atomic_write(ppath, json.dumps(parity, indent=2))


def _look_sections(ws: Path) -> dict:
    """Parse codex/LOOK.md by pair name. Headings must match the pair name exactly."""
    f = ws / "codex" / "LOOK.md" if ws else None
    out, cur = {}, None
    if not f or not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+(\S+)(.*)$", line)
        if m:
            cur = m.group(1)
            out[cur] = {"head": m.group(2), "bands": {}, "text": ""}
            continue
        if cur:
            out[cur]["text"] += line + "\n"
            b = re.match(r"^\s*[-*]?\s*band\s+(\d+)\s*:\s*(.+)$", line, re.I)
            if b:
                out[cur]["bands"][int(b.group(1))] = b.group(2).strip()
    return out


def _look_status(ws: Path, parity: dict) -> dict:
    """Which pairs were really looked at (sheet code quoted, every band judged).
    A `matches` the script measured as different is `contradicted`, unless it says stand-in content."""
    secs = _look_sections(ws)
    done, looked, missing_code, contradicted, differs, no_trace = [], [], [], [], [], 0
    for v in parity.get("visual", []):
        if not v.get("look"):
            continue
        sec = secs.get(v.get("name"))
        if not sec:
            continue
        code = v.get("look_code")
        if code and code not in sec["head"] + sec["text"]:
            missing_code.append(v["name"])
            continue
        bands = v.get("look_bands") or []
        if bands and len(sec["bands"]) < len(bands):
            missing_code.append(v["name"] + " (a verdict per band)")
            continue
        looked.append(v["name"])
        open_here = []
        for b in bands:
            verdict = sec["bands"].get(b["band"], "").lower()
            if verdict.startswith("differs"):
                if re.match(r"differs\W+no trace", verdict):
                    no_trace += 1
                else:  # open until fixed or shown untraceable
                    open_here.append(f"{v['name']} band {b['band']}")
            elif verdict.startswith("other moment"):
                pass
            elif verdict.startswith("matches") and not any(w in verdict for w in ("stand-in", "photo")):
                # "stand-in" or "photo" notes explain a measured difference
                # worst_tile = % of a tile's pixels that differ strongly
                if b.get("worst_tile", 0) > 3 or (b.get("sharpness") is not None and b["sharpness"] < 0.6):
                    contradicted.append(f"{v['name']} band {b['band']} ({b.get('worst_tile')}% of a tile differs "
                                        f"strongly, sharpness x{b.get('sharpness')})")
        if open_here:
            differs += open_here
        else:
            done.append(v["name"])
    return {"done": done, "looked": looked, "unproven": missing_code, "contradicted": contradicted, "differs": differs,
            "no_trace": no_trace}


def evidence_use(ws: Path, parity: dict) -> dict:
    """Which `seen` items a passing, asserting journey or a looked-at screen checks.
    Items named in codex/LEFT-OUT.md are excused. Other mentions don't count."""
    from ledger import read_jsonl
    kinds = {"ui", "behavior", "flow", "copy", "feature", "content"}
    items = [e for e in read_jsonl(ws / "evidence.jsonl")
             if e.get("tag") == "seen" and e.get("kind") in kinds and not e.get("superseded_by")]
    results = load(ws / "codex" / "verify-results.json", {})
    passed = {r.get("id") for r in results.get("results", []) if r.get("passed")}
    asserting = {"expect_visible", "expect_hidden", "expect_text", "expect_count", "eval", "check", "expect_url",
                 "request"}
    via_journeys = set()
    for jn in load(ws / "codex" / "journeys.json", []):
        if jn.get("id") in passed and any(st.get("do") in asserting or st.get("check") for st in jn.get("steps", [])):
            via_journeys.update(jn.get("evidence", []))
    if not _is_software(ws):
        # documents use what they cite (recipe.json, RULES.md...)
        deliverable = " ".join(f.read_text(errors="replace", encoding="utf-8") for f in (ws / "app").glob("*")
                               if f.is_file() and f.suffix in (".json", ".md", ".txt", ".html")) \
            if (ws / "app").is_dir() else ""
        via_journeys |= {e["id"] for e in items if re.search(rf"\b{e['id']}\b", deliverable)}
    looked = set(_look_status(ws, parity)["looked"])
    # a look shows layout, not behaviour, which needs a journey
    shown = {e["id"] for e in items if e.get("kind") in ("ui", "copy", "content")}
    via_screens = {i for v in parity.get("visual", []) if v.get("name") in looked
                   for i in v.get("evidence", []) if i in shown}
    left = (ws / "codex" / "LEFT-OUT.md").read_text(encoding="utf-8") if (ws / "codex" / "LEFT-OUT.md").exists() else ""
    checked, excused, unused = [], [], []
    for e in items:
        i = e["id"]
        if i in via_journeys or i in via_screens:
            checked.append(i)
        elif re.search(rf"\b{i}\b", left):
            excused.append(i)
        else:
            unused.append(e)
    return {"items": len(items), "checked": len(checked), "left_out": len(excused), "unused": unused}


def _is_software(ws) -> bool:
    st = load(ws / "state.json", {}) if ws else {}
    return st.get("type", "app") in ("website", "webapp", "app", "game", "api", "other")


def _coverage_rows(ws, parity) -> list[str]:
    rows = []
    screens = load(ws / "codex" / "screens.json", []) if ws else []
    if ws and not _is_software(ws):
        pass  # docs don't change what screens show
    elif screens:
        docs = (".md", ".txt")  # writing the README or MANIFEST does not change what the screens show
        app_files = [f.stat().st_mtime for f in (ws / "app").rglob("*") if f.is_file() and f.suffix.lower() not in docs
                     and f.name != "MANIFEST.json"] if (ws / "app").is_dir() else []
        newest_app = max(app_files) if app_files else 0
        by_name = {v.get("name"): v for v in parity.get("visual", [])}
        excused = [sc for sc in screens if sc.get("then") is None and sc.get("why") and not sc.get("then_frames")]
        names = [screen_pair_name(sc) for sc in screens if sc not in excused]
        compared, stale = 0, []
        for n in names:
            v = by_name.get(n)
            if not v or v.get("score") is None:
                continue
            nowp = ws / v["now"]
            if nowp.exists() and nowp.stat().st_mtime + 1 < newest_app:
                stale.append(n)  # app changed since, screenshot is stale
            else:
                compared += 1
        looked = set(_look_status(ws, parity)["looked"])
        diffed = sum(1 for sc in screens if sc not in excused and
                     any((ws / "codex" / "blueprints").glob(f"{sc.get('id')}*/**/rounds.json")))
        rows.append(f"| Screens compared | {compared} of {len(names)} inventoried"
                    + (f" ({len(excused)} excused: never recorded)" if excused else "")
                    + (f", {len(stale)} with screenshots older than the app" if stale else "")
                    + f"; {sum(1 for n in names if n in looked)} looked at; {diffed} diffed against their blueprint | "
                    "every screen in codex/screens.json against its reference frame (verify.py visual), its LOOK.md "
                    "verdict, and `blueprint.py diff` rounds under codex/blueprints/<screen id>/ |")
    else:
        rows.append("| Screens compared | no inventory | write codex/screens.json (one entry per screen with its "
                    "reference frame) so every screen is compared, not only the ones picked |")
    if ws and (ws / "evidence.jsonl").exists():
        u = evidence_use(ws, parity)
        if u["items"]:
            rows.append(f"| Evidence used (self-reported) | {u['checked']} of {u['items']} checked, {u['left_out']} "
                        f"left out with a reason, {len(u['unused'])} unused | `seen` items about screens, behaviour and "
                        "text listed by a journey that passed and asserts, by a screen looked at with its code, or (recipes, "
                        "rulesets) cited in the deliverable "
                        "(codex/UNUSED-EVIDENCE.md lists the rest) |")
            out = ["# Evidence found but not used", "",
                   "Each item below is something the original did or showed that no passing check covers. Build it "
                   "and check it (list the ID in the `evidence` of a journey that asserts it, or of the screen in "
                   "codex/screens.json, then look at that screen), or say why not in codex/LEFT-OUT.md.", ""]
            out += [f"- {e['id']} [{e.get('kind')}] {e.get('claim', '')[:240]}" for e in u["unused"]]
            (ws / "codex" / "UNUSED-EVIDENCE.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    return rows


def _look_row(ws, parity) -> str:
    """Scoreboard row: were all pairs looked at, and do verdicts match measurements?"""
    names = [v.get("name", "view") for v in parity.get("visual", []) if v.get("look")]
    if ws and not _is_software(ws) and not names:
        return ""
    if not names:
        return "| Up-close look (self-reported) | none | no look sheets yet: `verify.py visual` makes them |"
    st = _look_status(ws, parity)
    note = (f"OPEN, fix or show no trace: {', '.join(st['differs'])}" if st["differs"] else "no open differences")
    if st["no_trace"]:
        note += f"; {st['no_trace']} band(s) differ where no trace survives"
    if st["contradicted"]:
        note += f"; CONTRADICTED (marked matches, measured different): {', '.join(st['contradicted'])}"
    if st["unproven"]:
        note += f"; not counted (no sheet code or missing band verdicts): {', '.join(st['unproven'])}"
    return f"| Up-close look (self-reported) | {len(st['done'])} of {len(names)} pairs settled | {note} |"


def compute(parity: dict, ws: Path | None = None) -> dict:
    raw = parity.get("features", [])
    # exhibit/catalogue parts are shown, not rebuilt: count as missing
    feats = [dict(f, status="missing") if f.get("tier") in ("exhibit", "catalogue") else f for f in raw]
    last = parity.get("last_run")

    def w(f):
        return f.get("weight") or PRIORITY_WEIGHT.get(f.get("priority", "core"), 2)

    def fresh(f):
        return f.get("status") == "verified" and f.get("verified_run") in ({last, "manual", "check"} - {None})

    total = sum(w(f) for f in feats) or 1
    soul = [f for f in feats if f.get("priority") == "soul"]
    soul_total = sum(w(f) for f in soul) or 1
    vis = [v["score"] for v in parity.get("visual", [])  # reference pairs (photos of objects) don't score
           if isinstance(v.get("score"), (int, float)) and not v.get("reference")]
    return {
        "features": len(feats),
        "verified": sum(1 for f in feats if fresh(f)),
        "built_unverified": sum(1 for f in feats if f.get("status") == "built" or
                                (f.get("status") == "verified" and not fresh(f))),
        "broken": sum(1 for f in feats if f.get("status") == "broken"),
        "missing": sum(1 for f in feats if f.get("status", "missing") == "missing"),
        "fidelity_behaviour": round(sum(w(f) for f in feats if fresh(f)) / total, 3),
        "breadth": round(sum(w(f) for f in feats if f.get("status") in ("built", "verified")) / total, 3),
        "soul": round(sum(w(f) for f in soul if fresh(f)) / soul_total, 3) if soul else None,
        "visual": round(sum(vis) / len(vis), 3) if vis else None,
        "visual_pairs": len(vis),
        "shape": (round(sum(x["score"] for x in parity["shape"] if isinstance(x.get("score"), (int, float))) /
                        max(1, sum(1 for x in parity["shape"] if isinstance(x.get("score"), (int, float)))), 3)
                  if any(isinstance(x.get("score"), (int, float)) for x in parity.get("shape", [])) else None),
        "last_run": last,
        "tiers": {t: sum(1 for f in raw if f.get("tier", "playable") == t) for t in ("playable", "exhibit", "catalogue")}
        if any(f.get("tier") for f in raw) else None,
        "manual_checks": len(parity.get("manual", {})),
        **(honesty(ws, parity) if ws else {}),
    }


def run_checks(ws: Path, parity: dict) -> None:
    """Non-browser checks, e.g. a "label" journey passes when label-match.json reaches `min`."""
    checks = {}
    lm = ws / "codex" / "label-match.json"
    journeys = load(ws / "codex" / "journeys.json", [])
    for j in journeys:
        if j.get("kind") == "label":
            got = json.loads(lm.read_text(encoding="utf-8")).get("label_match") if lm.exists() else None
            checks[j["id"]] = {"feature": j.get("feature"), "passed": got is not None and got >= j.get("min", 0.9),
                               "value": got, "at": now(), "by": "pantry.py match"}
    if checks or parity.get("checks"):
        parity["checks"] = checks
        settle(parity, now(), journeys)


def cmd_score(a):
    ws = ws_path(a.ws)
    parity = load(ws / "codex" / "parity.json", {"features": [], "visual": []})
    run_checks(ws, parity)
    atomic_write(ws / "codex" / "parity.json", json.dumps(parity, indent=2))
    s = compute(parity, ws)
    pct = lambda x: "n/a" if x is None else (f"{x * 100:.1f}%" if 0.995 <= x < 1 else f"{round(x * 100)}%")  # noqa: E731
    is_recipe = (ws / "app" / "recipe.json").exists() or s.get("label_match") is not None
    tasting = is_recipe and s.get("manual_pending")
    if tasting:  # a recipe's soul is taste, pending until someone cooks it
        soul_txt = f"awaiting tasting ({s['manual_pending']} taste checks to do)"
    lines = [
        "# Scoreboard", "",
        f"_Generated {now()} from verification run `{s['last_run'] or 'none'}`._", "",
        "| Measure | Value | Meaning |", "|---|---|---|",
        f"| **Fidelity (behaviour)** | **{pct(s['fidelity_behaviour'])}"
        f"{' (%d manual checks pending)' % s['manual_pending'] if s.get('manual_pending') else ''}** | "
        "weighted share of features whose journeys passed (latest run plus recorded manual checks) |",
        (f"| Soul | {soul_txt} | the most-missed qualities (taste, texture) can only be verified by cooking it; "
         "the label match below is the check that runs without a kitchen |" if tasting else
         f"| Soul | {pct(s['soul'])} | same, for the most-missed features only |" if s.get("sentiment_items", 1)
         else f"| Core (soul) | {pct(s['soul'])} | same, for the features marked soul; no source said what people "
              "missed, so these are the core rules, not a measured 'most missed' |"),
        f"| Breadth | {pct(s['breadth'])} | features built (verified or awaiting verification), excluding broken |",
        *([f"| Tiers | {s['tiers']['playable']} playable · {s['tiers']['exhibit']} exhibit · {s['tiers']['catalogue']} "
           "catalogue | exhibit and catalogue parts are shown, not rebuilt, and count as not built above |"]
          if s.get("tiers") else []),
        *([f"| Original (edited) | {s['edited_files']} files | original files changed only for privacy (names, "
           "e-mails removed) or link rewriting: counted in the original share |"] if s.get("edited_files") else []),
        f"| Visual | {pct(s['visual'])} | mean then/now similarity over {s['visual_pairs']} screen pairs (thumbnails: "
        "blind to detail) |",
        *[r for r in [_look_row(ws, parity)] if r],
        *_coverage_rows(ws, parity),
    ]
    if s.get("shape") is not None:
        kinds = sorted({x.get("kind", "archived-response") for x in parity.get("shape", []) if "score" in x})
        lines.append(f"| Shape match | {pct(s['shape'])} | rebuild responses vs. references ({', '.join(kinds)}): "
                     "element/key structure, values ignored (verify.py shape) |")
    if s.get("label_match") is not None:
        src = s.get("label_source") or "source not stated in recipe.json target.source"
        extra = (f"; fit decided {round(s['fitted_share'] * 100)}% of the product" if s.get("fitted_share") is not None else "")
        extra += ("; UNDERDETERMINED: many recipes fit these numbers" if s.get("label_underdetermined") else "")
        extra += (f"; checked against {s['panel_numbers']} panel numbers" if s.get("panel_numbers") else "")
        lines.append(f"| Label match | {pct(s['label_match'])} | recipe nutrition vs. the target numbers ({src}{extra}) |")
    lines += [
        *(["| Original share | n/a | a recipe has no original files: what comes from the original is its label "
            "(ingredient order, nutrition), measured by the label match |"] if is_recipe else []),
        *([f"| Original share | {pct(s['original_share'])} | of the shipped app by size, literally original files "
            f"({s['original_files']} of {s['shipped_files']} files; app/**/MANIFEST.json"
            + (f"; {s['missing_originals']} referenced originals were never archived" if s.get('missing_originals') else "")
            + ") |"] if s.get("original_share") is not None and not is_recipe else []),
        *([f"| By role | " + "; ".join(f"{k}: {v['original']} original + {v['recovered']} recovered of {v['files']}"
                                       for k, v in s["by_role"].items()) + " | which parts came from the original "
            "(by file count; the byte share above is dominated by big files such as fonts and music) |"]
          if s.get("by_role") and not is_recipe else []),
        *([f"| Recovered | {s['recovered_files']} files | the original's own art and sound recovered from footage, GIFs "
           "or rips (MANIFEST origin 'recovered'), rather than redrawn |"] if s.get("recovered_files") else []),
        f"| Scope | {pct(s.get('scope'))} | evidence-backed features ({s.get('evidence_features', 0)}: tags missed by 1+ "
        f"source, documented by 2+, or seen in a primary source) that the scores above cover |",
        f"| Grounding | {pct(s.get('grounding'))} | Codex claims resting on evidence rather than [DESIGN] choices |",
        *([f"| Verified, resting on | " + ", ".join(f"{n} {k}" for k, n in s["verified_rest_on"].items()) +
           " | the best evidence behind each verified feature (artifact = its own file or page, recording = footage, "
           "firsthand/secondhand = people, inference = reasoning only). A passing test never upgrades it. |"]
          if s.get("verified_rest_on") else []),
        *([f"| Verbatim text | {s['copy_verbatim']} of {s['copy_verbatim'] + s['copy_reconstructed']} copy-deck lines | "
           "the rebuild's wording that is the original's own (cited), against wording written for it ([DESIGN]); for "
           "text games and sites this is the fidelity that matters most |"] if s.get("copy_verbatim") is not None else []),
        *([f"| Failure cases | {s['soul_with_failure_cases']} of {s['soul_features']} soul features | have a journey for a "
           "wrong input or an edge (game over, an error, a limit), not only the happy path |"]
          if s.get("soul_features") else []),
        "", "Counted tags: " + (", ".join(s.get("counted_tags", [])[:30]) or "none") + ".",
        *(["", "Not built: " + ", ".join(s["not_built"]) + "."] if s.get("not_built") else []),
        *(["", f"{len(s['untagged_feature_items'])} feature/behaviour items have no `feature` tag, so Scope cannot see "
               f"them: {', '.join(s['untagged_feature_items'][:12])}."] if s.get("untagged_feature_items") else []),
        *(["", f"{s['via_hooks']} of {s['auto_journeys']} automatic journeys drive the app through test hooks "
               "rather than real input (`\"via\": \"hooks\"`)."] if s.get("via_hooks") else []),
        "", "**Read the scores with the scope.** Fidelity only measures the features listed below." +
        (f" Left out: {', '.join(s['left_out'][:12])}." if s.get("left_out") else ""),
        "", f"Verified {s['verified']} · built but unverified {s['built_unverified']} · "
            f"broken {s['broken']} · missing {s['missing']} · total {s['features']}", "",
        "| Feature | Priority | Status | Evidence |", "|---|---|---|---|",
    ]
    for f in parity.get("features", []):
        st = f.get("status", "missing")
        if st == "verified" and f.get("verified_run") not in (s["last_run"], "manual", "check"):
            st = "verified (stale)"
        elif st == "built" and f.get("awaiting"):
            st = f"built (awaiting {f['awaiting']})"
        lines.append(f"| {f.get('name', f['id'])} | {f.get('priority', 'core')} | {st} | "
                     f"{', '.join(f.get('evidence', []))} |")
    md = "\n".join(lines) + "\n"
    (ws / "codex" / "SCOREBOARD.md").write_text(md, encoding="utf-8")
    print(json.dumps(s, indent=2))
    print(f"wrote {ws / 'codex' / 'SCOREBOARD.md'}", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ws")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run")
    s.add_argument("--base", help="where the rebuild is served, e.g. http://localhost:5173")
    s.add_argument("--serve", help="workspace folder to serve for the run (e.g. app); started and stopped for you")
    s.add_argument("--journeys")
    s.add_argument("--allow-console", help="regex of console errors to ignore in every journey (e.g. 'favicon|analytics')")
    s.add_argument("--width", type=int, help="viewport (default: state.json viewport from `ledger.py init --viewport`, else 1024)")
    s.add_argument("--height", type=int, help="default: state.json viewport, else 768")
    s.add_argument("--timeout", type=int, default=8, help="seconds per step")
    s.add_argument("--fake-media", action="store_true",
                   help="give pages a synthetic camera and microphone (getUserMedia works in tests)")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("manual", help="list manual checks, or record the user's results")
    s.add_argument("--record", nargs="+", metavar="J=pass|fail")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_manual)
    sub.add_parser("lint", help="check journeys.json before running").set_defaults(fn=cmd_lint)
    s = sub.add_parser("feature", help="set a feature's status (built/missing) or add tags, without hand-editing JSON")
    s.add_argument("id")
    s.add_argument("--status", choices=["built", "missing"], help="verified/broken come only from runs")
    s.add_argument("--tag", action="append")
    s.set_defaults(fn=cmd_feature)
    s = sub.add_parser("shape", help="APIs: structural match of rebuild responses vs archived ones")
    s.add_argument("--base")
    s.add_argument("--serve", help="folder to serve for the check (e.g. app)")
    s.set_defaults(fn=cmd_shape)
    sub.add_parser("visual").set_defaults(fn=cmd_visual)
    sub.add_parser("score").set_defaults(fn=cmd_score)
    for sp in {id(v): v for v in sub.choices.values()}.values():
        sp.add_argument("--ws", default=argparse.SUPPRESS, help="workspace folder")
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    from _common import use_heavy
    use_heavy("PIL", "playwright")
    main()
