#!/usr/bin/env python3
"""Measure a rebuild against footage of the original. When the original program is gone, this is the
closest thing to byte-matching. Drive the rebuild with the inputs the footage shows, record it, line the
two recordings up in time, then score how alike they look, when things happen and how things move.

Subcommands
  record URL --steps steps.json --seconds N --out rebuild.webm [--width W --height H]
  record [PAGE] --serve app --steps steps.json --seconds N --out rebuild.webm
        Play the rebuild in a real browser with timed inputs and record it as a video. --serve DIR serves
        that folder (relative to here or to the workspace) on a free local port for the take, then stops.
        PAGE is then a path inside it (default: its index).
        steps.json: [{"t": 0.5, "do": "click", "x": 160, "y": 300}, {"t": 2.0, "do": "down"},
                     {"t": 2.4, "do": "up"}, {"t": 3.0, "do": "key", "key": "Space"},
                     {"t": 4.0, "do": "move", "x": 200, "y": 120}]
  screens ORIGINAL REBUILD [--orig-start S --orig-end E]
        Every distinct screen of the original (title, menus, play, game over...) beside the closest screen
        of the rebuild, with a match score for each. It doesn't depend on how the play went. Written to
        dossier/footdiff/, which is private. The original's frames can show real people (names, faces), so
        redact them (`shots.py redact`) before copying any to the museum.
  compare ORIGINAL REBUILD [--orig-start S --orig-end E] [--orig-region x,y,w,h] [--rebuild-region ...]
        Align the two in time (offset and playback speed), then report visual match over time, the
        timing of screen changes in both, optional motion of one object (--track #hex), a contact sheet of
        matched moments and a side-by-side video. ORIGINAL and REBUILD are video files or folders of
        frames. The original can be a `footage.py get` copy.

Needs ffmpeg and Pillow; `record` needs Playwright.

The scores check the rebuild. They are not evidence about the original. Note them in codex/SCOREBOARD.md
or show them in the museum (exhibit.json "videos" for side-by-side.mp4). If you add them to the ledger,
use kind "verify" so they never count as traces of the original.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import atomic_write, enter_ws, log, locate_ws, pop_ws_arg  # noqa: E402

FPS = 10  # 10 fps is plenty for UI and game timing


def _secs(t):
    if t is None or t == "":
        return None
    v = 0.0
    for x in str(t).split(":"):
        v = v * 60 + float(x)
    return v


# ---------- record

def cmd_record(a):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright not installed: pip install playwright && playwright install chromium")
    steps = json.loads(Path(a.steps).read_text(encoding="utf-8")) if a.steps else []
    steps.sort(key=lambda s: s.get("t", 0))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    server = None
    if a.serve:  # serve it ourselves, nothing to start or kill by hand
        from serve import start
        ws = locate_ws()
        root = Path(a.serve) if Path(a.serve).is_dir() else (ws / a.serve if ws else Path(a.serve))
        if not root.is_dir():
            sys.exit(f"--serve: no folder {a.serve} (relative to here or to the workspace {ws})")
        if a.url and a.url.startswith(("http://", "https://")):
            sys.exit("give either a URL or --serve DIR (with an optional page path inside it), not both")
        page = a.url or ""
        if page and Path(page).is_absolute() and Path(page).resolve().is_relative_to(root.resolve()):
            page = Path(page).resolve().relative_to(root.resolve()).as_posix()  # the shell resolved a file in DIR
        server, base = start(root.resolve())
        a.url = base + "/" + page.lstrip("/")
    elif not a.url:
        sys.exit("give the rebuild's URL, or --serve app to have footdiff serve the folder itself")
    log(f"recording {a.url}")
    try:
        _record(a, steps, out, sync_playwright)
    finally:
        if server:
            server.shutdown()
            server.server_close()
    print(json.dumps({"out": str(out), "url": a.url, "seconds": a.seconds, "inputs": len(steps)}))


def _record(a, steps, out, sync_playwright):
    with tempfile.TemporaryDirectory() as td, sync_playwright() as p:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": a.width, "height": a.height},
                                  record_video_dir=td, record_video_size={"width": a.width, "height": a.height})
        page = ctx.new_page()
        page.goto(a.url, wait_until="load")
        page.wait_for_timeout(int(a.settle * 1000))
        import time
        t0 = time.time()
        for st in steps:  # t counts from the start of the take
            wait = st.get("t", 0) - (time.time() - t0)
            if wait > 0:
                page.wait_for_timeout(int(wait * 1000))
            do = st.get("do")
            if do == "click":
                page.mouse.click(st["x"], st["y"])
            elif do == "move":
                page.mouse.move(st["x"], st["y"], steps=int(st.get("steps", 4)))
            elif do == "down":
                if "x" in st:
                    page.mouse.move(st["x"], st["y"])
                page.mouse.down()
            elif do == "up":
                page.mouse.up()
            elif do == "key":
                page.keyboard.press(st["key"])
            elif do == "key_down":
                page.keyboard.down(st["key"])
            elif do == "key_up":
                page.keyboard.up(st["key"])
            elif do == "type":
                page.keyboard.type(st.get("text", ""), delay=int(st.get("ms", 60)))
        rest = a.seconds - (time.time() - t0)
        if rest > 0:
            page.wait_for_timeout(int(rest * 1000))
        video = page.video.path() if page.video else None
        ctx.close()
        browser.close()
        if not video or not Path(video).exists():
            sys.exit("no video was recorded")
        # Playwright's video includes load and settle time, trim it
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{a.settle + 0.2:.2f}", "-i", str(video),
                        "-t", f"{a.seconds:.2f}", "-an", str(out)], check=True)


# ---------- frames and features

def _frames(src: str, start, end, region, size, td: Path, tag: str) -> list:
    """Frames at FPS as (t, small RGB image), cropped to the region and scaled to one size."""
    from PIL import Image
    p = Path(src)
    outdir = td / tag
    outdir.mkdir()
    if p.is_dir():
        files = sorted(f for f in p.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg"))
    else:
        seek = (["-ss", str(start)] if start is not None else []) + (["-to", str(end)] if end is not None else [])
        vf = f"fps={FPS}" + (f",crop={region[2]}:{region[3]}:{region[0]}:{region[1]}" if region else "")
        r = subprocess.run(["ffmpeg", "-loglevel", "error", *seek, "-i", str(p), "-vf", vf, "-q:v", "3",
                            str(outdir / "f%06d.jpg")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode:
            sys.exit(f"ffmpeg failed on {src}: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        files = sorted(outdir.glob("f*.jpg"))
        region = None
    out = []
    for i, f in enumerate(files):
        im = Image.open(f).convert("RGB")
        if region and p.is_dir():
            im = im.crop((region[0], region[1], region[0] + region[2], region[1] + region[3]))
        out.append((i / FPS, im.resize(size), f))
    if not out:
        sys.exit(f"no frames from {src}")
    return out


def _feat(im):
    """Grey thumbnail plus coarse colour, enough to tell screens apart."""
    g = im.convert("L").resize((32, 24)).tobytes()
    c = im.resize((8, 6)).tobytes()
    return g, c


def _sim(fa, fb) -> float:
    ga, ca = fa
    gb, cb = fb
    dg = sum(abs(x - y) for x, y in zip(ga, gb)) / len(ga)
    dc = sum(abs(x - y) for x, y in zip(ca, cb)) / len(ca)
    return max(0.0, 1 - (0.7 * dg + 0.3 * dc) / 128)


def _events(feats, thr):
    """Times where the screen changes a lot between frames."""
    ev, last = [], -99
    for i in range(1, len(feats)):
        change = 1 - _sim(feats[i - 1], feats[i])
        if change > thr and i - last > 3:
            ev.append(i / FPS)
            last = i
    return ev


def _align(fo, fr, max_offset, speeds):
    """Best (speed, offset) where rebuild time = speed * original time + offset."""
    best = (-1.0, 1.0, 0.0, 0)
    n_o, n_r = len(fo), len(fr)
    for sp in speeds:
        for off_i in range(-int(max_offset * FPS), int(max_offset * FPS) + 1):
            tot, n = 0.0, 0
            for i in range(0, n_o, 2):
                j = int(round(i * sp)) + off_i
                if 0 <= j < n_r:
                    tot += _sim(fo[i], fr[j])
                    n += 1
            if n >= max(10, n_o // 4) and tot / n > best[0]:
                best = (tot / n, sp, off_i / FPS, n)
    return best


def _track(frames, hexcol, tol=40):
    tgt = tuple(int(hexcol.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    pos = []
    for t, im, _ in frames:
        px, (w, h) = im.load(), im.size
        xs, ys = [], []
        for y in range(0, h, 2):
            for x in range(0, w, 2):
                c = px[x, y]
                if all(abs(c[k] - tgt[k]) <= tol for k in range(3)):
                    xs.append(x)
                    ys.append(y)
        pos.append((sum(xs) / len(xs) / w, sum(ys) / len(ys) / h) if len(xs) >= 3 else None)
    return pos


def cmd_compare(a):
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not installed")
    from PIL import Image, ImageDraw
    reg = lambda s: [int(v) for v in s.split(",")] if s else None  # noqa: E731
    size = tuple(int(v) for v in a.size.split("x"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tdn:
        td = Path(tdn)
        fo = _frames(a.original, _secs(a.orig_start), _secs(a.orig_end), reg(a.orig_region), size, td, "o")
        fr = _frames(a.rebuild, _secs(a.rebuild_start), _secs(a.rebuild_end), reg(a.rebuild_region), size, td, "r")
        feo, fer = [_feat(im) for _, im, _ in fo], [_feat(im) for _, im, _ in fr]
        speeds = [1.0] if a.fixed_speed else [round(0.8 + 0.05 * k, 2) for k in range(9)]
        score, sp, off, n = _align(feo, fer, a.max_offset, speeds)
        if not a.fixed_speed:  # refine around the best speed
            score, sp, off, n = max(_align(feo, fer, a.max_offset, [round(sp + 0.01 * k, 2) for k in range(-4, 5)]),
                                    (score, sp, off, n))
        # per-moment similarity over the aligned overlap
        per = []
        for i in range(len(fo)):
            j = int(round(i * sp + off * FPS))
            if 0 <= j < len(fr):
                per.append((fo[i][0], _sim(feo[i], fer[j]), i, j))
        sims = [s for _, s, _, _ in per]
        # match each original screen change to the nearest rebuild one
        eo, er = _events(feo, a.event), _events(fer, a.event)
        timing = []
        for t in eo:
            expect = t * sp + off
            near = min(er, key=lambda x: abs(x - expect)) if er else None
            timing.append({"original_s": round(t, 2), "rebuild_s": round(near, 2) if near is not None else None,
                           "error_s": round(near - expect, 2) if near is not None else None})
        matched = [x for x in timing if x["error_s"] is not None and abs(x["error_s"]) <= a.event_window]
        motion = None
        if a.track:
            po, pr = _track(fo, a.track), _track(fr, a.track_rebuild or a.track)
            errs = []
            for _, _, i, j in per:
                if po[i] and pr[j]:
                    errs.append(math.hypot(po[i][0] - pr[j][0], po[i][1] - pr[j][1]))
            if errs:
                motion = {"frames_compared": len(errs), "mean_error_pct_of_frame": round(100 * sum(errs) / len(errs), 1),
                          "worst_pct": round(100 * max(errs), 1)}
        # contact sheet of matched moments
        picks = per[:: max(1, len(per) // a.pairs)][: a.pairs]
        W, H = size
        sheet = Image.new("RGB", (W * 2 + 30, (H + 22) * len(picks) + 10), (20, 20, 20))
        d = ImageDraw.Draw(sheet)
        for k, (t, s, i, j) in enumerate(picks):
            y = 10 + k * (H + 22)
            sheet.paste(fo[i][1], (10, y + 16))
            sheet.paste(fr[j][1], (W + 20, y + 16))
            d.text((10, y), f"original {t:5.1f}s", fill=(230, 230, 230))
            d.text((W + 20, y), f"rebuild {fr[j][0]:5.1f}s   match {s:.2f}", fill=(255, 210, 0))
        sheet.save(out / "pairs.png")
        seq = td / "sbs"
        seq.mkdir()
        for k, (t, s, i, j) in enumerate(per):
            frame = Image.new("RGB", (W * 2 + 10, H), (0, 0, 0))
            frame.paste(fo[i][1], (0, 0))
            frame.paste(fr[j][1], (W + 10, 0))
            frame.save(seq / f"s{k:06d}.jpg", quality=88)
        if per:
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", str(FPS), "-i", str(seq / "s%06d.jpg"),
                            "-pix_fmt", "yuv420p", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", str(out / "side-by-side.mp4")],
                           check=False)
    uncertain = (sum(sims) / len(sims) if sims else 0) < 0.85 and len(matched) < 2
    report = {
        "alignment": {"speed": sp, "offset_s": round(off, 2), "overlap_frames": n,
                      **({"uncertain": "weak match and few shared screen changes: the two runs probably show different "
                                       "play, so speed and per-moment scores mean little. Use `screens`, or drive the "
                                       "rebuild into the same state (test hooks) for a deterministic stretch."}
                         if uncertain else {}),
                      # speed < 1 means things happen sooner in the rebuild
                      "note": ("the rebuild runs " + (f"{abs(1 / sp - 1) * 100:.0f}% {'faster' if sp < 1 else 'slower'} than "
                                                       "the original" if abs(sp - 1) > 0.005 else "at the original's speed"))},
        "visual_match": {"mean": round(sum(sims) / len(sims), 3) if sims else None,
                         "worst": round(min(sims), 3) if sims else None,
                         "share_above_0_8": round(sum(1 for s in sims if s >= 0.8) / len(sims), 3) if sims else None},
        "events": {"original": len(eo), "rebuild": len(er), "matched_within_s": a.event_window,
                   "matched": len(matched),
                   "mean_abs_error_s": round(sum(abs(x["error_s"]) for x in matched) / len(matched), 2) if matched else None,
                   "detail": timing[:40]},
        **({"motion": motion} if motion else {}),
        "worst_moments": [{"original_s": round(t, 1), "match": round(s, 2)} for t, s, _, _ in sorted(per, key=lambda x: x[1])[:5]],
        "files": {"pairs": str(out / "pairs.png"), "side_by_side": str(out / "side-by-side.mp4")},
    }
    atomic_write(out / "footdiff.json", json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    log(f"open {out}/pairs.png with Read; the worst moments are where to look first. These scores are verification "
        "results, not evidence about the original: note them in codex/SCOREBOARD.md, and show side-by-side.mp4 in "
        f'the museum (exhibit.json "videos": [{{"file": "{out}/side-by-side.mp4", "caption": "..."}}]). If you add '
        'them to the ledger, use kind "verify" so they are not counted as traces.')


def _screens(frames, still=0.03, same=0.08):
    """Frames that hold still for half a second, deduplicated."""
    feats = [_feat(im) for _, im, _ in frames]
    picked = []
    for i in range(len(frames) - 5):
        if all(1 - _sim(feats[i], feats[i + k]) < still for k in (1, 3, 5)):
            if not any(1 - _sim(feats[i], feats[j]) < same for j, _ in picked):
                picked.append((i, feats[i]))
    return picked


def cmd_screens(a):
    """Match each original screen to the closest rebuild screen, whatever the play was."""
    from PIL import Image, ImageDraw
    reg = lambda s_: [int(v) for v in s_.split(",")] if s_ else None  # noqa: E731
    size = tuple(int(v) for v in a.size.split("x"))
    out = Path(a.out)
    if "museum" in out.parts:  # unredacted frames show names and faces, keep them private
        i = out.parts.index("museum")
        rest = [p for p in out.parts[i + 1:] if p != "footdiff"]
        out = Path(*out.parts[:i], "dossier", "footdiff", *rest)
        log(f"note: screens writes the original's frames unredacted, so not into museum/: writing to {out} instead")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tdn:
        td = Path(tdn)
        fo = _frames(a.original, _secs(a.orig_start), _secs(a.orig_end), reg(a.orig_region), size, td, "o")
        fr = _frames(a.rebuild, _secs(a.rebuild_start), _secs(a.rebuild_end), reg(a.rebuild_region), size, td, "r")
        so, sr = _screens(fo), _screens(fr)
        if not so:
            sys.exit("no settled screens in the original stretch: pick a stretch with menus or pauses")
        rows = []
        for i, f in so:
            best = max(((j, _sim(f, g)) for j, g in sr), key=lambda x: x[1], default=(None, 0.0))
            rows.append({"original_s": round(fo[i][0] + (_secs(a.orig_start) or 0), 2), "rebuild_s": round(fr[best[0]][0], 2)
                         if best[0] is not None else None, "match": round(best[1], 3), "i": i, "j": best[0]})
        W, H = size
        sheet = Image.new("RGB", (W * 2 + 30, (H + 22) * len(rows) + 10), (20, 20, 20))
        d = ImageDraw.Draw(sheet)
        for k, r in enumerate(rows):
            y = 10 + k * (H + 22)
            sheet.paste(fo[r["i"]][1], (10, y + 16))
            d.text((10, y), f"original screen @ {r['original_s']}s", fill=(230, 230, 230))
            if r["j"] is not None:
                sheet.paste(fr[r["j"]][1], (W + 20, y + 16))
            d.text((W + 20, y), f"closest rebuild screen  match {r['match']:.2f}", fill=(255, 210, 0))
        sheet.save(out / "screens.png")
    good = [r for r in rows if r["match"] >= a.good]
    report = {"original_screens": len(rows), "rebuild_screens": len(sr), "matched_at_least": a.good,
              "matched": len(good), "mean_best_match": round(sum(r["match"] for r in rows) / len(rows), 3),
              "screens": [{k: r[k] for k in ("original_s", "rebuild_s", "match")} for r in rows],
              "unmatched_original_screens": [r["original_s"] for r in rows if r["match"] < a.good],
              "sheet": str(out / "screens.png")}
    atomic_write(out / "screens.json", json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    log("open screens.png: every original screen beside its closest rebuild screen. Unmatched ones are missing "
        "screens or screens that look different; fix those first. The scores are verification results: note the "
        'numbers in codex/SCOREBOARD.md or the museum, not as evidence (in the ledger, only as kind "verify").')
    log(f"PRIVATE: {out}/screens.png holds unredacted frames of the original footage (real people's names, faces, "
        "other people's content). Check every frame and redact it (`shots.py redact`) before copying any of it "
        "to the museum; never copy this sheet there as it is.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("record", help="play the rebuild with timed inputs and record a video")
    s.add_argument("url", nargs="?", help="the rebuild's URL; with --serve, a page path inside the served folder")
    s.add_argument("--serve", metavar="DIR", help="serve this folder (e.g. app) on a free local port for the take")
    s.add_argument("--steps", help="JSON list of timed inputs")
    s.add_argument("--seconds", type=float, required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--width", type=int, default=800)
    s.add_argument("--height", type=int, default=600)
    s.add_argument("--settle", type=float, default=1.0, help="seconds after load before the take starts")
    s.set_defaults(fn=cmd_record)
    s = sub.add_parser("screens", help="match every screen the original shows to the closest rebuild screen")
    s.add_argument("original")
    s.add_argument("rebuild")
    s.add_argument("--orig-start")
    s.add_argument("--orig-end")
    s.add_argument("--rebuild-start")
    s.add_argument("--rebuild-end")
    s.add_argument("--orig-region")
    s.add_argument("--rebuild-region")
    s.add_argument("--size", default="320x240")
    s.add_argument("--good", type=float, default=0.85, help="match that counts as the same screen")
    s.add_argument("--out", default="dossier/footdiff",
                   help="private working folder (never museum/: the original's frames are unredacted)")
    s.set_defaults(fn=cmd_screens)
    s = sub.add_parser("compare", help="align and score a rebuild recording against original footage")
    s.add_argument("original", help="original footage (video file or frames folder)")
    s.add_argument("rebuild", help="rebuild recording (video file or frames folder)")
    s.add_argument("--orig-start")
    s.add_argument("--orig-end")
    s.add_argument("--rebuild-start")
    s.add_argument("--rebuild-end")
    s.add_argument("--orig-region", help="x,y,w,h of the play area in the original video")
    s.add_argument("--rebuild-region", help="x,y,w,h of the play area in the rebuild video")
    s.add_argument("--size", default="320x240", help="common size both are compared at")
    s.add_argument("--max-offset", type=float, default=3.0, help="seconds of misalignment to search")
    s.add_argument("--fixed-speed", action="store_true", help="do not search playback speed")
    s.add_argument("--event", type=float, default=0.18, help="frame-to-frame change that counts as a screen event")
    s.add_argument("--event-window", type=float, default=0.5, help="seconds within which events count as matched")
    s.add_argument("--track", help="#hex colour of one object to compare paths (in the original)")
    s.add_argument("--track-rebuild", help="#hex of the same object in the rebuild, if it differs")
    s.add_argument("--pairs", type=int, default=8)
    s.add_argument("--out", default="museum/footdiff")
    s.set_defaults(fn=cmd_compare)
    ws_arg = pop_ws_arg() if argv is None else None
    a = ap.parse_args(argv)
    enter_ws(ws_arg)
    a.fn(a)


if __name__ == "__main__":
    from _common import use_heavy
    use_heavy("PIL", "playwright")
    main()
