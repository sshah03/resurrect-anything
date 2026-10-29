#!/usr/bin/env python3
"""Recover assets from footage. When the original files are gone, videos and GIFs still hold the
original's screens, sprites, sounds, music and timings, and these commands turn them into files.

Subcommands
  get URL [--size 720] [--force]         Download a video once into dossier/media/ (with its metadata),
                                         so the other commands (and you) work on one local copy. --size is
                                         the short side, portrait phone videos too. --force replaces it.
  scenes SRC [--out DIR] [--region x,y,w,h]   (default DIR: dossier/screens/scenes/<video id>/)
        Every distinct screen in a video with its timestamp (title, menu, level, game over, dialogs),
        one frame each wherever a screen settles (cuts and fades), revisits marked, plus a contact
        sheet. Think of it as the storyboard / screen inventory. --region looks only at the game window
        inside a desktop recording and ignores the facecam, browser chrome and desktop.
  gif SRC --out DIR                      Decode a GIF (or APNG/WebP) into every frame with its own delay.
                                         Those delays are the original animation timing.
  sprite FRAMES... --out DIR [--region x,y,w,h] [--bg auto|#hex|IMG] [--follow x,y]
        Cut moving objects out as transparent PNGs. The background is the per-pixel median of the
        frames (or a key colour / a clean background frame), and whatever differs from it is the sprite.
        Over a burst you get the animation frames, deduplicated, with a strip and a native pixel scale.
  cut FRAMES... --box x,y,w,h --out FILE [--plate IMG [--fill-holes N] [--largest]] [--drop-pointer]
        For static or smooth (vector/Flash) art. Crops the same box from one or more frames, merges the
        takes, and clears the background inward from the crop's edges. Outlines stop the fill, so skin and
        highlights that share the background colour survive. Full resolution, no pixel reduction.
        --plate keys against a clean background instead. Enclosed holes where the object matches the plate
        get filled (under --fill-holes px, default 0.5% of the box) and --largest keeps one piece.
        --drop-pointer removes a desktop mouse pointer (white arrow or hand) caught in the cut.
  hud SRC --region x,y,w,h [--start --end --fps 4] [--measure bar|marker --fill #hex]
        One screen region over time (score, timer, counters, floaters) as a timestamped strip of
        crops, each change kept once, so you can read a game's scoring and timing rules in one look.
        --measure gives a timer/health bar's filled length (or a marker's x) per sample, its rate in px/s
        and refills. Warns when the changes are motion rather than values (a gun or particles crossing
        the box). At most --max-sheets sheets.
  merge FRAMES... --out FILE [--ignore #hex] [--scroll auto --band y,h | --layers auto [--strip 8]] [--fps N]
        Same picture with different parts hidden in each frame (pieces, popups, cursors, a sprite moving
        across). Keeps, per pixel, what most frames show. Gives clean background plates and filled-in
        pictures, plus a mask of what no frame showed and the regions that move between frames (a
        scrolling strip, or an animation the median erases). Scrolling ones come with their speed.
        --scroll auto stitches a side-scrolling background into one panorama. Repeated frames are dropped
        first (a 15 fps game in a 30 fps video), then it finds sub-pixel offsets, speed per game frame and
        per second (frame times from the names or --fps), and the repeating tile if there is one.
        --layers auto is for parallax. Strips are grouped into layers by speed, with one panorama + tile
        per scrolling layer and static rows as one median plate. Scroll modes also save the sharpest
        frame's band as <out>_plain.png to compare against. Every merge reports sharpness_ratio (result vs
        source frames) and warns when the result is blurrier (< 0.7).
  count FRAMES... --region x,y,w,h [--bg auto|IMG] [--min-area N] [--exclude-static]
        How many moving objects are on screen at once (enemies, coins, bullets). Counts foreground blobs
        per frame against the median background, skipping repeats, and gives per-frame counts and the
        distribution so a rebuild doesn't invent crowds or thin them out.
  stage FRAMES... --size 640x480 --out DIR
        Frames from differently framed videos, trimmed of borders and resized to the original stage, so
        they line up for merge, cut and comparison.
  tiles FRAMES... --origin x,y --step dx,dy --cols N --rows M --size w,h [--odd-shift dx/2 --mask circle]
        Puzzle boards (gems, bubbles, stones, letter tiles). Cuts every cell of a lattice, groups them into
        tile types, and merges each type from all its takes into one clean sprite.
  lines SRC --cues ID.cues.json --out DIR [--offset S]
        Narration and voice lines cut out by caption timing (community.py yt --timed), trimmed to the speech.
        SRC can be a vocals stem. --offset (or the stems.json beside it) maps the cue times onto it.
  audio-check URL... | common-audio A B [--window 5]
        Triage recordings by audio before downloading video (silent, effects only, music bed), and test
        whether two independent recordings share music. If they do, it's the game's music and not the
        uploader's. Every few-second window of A is searched for in all of B, so pieces shared by edited
        uploads show up as stretches (time in A, time in B, score).
  sfx SRC --out DIR [--start --end] [--offset S] [--events 12.4:jump,...] [--sheet [--video SRC]] [--minus-loop LOOP.wav]
        Cut sound effects out of gameplay audio. Each event is cut against the local background
        level, events that sound alike are grouped, and the cleanest take of each group is saved as
        WAV. A sound that recurs (every jump, every coin) is an effect, not music. --minus-loop
        subtracts a known music loop (from `loop`) first so effects under the music stand out. If
        `stems` has split this recording, detection runs on its drums stem (--no-stems uses the mix).
        Over a steady bed the threshold drops. --events searches +-0.5 s around each labelled moment.
  stems SRC --out DIR [--start --end]    Split a recording into drums / bass / other / vocals with Demucs
                                         (optional, via enhance.py setup --audio) to get at effects buried
                                         under music. stems.json records --start so `sfx`/`lines` report
                                         source times.
  loop SRC --start --end --out FILE [--stack]
        Find the repeat period of background music in a stretch with few effects (menus, pauses) and
        save exactly one loop. --stack aligns every full cycle and keeps the per-sample median, so
        narration or effects over any one cycle drop out.

SRC is a local file or a URL (downloaded once via `get`). Needs ffmpeg, plus yt-dlp for URLs and Pillow
for images. Everything recovered is evidence, so cite the video and timestamp (`locator` fields) and record
shipped files in app/original/MANIFEST.json with "origin": "recovered".
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import USER_AGENT, atomic_write, emit, enter_ws, jsonl_lines, log, need_pil, pop_ws_arg  # noqa: E402

MEDIA = Path("dossier/media")


def _secs(t):
    if t is None or t == "":
        return None
    v = 0.0
    for x in str(t).split(":"):
        v = v * 60 + float(x)
    return v


def _stamp(t):
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def _region(spec):
    try:
        x, y, w, h = (int(round(float(v))) for v in spec.split(","))
    except ValueError:
        sys.exit(f"region {spec!r} must be x,y,w,h")
    return x, y, w, h


def _even_region(spec):
    """Crop box grown out to even edges for yuv420 video.
    ffmpeg silently rounds odd offsets and sizes down, and the frames come out sheared."""
    x, y, w, h = _region(spec)
    x0, y0 = x - x % 2, y - y % 2
    x1, y1 = x + w + (x + w) % 2, y + h + (y + h) % 2
    return x0, y0, x1 - x0, y1 - y0


def _duration(path):
    """Length of a media file in seconds via ffprobe, or None."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _need(*tools):
    for tool in tools:
        if not shutil.which(tool):
            sys.exit(f"{tool} not installed (yt-dlp: `pip install yt-dlp`; ffmpeg: `brew install ffmpeg`)")


def download(url: str, height: int | None = 720, force: bool = False) -> tuple[Path, dict]:
    """One local copy per video id. height is the short side (720 = 1280x720, or 720x1280 portrait)."""
    _need("yt-dlp")
    height = height or 720
    MEDIA.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["yt-dlp", "--skip-download", "--dump-json", "--no-warnings", "--no-playlist", url],
                       capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")
    try:
        info = json.loads(jsonl_lines(r.stdout)[-1])
    except (IndexError, ValueError):
        sys.exit("could not read the video: " + (r.stderr.strip().splitlines() or ["?"])[-1])
    vid = info.get("id") or hashlib.md5(url.encode()).hexdigest()[:11]
    have = [p for p in MEDIA.glob(f"{vid}.*") if p.suffix not in (".json", ".part") and ".audio." not in p.name]
    meta = {"id": vid, "url": info.get("webpage_url", url), "title": info.get("title"),
            "duration": info.get("duration"), "upload_date": info.get("upload_date"),
            "chapters": [{"t": c.get("start_time"), "title": c.get("title")} for c in info.get("chapters") or []]}
    if have and force:
        for p in have:
            p.unlink()
        have = []
    if not have:
        # res = short side, so portrait videos get the same detail
        base = ["yt-dlp", "--no-warnings", "--no-playlist", "-f", "bv*+ba/b", "-S", f"res:{height}",
                "--merge-output-format", "mp4", "-o", str(MEDIA / "%(id)s.%(ext)s"), "--print", "after_move:%(filepath)s"]
        r = subprocess.run(base + [url], capture_output=True, text=True, timeout=1800,
                           encoding="utf-8", errors="replace")
        path = (jsonl_lines(r.stdout.strip()) or [""])[-1]
        if (not path or not Path(path).exists()) and "403" in r.stderr and shutil.which("node"):
            # YouTube sometimes needs its player JS solved by Node
            r = subprocess.run(base[:1] + ["--js-runtimes", "node"] + base[1:] + [url], capture_output=True, text=True,
                               timeout=1800, encoding="utf-8", errors="replace")
            path = (jsonl_lines(r.stdout.strip()) or [""])[-1]
        if not path or not Path(path).exists():
            sys.exit("download failed: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        have = [Path(path)]
    try:  # which exact file the pieces came from
        meta["sha256"] = hashlib.sha256(have[0].read_bytes()).hexdigest()
    except OSError:
        pass
    try:  # yt-dlp silently falls back if that height is missing
        pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                             "-of", "csv=p=0", str(have[0])], capture_output=True, text=True, timeout=60,
                            encoding="utf-8", errors="replace")
        w_h = pr.stdout.strip().split(",")[:2]
        meta["got"] = f"{w_h[0]}x{w_h[1]}" if len(w_h) == 2 else None
        fmts = info.get("formats", [])
        meta["heights_available"] = sorted({f.get("height") for f in fmts if f.get("height")})
        shorts = sorted({min(f["width"], f["height"]) for f in fmts if f.get("width") and f.get("height")}) \
            or meta["heights_available"]
        meta["short_sides_available"] = shorts
        got_short = min(int(w_h[0]), int(w_h[1])) if meta["got"] else None
        better = [x for x in shorts if got_short and got_short < x <= max(height, 1080)]
        if got_short and got_short < height and better:
            log(f"note: got {meta['got']} although a {max(better)}px short side exists. To replace the local copy: "
                f"`footage.py get {meta['url']} --size {max(better)} --force` (--size is the short side: "
                "720 = 1280x720 landscape or 720x1280 portrait)")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    atomic_write(MEDIA / f"{vid}.json", json.dumps(meta, indent=1))
    return have[0], meta


def resolve(src: str, height: int = 720) -> tuple[Path, dict]:
    if src.startswith(("http://", "https://")):
        return download(src, height)
    p = Path(src)
    if not p.exists():
        sys.exit(f"no such file: {src}")
    side = p.with_suffix(".json")
    meta = json.loads(side.read_text(encoding="utf-8")) if side.exists() and p.parent.name == "media" else {}
    return p, {"id": meta.get("id", p.stem), "url": meta.get("url", str(p)), **meta}


def _loc(meta, t):
    return f"{meta.get('url', meta.get('id'))} @ {_stamp(t)}"


def cmd_get(a):
    path, meta = download(a.url, a.height, getattr(a, "force", False))
    emit([{"file": str(path), **meta}], a.format)


# ---------- scenes ----------

SEEK_NOTE = ("t_seconds is the saved frame's own time in the video (from its decoded timestamp): "
             "`ffmpeg -ss <t_seconds> -i SRC -frames:v 1 x.png` gives that same frame")


def _sample_frames(path, seek, rate, height, region, td: Path):
    """About `rate` real frames a second (first in each 1/rate slot) with their decoded times.
    A listed time seeks back to that exact frame, even in variable frame rate phone videos.
    Returns (files, seconds from the start of the sampled stretch)."""
    vf = (f"select='isnan(prev_selected_t)+gte(floor(t*{rate})-floor(prev_selected_t*{rate}),1)'"
          + (f",scale=-2:{height}" if height else "")  # source resolution by default
          + (",crop={2}:{3}:{0}:{1}".format(*region) if region else "")  # just the game window
          + ",showinfo")
    for mode in (["-fps_mode", "passthrough"], ["-vsync", "0"]):  # older ffmpeg only knows -vsync
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", *seek, "-i", str(path), "-vf", vf,
               *mode, "-q:v", "2", str(td / "f%06d.jpg")]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=1800, encoding="utf-8")
        if "Unrecognized option" not in r.stderr:
            break
    files = sorted(td.glob("f*.jpg"))
    if not files:
        sys.exit("ffmpeg produced no frames" + (" (is --region inside the frame?)" if region else "") + ": "
                 + ([x for x in r.stderr.strip().splitlines() if "showinfo" not in x] or ["?"])[-1])
    times = [float(m.group(1)) for m in re.finditer(r"\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:(-?[\d.]+)", r.stderr)]
    if len(times) != len(files):  # shouldn't happen with a working showinfo
        log(f"note: frame times unreadable ({len(times)} for {len(files)} frames): times are estimates")
        times = [i / rate for i in range(len(files))]
    return files, [math.floor(max(0.0, x) * 100) / 100 for x in times]  # round down so -ss lands on the frame

def _thumbhash(img):
    """Small grey thumbnail used to tell screens apart and spot revisits."""
    return list(img.convert("L").resize((16, 9)).tobytes())


def _dist(h1, h2):
    return sum(abs(x - y) for x, y in zip(h1, h2)) / len(h1)


def cmd_scenes(a):
    """Keep a frame wherever a new screen settles (cuts and fades). Scrolling never does. Marks revisits."""
    need_pil()
    _need("ffmpeg")
    from PIL import Image
    path, meta = resolve(a.src, a.height)
    # per-video folder so scene001s don't collide
    out = Path(a.out or f"dossier/screens/scenes/{re.sub(r'[^A-Za-z0-9._-]+', '_', str(meta.get('id') or path.stem))}")
    out.mkdir(parents=True, exist_ok=True)
    start, end = _secs(a.start), _secs(a.end)
    seek = (["-ss", str(start)] if start is not None else []) + (["-to", str(end)] if end is not None else [])
    region = _even_region(a.region) if a.region else None
    with tempfile.TemporaryDirectory() as td:
        files, times = _sample_frames(path, seek, a.rate, a.height, region, Path(td))
        hashes = [_thumbhash(Image.open(f)) for f in files]
        rows, kept = [], []
        for i, (f, h) in enumerate(zip(files, hashes)):
            t = (start or 0) + times[i]
            settled = all(_dist(h, hashes[j]) < a.still for j in range(i + 1, min(len(files), i + 1 + a.hold)))
            cut = i == 0 or _dist(h, hashes[i - 1]) > a.cut
            flat = max(h) - min(h) < 12 and (sum(h) / len(h) > 235 or sum(h) / len(h) < 20)
            if kept and flat and not settled:
                continue  # white or black flash, a transition
            if not kept:
                pick = True
            elif _dist(h, kept[-1]["hash"]) < a.same:
                pick = False  # still the same screen
            else:
                # one sample per stretch of continuous motion
                moving = t - kept[-1]["t"] >= a.moving and kept[-1].get("kind") != "moving"
                pick = settled or (cut and t - kept[-1]["t"] >= a.min_gap) or moving
            if not pick:
                continue
            kind = ("settled" if settled else "cut" if cut else "moving") if kept else "first"
            revisit = next((k["n"] for k in kept if _dist(h, k["hash"]) < a.same), None)
            n = len(kept) + 1
            name = f"scene{n:03d}@{int(t // 60):02d}m{t % 60:05.2f}s.jpg".replace(".", "_", 1)
            shutil.copy(f, out / name)
            kept.append({"n": n, "t": t, "hash": h, "kind": kind})
            rows.append({"scene": n, "t_seconds": round(t, 2), "frame": str(out / name), "locator": _loc(meta, t),
                         "kind": kind, **({"revisit_of": revisit} if revisit else {})})
    total = end or meta.get("duration") or _duration(path)  # no sidecar, ask ffprobe
    for i, r_ in enumerate(rows):
        nxt = rows[i + 1]["t_seconds"] if i + 1 < len(rows) else (total or r_["t_seconds"])
        r_["until_next_s"] = round(nxt - r_["t_seconds"], 2)
    atomic_write(out / "scenes.json", json.dumps({"source": meta, "region": list(region) if region else None,
                                                  "scenes": rows, "seek_note": SEEK_NOTE}, indent=1))
    if region and list(region) != list(_region(a.region)):
        log(f"region grown to even edges for ffmpeg: {','.join(map(str, region))} (asked {a.region})")
    try:
        import contextlib
        import io
        from shots import cmd_sheet
        frames_ = [r_["frame"] for r_ in rows]
        for k in range(0, len(frames_), 60):  # pages of 60, so nothing is silently left off
            name = "sheet.png" if k == 0 else f"sheet-{k // 60 + 1}.png"
            with contextlib.redirect_stdout(io.StringIO()):
                # absolute, else cmd_sheet prints its workspace note every time
                cmd_sheet(argparse.Namespace(images=frames_[k:k + 60], out=str((out / name).resolve()), cols=a.cols,
                                             width=200, max=60))
    except Exception as e:  # the sheet is just a convenience
        log(f"(no contact sheet: {e})")
    distinct = len([r_ for r_ in rows if "revisit_of" not in r_])
    pages = (len(rows) + 59) // 60
    import _common
    launch = getattr(_common, "LAUNCH_DIR", Path.cwd())  # mention where a relative --out went only if we moved
    where = out if out.is_absolute() or Path.cwd().resolve() == Path(launch).resolve() else out.resolve()
    log(f"{len(rows)} screens ({distinct} distinct) in {where}; " + ("sheet.png shows them all. " if pages == 1 else
        f"sheet.png to sheet-{pages}.png show them all (60 per page). ") + " Name each in the Codex "
        "(title, get ready, play, game over, menu...) and build every one; `revisit_of` gives the flow between them. "
        + ("Many screens: raise --same or lower --rate." if len(rows) > 100 else ""))
    emit(rows, a.format)


# ---------- gif ----------

def cmd_gif(a):
    need_pil()
    from PIL import Image, ImageSequence
    src = a.src
    if src.startswith(("http://", "https://")):
        import urllib.request
        req = urllib.request.Request(src, headers={"User-Agent": USER_AGENT})
        data = urllib.request.urlopen(req, timeout=60).read()
        tmp = Path(tempfile.mkdtemp()) / "in.gif"
        tmp.write_bytes(data)
        src = str(tmp)
    im = Image.open(src)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows, t, seen = [], 0, {}
    for i, fr in enumerate(ImageSequence.Iterator(im)):
        d = fr.info.get("duration", im.info.get("duration", 100)) or 0
        rgba = fr.convert("RGBA")
        h = hashlib.md5(rgba.tobytes()).hexdigest()
        name = f"f{i:04d}.png"
        if h not in seen:
            rgba.save(out / name)
            seen[h] = name
        rows.append({"frame": i, "file": str(out / seen[h]), "delay_ms": d, "t_ms": t,
                     **({"repeat_of": seen[h]} if seen[h] != name else {})})
        t += d
    info = {"source": a.src, "size": list(im.size), "frames": len(rows), "unique": len(seen),
            "total_ms": t, "loop": im.info.get("loop"), "delays_ms": sorted({r["delay_ms"] for r in rows})}
    if any(r["delay_ms"] in (0, 10) for r in rows):
        info["note"] = "0/10 ms delays are played as ~100 ms by most browsers; read them as 'as fast as possible'"
    atomic_write(out / "gif.json", json.dumps({**info, "frames_detail": rows}, indent=1))
    print(json.dumps(info))
    if a.frames:
        emit(rows, a.format)


# ---------- sprite ----------

def _median_bg(imgs):
    from PIL import Image
    datas = [im.tobytes() for im in imgs]
    mid = len(datas) // 2
    return Image.frombytes("RGB", imgs[0].size, bytes(sorted(d[i] for d in datas)[mid] for i in range(len(datas[0]))))


def _components(mask, step):
    """Connected blobs in a 0/255 mask, sampled every `step` px. Returns a list of (pixels, bbox)."""
    w, h = mask.size
    px = mask.load()
    hits = {(x, y) for y in range(0, h, step) for x in range(0, w, step) if px[x, y]}
    seen, blobs = set(), []
    for s in hits:
        if s in seen:
            continue
        stack, pts = [s], []
        seen.add(s)
        while stack:
            cx, cy = stack.pop()
            pts.append((cx, cy))
            for dx in (-step, 0, step):
                for dy in (-step, 0, step):
                    nb = (cx + dx, cy + dy)
                    if nb in hits and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        blobs.append((len(pts), (min(xs), min(ys), min(w, max(xs) + step), min(h, max(ys) + step))))
    return sorted(blobs, reverse=True)


def _pixel_scale(img):
    """Upscale factor of pixel art, from the commonest same-colour run length along rows."""
    from collections import Counter
    w, h = img.size
    px, runs = img.load(), Counter()
    for y in range(0, h, max(1, h // 24)):
        run = 1
        for x in range(1, w):
            if px[x, y][:3] == px[x - 1, y][:3] and px[x, y][3]:
                run += 1
            else:
                if 1 < run < 16:
                    runs[run] += 1
                run = 1
    if not runs:
        return 1
    best = runs.most_common(1)[0][0]
    return best if runs[best] >= 6 else 1


def _poses(cuts, out, scale, tol, colors):
    """Group the cuts of one object into poses, and merge each pose's takes into one clean sprite."""
    from PIL import Image
    def thumb(im):
        bg = Image.new("RGBA", im.size, (255, 0, 255, 255))
        bg.alpha_composite(im)
        return bg.convert("RGB").resize((12, 12)).tobytes()
    groups = []
    for c in cuts:
        im = Image.open(c["file"]).convert("RGBA")
        th = thumb(im)
        for g in groups:
            if abs(im.width - g["w"]) <= 3 * scale and abs(im.height - g["h"]) <= 3 * scale and \
                    sum(abs(p - q) for p, q in zip(th, g["thumb"])) / len(th) < tol:
                g["members"].append((c, im))
                break
        else:
            groups.append({"thumb": th, "w": im.width, "h": im.height, "members": [(c, im)]})
    groups.sort(key=lambda g: -len(g["members"]))
    pd = out / "poses"
    pd.mkdir(exist_ok=True)
    poses = []
    for n, g in enumerate(groups, 1):
        ims = [im for _, im in g["members"]]
        W, H = max(i.width for i in ims), max(i.height for i in ims)
        canv = []
        for im in ims:  # centre each take
            c_ = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            c_.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
            canv.append(c_.tobytes())
        mid = len(canv) // 2
        merged = Image.frombytes("RGBA", (W, H), bytes(sorted(d[i] for d in canv)[mid] for i in range(len(canv[0]))))
        merged.save(pd / f"pose{n:02d}_x{len(ims)}.png")
        if scale > 1:  # nearest sample at cell centres, averaging makes mud
            nw, nh = max(1, round(W / scale)), max(1, round(H / scale))
            src = merged.load()
            native = Image.new("RGBA", (nw, nh))
            for yy in range(nh):
                for xx in range(nw):
                    native.putpixel((xx, yy), src[min(W - 1, int((xx + 0.5) * W / nw)), min(H - 1, int((yy + 0.5) * H / nh))])
        else:
            native = merged
        if colors:  # hard alpha and a small palette for pixel art
            alpha = native.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
            rgb = native.convert("RGB").quantize(colors=colors, method=Image.Quantize.MEDIANCUT).convert("RGB")
            native = rgb.convert("RGBA")
            native.putalpha(alpha)
        native.save(pd / f"pose{n:02d}_native.png")
        poses.append({"pose": n, "takes": len(ims), "size": [native.width, native.height],
                      "merged": str(pd / f"pose{n:02d}_x{len(ims)}.png"), "native": str(pd / f"pose{n:02d}_native.png"),
                      "first_seen": g["members"][0][0]["from"]})
    try:  # pose sheet at 4x
        cell = max(max(Image.open(p_["native"]).size) for p_ in poses) * 4 + 8
        sheet = Image.new("RGBA", (cell * min(8, len(poses)), cell * ((len(poses) + 7) // 8)), (255, 0, 255, 255))
        for i, p_ in enumerate(poses):
            im = Image.open(p_["native"])
            im = im.resize((im.width * 4, im.height * 4), Image.NEAREST)
            sheet.alpha_composite(im, ((i % 8) * cell + 4, (i // 8) * cell + 4))
        sheet.save(pd / "sheet.png")
    except ValueError:
        pass
    return poses


def cmd_sprite(a):
    need_pil()
    from PIL import Image, ImageChops, ImageFilter
    paths = []
    for x in a.images:
        p = Path(x)
        paths += sorted(str(f) for f in p.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")) \
            if p.is_dir() else [x]
    if not paths:
        sys.exit("no frames")
    rx = [int(v) for v in a.region.split(",")] if a.region else None
    imgs = []
    for p in paths:
        im = Image.open(p).convert("RGB")
        if rx:
            im = im.crop((rx[0], rx[1], rx[0] + rx[2], rx[1] + rx[3]))
        imgs.append(im)
    if a.bg == "auto":
        if len(imgs) < 3:
            sys.exit("--bg auto needs 3+ frames where the object moves; or pass --bg #hex / --bg clean_frame.png")
        sample = imgs[:: max(1, len(imgs) // 15)][:15]
        bg = _median_bg(sample)
    elif a.bg.startswith("#"):
        bg = Image.new("RGB", imgs[0].size, tuple(int(a.bg.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)))
    else:
        bg = Image.open(a.bg).convert("RGB")
        if rx:
            bg = bg.crop((rx[0], rx[1], rx[0] + rx[2], rx[1] + rx[3]))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    bg.save(out / "background.png")
    cuts, seen = [], {}
    follow = None
    if a.follow:
        fx, fy = (float(v) for v in a.follow.split(","))
        follow = (fx - (rx[0] if rx else 0), fy - (rx[1] if rx else 0), 0)
    for p, im in zip(paths, imgs):
        diff = ImageChops.difference(im, bg).convert("L")
        mask = diff.point(lambda v: 255 if v > a.tol else 0).filter(ImageFilter.MedianFilter(3))
        blobs = [b for b in _components(mask, a.step) if b[0] * a.step * a.step >= a.min_pixels]
        if follow is not None and blobs:  # track the blob nearest the point, frame to frame
            def centre(b):
                return ((b[1][0] + b[1][2]) / 2, (b[1][1] + b[1][3]) / 2)
            def cost(b):
                (cx, cy), size = centre(b), (b[1][2] - b[1][0]) * (b[1][3] - b[1][1])
                d = math.hypot(cx - follow[0], cy - follow[1])
                return d + (abs(size - follow[2]) ** 0.5 if follow[2] else 0)
            best = min(blobs, key=cost)
            if follow[2] and math.hypot(*(p - q for p, q in zip(centre(best), follow[:2]))) > a.jump:
                continue  # lost it (occluded or merged), skip it
            follow = (*centre(best), (best[1][2] - best[1][0]) * (best[1][3] - best[1][1]))
            blobs = [best]
        for k, (_, box) in enumerate(blobs[: a.max_objects]):
            alpha = mask.crop(box).filter(ImageFilter.MaxFilter(3)) if a.soften else mask.crop(box)
            cut = im.crop(box).convert("RGBA")
            cut.putalpha(alpha)
            h = hashlib.md5(cut.tobytes()).hexdigest()
            if h in seen:
                continue
            name = f"{Path(p).stem}_obj{k + 1}.png"
            cut.save(out / name)
            seen[h] = name
            off = (rx[0], rx[1]) if rx else (0, 0)
            cuts.append({"file": str(out / name), "from": p, "object": k + 1,
                         "x": box[0] + off[0], "y": box[1] + off[1], "w": box[2] - box[0], "h": box[3] - box[1]})
    if not cuts:
        sys.exit("nothing differed from the background: lower --tol, give more frames, or pass --bg")
    main = [c for c in cuts if c["object"] == 1]
    sizes = sorted((c["w"], c["h"]) for c in main)
    scale = _pixel_scale(Image.open(main[len(main) // 2]["file"]))
    if a.native_width:  # known logical width gives the exact factor
        full_w = Image.open(paths[0]).width
        scale = full_w / a.native_width
    elif a.scale:
        scale = a.scale
    if main:  # animation strip, in frame order
        cw, ch = max(c["w"] for c in main), max(c["h"] for c in main)
        strip = Image.new("RGBA", (cw * len(main), ch), (0, 0, 0, 0))
        for i, c in enumerate(main):
            strip.paste(Image.open(c["file"]), (i * cw, ch - c["h"]))
        strip.save(out / "strip.png")
    if a.vector:
        scale = 1  # smooth Flash/vector art stays full-res, full palette
    poses = _poses(main, out, scale, a.pose_tol, a.colors if not a.vector else 0) if main else []
    info = {"frames": len(paths), "cuts": len(cuts), "main_object_cuts": len(main), "poses": len(poses),
            "median_size": list(sizes[len(sizes) // 2]) if sizes else None,
            "out": str(out)}
    info["pixel_scale"] = round(scale, 3)
    if scale > 1 and not (a.native_width or a.scale):
        info["native"] = (f"guessed {scale}x from colour runs. Videos are rarely an exact multiple: pass --native-width "
                          "(the game's logical width, e.g. the SWF stage or 144 for Flappy Bird) for the exact factor")
    info["pose_files"] = [{k: p_[k] for k in ("native", "takes", "size")} for p_ in poses[:12]]
    info["next"] = ("open poses/sheet.png with Read. Each pose is the median of every take of it (compression noise "
                    "cancels out) reduced to native pixels and a small palette. Keep the real animation poses, fix "
                    "stray pixels by hand at native size against the clearest frame, and record each shipped file in "
                    "app/original/MANIFEST.json with origin 'recovered' and the video locator.")
    atomic_write(out / "sprites.json", json.dumps({**info, "cuts": cuts, "poses_detail": poses}, indent=1))
    print(json.dumps(info, indent=1))
    if a.list:
        emit(cuts, a.format)


def _drop_pointer(img, alpha, at=(0, 0)):
    """Clear a desktop mouse pointer and its outline from the cut. Returns (alpha, info).
    Looks for a white pointer-shaped island (arrow about 12x19 px at 1x, or the hand) with a dark rim."""
    from PIL import Image, ImageChops, ImageFilter
    hsv = img.convert("HSV")
    sat, val = hsv.getchannel("S"), hsv.getchannel("V")
    white = ImageChops.darker(val.point(lambda v: 255 if v > 215 else 0), sat.point(lambda v: 255 if v < 40 else 0))
    white = ImageChops.darker(white, alpha.point(lambda v: 255 if v > 127 else 0))
    dark = val.point(lambda v: 255 if v < 100 else 0)
    best = None
    for count, (x0, y0, x1, y1) in _components(white, 1):
        bw, bh = x1 - x0, y1 - y0
        fill = count / (bw * bh)
        if not (8 <= bh <= 64 and 0.45 <= bw / bh <= 0.95 and 0.3 <= fill <= 0.78):
            continue
        box = (max(0, x0 - 3), max(0, y0 - 3), min(img.width, x1 + 3), min(img.height, y1 + 3))
        m = white.crop(box)
        if sum(1 for v in m.tobytes() if v) != count:
            continue  # touches other white, so it's art, not a pointer
        ring = ImageChops.subtract(m.filter(ImageFilter.MaxFilter(3)), m)
        rim = [d for r_, d in zip(ring.tobytes(), dark.crop(box).tobytes()) if r_]
        share = sum(1 for d in rim if d) / max(1, len(rim))
        if share >= 0.7 and (best is None or share > best[0]):
            best = (share, box, m, bh, fill)
    if not best:
        return alpha, None
    share, box, m, bh, _ = best
    grow = 2 * max(1, round(bh / 16)) + 3  # outline too, wider for scaled-up video
    cover = Image.new("L", alpha.size, 0)
    cover.paste(m.filter(ImageFilter.MaxFilter(grow)), box[:2])
    return ImageChops.subtract(alpha, cover), {"kind": "mouse pointer", "dark_rim": round(share, 2),
                                               "box": [box[0] + at[0], box[1] + at[1], box[2] - box[0], box[3] - box[1]]}


def _pieces(data, w, h, holes=False):
    """Connected pieces of a w x h byte mask, by run-length union-find (fast in pure Python).
    Set pixels 8-connected, or with holes=True the unset ones 4-connected.
    Returns [(pixels, touches_edge, runs)] largest first, runs being (y, x0, x1)."""
    pat = re.compile(rb"\x00+" if holes else rb"[^\x00]+")
    d = 0 if holes else 1
    parent, runs, prev = [], [], []

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for y in range(h):
        cur, j = [], 0
        for m in pat.finditer(data, y * w, (y + 1) * w):
            x0, x1 = m.start() - y * w, m.end() - y * w
            rid = len(runs)
            runs.append((y, x0, x1))
            parent.append(rid)
            while j < len(prev) and prev[j][1] + d <= x0:
                j += 1
            k = j
            while k < len(prev) and prev[k][0] < x1 + d:
                ra, rb = find(prev[k][2]), find(rid)
                if ra != rb:
                    parent[ra] = rb
                k += 1
            cur.append((x0, x1, rid))
        prev = cur
    comps = {}
    for rid, (y, x0, x1) in enumerate(runs):
        c = comps.setdefault(find(rid), [0, False, []])
        c[0] += x1 - x0
        c[1] = c[1] or y == 0 or y == h - 1 or x0 == 0 or x1 == w
        c[2].append((y, x0, x1))
    return sorted((tuple(c) for c in comps.values()), key=lambda c: -c[0])


def _paint(buf, w, runs, v):
    for y, x0, x1 in runs:
        buf[y * w + x0:y * w + x1] = bytes([v]) * (x1 - x0)


def _tidy_mask(alpha, smooth=0, largest=False, fill=0):
    """Open/close by `smooth` px, optionally keep the largest piece, fill holes under `fill` px.
    Returns (mask, report)."""
    from PIL import Image, ImageFilter
    rep = {}
    if smooth:
        k = 2 * smooth + 1
        alpha = alpha.filter(ImageFilter.MinFilter(k)).filter(ImageFilter.MaxFilter(k))  # open, drops specks
        alpha = alpha.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k))  # close, shuts cracks
    w, h = alpha.size
    buf = bytearray(alpha.point(lambda v: 255 if v > 127 else 0).tobytes())
    pieces = _pieces(buf, w, h)
    rep["pieces"] = len(pieces)
    if largest and len(pieces) > 1:
        for p in pieces[1:]:
            _paint(buf, w, p[2], 0)
        rep["dropped_pieces_px"] = sum(p[0] for p in pieces[1:])
    holes = [p for p in _pieces(buf, w, h, holes=True) if not p[1]]  # background the edge can't reach
    small = [p for p in holes if p[0] < fill]
    for p in small:
        _paint(buf, w, p[2], 255)
    rep["holes_filled"] = {"count": len(small), "px": sum(p[0] for p in small)}
    left = [p[0] for p in holes if p[0] >= fill]
    if left:
        rep["holes_left"] = len(left)
        rep["holes_left_px"] = left[:5]
    return Image.frombytes("L", (w, h), bytes(buf)), rep


def cmd_cut(a):
    need_pil()
    from PIL import Image, ImageFilter
    x, y, w, h = (int(v) for v in a.box.split(","))
    first = Image.open(a.images[0])
    if w < 3 or h < 3 or x < 0 or y < 0 or x + w > first.width or y + h > first.height:
        sys.exit(f"--box {a.box} must be x,y,width,height inside the {first.width}x{first.height} frame")
    takes = [Image.open(p).convert("RGB").crop((x, y, x + w, y + h)) for p in a.images]
    if len({t.size for t in takes}) > 1:
        sys.exit("frames differ in size: use frames from one video")
    img = _median_bg(takes) if len(takes) >= 3 else takes[0]  # the median cancels out compression noise
    px = img.load()
    fallback = None
    if a.plate:  # object = whatever differs from the clean plate
        from PIL import ImageChops
        pl = Image.open(a.plate).convert("RGB").crop((x, y, x + w, y + h))
        r_, g_, b_ = ImageChops.difference(img, pl).split()  # max channel diff, so hue changes count
        alpha = ImageChops.lighter(ImageChops.lighter(r_, g_), b_).point(lambda v: 255 if v > a.tol else 0)
        if sum(1 for v in alpha.tobytes() if v) >= 0.002 * w * h:
            pointer = None
            if a.drop_pointer:
                alpha, pointer = _drop_pointer(img, alpha, (x, y))
            # parts matching the plate (grey on grey) leave holes
            # fill enclosed ones under --fill-holes px (default 0.5%)
            fill = a.fill_holes if a.fill_holes is not None else max(16, int(0.005 * w * h))
            smooth = a.smooth if a.smooth is not None else max(1, round(min(w, h) / 250))  # wider for big boxes
            alpha, tidy = _tidy_mask(alpha, smooth, a.largest, fill)
            out = img.convert("RGBA")
            out.putalpha(alpha)
            if a.trim and alpha.getbbox():
                out = out.crop(alpha.getbbox())
            if a.width:
                out = out.resize((a.width, round(out.height * a.width / out.width)), Image.LANCZOS)
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            out.save(a.out)
            hint = []
            if tidy.get("holes_left_px"):
                hint.append(f"{tidy['holes_left']} enclosed holes kept (largest {tidy['holes_left_px'][0]} px): "
                            f"real gaps stay; if they are object, --fill-holes {tidy['holes_left_px'][0] + 1}")
            if tidy["pieces"] > 1 and not a.largest:
                hint.append(f"{tidy['pieces']} separate pieces: --largest keeps only the biggest (not for text or "
                            "split icons)")
            print(json.dumps({"out": a.out, "size": list(out.size), "takes": len(takes), "mode": "plate difference",
                              "mask": tidy, **({"pointer_removed": pointer} if pointer else {}),
                              "next": "; ".join(hint) or "open it on a dark and a light background: a halo means "
                              "--tol is too low, missing edges too high"}, indent=1))
            return
        # no difference from the plate, e.g. sprites in sky
        fallback = "the plate equals the frame inside the box (no difference): used the edge flood-fill cut instead"
        log(fallback)
    bgmask = bytearray(w * h)
    stack = [(i, 0) for i in range(w)] + [(i, h - 1) for i in range(w)] + \
            [(0, j) for j in range(h)] + [(w - 1, j) for j in range(h)]
    for pt in a.seed or []:
        try:
            sx, sy = (int(round(float(v))) for v in pt.split(","))
        except ValueError:
            sys.exit(f"--seed {pt!r} must be x,y")
        if not (x <= sx < x + w and y <= sy < y + h):
            sys.exit(f"--seed {pt} lies outside --box {a.box}: seeds are frame coordinates of a background pocket "
                     f"inside the box (x {x}..{x + w - 1}, y {y}..{y + h - 1})")
        stack.append((sx - x, sy - y))
    ref = [px[p] for p in stack]
    lo_ref = (sum(c[0] for c in ref) / len(ref), sum(c[1] for c in ref) / len(ref), sum(c[2] for c in ref) / len(ref))
    for p in stack:
        bgmask[p[1] * w + p[0]] = 1
    while stack:  # spread to neighbours close to the pixel we came from
        cx, cy = stack.pop()
        c = px[cx, cy]
        for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
            if 0 <= nx < w and 0 <= ny < h and not bgmask[ny * w + nx]:
                d = px[nx, ny]
                if sum(abs(d[i] - c[i]) for i in range(3)) <= a.tol and \
                        sum(abs(d[i] - lo_ref[i]) for i in range(3)) <= a.max_drift:
                    bgmask[ny * w + nx] = 1
                    stack.append((nx, ny))
    if a.key:  # flat Flash backgrounds, enclosed pockets included
        kc = tuple(int(a.key.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        for j in range(h):
            for i in range(w):
                if sum(abs(px[i, j][c] - kc[c]) for c in range(3)) <= a.tol:
                    bgmask[j * w + i] = 1
    alpha = Image.frombytes("L", (w, h), bytes(0 if v else 255 for v in bgmask))
    pointer = None
    if a.drop_pointer:  # first, or a pointer on the art joins the largest piece
        alpha, pointer = _drop_pointer(img, alpha, (x, y))
    if a.erode:  # trim the fringe of background-tinted edge pixels
        alpha = alpha.filter(ImageFilter.MinFilter(2 * a.erode + 1))
    if not a.keep_islands:  # drop stray bits of neighbouring art
        blobs = _components(alpha.point(lambda v: 255 if v > 127 else 0), 1)
        if len(blobs) > 1:
            keep = Image.new("L", (w, h), 0)
            bx = blobs[0][1]
            keep.paste(255, (bx[0], bx[1], bx[2], bx[3]))
            ap_, kp = alpha.load(), keep.load()
            # flood the largest blob, its bbox may hold other islands
            seen = bytearray(w * h)
            av = alpha.load()
            start = next(((i, j) for j in range(bx[1], bx[3]) for i in range(bx[0], bx[2]) if av[i, j] > 127), None)
            st = [start] if start else []
            while st:
                cx, cy = st.pop()
                if seen[cy * w + cx]:
                    continue
                seen[cy * w + cx] = 1
                for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and av[nx, ny] > 127:
                        st.append((nx, ny))
            alpha = Image.frombytes("L", (w, h), bytes(av[i % w, i // w] if seen[i] else 0 for i in range(w * h)))
    tidy = None
    if a.fill_holes:  # unreached pockets stay transparent unless asked
        alpha, tidy = _tidy_mask(alpha, 0, False, a.fill_holes)
    if a.feather:
        alpha = alpha.filter(ImageFilter.GaussianBlur(0.7))
    out = img.convert("RGBA")
    out.putalpha(alpha)
    bbox = alpha.getbbox()
    if bbox and a.trim:
        out = out.crop(bbox)
    if a.width:
        out = out.resize((a.width, round(out.height * a.width / out.width)), Image.LANCZOS)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    out.save(a.out)
    kept = sum(1 for v in bgmask if not v) / (w * h)
    res = {"out": a.out, "size": list(out.size), "takes": len(takes), "kept_share": round(kept, 3),
           "box": [x, y, w, h], "sources": list(a.images), "mode": "edge flood fill"}
    if fallback:
        res["fallback"] = fallback
    if tidy:
        res["mask"] = tidy
    if pointer:
        res["pointer_removed"] = {**pointer, "note": "the art under it is now a hole: merge a take without the "
                                                     "pointer, or fill it by hand"}
    elif a.drop_pointer:
        res["pointer_removed"] = None
    if kept > 0.97:
        res["warning"] = "almost nothing was cleared: the background touching the edges differs; lower --tol or add --seed points"
    if kept < 0.05:
        res["warning"] = "almost everything was cleared: the object has no outline here; raise precision with lower --tol"
    res["next"] = "open it with Read on a dark and a light background; holes mean --tol is too high, a halo too low"
    print(json.dumps(res, indent=1))


def _fill_colour(im, share=0.03):
    """Most colourful colour covering `share` of the region (max - min channel, so dark tints don't count)."""
    from collections import Counter
    b = im.tobytes()
    px = list(zip(b[0::3], b[1::3], b[2::3]))
    buckets = Counter((r // 24, g // 24, b // 24) for r, g, b in px)
    common = [k for k, n in buckets.items() if n >= share * len(px)] or [buckets.most_common(1)[0][0]]
    k = max(common, key=lambda k: (max(k) - min(k), buckets[k]))
    members = [p for p in px if (p[0] // 24, p[1] // 24, p[2] // 24) == k]
    return tuple(sorted(m[i] for m in members)[len(members) // 2] for i in range(3))


def _bar_counts(im, fill, tol, axis):
    """Fill-colour pixels across each position along the main axis."""
    w, h = im.size
    px = im.load()
    near = [[sum(abs(px[i, j][c] - fill[c]) for c in range(3)) <= tol for j in range(h)] for i in range(w)]
    return [sum(col) for col in near] if axis == "x" else [sum(near[i][j] for i in range(w)) for j in range(h)]


def _longest_run(on):
    best, run = (0, None), 0
    for i, v in enumerate(on + [False]):
        if v:
            run += 1
        else:
            if run > best[0]:
                best = (run, i - run)
            run = 0
    return best


def _bar_series(samples, axis, x0, kind):
    """Bar length (or marker x) per sample, the jumps, and a linear fit between jumps."""
    top = max((max(c) for _, c in samples if c), default=0)
    if not top:
        sys.exit("the fill colour never appears in the region: pass --fill #hex (or check --region)")
    rows = []
    for t, counts in samples:
        n, at = _longest_run([c >= max(1, top * 0.5) for c in counts])
        if kind == "bar":  # no fill = bar hidden (flash, menu), not empty
            rows.append({"t": round(t, 2), "length_px": n or None})
        else:
            rows.append({"t": round(t, 2), "x_px": round(x0 + at + n / 2, 1) if n else None})
    key = "length_px" if kind == "bar" else "x_px"
    pts = [(r_["t"], r_[key]) for r_ in rows if r_[key] is not None]
    if len(pts) < 2:
        sys.exit("the fill colour shows in fewer than 2 samples: pass --fill #hex, or a longer --start/--end")
    diffs = [b[1] - a_[1] for a_, b in zip(pts, pts[1:])]
    moving = sorted(d for d in diffs if d)
    trend = (moving[len(moving) // 2] > 0) - (moving[len(moving) // 2] < 0) if moving else 0
    thr = max(3, 0.05 * len(samples[0][1]))
    # jumps go against the trend (refill, marker sent back)
    sign = -trend if trend else -1
    cuts = [i + 1 for i, d in enumerate(diffs) if d * sign > thr]
    segs, s0 = [], 0
    for c in cuts + [len(pts)]:
        seg = pts[s0:c]
        if len(seg) >= 3:
            n = len(seg)
            mt, mv = sum(p[0] for p in seg) / n, sum(p[1] for p in seg) / n
            var = sum((p[0] - mt) ** 2 for p in seg)
            slope = sum((p[0] - mt) * (p[1] - mv) for p in seg) / var if var else 0.0
            ss = sum((p[1] - mv) ** 2 for p in seg)
            res = sum((p[1] - (mv + slope * (p[0] - mt))) ** 2 for p in seg)
            segs.append({"t0": seg[0][0], "t1": seg[-1][0], "px_per_s": round(slope, 2),
                         "r2": round(1 - res / ss, 3) if ss else 1.0, "start_px": round(mv + slope * (seg[0][0] - mt), 1)})
        s0 = c
    out = {"rows": rows, "jumps_t": [pts[c][0] for c in cuts], "skipped_no_fill": len(rows) - len(pts)}
    if segs:
        main = max(segs, key=lambda g: g["t1"] - g["t0"])
        out["fit"] = main
        out["segments"] = segs
        if kind == "bar" and main["px_per_s"] < 0:
            out["seconds_full_to_empty"] = round(max(v for _, v in pts) / -main["px_per_s"], 2)
    return out


def cmd_hud(a):
    need_pil()
    _need("ffmpeg")
    from PIL import Image, ImageChops, ImageDraw
    path, meta = resolve(a.src)
    x, y, w, h = _even_region(a.region)
    start, end = _secs(a.start), _secs(a.end)
    seek = (["-ss", str(start)] if start is not None else []) + (["-to", str(end)] if end is not None else [])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    axis = "x" if w >= h else "y"
    fill, samples = (tuple(int(a.fill.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) if a.fill else None), []
    with tempfile.TemporaryDirectory() as td:
        scale = f"scale=-2:{a.height}," if a.height else ""  # keep source pixels by default
        r = subprocess.run(["ffmpeg", "-loglevel", "error", *seek, "-i", str(path), "-vf",
                            f"fps={a.fps},{scale}crop={w}:{h}:{x}:{y}", str(Path(td) / "h%06d.png")],
                           capture_output=True, text=True, timeout=1800, encoding="utf-8", errors="replace")
        if r.returncode:
            sys.exit("ffmpeg failed (is --region inside the frame?): " + (r.stderr.strip().splitlines() or ["?"])[-1])
        files = sorted(Path(td).glob("h*.png"))
        rows, last, prev, shares, busy = [], None, None, [], None
        need = max(5, w * h // 8000)  # changed pixels for a new state (a digit, not speckle)
        thr = min(255, int(a.same * 20))
        for i, f in enumerate(files):
            im = Image.open(f).convert("RGB")
            t = (start or 0) + i / a.fps
            if a.measure:  # every sample, not just changes
                fill = fill or _fill_colour(im, 0.03 if a.measure == "bar" else 0.004)
                samples.append((t, _bar_counts(im, fill, a.fill_tol, axis)))
            g = im.convert("L")
            if prev is not None:  # motion lights up a large area
                d = ImageChops.difference(g, prev).point(lambda v: 1 if v >= thr else 0)
                busy = d if busy is None else ImageChops.add(busy, d)
            prev = g
            if last is not None:
                # count, don't average. A changed digit is only a few dozen px
                # and a mean loses it in compression noise
                hist = ImageChops.difference(g, last).histogram()
                changed = sum(hist[thr:])
                if changed < need:
                    continue
                shares.append(changed / (w * h))
            last = g
            rows.append((t, im.copy()))
    if not rows:
        sys.exit("no frames in the window (check --region against the video size at --height)")
    motion = None
    med_share = sorted(shares)[len(shares) // 2] if shares else 0.0
    if len(rows) >= 40 and len(rows) >= 0.25 * len(files) and med_share >= 0.01:
        # counters change rarely and in a small patch, unlike this
        moving = busy.point(lambda v: 255 if v >= max(3, 0.05 * len(files)) else 0).getbbox() if busy else None
        if moving and (moving[2] - moving[0]) * (moving[3] - moving[1]) > 0.8 * w * h:
            moving = None  # whole box busy, nothing to point at
        motion = {"states": len(rows), "samples": len(files), "median_changed_share": round(med_share, 3),
                  **({"busiest_box": [x + moving[0], y + moving[1], moving[2] - moving[0], moving[3] - moving[1]]}
                     if moving else {})}
        log(f"warning: these look like motion, not value changes: {len(rows)} states in {len(files)} samples (each "
            f"held ~{len(files) / len(rows) / a.fps:.2f} s), a median {med_share:.0%} of the box changing each time. "
            "Unless it is a running timer, something moves through or behind the box (a gun, sprites, "
            "particles, the camera): tighten --region to the counter alone" + (
                f", clear of the busiest part {','.join(map(str, motion['busiest_box']))}" if moving else "")
            + "; over a moving background raise --same (e.g. 10-12: only strong changes such as digits count).")
    cols = max(1, min(a.cols, 1600 // max(w, 1)))
    cell_h = h + 14
    sheets = []
    per = cols * max(1, 4000 // cell_h)
    shown = rows
    if len(rows) > per * a.max_sheets:  # spread evenly, hud.json keeps every change
        step_ = len(rows) / (per * a.max_sheets)
        shown = [rows[int(k * step_)] for k in range(per * a.max_sheets)]
    for k in range(0, len(shown), per):
        chunk = shown[k:k + per]
        sh = Image.new("RGB", (cols * w, ((len(chunk) + cols - 1) // cols) * cell_h), (16, 16, 16))
        d = ImageDraw.Draw(sh)
        for i, (t, im) in enumerate(chunk):
            cx, cy = (i % cols) * w, (i // cols) * cell_h
            d.text((cx + 2, cy + 1), _stamp(t), fill=(255, 220, 0))
            sh.paste(im, (cx, cy + 14))
        name = out / f"hud-{len(sheets) + 1:02d}.png"
        sh.save(name)
        sheets.append(str(name))
    res = {"sheets": sheets, "changes": len(rows), "region": [x, y, w, h]}
    if len(shown) < len(rows):
        res["sheets_note"] = (f"sheets show {len(shown)} of {len(rows)} states, evenly spread (--max-sheets "
                              f"{a.max_sheets}); hud.json lists every change")
    if motion:
        res["motion"] = motion
    if [x, y, w, h] != list(_region(a.region)):
        res["region_note"] = f"grown to even edges for ffmpeg's crop (asked {a.region})"
    measured = None
    if a.measure:
        measured = _bar_series(samples, axis, x if axis == "x" else y, a.measure)
        res.update({"measure": a.measure, "axis": axis, "fill": "#%02x%02x%02x" % fill,
                    **{k: v for k, v in measured.items() if k != "rows"}, "rows": measured["rows"]})
        if measured["skipped_no_fill"]:
            res["skipped_note"] = (f"{measured['skipped_no_fill']} samples had no fill colour (hidden, empty or "
                                   "covered) and were left out of the fit")
    atomic_write(out / "hud.json", json.dumps({"source": meta, "region": [x, y, w, h], "height": a.height,
                                               "changes": [{"t": round(t, 2), "locator": _loc(meta, t)} for t, _ in rows],
                                               **({"measured": measured} if measured else {})}, indent=1))
    if not motion:
        log(f"{len(rows)} distinct states of the region; read {', '.join(sheets)} and write each reading "
            "(time, value) into the ledger.")
    if a.measure:
        log(f"{a.measure}: fill {res['fill']} along {axis}; fit {res.get('fit')}; jumps at {res['jumps_t']}. "
            "Check the fill colour against the sheet: a wrong colour measures the wrong thing.")
    print(json.dumps(res))


def _ncc(a, b):
    """Normalised correlation of two sequences over their common length."""
    import operator
    num = sum(map(operator.mul, a, b))
    n = min(len(a), len(b))
    den = math.sqrt(sum(map(operator.mul, a[:n], a[:n])) * sum(map(operator.mul, b[:n], b[:n])))
    return num / den if den else 0.0


def _colprofile(im):
    """Mean luminance of each column, mean removed."""
    from PIL import Image
    p = list(array.array("f", im.convert("F").resize((im.width, 1), Image.BOX).tobytes()))
    m = sum(p) / len(p)
    return [v - m for v in p]


_CAPSQ = [min(v, 32) ** 2 for v in range(256)]  # _shift's per-pixel cost, capped at 32


def _shift(prev, cur, radius, near=0.0):
    """Horizontal camera move d between two greyscale pictures, so cur[x] shows prev[x + d].
    Scores every whole-pixel move by capped mean squared difference (in C, via Pillow), so a HUD, a
    centred player or a sprite can't outvote the scroll. A parabola over moving pixels gives the sub-pixel
    part. With a repeating background, the minimum nearest the previous move wins.
    Returns (d, confidence 0-1), confidence being how much better it is than a typical wrong match."""
    import operator
    from PIL import Image, ImageChops, ImageFilter, ImageStat
    W, H = prev.size
    if H > 96:  # fewer rows, same horizontal detail, less work
        prev, cur = (im.resize((W, 96), Image.BOX) for im in (prev, cur))
        H = 96
    radius = max(1, min(radius, W - 8))
    cap = [min(v, 32) for v in range(256)]  # so sprites and overlays cost the same at every move
    def boxes(d):  # overlapping parts of prev and cur
        return ((d, 0, W, H), (0, 0, W - d, H)) if d >= 0 else ((0, 0, W + d, H), (-d, 0, W, H))
    def diff(d):
        pb, cb = boxes(d)
        return ImageChops.difference(prev.crop(pb), cur.crop(cb)).point(cap)
    def msd(d):  # from the histogram, faster than copy + ImageStat
        pb, cb = boxes(d)
        h = ImageChops.difference(prev.crop(pb), cur.crop(cb)).histogram()
        return sum(map(operator.mul, h, _CAPSQ)) / ((pb[2] - pb[0]) * H)
    cost = {d: msd(d) for d in range(-radius, radius + 1)}
    best = min(cost.values())
    lows = [d for d, v in cost.items()
            if v <= best * 1.15 + 2 and v <= cost.get(d - 1, 1e9) and v <= cost.get(d + 1, 1e9)]
    d = min(lows or [min(cost, key=cost.get)], key=lambda d: abs(d - near))
    typical = _med(cost.values())
    conf = max(0.0, 1 - cost[d] / typical) if typical else 0.0
    if d and d - 1 in cost and d + 1 in cost:
        # sub-pixel fit on scrolling pixels only. Static overlays
        # match themselves at small moves and pull it toward 0
        cb = boxes(d)[1]
        still = ImageChops.subtract(diff(d), ImageChops.difference(prev, cur).point(cap).crop(cb))
        keep = Image.new("L", (W, H), 0)
        worse = still.point(lambda v: 255 if v > 12 else 0).filter(ImageFilter.MaxFilter(3))
        keep.paste(ImageChops.invert(worse), cb[:2])
        if ImageStat.Stat(keep).mean[0] > 64:
            for e in (d - 1, d, d + 1):
                cost[e] = ImageStat.Stat(diff(e), keep.crop(boxes(e)[1])).rms[0] ** 2
    if d - 1 in cost and d + 1 in cost:
        y0, y1, y2 = cost[d - 1], cost[d], cost[d + 1]
        den = y0 - 2 * y1 + y2
        if den > 0:
            return d + max(-0.5, min(0.5, 0.5 * (y0 - y2) / den)), conf
    return float(d), conf


def _frame_paths(items):
    paths = []
    for x in items:
        p = Path(x)
        paths += sorted(str(f) for f in p.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")) \
            if p.is_dir() else [x]
    return paths


_T_NAME = re.compile(r"@(\d+)m(\d+)(?:[_.](\d+))?s\.[a-z]+$", re.I)


def _frame_times(paths, fps=None):
    """Frame times in seconds, from names like ...@01m25_500s.jpg (`community.py frames`) or
    ...@01m25_50s.jpg (`scenes`), else evenly spaced at --fps, else None."""
    ts = []
    for p in paths:
        m = _T_NAME.search(Path(p).name)
        if not m:
            break
        ts.append(int(m.group(1)) * 60 + float(f"{m.group(2)}.{m.group(3) or 0}"))
    if len(ts) == len(paths) and len(set(ts)) == len(ts):
        return ts
    return [i / fps for i in range(len(paths))] if fps else None


def _drop_dups(imgs, tol):
    """Indices of frames that differ from the last kept one (mean abs diff >= tol).
    A 15 fps game in a 30 fps video shows each picture twice, which would fake zero motion."""
    from PIL import ImageChops, ImageStat
    keep = [0]
    for k in range(1, len(imgs)):
        d = ImageStat.Stat(ImageChops.difference(imgs[k], imgs[keep[-1]])).mean
        if sum(d) / len(d) >= tol:
            keep.append(k)
    return keep


def _lap(im):
    """|Laplacian| of the luminance (0-255), one-pixel border cut off."""
    from PIL import ImageChops, ImageFilter
    g = im.convert("L")
    k = [0, 1, 0, 1, -4, 1, 0, 1, 0]
    lap = ImageChops.add(g.filter(ImageFilter.Kernel((3, 3), k, 1, 0)),
                         g.filter(ImageFilter.Kernel((3, 3), [-v for v in k], 1, 0)))  # both signs
    out = lap.crop((1, 1, max(2, g.width - 1), max(2, g.height - 1)))
    return out


def _sharpness(im):
    """Mean absolute Laplacian, roughly how much fine detail there is."""
    from PIL import ImageStat
    return ImageStat.Stat(_lap(im)).mean[0] if im.width >= 3 and im.height >= 3 else 0.0


def _sharp_ratio(results, frames, block=16):
    """Median per-block sharpness of result vs frame, for same-sized pairs.
    Per block, so a removed sprite only lowers its own blocks while a smear lowers them all."""
    from PIL import ImageStat
    rs = []
    for r_, f_ in zip(results, frames):
        lr, lf = _lap(r_), _lap(f_)
        bw, bh = min(block, lf.width), min(block, lf.height)
        for y in range(0, lf.height - bh + 1, bh):
            for x in range(0, lf.width - bw + 1, bw):
                b = (x, y, x + bw, y + bh)
                sf = ImageStat.Stat(lf.crop(b)).mean[0]
                if sf >= 2:  # flat blocks have no detail to lose
                    rs.append(ImageStat.Stat(lr.crop(b)).mean[0] / sf)
    return round(_med(rs), 3) if rs else 1.0


def _med(v):
    s = sorted(v)
    return s[len(s) // 2] if s else 0.0


def _trimmed(v, cut=0.2):
    """Mean minus the extreme fifth each side. 2,3,2,3 px steps still give 2.5."""
    s = sorted(v)
    k = int(len(s) * cut)
    s = s[k:len(s) - k] or s
    return sum(s) / len(s) if s else 0.0


def _spread(n, m=15):
    return list(range(0, n, max(1, math.ceil(n / m))))


BLUR_WARN = ("blurrier than its source frames: misaligned or moving content; check the offsets / use --layers / "
             "cut a tile from the single clearest frame instead")


def _pano(imgs, out):
    """Panorama of one band. Returns (result, panorama, positions).
    Frames go at their rounded running world x from sub-pixel offsets, so error doesn't build up.
    The median outvotes whatever moves with the camera (HUD, a centred player)."""
    from PIL import Image
    W, H = imgs[0].size
    grey = [im.convert("L") for im in imgs]
    shifts, scores, wx = [0.0], [1.0], [0.0]
    for k in range(1, len(imgs)):
        d, sc = _shift(grey[k - 1], grey[k], max(1, W // 3), shifts[-1])
        shifts.append(d)
        scores.append(sc)
        wx.append(wx[-1] + d)
    x0 = min(wx)
    # 2.5 px/frame puts alternate frames half a pixel off the grid
    # and a median of both blurs. Keep the frames of one phase
    # (within 0.2 px), the one with most detail
    frac = [(v - x0) % 1 for v in wx]
    def off(f, c):
        return min(abs(f - c), 1 - abs(f - c))
    sharp = [_sharpness(im) for im in imgs]
    ph = max((i / 10 for i in range(10)), key=lambda c: sum(sharp[k] for k, f in enumerate(frac) if off(f, c) <= 0.2))
    inph = {k for k, f in enumerate(frac) if off(f, ph) <= 0.2}
    pos = [round(v - x0 - ph) for v in wx]  # round the running total so there's no drift
    pos = [p - min(pos) for p in pos]
    CW = max(pos) + W
    datas = [im.tobytes() for im in imgs]
    buf, cover = bytearray(CW * H * 3), [0] * CW
    for X in range(CW):
        ks = [k for k in range(len(imgs)) if pos[k] <= X < pos[k] + W]
        if len([k for k in ks if k in inph]) >= 3:
            ks = [k for k in ks if k in inph]
        if len(ks) > 15:  # 15 spread takes give as good a median
            ks = ks[:: math.ceil(len(ks) / 15)]
        cover[X] = len(ks)
        if not ks:
            continue
        offs = [(k, (X - pos[k]) * 3) for k in ks]
        mid = len(ks) // 2
        for yy in range(H):
            row, orow = yy * W * 3, (yy * CW + X) * 3
            for c in range(3):
                buf[orow + c] = sorted(datas[k][row + o + c] for k, o in offs)[mid]
    pano = Image.frombytes("RGB", (CW, H), bytes(buf))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    pano.save(out)
    steps = shifts[1:]
    res = {"out": str(out), "size": [CW, H], "speed_px_per_frame": round(_trimmed(steps), 2),
           "offsets_px_per_frame": [round(v, 2) for v in steps], "world_x": [round(v - x0, 1) for v in wx],
           "thin_share": round(sum(1 for c in cover if c < 3) / CW, 3)}
    weak = [i + 1 for i, sc in enumerate(scores[1:]) if sc < 0.6]
    if weak:
        res["weak_matches"] = weak[:20]  # poor match (flat band, cut, fade)
    # repeating background? shortest lag where the profile matches itself
    prof = _colprofile(pano)
    lags = {L: _ncc(prof[L:], prof[:CW - L]) for L in range(8, CW // 2 + 1)}
    if lags:
        top = max(lags.values())
        if top > 0.8:
            P = min(L for L, v in lags.items() if v >= top - 0.02)
            pre = [0]
            for c in cover:
                pre.append(pre[-1] + c)
            tx = max(range(0, CW - 2 * P + 1), key=lambda i: pre[i + 2 * P] - pre[i])  # best-covered stretch
            t1, t2 = pano.crop((tx, 0, tx + P, H)), pano.crop((tx + P, 0, tx + 2 * P, H))
            diff = sum(abs(p - q) for p, q in zip(t1.tobytes(), t2.tobytes())) / (P * H * 3)
            tile = {"period_px": P, "profile_match": round(lags[P], 3), "pixel_diff": round(diff, 1)}
            if diff < 20:  # pixels repeat, not just brightness
                tile["out"] = str(Path(out).with_name(Path(out).stem + "-tile.png"))
                t1.save(tile["out"])
            res["tile"] = tile
    # self-check against each frame
    idx = _spread(len(imgs))
    res["sharpness_ratio"] = _sharp_ratio([pano.crop((pos[k], 0, pos[k] + W, H)) for k in idx], [imgs[k] for k in idx])
    if res["sharpness_ratio"] < 0.7:
        res["warning"] = BLUR_WARN
    return res, pano, pos


def _speed_per_s(res, times):
    """Travel over real elapsed time, so duplicates and uneven sampling don't matter."""
    if times and len(times) > 1 and times[-1] > times[0]:
        return round((res["world_x"][-1] - res["world_x"][0]) / (times[-1] - times[0]), 1)
    return None


def _sharpest(imgs):
    sh = [_sharpness(im) for im in imgs]
    return max(range(len(imgs)), key=sh.__getitem__)


def _scroll_merge(a, imgs, paths):
    """Side-scrolling panorama (one per layer with --layers), repeats dropped first.
    Also saves the sharpest frame's band as a plain crop."""
    by = 0
    if a.band:
        by, bh = (int(v) for v in a.band.split(","))
        imgs = [im.crop((0, by, im.width, by + bh)) for im in imgs]
    times = _frame_times(paths, a.fps)
    keep = _drop_dups(imgs, a.dup_tol)
    if len(keep) < 2:
        sys.exit("every frame shows the same picture here: nothing scrolls in this band (plain `merge`, or more "
                 "frames)")
    kimgs, ktimes = [imgs[k] for k in keep], ([times[k] for k in keep] if times else None)
    base = {"frames": len(imgs), "kept_frames": len(keep), "duplicates_dropped": len(imgs) - len(keep)}
    if ktimes and ktimes[-1] > ktimes[0]:
        base["distinct_frames_per_s"] = round((len(keep) - 1) / (ktimes[-1] - ktimes[0]), 2)
    elif not times:
        base["timing"] = "no frame times (names without @MMmSS_ss, no --fps): speeds per frame only"
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    best = _sharpest(kimgs)
    plain = out.with_name(out.stem + "_plain.png")
    kimgs[best].save(plain)
    base["plain"] = {"out": str(plain), "from": paths[keep[best]]}
    if a.layers:
        return _layers(a, kimgs, ktimes, by, base, [paths[k] for k in keep])
    res, _, _ = _pano(kimgs, str(out))
    res = {"how": "scroll panorama (median in world coordinates)", **base,
           "band": [int(v) for v in a.band.split(",")] if a.band else None, **res}
    res["speed_px_per_s"] = _speed_per_s(res, ktimes)
    if res["thin_share"] > 0.05:
        res["note"] = "columns seen in fewer than 3 frames (the ends) are noisy: trim them or add frames"
    res["next"] = ("open the panorama, its tile and the plain crop (" + str(plain) + ") with Read and ship whichever "
                   "is closer to the footage; a smear or a doubled object means the offsets went wrong there "
                   "(see weak_matches) or the band holds layers at different speeds (--layers auto)")
    print(json.dumps(res, indent=1))


def _layers(a, imgs, times, by, base, paths):
    """Parallax. Strips at the same speed (within 10%) become layers, boundaries moved to the best row.
    Moving layers get a panorama + tile each, static strips one median plate."""
    from PIL import Image, ImageChops, ImageStat
    W, H = imgs[0].size
    sh = max(2, a.strip)
    strips = [[y, min(H, y + sh)] for y in range(0, H, sh)]
    if len(strips) > 1 and strips[-1][1] - strips[-1][0] < sh // 2:
        strips[-2][1] = strips.pop()[1]
    n = len(imgs)
    pairs = [k for k in range(n - 1)][:: max(1, math.ceil((n - 1) / 24))]  # at most 24 frame pairs
    grey = [im.convert("L") for im in imgs]
    info = []
    for y0, y1 in strips:
        cr = {}
        for k in pairs:
            for j in (k, k + 1):
                if j not in cr:
                    cr[j] = grey[j].crop((0, y0, W, y1))
        # horizontal detail. A flat sky has nothing to track
        energy = _med([ImageStat.Stat(ImageChops.difference(c.crop((1, 0, W, c.height)),
                                                            c.crop((0, 0, W - 1, c.height)))).mean[0]
                       for c in list(cr.values())[:6]])
        per, near = {}, 0.0
        for k in pairs:
            d, sc = _shift(cr[k], cr[k + 1], max(1, W // 3), near)
            if sc >= 0.5:
                per[k], near = d, d
        vals = list(per.values())
        speed = _trimmed(vals) if vals and energy >= 1.0 else None  # flat strip, can't tell
        if speed is not None:
            if sum(1 for v in vals if abs(v) < 0.5) >= 0.5 * len(vals):
                speed = 0.0  # still in most pairs, so sprites over a static strip
            elif sum(1 for v in vals if abs(v - speed) <= max(1.0, 0.2 * abs(speed))) < 0.5 * len(vals):
                speed = 0.0  # no steady speed, so animation or sprites. Stays static
        info.append({"y": y0, "h": y1 - y0, "speed": speed, "per": per})
    def same(u, v):  # both still, or same direction within 10%
        return (abs(u) < 0.25) == (abs(v) < 0.25) and (abs(u) < 0.25 or (u * v > 0 and abs(u - v) <= max(
            0.1 * max(abs(u), abs(v)), 0.15)))
    def lspeed(L):
        sp = [info[j]["speed"] for j in L["strips"] if info[j]["speed"]]
        return sum(sp) / len(sp) if sp and not L["static"] else 0.0
    layers, pending = [], []
    for i, s in enumerate(info):
        if s["speed"] is None:  # flat strips join the layer above
            if layers:
                layers[-1]["strips"].append(i)
            else:
                pending.append(i)
            continue
        if layers and same(layers[-1]["speed"], s["speed"]):
            layers[-1]["strips"].append(i)
            layers[-1]["speed"] = lspeed(layers[-1])
        else:
            layers.append({"strips": pending + [i], "static": abs(s["speed"]) < 0.25, "speed": s["speed"]})
            pending = []
    i = 1
    while i < len(layers) - 1:  # rejoin a layer split by one odd strip (sprites)
        A, M, B = layers[i - 1:i + 2]
        if len(M["strips"]) == 1 and not A["static"] and same(A["speed"], B["speed"]):
            A["strips"] += M["strips"] + B["strips"]
            A["speed"] = lspeed(A)
            del layers[i:i + 2]
        else:
            i += 1
    if not layers:
        sys.exit("every strip is flat (no texture to track): pass a --band with detail, or plain `merge`")
    for L in layers:
        L["y0"], L["y1"] = strips[L["strips"][0]][0], strips[L["strips"][-1]][1]
        # median over its strips, 0 if static
        L["per"] = {k: 0.0 if L["static"] else _med([info[j]["per"][k] for j in L["strips"] if k in info[j]["per"]]
                                                    or [L["speed"]]) for k in pairs}
    rows = [g.tobytes() for g in grey]

    def rowcost(y, per):  # row y's frame-to-frame difference under a layer's motion
        tot = cnt = 0
        for k in pairs:
            d = round(per[k])
            lo, hi = max(0, -d), min(W, W - d)
            p, c, o = rows[k], rows[k + 1], y * W
            tot += sum(abs(c[o + x] - p[o + x + d]) for x in range(lo, hi))
            cnt += max(0, hi - lo)
        return tot / cnt if cnt else 0.0
    def refine():
        for A, B in zip(layers, layers[1:]):  # move each boundary to the best-fitting row
            lo, hi = max(A["y0"] + 1, A["y1"] - sh), min(B["y1"] - 1, B["y0"] + sh)
            if hi <= lo:
                continue
            ca = {y: rowcost(y, A["per"]) for y in range(lo, hi)}
            cb = {y: rowcost(y, B["per"]) for y in range(lo, hi)}
            cut = min(range(lo, hi + 1),
                      key=lambda s_: sum(ca[y] for y in range(lo, s_)) + sum(cb[y] for y in range(s_, hi)))
            A["y1"] = B["y0"] = cut

    def measure(L):  # re-measure now that the edges moved
        if L["static"]:
            return
        per, near = {}, L["speed"]
        for k in pairs:
            d, sc = _shift(grey[k].crop((0, L["y0"], W, L["y1"])), grey[k + 1].crop((0, L["y0"], W, L["y1"])),
                           max(1, W // 3), near)
            if sc >= 0.5:
                per[k] = near = d
        if per:
            L["speed"] = _trimmed(per.values())
            L["per"] = {k: per.get(k, L["speed"]) for k in pairs}

    def join(i, j):  # fold layer j into i, speed weighted by rows
        K, D = layers[i], layers[j]
        hk, hd = K["y1"] - K["y0"], D["y1"] - D["y0"]
        K["speed"] = (K["speed"] * hk + (0.0 if K["static"] else D["speed"]) * hd) / (hk + hd)
        K["strips"] = sorted(K["strips"] + D["strips"])
        K["y0"], K["y1"] = min(K["y0"], D["y0"]), max(K["y1"], D["y1"])
        del layers[j]

    def tidy():  # fold in slivers, then join same-speed neighbours
        changed = False
        while len(layers) > 1:
            thin = [j for j, L in enumerate(layers) if L["y1"] - L["y0"] < 3]
            if not thin:
                break
            j = thin[0]
            rows_ = range(layers[j]["y0"], layers[j]["y1"])
            join(min((i for i in (j - 1, j + 1) if 0 <= i < len(layers)),
                     key=lambda i: sum(rowcost(y, layers[i]["per"]) for y in rows_)), j)
            changed = True
        i = 0
        while i < len(layers) - 1:
            if layers[i]["static"] == layers[i + 1]["static"] and same(layers[i]["speed"], layers[i + 1]["speed"]):
                join(i, i + 1)
                changed = True
            else:
                i += 1
        return changed
    # a moved boundary can leave a sliver, and the strip
    # across it read a mixed speed. So tidy and re-measure.
    refine()
    changed = tidy()
    for L in layers:
        measure(L)
    if tidy() or changed:
        refine()
        tidy()
    out = Path(a.out)
    oy = int(a.box.split(",")[1]) if a.box else 0  # report rows in frame coordinates
    rep, warn = [], []
    sample = _spread(n)
    plate = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for i, L in enumerate(layers, 1):
        crops = [im.crop((0, L["y0"], W, L["y1"])) for im in imgs]
        row = {"layer": i, "y": oy + by + L["y0"], "h": L["y1"] - L["y0"]}
        if L["static"]:
            med = _median_bg([crops[k] for k in sample])
            plate.paste(med, (0, L["y0"]))
            ratio = _sharp_ratio([med] * len(sample), [crops[k] for k in sample])
            row.update({"speed_px_per_frame": 0.0, "static": True, "sharpness_ratio": ratio})
            if ratio < 0.7:
                row["warning"] = ("blurrier than its source frames: something moves or animates here (fine if it is "
                                  "sprites crossing a still backdrop; otherwise cut it from one frame)")
        else:
            name = out.with_name(f"{out.stem}-layer{i}.png")
            res, _, _ = _pano(crops, str(name))
            row.update({"speed_px_per_frame": res["speed_px_per_frame"], "speed_px_per_s": _speed_per_s(res, times),
                        **{k: res[k] for k in ("out", "size", "tile", "sharpness_ratio", "warning", "weak_matches")
                           if k in res}})
            pb = _sharpest(crops)
            row["plain"] = str(out.with_name(f"{out.stem}-layer{i}_plain.png"))
            crops[pb].save(row["plain"])
        if "warning" in row:
            warn.append(i)
        rep.append(row)
    res = {"how": "parallax layers (one panorama per scrolling layer)", **base,
           "band": [oy + by, H], "strip_px": sh,
           "strip_speeds": [[oy + by + s["y"], None if s["speed"] is None else round(s["speed"], 2)] for s in info],
           "layers": rep}
    if any(L["static"] for L in layers):
        res["static_plate"] = str(out.with_name(out.stem + "-static.png"))
        plate.save(res["static_plate"])
    if warn:
        res["warning"] = f"layers {warn} came out blurrier than their source frames: see each layer's warning"
    res["next"] = ("open each layer panorama, its tile and its _plain crop with Read and ship whichever is closer to "
                   "the footage; speeds give the parallax factors (layer speed / fastest layer). Static rows are in "
                   "the -static plate (transparent where layers scroll); whole-band plain crop: "
                   + base["plain"]["out"])
    print(json.dumps(res, indent=1))


def _moving_regions(imgs, tol, off=(0, 0)):
    """Boxes where content keeps changing between distinct frames, speckle removed.
    Scrolling strips also get their speed."""
    from PIL import Image, ImageChops, ImageFilter
    keep = _drop_dups(imgs, tol)
    if len(keep) < 3:
        return [], len(keep)
    W, H = imgs[0].size
    acc = Image.new("L", (W, H), 0)
    for p, q in zip(keep, keep[1:]):
        d = ImageChops.difference(imgs[p], imgs[q]).convert("L")
        acc = ImageChops.add(acc, d.point(lambda v: 1 if v > 20 else 0))
    npair = len(keep) - 1
    need = max(2, round(0.2 * npair))
    mask = acc.point(lambda v: 255 if v >= need else 0)
    mask = mask.filter(ImageFilter.MedianFilter(5)).filter(ImageFilter.MaxFilter(5))  # remove speckle, join pieces
    boxes = [list(b) for count, b in _components(mask, 2)
             if count * 4 >= max(48, 0.001 * W * H) and b[2] - b[0] >= 4 and b[3] - b[1] >= 4]
    merged = True
    while merged:  # flat stretches don't change, so join a strip's pieces
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                p, q = boxes[i], boxes[j]
                ov = min(p[3], q[3]) - max(p[1], q[1])
                if ov >= 0.6 * max(p[3] - p[1], q[3] - q[1]) and max(p[0], q[0]) - min(p[2], q[2]) <= 16:
                    boxes[i] = [min(p[0], q[0]), min(p[1], q[1]), max(p[2], q[2]), max(p[3], q[3])]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    boxes.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    found = []
    for x0, y0, x1, y1 in boxes:
        box = (x0, y0, x1, y1)
        a_ = acc.crop(box).tobytes()
        hot = [v for v in a_ if v >= need]
        r = {"box": [x0 + off[0], y0 + off[1], x1 - x0, y1 - y0],
             "changes_in_share_of_frames": round(sum(hot) / max(1, len(hot)) / npair, 2)}
        profs = [imgs[k].crop(box).convert("L") for k in keep[:25]]
        if x1 - x0 >= 16:
            st, near = [], 0.0
            for p, q in zip(profs, profs[1:]):
                d, sc = _shift(p, q, max(1, (x1 - x0) // 3), near)
                if sc >= 0.5:
                    st.append(d)
                    near = d
            m = _trimmed(st)
            if len(st) >= 2 and abs(m) >= 0.5 and sum(1 for v in st if abs(v - m) <= 1) >= 0.6 * len(st):
                r["scrolls_px_per_frame"] = round(m, 2)
        found.append(r)
        if len(found) >= 8:
            break
    return found, len(keep)


def cmd_merge(a):
    """Per pixel, the most common value among frames where it isn't hidden.
    Fills holes, gives clean plates, and writes a mask of pixels no frame showed clearly."""
    need_pil()
    from PIL import Image
    paths = _frame_paths(a.images)
    imgs = [Image.open(p).convert("RGB") for p in paths]
    off = (0, 0)
    if a.box:
        x, y, w, h = (int(v) for v in a.box.split(","))
        imgs = [im.crop((x, y, x + w, y + h)) for im in imgs]
        off = (x, y)
    if len({im.size for im in imgs}) > 1:
        sys.exit("frames differ in size: normalise them first (`footage.py stage`) or pass --box")
    if a.scroll or a.layers:
        if len(imgs) < 2:
            sys.exit("--scroll needs 2+ frames in order (a burst from `community.py frames --fps`)")
        return _scroll_merge(a, imgs, paths)
    W, H = imgs[0].size
    ign = [tuple(int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) for c in (a.ignore or [])]
    sample = _spread(len(imgs))
    if a.how == "sharpest":  # smooth or animated art, the clearest frame beats a blend
        best = _sharpest(imgs)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        imgs[best].save(a.out)
        print(json.dumps({"out": a.out, "frames": len(imgs), "how": "sharpest single frame", "from": paths[best],
                          "sharpness_ratio": _sharp_ratio([imgs[best]] * len(sample), [imgs[k] for k in sample])},
                         indent=1))
        return
    datas = [im.tobytes() for im in imgs]
    q = a.quant
    out_px, unsure = bytearray(W * H * 3), bytearray(W * H)
    for i in range(W * H):
        vals = []
        for d in datas:
            c = (d[3 * i], d[3 * i + 1], d[3 * i + 2])
            if any(sum(abs(c[k] - g[k]) for k in range(3)) <= a.tol for g in ign):
                continue
            vals.append(c)
        if not vals:
            unsure[i] = 255
            continue
        if a.how == "median":
            pick = tuple(sorted(v[k] for v in vals)[len(vals) // 2] for k in range(3))
            share = 1.0
        else:  # most common quantised colour, so moving things vanish
            counts = {}
            for v in vals:
                key = (v[0] // q, v[1] // q, v[2] // q)
                counts.setdefault(key, []).append(v)
            best = max(counts.values(), key=len)
            pick = tuple(sorted(v[k] for v in best)[len(best) // 2] for k in range(3))
            share = len(best) / len(vals)
        out_px[3 * i:3 * i + 3] = bytes(pick)
        if share < 0.5 or len(vals) < max(2, len(datas) // 4):
            unsure[i] = 255
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    merged = Image.frombytes("RGB", (W, H), bytes(out_px))
    merged.save(a.out)
    mask = Path(a.out).with_name(Path(a.out).stem + "-unsure.png")
    if "app" in Path(a.out).parts:  # masks are evidence, not part of the app
        mask = Path("dossier/screens/masks") / mask.name
        mask.parent.mkdir(parents=True, exist_ok=True)
    Image.frombytes("L", (W, H), bytes(unsure)).save(mask)
    share = sum(1 for v in unsure if v) / (W * H)
    res = {"out": a.out, "frames": len(imgs), "size": [W, H], "unsure_share": round(share, 4), "unsure_mask": str(mask)}
    res["sharpness_ratio"] = _sharp_ratio([merged] * len(sample), [imgs[k] for k in sample])
    if res["sharpness_ratio"] < 0.7:
        res["warning"] = BLUR_WARN
    moving, kept = _moving_regions(imgs, a.dup_tol, off)
    res["distinct_frames"] = kept
    if moving:
        res["moving_regions"] = moving
        res["moving_note"] = ("this region moves (scrolling strip or animation): the median erases it; build it with "
                              "merge --scroll/--layers or cut it from one frame. (A sprite, cursor or popup you meant "
                              "to remove is fine to lose.)")
    if share > 0.02:
        res["next"] = ("white pixels in the mask were hidden in most frames or never seen: add frames where that "
                       "part is uncovered, or fill them by hand from the neighbours (and mark them reconstructed)")
    print(json.dumps(res, indent=1))


def cmd_count(a):
    """Moving objects per frame (blobs against the median background, as `sprite` cuts) and their spread."""
    need_pil()
    from PIL import Image, ImageChops, ImageFilter
    paths = _frame_paths(a.images)
    rx = _region(a.region)
    imgs = []
    for p in paths:
        im = Image.open(p).convert("RGB")
        imgs.append(im.crop((rx[0], rx[1], rx[0] + rx[2], rx[1] + rx[3])))
    keep = _drop_dups(imgs, a.dup_tol)
    kimgs = [imgs[k] for k in keep]
    if a.bg == "auto":
        if len(kimgs) < 3:
            sys.exit("--bg auto needs 3+ distinct frames where the objects move; or pass --bg clean_frame.png")
        bg = _median_bg([kimgs[k] for k in _spread(len(kimgs))])
    else:
        bg = Image.open(a.bg).convert("RGB")
        if bg.size != kimgs[0].size:
            bg = bg.crop((rx[0], rx[1], rx[0] + rx[2], rx[1] + rx[3]))
    W, H = kimgs[0].size
    per, fg = [], []
    for k, im in zip(keep, kimgs):
        mask = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > a.tol else 0)
        mask = mask.filter(ImageFilter.MedianFilter(3))
        fg.append(sum(1 for v in mask.tobytes() if v) / (W * H))
        if a.join > 1:  # bridge small gaps so one sprite counts once
            mask = mask.filter(ImageFilter.MaxFilter(a.join | 1))
        objs = []
        for count, (x0, y0, x1, y1) in _components(mask, 2):
            if count * 4 >= a.min_area:
                objs.append({"box": [x0 + rx[0], y0 + rx[1], x1 - x0, y1 - y0], "c": ((x0 + x1) / 2, (y0 + y1) / 2)})
        per.append({"frame": paths[k], "objs": objs})
    excluded = []
    if a.exclude_static:  # static ones are the player or a HUD readout
        for f in per:
            for o in f["objs"]:
                r = max(6, 0.3 * max(o["box"][2], o["box"][3]))
                o["static"] = sum(1 for g in per if any(math.hypot(o["c"][0] - p["c"][0], o["c"][1] - p["c"][1]) <= r
                                                        for p in g["objs"])) >= 0.5 * len(per)
        for f in per:
            excluded += [o["box"] for o in f["objs"] if o["static"]]
            f["objs"] = [o for o in f["objs"] if not o["static"]]
    counts = [len(f["objs"]) for f in per]
    s = sorted(counts)
    dist = {"min": s[0], "median": s[len(s) // 2], "max": s[-1], "mean": round(sum(s) / len(s), 2),
            "histogram": {str(c): counts.count(c) for c in sorted(set(counts))}}
    lo_hi = f"{dist['min']}-{dist['max']}" if dist["min"] != dist["max"] else str(dist["min"])
    res = {"frames": len(imgs), "kept_frames": len(keep), "duplicates_dropped": len(imgs) - len(keep),
           "region": list(rx), "distribution": dist,
           "summary": f"{lo_hi} objects on screen (median {dist['median']}) over {len(per)} frames",
           "per_frame": [{"frame": f["frame"], "objects": len(f["objs"]), "boxes": [o["box"] for o in f["objs"]]}
                         for f in per]}
    if excluded:
        res["excluded_static"] = {"instances": len(excluded), "typical_box": sorted(excluded)[len(excluded) // 2]}
    if _med(fg) > 0.3:
        res["warning"] = ("most of the region differs from the median background: the screen scrolls, flashes or the "
                          "objects barely move; count over a static stretch, or pass --bg with a clean frame")
    res["next"] = ("check a few frames against their boxes (an object split in two: raise --join; noise, sparkles or "
                   "a pointer counted: raise --min-area or --tol; drifting clouds or a scrolling backdrop count too: "
                   "narrow --region). Objects that sit still in most frames melt into the median background.")
    print(json.dumps(res, indent=1))


def cmd_stage(a):
    """Trim letterbox/pillarbox borders (or crop --box) and resize frames to the original's stage."""
    need_pil()
    from PIL import Image, ImageChops
    W, H = (int(v) for v in a.size.lower().split("x"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for p in a.images:
        im = Image.open(p).convert("RGB")
        if a.box:
            x, y, w, h = (int(v) for v in a.box.split(","))
            im = im.crop((x, y, x + w, y + h))
        else:  # trim rows/columns matching the corner colour
            bg = Image.new("RGB", im.size, im.getpixel((0, 0)))
            bbox = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > a.tol else 0).getbbox()
            if bbox:
                im = im.crop(bbox)
        ratio = (im.width / im.height) / (W / H)
        res = im.resize((W, H), Image.LANCZOS)
        dest = out / Path(p).name
        res.save(dest)
        rows.append({"frame": str(dest), "from": p, "content": [im.width, im.height],
                     **({"warning": f"aspect differs by {abs(ratio - 1) * 100:.0f}%: the crop is off, or the video "
                                    "was stretched; pass --box"} if abs(ratio - 1) > 0.04 else {})})
    emit(rows, a.format)


def cmd_tiles(a):
    """Cut every lattice cell, group cells into tile types, merge each type into one clean sprite."""
    need_pil()
    from PIL import Image, ImageDraw
    ox, oy = (float(v) for v in a.origin.split(","))
    dx, dy = (float(v) for v in a.step.split(","))
    tw, th = (int(v) for v in a.size.split(","))
    def score(im, box):
        """Contrast between the disc/square and the ring just outside it."""
        inner = im.crop(box).convert("L").resize((6, 6))
        outer = im.crop((box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2)).convert("L").resize((10, 10))
        iv, ov = list(inner.tobytes()), list(outer.tobytes())
        edge = [ov[i] for i in range(100) if i // 10 in (0, 9) or i % 10 in (0, 9)]
        return abs(sum(iv) / len(iv) - sum(edge) / len(edge)) + (max(iv) - min(iv)) * 0.3
    cells = []
    for p in a.images:
        im = Image.open(p).convert("RGB")
        for r in range(a.rows):
            shift = a.odd_shift if r % 2 else 0.0
            for c in range(a.cols):
                cx, cy = ox + shift + c * dx, oy + r * dy
                best = None
                for sx in range(-a.snap, a.snap + 1):  # the lattice drifts a pixel or two
                    for sy in range(-a.snap, a.snap + 1):
                        x0, y0 = int(round(cx - tw / 2)) + sx, int(round(cy - th / 2)) + sy
                        box = (x0, y0, x0 + tw, y0 + th)
                        if box[0] < 2 or box[1] < 2 or box[2] > im.width - 2 or box[3] > im.height - 2:
                            continue
                        sc = score(im, box)
                        if best is None or sc > best[0]:
                            best = (sc, box)
                if best:
                    cells.append((p, r, c, im.crop(best[1]), best[1]))
    if not cells:
        sys.exit("no cells inside the frame: check --origin (centre of the top-left cell), --step and --size")
    mask = Image.new("L", (tw, th), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, tw - 1, th - 1), fill=255) if a.mask == "circle" else mask.paste(255)
    mpx = mask.load()
    inner_m = Image.new("L", (tw, th), 0)
    ImageDraw.Draw(inner_m).ellipse((tw * 0.25, th * 0.25, tw * 0.75, th * 0.75), fill=255)
    ring_m = Image.new("L", (tw, th), 0)
    ImageDraw.Draw(ring_m).ellipse((tw * 0.08, th * 0.08, tw * 0.92, th * 0.92), fill=255)
    ImageDraw.Draw(ring_m).ellipse((tw * 0.3, th * 0.3, tw * 0.7, th * 0.7), fill=0)
    from PIL import ImageStat
    def feat(im):
        """Mean colour of centre and rim. Shrugs off 1-px shifts and compression."""
        return ImageStat.Stat(im, inner_m).mean + ImageStat.Stat(im, ring_m).mean
    plate = Image.open(a.bg_plate).convert("RGB") if a.bg_plate else None
    groups = []
    for p, r, c, im, box in cells:
        f = feat(im)
        if plate is not None:  # empty cells look like the board behind them
            pf = feat(plate.crop(box))
            if sum(abs(x - y) for x, y in zip(f, pf)) / len(f) < a.tol / 2:
                continue
        if a.empty and sum(abs(f[i] - a.empty_rgb[i % 3]) for i in range(len(f))) / len(f) < a.tol:
            continue  # empty cell
        for g in groups:
            if sum(abs(x - y) for x, y in zip(f, g["f"])) / len(f) < a.tol:
                g["takes"].append(im)
                break
        else:
            groups.append({"f": f, "takes": [im], "first": f"{p} r{r} c{c}"})
    groups.sort(key=lambda g: -len(g["takes"]))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for n, g in enumerate(groups, 1):
        takes = g["takes"]
        datas = [t.tobytes() for t in takes]
        mid = len(datas) // 2
        merged = Image.frombytes("RGB", (tw, th), bytes(sorted(d[i] for d in datas)[mid] for i in range(len(datas[0]))))
        tile = merged.convert("RGBA")
        tile.putalpha(mask)
        name = out / f"tile{n:02d}_x{len(takes)}.png"
        tile.save(name)
        rows.append({"tile": n, "file": str(name), "takes": len(takes), "first_seen": g["first"]})
    common = [r for r in rows if r["takes"] >= 3] or rows
    sheet = Image.new("RGBA", (len(common) * (tw * 3 + 6) + 6, th * 3 + 12), (128, 128, 128, 255))
    for i, r in enumerate(common):
        im = Image.open(r["file"])
        sheet.alpha_composite(im.resize((tw * 3, th * 3), Image.NEAREST), (6 + i * (tw * 3 + 6), 6))
    sheet.save(out / "sheet.png")
    rare = len(rows) - len(common)
    log(f"{len(cells)} cells -> {len(common)} tile types seen 3+ times (sheet.png) and {rare} rare ones (empty "
        "background, cells mid-animation or under the cursor: open them only if a real tile is missing). Raise --tol "
        "if one tile type is split in two; lower it if two types merge.")
    emit(rows, a.format)


# ---------- audio ----------

SR = 22050


def _pcm(path, start=None, end=None, strict=True):
    """Mono 16-bit samples at SR. strict exits on no audio or silence.
    strict=False returns them anyway, for measuring levels."""
    seek = (["-ss", str(start)] if start is not None else []) + (["-to", str(end)] if end is not None else [])
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", *seek, "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR),
                          "-f", "s16le", "-"], capture_output=True, timeout=900).stdout
    x = array.array("h", raw)
    if not strict:
        return x
    if not x:
        sys.exit("no audio decoded (does the source have an audio track?)")
    if max(max(x), -min(x)) < 60:
        sys.exit("the audio track is silent (screen recorders often captured none): use another recording")
    return x


def _write_wav(path, samples, rate=SR, channels=1):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(array.array("h", samples).tobytes())


def _decimate(x, k):
    """Average every k samples, a cheap low-pass and downsample."""
    return [sum(x[i:i + k]) / k for i in range(0, len(x) - k + 1, k)]


def _align(ref, sig, at, radius, dec=8):
    """Best match of ref in sig near `at` (+-radius), coarse then sample-exact.
    Returns (position, normalised correlation)."""
    n = len(ref)
    lo, hi = max(0, at - radius), min(len(sig) - n, at + radius)
    if hi < lo:
        return None, 0.0
    rd, sd = _decimate(ref, dec), _decimate(sig[lo:hi + n], dec)
    j = max(range(0, (hi - lo) // dec + 1), key=lambda j: _ncc(rd, sd[j:j + len(rd)]))
    c = lo + j * dec
    best = max(range(max(lo, c - dec), min(hi, c + dec) + 1), key=lambda p: _ncc(ref, sig[p:p + n]))
    return best, _ncc(ref, sig[best:best + n])


def _bands(seg, bg=None):
    """Energy in 10 log-spaced bands (4 Goertzel bins each) minus the background's, normalised.
    A timbre fingerprint that ignores the music underneath."""
    def energies(x):
        n = len(x)
        out = []
        for i in range(10):
            lo, hi = 120 * 2 ** (i * 0.65), 120 * 2 ** ((i + 1) * 0.65)
            tot = 0.0
            for f in (lo + (hi - lo) * q / 4 for q in range(4)):
                if f >= SR / 2 or n < 64:
                    continue
                k = 2 * math.cos(2 * math.pi * f / SR)
                s1 = s2 = 0.0
                for v in x:
                    s1, s2 = v + k * s1 - s2, s1
                tot += max(0.0, s1 * s1 + s2 * s2 - k * s1 * s2) / n
            out.append(tot)
        return out
    e = energies(seg)
    if bg is not None and len(bg) >= 64:
        e = [max(0.0, v - w * len(seg) / len(bg)) for v, w in zip(e, energies(bg))]
    e = [math.sqrt(v) for v in e]
    tot = math.sqrt(sum(v * v for v in e)) or 1
    return [v / tot for v in e]


def _peak_freq(seg):
    n = min(len(seg), 1024)
    if n < 64:
        return None
    def power(f):
        k = 2 * math.cos(2 * math.pi * f / SR)
        s1 = s2 = 0.0
        for v in seg[:n]:
            s1, s2 = v + k * s1 - s2, s1
        return s1 * s1 + s2 * s2 - k * s1 * s2
    coarse = max(range(100, 9000, 150), key=power)  # coarse sweep, then refine around the best
    return max(range(max(50, coarse - 150), coarse + 151, 10), key=power)


STEM_NAMES = {"drums", "bass", "other", "vocals", "no_vocals", "guitar", "piano"}


def _has_video(path):
    """True for a real picture stream. Cover art doesn't count."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                        "stream=codec_type:stream_disposition=attached_pic", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    try:
        streams = json.loads(r.stdout or "{}").get("streams", [])
    except ValueError:
        return False
    return any(not (s_.get("disposition") or {}).get("attached_pic") for s_ in streams)


def _fft(a, inv=False):
    """Iterative radix-2 FFT. Length must be a power of two."""
    import cmath
    n, a = len(a), list(a)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    size = 2
    while size <= n:
        half = size // 2
        ws = [cmath.exp((2j if inv else -2j) * math.pi * k / size) for k in range(half)]
        for s0 in range(0, n, size):
            for k in range(half):
                u, v = a[s0 + k], a[s0 + k + half] * ws[k]
                a[s0 + k], a[s0 + k + half] = u + v, u - v
        size *= 2
    return [v / n for v in a] if inv else a


def _minus_loop(x, loop_path):
    """Subtract a known music loop. Returns (residual, info).
    Folding the window into one loop length adds up the music and averages out voices and effects.
    An FFT cross-correlation (decimated) then finds the loop's start. Each cycle's copy is
    re-aligned (+-30 ms) and scaled by least squares."""
    lp = _pcm(loop_path)
    L = len(lp)
    if L < SR // 2:
        sys.exit("--minus-loop: the loop is shorter than 0.5 s")
    fold = [0.0] * L
    for i, v in enumerate(x):
        fold[i % L] += v
    dec = 8
    fd, ld = _decimate(fold, dec), _decimate(lp, dec)
    n = 1 << (2 * len(ld) - 1).bit_length()
    A = _fft([complex(v) for v in fd] + [0j] * (n - len(fd)))
    B = _fft([complex(v) for v in ld + ld] + [0j] * (n - 2 * len(ld)))
    r = _fft([p.conjugate() * q for p, q in zip(A, B)], inv=True)
    # r[m] = sum_j fold[j]*loop[j+m], so start = L - m (mod L)
    m = max(range(1, len(ld) + 1), key=lambda m_: r[m_].real)
    p0 = ((len(ld) - m) * dec) % L
    while p0 > 0:
        p0 -= L  # back to the cycle covering the start
    radius = int(0.03 * SR)
    # music restarts off-grid with each round or menu,
    # so find each restart and run the grid from there
    hop = SR // 50
    env = [math.sqrt(sum(v * v for v in x[i:i + hop]) / hop) for i in range(0, len(x) - hop, hop)]
    head = lp[:min(L, 3 * SR)]
    renv = [math.sqrt(sum(v * v for v in head[i:i + hop]) / hop) for i in range(0, len(head) - hop, hop)]
    anchors, tried = [], []
    if len(renv) > 10 and len(env) > len(renv):
        for sc_, j in sorted(((_ncc(renv, env[j:j + len(renv)]), j) for j in range(len(env) - len(renv))), reverse=True):
            if sc_ < 0.6:
                break
            if any(abs(j * hop - f) < 0.8 * L for f in anchors) or any(abs(j * hop - t) < SR for t in tried):
                continue
            tried.append(j * hop)
            pos, sc = _align(head, x, j * hop, int(0.06 * SR), dec=4)
            if pos is not None and sc >= 0.3 and all(abs(pos - f) >= 0.8 * L for f in anchors):
                anchors.append(pos)
    anchors.sort()
    if anchors:  # from each anchor up to the next
        p0 = anchors[0]
        while p0 > 0:
            p0 -= L
    starts, q = [], p0
    while q < len(x):
        nxt = [f for f in anchors if f > q + L // 2]
        if anchors and q in anchors:
            starts.append(q)
            q += L
            continue
        if nxt and q + L > nxt[0] - L // 2:  # music restarted before this cycle ended
            starts.append(q)
            q = nxt[0]
            continue
        j0 = max(0, -q) + radius  # part inside the window, clear of the search margin
        n_ = min(L - j0, 2 * SR, len(x) - q - j0 - radius)
        if n_ >= SR // 4:
            pos, sc = _align(lp[j0:j0 + n_], x, q + j0, radius, dec=4)
            if pos is not None and sc > 0.1:
                q = pos - j0
        starts.append(q)
        q += L
    starts = sorted(set(starts))
    res = array.array("h", x)
    gains = []
    for k, q in enumerate(starts):
        a0, a1 = max(0, q), min(len(x), starts[k + 1] if k + 1 < len(starts) else len(x))
        if a1 <= a0:
            continue
        idx = [(i - q) % L for i in range(a0, a1)]
        num = sum(x[i] * lp[j] for i, j in zip(range(a0, a1), idx))
        den = sum(lp[j] * lp[j] for j in idx) or 1
        g = num / den
        gains.append(round(g, 3))
        for i, j in zip(range(a0, a1), idx):
            res[i] = max(-32768, min(32767, round(x[i] - g * lp[j])))
    e0, e1 = sum(v * v for v in x) or 1, sum(v * v for v in res) or 1
    return res, {"loop_start_s": round(starts[0] % L / SR, 3), "cycles": len(starts), "restarts_found": len(anchors),
                 "gains": gains[:12],
                 "removed_db": round(10 * math.log10(e0 / e1), 1)}


def _stem_offset(path: Path, given) -> float:
    """Seconds into the source this file starts, from --offset or the stems.json `stems --start` wrote."""
    if given is not None:
        return float(given)
    side = path.parent / "stems.json"
    try:
        info = json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    if any(Path(s_.get("file", "")).name == path.name for s_ in info.get("stems", [])) and info.get("offset_s"):
        log(f"note: {path.name} is a stem cut {info['offset_s']}s into {info.get('source')} ({side}): times below "
            "are source times (pass --offset 0 to report stem times)")
        return float(info["offset_s"])
    return 0.0


def _p95_rms(x, hop=int(SR * 0.05)) -> float:
    """95th-percentile RMS over 50 ms windows.
    Narration is mostly near silence, but only a silent track is quiet at its loud moments too."""
    env = sorted(math.sqrt(sum(v * v for v in x[i:i + hop]) / hop) for i in range(0, max(0, len(x) - hop), hop))
    return env[int(len(env) * 0.95)] if env else 0.0


SILENT_RMS = 30  # ~-60 dBFS, nothing audible


def _stems_for(path: Path):
    """(stem file, stems.json) for this recording's drums stem from `stems`, or None."""
    if path.stem.lower() in STEM_NAMES or path.suffix.lower() == ".wav" and not _has_video(path):
        return None
    vid = path.name.split(".")[0]
    roots = [Path("dossier")] + [p for p in path.resolve().parents if p.name == "dossier"][:1]
    seen = set()
    for root in roots:
        for side in sorted(root.glob("**/stems.json")) if root.is_dir() else []:
            if side.resolve() in seen:
                continue
            seen.add(side.resolve())
            try:
                info = json.loads(side.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if Path(str(info.get("source", ""))).name.split(".")[0] != vid:
                continue
            for s_ in info.get("stems", []):
                f = side.parent / Path(s_.get("file", "")).name
                if s_.get("stem") == "drums" and f.exists():
                    return f, side
    return None


def _env(x, hop):
    return [math.sqrt(_dot(x[i:i + hop], x[i:i + hop]) / hop) for i in range(0, len(x) - hop, hop)]


def _rolling_median(env, W=75):
    """Rolling median over 2*W frames, so the music bed isn't mistaken for effects."""
    bgl = []
    for i in range(0, len(env), 10):
        win = sorted(env[max(0, i - W): i + W])
        bgl += [win[len(win) // 2] or 1.0] * 10
    return bgl[:len(env)]


def _onsets(env, bgl, ratio, floor, min_ms, max_ms):
    events, i = [], 0
    while i < len(env):
        if env[i] > max(bgl[i] * ratio, floor) and (i == 0 or env[i] > env[i - 1] * 1.5 or env[i - 1] < bgl[i] * 2):
            j = _event_end(env, bgl, i, max_ms)
            if (j - i) * 10 >= min_ms:
                events.append((i, j))
            i = j + 1
        else:
            i += 1
    return events


def _event_end(env, bgl, i, max_ms, keep=1.6):
    j = i
    while j < len(env) and env[j] > bgl[min(j, len(bgl) - 1)] * keep and j - i < max_ms // 10:
        j += 1
    return j


def _search_near(env, i, radius, floor):
    """Sharpest onset within +-radius frames of i, judged against the quietest 80 ms before it
    and the median of the 3 s around it. Returns (frame, rise, over_median) or None."""
    best = None
    for k in range(max(1, i - radius), min(len(env), i + radius + 1)):
        rise = env[k] / max(1.0, min(env[max(0, k - 8):k]))
        if best is None or rise > best[1]:
            best = (k, rise)
    if best is None:
        return None
    k, rise = best
    win = sorted(env[max(0, k - 150):k + 150])
    over = env[k] / max(1.0, win[len(win) // 2])
    return (k, rise, over) if rise >= 2.0 and over >= 1.4 and env[k] >= floor / 4 else None


def cmd_sfx(a):
    _need("ffmpeg")
    if a.sheet:
        need_pil()  # check now, not after a long analysis
    path, meta = resolve(a.src)
    stem_note = None
    found = None if (a.no_stems or a.minus_loop) else _stems_for(path)
    if found:  # Demucs split it already, effects are in the drums stem
        stem, side = found
        soff = _stem_offset(stem, None)
        sdur, full = _duration(stem) or 0.0, meta.get("duration") or _duration(path) or 0.0
        st, en = _secs(a.start) or 0.0, _secs(a.end) or full
        if st >= soff - 0.05 and en <= soff + sdur + 0.5:
            stem_note = (f"detecting on the drums stem {stem} ({side}), not the full mix: effects stand clear of the "
                         "music there. Saved clips come from the stem; pass --no-stems to use the mix")
            log(stem_note)
            a.video = a.video or (str(path) if _has_video(path) else None)
            path, a.offset = stem, soff
    off = _stem_offset(path, a.offset)  # a stem's 0 s is `off` s into the source
    start, end = _secs(a.start), _secs(a.end)  # source times, like everything we report
    seek0 = max(0.0, start - off) if start is not None else None
    x = _pcm(path, seek0, end - off if end is not None else None)
    base = off + (seek0 or 0.0)  # source time of x[0]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    minus = None
    if a.minus_loop:  # known music, so what's left is effects and voices
        x, minus = _minus_loop(x, a.minus_loop)
        _write_wav(out / "residual.wav", x)
        minus["residual"] = str(out / "residual.wav")
        log(f"loop subtracted: {minus['removed_db']} dB of energy removed over {minus['cycles']} cycles "
            f"(gains {minus['gains']}); detecting effects on {minus['residual']}")
    hop = int(SR * 0.01)  # 10 ms RMS envelope
    env = _env(x, hop)
    bgl = _rolling_median(env)
    bed = sorted(bgl)[len(bgl) // 2] if bgl else 0.0
    ratio, tuned = a.ratio, []
    if bed >= a.floor / 2:  # steady bed (music, ambience) under everything
        if a.ratio is None:
            ratio = 2.0
            tuned.append("ratio 3.0 -> 2.0")
        if not found and not minus:  # effects are brighter than a bed, detect on the highs
            xd = [x[i] - 0.95 * x[i - 1] for i in range(1, len(x))]
            env, bgl = _env(xd, hop), None
            bgl = _rolling_median(env)
            tuned.append("detected on the treble (pre-emphasis)")
        if tuned:
            log(f"steady bed (median RMS {bed:.0f}): {', '.join(tuned)}; the saved clips are the unfiltered sound")
    ratio = ratio or 3.0
    events = _onsets(env, bgl, ratio, a.floor, a.min_ms, a.max_ms)
    marks, mark_hit = [], {}
    if a.events:  # find the sound near each known moment
        try:
            marks = [(float(t), lbl.strip()) for t, lbl in (e.split(":", 1) for e in a.events.split(",") if e.strip())]
        except ValueError:
            sys.exit("--events must look like '12.4:jump,15.0:coin'")
        radius = max(1, int(a.event_tol * 100))
        for m_, (t, lbl) in enumerate(marks):
            i = int(round((t - base) * 100))
            if not 0 <= i < len(env):
                continue
            near = [e for e in events if abs(e[0] - i) <= radius]
            if near:
                mark_hit[m_] = max(near, key=lambda e: max(env[e[0]:e[1] + 1]))  # the loudest is what got labelled
                continue
            got = _search_near(env, i, radius, a.floor)
            if got:
                k = got[0]
                j = max(k + a.min_ms // 10, _event_end(env, bgl, k, a.max_ms, keep=1.2))
                events.append((k, min(len(env), j)))
                mark_hit[m_] = events[-1]
        events = sorted(set(events))
    if not events:
        sys.exit("no effects stood out: lower --ratio, or pick a stretch with less music"
                 + ("" if found or a.no_stems else "; or split the music off first (`stems`), then run sfx again"))
    has_video = _has_video(path)
    partial = path.stem.lower() in STEM_NAMES or not has_video or minus  # stems and residuals are only part of the mix
    if bed >= a.floor / 2:
        music_note = f"a steady background bed (median RMS {bed:.0f}) runs under the effects: music or ambience"
    elif minus:
        music_note = "no steady bed left after subtracting the loop: it accounted for the music here"
    elif partial:
        music_note = ("no steady bed in this recording/stem: near silence between effects. A stem or a separate "
                      "audio file says nothing about whether the game had music; check the full mix or other stems")
    else:
        music_note = ("no music bed: the audio is near silence between effects, so the game (or this recording) had "
                      "no background music here")
    pad = int(a.pad_ms / 10)
    rows = []
    labels = {}
    for m_, e in mark_hit.items():
        labels.setdefault(e, []).append(marks[m_][1])
    for i0, i1 in sorted(set(events[: a.max]) | set(labels)):  # labelled moments are always kept, even past --max
        s0, s1 = max(0, (i0 - pad) * hop), min(len(x), (i1 + pad) * hop)
        bgs = sum(bgl[i0:i1]) / max(1, i1 - i0)
        rows.append({"t": round(base + i0 * 0.01, 2), "length_ms": (i1 - i0) * 10, "labels": labels.get((i0, i1), []),
                     "snr_db": round(20 * math.log10(max(env[i0:i1]) / max(bgs, 1.0)), 1),
                     "freq_hz": _peak_freq(x[i0 * hop: i0 * hop + 1024]),
                     "fp": _bands(x[i0 * hop: min(i1 * hop, i0 * hop + 2048)],
                                  x[max(0, i0 * hop - 2048 - pad * hop): max(0, i0 * hop - pad * hop)]),
                     "s0": s0, "s1": s1})
    # group by timbre (band-energy cosine) and length
    groups = []
    for r in rows:
        for g in groups:
            ref = g[0]
            cos = sum(p * q for p, q in zip(r["fp"], ref["fp"]))
            if cos > a.similar and abs(r["length_ms"] - ref["length_ms"]) <= 0.35 * max(r["length_ms"], ref["length_ms"]):
                g.append(r)
                break
        else:
            groups.append([r])
    groups.sort(key=len, reverse=True)

    def on_beat(g):
        """Hits on whole multiples of one interval are the music's drums."""
        ts = sorted(r["t"] for r in g)
        gaps = [b - a_ for a_, b in zip(ts, ts[1:]) if b - a_ > 0.05]
        if len(gaps) < 3:
            return False
        unit = min(gaps)
        return unit > 0.2 and sum(1 for d in gaps if abs(d / unit - round(d / unit)) < 0.06) >= 0.85 * len(gaps)
    table = []
    for n, g in enumerate(groups, 1):
        best = max(g, key=lambda r: r["snr_db"])
        name = f"sfx{n:02d}_x{len(g)}_{best['length_ms']}ms.wav"
        _write_wav(out / name, x[best["s0"]:best["s1"]])
        if a.all_takes and len(g) > 1:
            for k, r in enumerate(g):
                _write_wav(out / f"sfx{n:02d}_take{k + 1}.wav", x[r["s0"]:r["s1"]])
        table.append({"sound": n, "file": str(out / name), "occurrences": len(g),
                      "times": [r["t"] for r in g][:12], "length_ms": best["length_ms"],
                      "freq_hz": best["freq_hz"], "best_snr_db": best["snr_db"],
                      "best_t": best["t"], "locator": _loc(meta, best["t"]),
                      "kind_hint": "on a regular beat: likely the music's drums, not an effect" if on_beat(g) else
                      "repeated effect" if len(g) >= 3 else ("pair" if len(g) == 2 else "one-off (could be "
                                                                                                    "a music hit or a jingle)")})
    found_marks = []
    if marks:  # name groups from the labelled moments
        for row, g in zip(table, groups):
            hits = [lbl for r in g for lbl in r["labels"]]
            if hits:
                row["label"] = max(set(hits), key=hits.count)
        for m_, (t, lbl) in enumerate(marks):
            if m_ in mark_hit:
                ts = round(base + mark_hit[m_][0] * 0.01, 2)
                found_marks.append({"label": lbl, "t_label": t, "t_sound": ts,
                                    "sound": next(row["sound"] for row, g in zip(table, groups)
                                                  if any(r["t"] == ts for r in g))})
        unmatched = [lbl for m_, (t, lbl) in enumerate(marks) if m_ not in mark_hit]
        lags = sorted(f["t_sound"] - f["t_label"] for f in found_marks)
        log(f"--events: {len(found_marks)}/{len(marks)} moments have a sound within {a.event_tol} s"
            + (f"; sounds start {lags[len(lags) // 2]:+.2f} s from the labels (median): mind that lag when timing "
               "the rebuild" if lags and abs(lags[len(lags) // 2]) >= 0.1 else "")
            + (f". None near: {', '.join(unmatched)} (a silent event, masked by music, or a label off by more than "
               f"--event-tol)" if unmatched else ""))
    if a.sheet:  # for an agent that can't listen
        video = resolve(a.video)[0] if a.video else (path if has_video else None)
        if video is None:
            log("no video stream in the source: the sheet shows spectrograms only (pass --video for frames)")
        _sfx_sheet(video, table, out, a.region, a.video_offset)
    atomic_write(out / "sfx.json", json.dumps({"source": meta, "window": [start, end], "offset_s": off,
                                               "background": music_note, "detected_on": str(path),
                                               **({"tuned": tuned} if tuned else {}),
                                               **({"events": found_marks} if marks else {}),
                                               **({"minus_loop": minus} if minus else {}), "sounds": table}, indent=1))
    log(music_note)
    if minus:
        log(f"minus-loop: removed {minus['removed_db']} dB")
    noisy = [t["sound"] for t in table if t["best_snr_db"] < 10]
    log(f"{len(rows)} events -> {len(table)} distinct sounds in {out}. Listen-check each against the moment "
        "in the video (`frames --start` at its time) and name it (jump, coin, hit...). "
        + (f"Sounds {noisy} sit under music (SNR < 10 dB): use them for timing and pitch, and look for a cleaner "
           "source (a quieter stretch, the sound-rip sites, another video)." if noisy else ""))
    emit(table, a.format)


def _sfx_sheet(video, table, out, region, offset=0.0):
    """One row per sound with frames at its best take and 0.2 s later, plus its spectrogram.
    No video (a stem, a wav) means spectrogram only."""
    from PIL import Image, ImageDraw
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for t in table:
            tt = float(t["best_t"]) + offset
            cells = []
            for k, dt in enumerate((0.0, 0.2) if video else ()):
                f = Path(td) / f"f{t['sound']}_{k}.png"
                subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(max(0, tt + dt)), "-i", str(video),
                                "-frames:v", "1", "-vf", "scale=-2:200", str(f)], capture_output=True, timeout=120)
                if f.exists():
                    im = Image.open(f).convert("RGB")
                    if region:
                        x, y, w, h = (int(v) for v in region.split(","))
                        k_ = 200 / Image.open(f).height
                        im = im.crop((int(x * k_), int(y * k_), int((x + w) * k_), int((y + h) * k_)))
                    cells.append(im)
            sp = Path(td) / f"s{t['sound']}.png"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", t["file"], "-lavfi",
                            "showspectrumpic=s=300x160:legend=0:scale=log", str(sp)], capture_output=True, timeout=60)
            if sp.exists():
                cells.append(Image.open(sp).convert("RGB"))
            rows.append((t, cells))
    if not rows:
        return
    W = max(sum(c.width for c in cells) + 8 * len(cells) for _, cells in rows) + 10
    H = sum((max((c.height for c in cells), default=40) + 22) for _, cells in rows)
    sheet = Image.new("RGB", (W, H), (20, 20, 20))
    d = ImageDraw.Draw(sheet)
    y = 0
    for t, cells in rows:
        d.text((4, y + 4), f"sfx{t['sound']:02d}  x{t['occurrences']}  {t['length_ms']} ms  ~{t['freq_hz']} Hz  "
                           f"{t['kind_hint']}  @ {t['locator'].rsplit('@', 1)[1].strip()}", fill=(255, 220, 0))
        x = 4
        for c in cells:
            sheet.paste(c, (x, y + 20))
            x += c.width + 8
        y += max((c.height for c in cells), default=40) + 22
    sheet.save(out / "sheet.png")
    log(f"{out}/sheet.png: each sound " + ("with the frame when it played, 0.2 s later, and " if video else "and ")
        + "its spectrogram (short bright vertical = click/hit; rising/falling band = sweep; stacked horizontal "
        "lines = tone/chime)")


def _audio_only(url: str) -> Path:
    """Just the audio track (a few MB), cached beside the videos."""
    _need("yt-dlp")
    MEDIA.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["yt-dlp", "--no-warnings", "--no-playlist", "-f", "ba/b", "-x", "--audio-format", "m4a",
                        "-o", str(MEDIA / "%(id)s.audio.%(ext)s"), "--print", "after_move:%(filepath)s", url],
                       capture_output=True, text=True, timeout=900, encoding="utf-8", errors="replace")
    path = (jsonl_lines(r.stdout.strip()) or [""])[-1]
    if not path or not Path(path).exists():
        raise RuntimeError((r.stderr.strip().splitlines() or ["download failed"])[-1])
    return Path(path)


def cmd_stems(a):
    """Split a recording into stems with Demucs (optional, `enhance.py setup --audio`)."""
    _need("ffmpeg")
    from enhance import heavy_python
    py = None
    for c in (sys.executable, heavy_python()):
        if c and subprocess.run([c, "-c", "import demucs"], capture_output=True).returncode == 0:
            py = c
            break
    if not py:
        sys.exit("Demucs is not installed: run `enhance.py setup --audio` (~600 MB, once)")
    src = _audio_only(a.src) if a.src.startswith(("http://", "https://")) else Path(a.src)
    if not src.exists():
        sys.exit(f"no such file: {a.src}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        clip = Path(td) / "clip.wav"
        cut = (["-ss", str(_secs(a.start))] if a.start else []) + (["-to", str(_secs(a.end))] if a.end else [])
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *cut, "-i", str(src), "-ac", "2", "-ar", "44100",
                        str(clip)], check=True)
        cmd = [py, "-m", "demucs", "-n", a.model, "-o", str(Path(td) / "sep"), "--filename", "{stem}.{ext}"]
        if a.two_stems:
            cmd += ["--two-stems", a.two_stems]
        r = subprocess.run([*cmd, str(clip)], capture_output=True, text=True, timeout=3600,
                           encoding="utf-8", errors="replace")
        if r.returncode:
            sys.exit(f"demucs failed: {(r.stderr or r.stdout).strip()[-400:]}")
        stems = []
        for f in sorted((Path(td) / "sep").rglob("*.wav")):
            dst = out / f.name
            shutil.move(str(f), dst)
            x = _pcm(dst, strict=False)  # near silence is a reading, not an error
            rms = math.sqrt(sum(v * v for v in x) / max(1, len(x))) if x else 0
            db = max(-120.0, 20 * math.log10(rms + 1e-9))
            stems.append({"stem": f.stem, "file": str(dst), "level_db": round(db, 1)})
    loud = max((s_["level_db"] for s_ in stems), default=0)
    for s_ in stems:
        s_["share"] = "main" if s_["level_db"] > loud - 6 else ("present" if s_["level_db"] > loud - 20 else "faint")
    # so sfx and lines can report source times
    atomic_write(out / "stems.json", json.dumps({"source": str(src), "start": a.start, "end": a.end,
                                                 "offset_s": _secs(a.start) or 0.0, "model": a.model, "stems": stems},
                                                indent=1))
    print(json.dumps({"source": str(src), "start": a.start, "end": a.end, "offset_s": _secs(a.start) or 0.0,
                      "model": a.model, "stems": stems,
                      "next": ("listen to each stem. Demucs was trained on songs: in game audio, hits, clicks and "
                               "thuds usually land in 'drums', voices in 'vocals', the tune in 'other'/'bass'. Run "
                               "`sfx` on the stem that holds the effects, and `loop` on the music stems. What a stem "
                               "holds is a separation, not the original file: record it as recovered, with the model "
                               "name in MANIFEST.json")}, indent=1))


def cmd_audio_check(a):
    """Triage recordings by audio before downloading video.
    Silent, quiet effects only, or a steady bed (music, the game's or the uploader's)."""
    _need("ffmpeg")
    rows = []
    for src in a.sources:
        if re.fullmatch(r"[\w-]{11}", src) and not Path(src).exists():  # bare YouTube id
            src = f"https://www.youtube.com/watch?v={src}"
        try:
            path = _audio_only(src) if src.startswith(("http://", "https://")) else Path(src)
            raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-t", str(a.seconds), "-i", str(path), "-vn", "-ac",
                                  "1", "-ar", str(SR), "-f", "s16le", "-"], capture_output=True, timeout=600).stdout
        except (RuntimeError, subprocess.SubprocessError) as e:
            rows.append({"source": src, "verdict": f"error: {e}"})
            continue
        x = array.array("h", raw)
        if not x:
            rows.append({"source": src, "verdict": "no audio track"})
            continue
        hop = int(SR * 0.05)
        env = sorted(math.sqrt(sum(v * v for v in x[i:i + hop]) / hop) for i in range(0, len(x) - hop, hop))
        peak = max(max(x), -min(x))
        floor, med, loud = env[len(env) // 10], env[len(env) // 2], env[int(len(env) * 0.95)]
        verdict = ("silent (no usable sound)" if peak < 60 else
                   "near silence with short events: effects with no music bed (best for sfx)" if med < 150 and loud > 600 else
                   "steady bed: music or speech under everything (check with common-audio whether it is the game's)"
                   if med > 800 else "quiet mixed audio")
        rows.append({"source": src, "file": str(path), "seconds": round(len(x) / SR, 1), "peak": peak,
                     "floor_rms": round(floor), "median_rms": round(med), "loud_rms": round(loud), "verdict": verdict})
    emit(rows, a.format)


ENV_RATE = 10  # loudness frames per second for common-audio
BANDS = ("lowpass=f=300", "bandpass=f=800:w=800", "bandpass=f=2000:w=1500", "highpass=f=3000")


def _dot(p, q):
    return math.sumprod(p, q) if hasattr(math, "sumprod") else sum(map(lambda u, v: u * v, p, q))


def _band_loudness(path, start=None, end=None, rate=ENV_RATE):
    """Log loudness per 1/rate s in four bands (mono, 8 kHz), one list per band.
    Survives re-encoding, resampling, gain changes and edits, so two uploads line up on it."""
    sr, nb = 8000, len(BANDS)
    seek = (["-ss", str(start)] if start is not None else []) + (["-to", str(end)] if end is not None else [])
    graph = (f"[0:a]aformat=channel_layouts=mono,aresample={sr},asplit={nb}" + "".join(f"[i{b}]" for b in range(nb))
             + "".join(f";[i{b}]{f}[o{b}]" for b, f in enumerate(BANDS))
             + ";" + "".join(f"[o{b}]" for b in range(nb)) + f"join=inputs={nb}:channel_layout=4.0[out]")
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", *seek, "-i", str(path), "-vn", "-filter_complex", graph,
                          "-map", "[out]", "-f", "s16le", "-"], capture_output=True, timeout=900).stdout
    x = array.array("h", raw[:len(raw) - len(raw) % (2 * nb)])
    hop = sr // rate
    out = []
    for b in range(nb):
        y = x[b::nb]
        out.append([math.log10(1.0 + _dot(y[i:i + hop], y[i:i + hop]) / hop) for i in range(0, len(y) - hop + 1, hop)])
    return out


def _window_matches(ea, eb, n, step):
    """Pearson-correlate each n-frame window of ea (every `step` frames) with every offset of eb, by FFT.
    Returns [(frame in A, frame in B, score, margin)], margin being the lead over the best elsewhere."""
    nb, la, m = len(ea), len(ea[0]), len(eb[0])
    N = 1 << (m + n).bit_length()
    FB = [_fft([complex(v) for v in band] + [0j] * (N - m)) for band in eb]
    ks = range(m - n + 1)
    var = [0.0] * len(ks)
    for band in eb:  # variance per stretch of B, from running sums
        s1, s2 = [0.0], [0.0]
        for v in band:
            s1.append(s1[-1] + v)
            s2.append(s2[-1] + v * v)
        for k in ks:
            var[k] += max(0.0, (s2[k + n] - s2[k]) - (s1[k + n] - s1[k]) ** 2 / n)
    sd_b = [math.sqrt(v) for v in var]
    flat = 0.04 * math.sqrt(n * nb)  # flatter than this (silence, held tone) matches anything
    wins = []
    for i in range(0, la - n + 1, step):
        w = []
        for band in ea:
            seg = band[i:i + n]
            mu = sum(seg) / n
            w.append([v - mu for v in seg])
        sd = math.sqrt(sum(_dot(c, c) for c in w))
        if sd >= flat:
            wins.append((i, [[v / sd for v in c] for c in w]))
    out = []
    for p in range(0, len(wins), 2):  # two real windows per complex FFT
        (i1, w1) = wins[p]
        (i2, w2) = wins[p + 1] if p + 1 < len(wins) else (None, [[0.0] * n] * nb)
        acc = [0j] * N
        for b in range(nb):
            FA = _fft([complex(u, v) for u, v in zip(w1[b], w2[b])] + [0j] * (N - n))
            acc = [s + q.conjugate() * f for s, q, f in zip(acc, FA, FB[b])]
        R = _fft(acc, inv=True)
        for i, sign in ((i1, 1), (i2, -1)):
            if i is None:
                continue
            sc = [((R[k].real if sign > 0 else -R[k].imag) / sd_b[k]) if sd_b[k] > flat else 0.0 for k in ks]
            k = max(ks, key=sc.__getitem__)
            far = max((v for j, v in enumerate(sc) if abs(j - k) > n // 2), default=0.0)
            out.append((i, k, sc[k], sc[k] - far))
    return sorted(out)


def cmd_common_audio(a):
    """Do two recordings share audio? If independent uploads share music, it's the game's.
    Edits move pieces around, so each window of A is searched in all of B, then joined into stretches."""
    _need("ffmpeg")
    missing = [f"{k} {x}" for k, x in (("A", a.a), ("B", a.b)) if not x.startswith("http") and not Path(x).exists()]
    if missing:
        sys.exit(f"no such file: {'; '.join(missing)}")
    pa = _audio_only(a.a) if a.a.startswith("http") else Path(a.a)
    pb = _audio_only(a.b) if a.b.startswith("http") else Path(a.b)
    a0 = _secs(a.a_start) or 0.0
    a1 = _secs(a.a_end) if a.a_end else a0 + a.max_a
    ea, eb = _band_loudness(pa, a0 or None, a1), _band_loudness(pb, None, a.max_b)
    r = ENV_RATE
    n, step = max(20, int(a.window * r)), max(1, int((a.hop or a.window / 2) * r))
    if len(ea[0]) < n or len(eb[0]) < n:
        empty = [k for k, e in (("A", ea), ("B", eb)) if not e[0]]
        sys.exit(f"too little audio: A has {len(ea[0]) / r:.1f} s, B {len(eb[0]) / r:.1f} s; the window is {n / r:.1f} s"
                 + (f" ({' and '.join(empty)}: no audio stream ffmpeg can decode)" if empty else ""))
    wins = _window_matches(ea, eb, n, step)
    if not wins:
        sys.exit("A is flat (silent or one held sound) in every window: pick another --a-start")
    # needs a good score AND a clear win over the rest of B,
    # since a steady beat matches itself everywhere
    segs = []
    for i, k, s, mg in wins:
        if s < a.min_score - 0.05:
            continue
        g = segs[-1] if segs else None
        if g and i - g["i1"] <= step and abs((k - i) - g["shift"]) <= max(2, r // 4):
            g["i1"] = i
            g["w"].append((s, mg))
        else:
            segs.append({"i0": i, "i1": i, "shift": k - i, "w": [(s, mg)]})
    keep = []
    for g in segs:
        sc, mg = [s for s, _ in g["w"]], [m_ for _, m_ in g["w"]]
        mean, mmg, ms = sum(sc) / len(sc), sum(mg) / len(mg), a.min_score
        # fewer windows must score higher (chance matches in long audio)
        if mmg >= 0.1 and (len(sc) >= 3 and mean >= ms + 0.05 or len(sc) == 2 and mean >= ms + 0.15) or \
                len(sc) == 1 and mean >= ms + 0.15 and mmg >= 0.15:
            keep.append({"a": f"{a0 + g['i0'] / r:.1f}-{a0 + (g['i1'] + n) / r:.1f}",
                         "b": f"{(g['i0'] + g['shift']) / r:.1f}-{(g['i1'] + g['shift'] + n) / r:.1f}",
                         "b_minus_a_s": round(g["shift"] / r - a0, 1), "windows": len(sc),
                         "score": round(sum(sc) / len(sc), 2), "margin": round(sum(mg) / len(mg), 2)})
    keep.sort(key=lambda g: (-g["windows"], -g["score"]))
    strong = [g for g in keep if g["windows"] >= 2 or g["score"] >= 0.9 and g["margin"] >= 0.2]
    shifts = {round(g["b_minus_a_s"]) for g in strong}
    verdict = ((f"shared audio: {len(strong)} stretches of A recur in B" + (" at different shifts (edited uploads: "
               "cut, reordered or padded)" if len(shifts) > 1 else "") if len(strong) > 1 else
               "shared audio: one stretch of A recurs in B") + ". The same sounds in independent recordings are the "
               "game's own" if strong else
               "weak: a lone window matched; check it (`frames`/listen at both times) or try --window 8"
               if keep else "no shared audio found in any window")
    res = {"a": str(pa), "b": str(pb), "verdict": verdict, "window_s": round(n / r, 1), "hop_s": round(step / r, 1),
           "windows_tested": len(wins), "stretches": keep[:12],
           # where in B each window fits best, and by what margin
           ("matched_windows" if keep else "best_windows"): [
               f"A {a0 + i / r:.1f} = B {k / r:.1f}  score {s:.2f}  margin {mg:.2f}"
               for i, k, s, mg in (sorted(wins) if keep else sorted(wins, key=lambda w: -w[2]))
               if not keep or s >= a.min_score - 0.05 and any(
                   abs(round(k / r - i / r - a0, 1) - g["b_minus_a_s"]) <= 0.3 and
                   float(g["a"].split("-")[0]) <= a0 + i / r <= float(g["a"].split("-")[1]) for g in keep)][:20 if keep else 3]}
    if len(keep) > 12:
        res["stretches_note"] = f"{len(keep) - 12} more stretches not listed"
    if not keep:
        res["note"] = ("the windows compare loudness in four bands; a voice-over or music added by one uploader hides "
                       "shared game audio: try --a-start/--a-end on a stretch without narration")
    print(json.dumps(res, indent=1))


def cmd_lines(a):
    """One clip per caption cue, trimmed to the speech. Needs `community.py yt <id> --timed DIR`."""
    _need("ffmpeg")
    import re as _re
    path, meta = resolve(a.src)
    off = _stem_offset(path, a.offset)  # a stem starts `off` s into the source
    cues = json.loads(Path(a.cues).read_text(encoding="utf-8"))
    if a.merge_gap:  # auto-captions split sentences, join them back
        merged = []
        for c in cues:
            if merged and c["start"] - merged[-1]["end"] <= a.merge_gap and c["end"] - merged[-1]["start"] <= a.span \
                    and not merged[-1]["text"].rstrip().endswith((".", "!", "?")):
                merged[-1] = {"start": merged[-1]["start"], "end": c["end"], "text": merged[-1]["text"] + " " + c["text"]}
            else:
                merged.append(dict(c))
        cues = merged
    if a.match:
        cues = [c for c in cues if _re.search(a.match, c["text"], _re.I)]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows, quiet, outside = [], [], []
    for i, c in enumerate(cues[: a.max], 1):
        t0, t1 = max(0.0, c["start"] - a.pad, off), c["end"] + a.pad
        if t1 <= t0:
            outside.append(c["start"])  # before the stem starts
            continue
        x = _pcm(path, t0 - off, t1 - off, strict=False)
        if not x:
            outside.append(c["start"])  # past the end of the file
            continue
        if _p95_rms(x) < SILENT_RMS:  # loud part, not median, since speech has pauses
            quiet.append(c["start"])
            continue
        hop = int(SR * 0.02)
        env = [math.sqrt(sum(v * v for v in x[k:k + hop]) / hop) for k in range(0, len(x) - hop, hop)]
        if not env:
            continue
        thr = max(max(env) * 0.12, 250)  # trim only real quiet, music under speech stays
        on = [k for k, v in enumerate(env) if v > thr]
        if on:  # keep a little air
            s0, s1 = max(0, on[0] - 3) * hop, min(len(x), (on[-1] + 5) * hop)
            x = x[s0:s1]
            t0 += s0 / SR
        slug = _re.sub(r"[^a-z0-9]+", "-", c["text"].lower()).strip("-")[:40] or "line"
        f = out / f"line{i:03d}_{slug}.wav"
        _write_wav(f, x)
        rows.append({"line": i, "file": str(f), "text": c["text"], "t": round(t0, 2), "seconds": round(len(x) / SR, 2),
                     "locator": _loc(meta, t0)})
    if outside:
        log(f"{len(outside)} cue(s) fall outside the audio (first at {outside[0]}s): " +
            ("check --offset (seconds the file starts into the source)" if off or path.stem.lower() in STEM_NAMES
             else "is SRC a stem or a clip? pass --offset <seconds it starts into the source>"))
    if quiet:
        log(f"{len(quiet)} cue(s) silent in this audio (95th-percentile RMS < {SILENT_RMS}; first at {quiet[0]}s)")
    if not rows and (quiet or outside):
        sys.exit("no audible speech at any cue: " + ("the audio is silent at every cue time" if quiet and not outside
                 else "the cue times do not fall inside this file") + ". A stem cut with `stems --start` needs "
                 "--offset (or its stems.json beside it); otherwise use another recording.")
    atomic_write(out / "lines.json", json.dumps({"source": meta, "offset_s": off, "lines": rows}, indent=1))
    log(f"{len(rows)} lines cut to {out}. Captions are machine-made: correct the text against the video, and check "
        "each clip's start/end on a frame sheet (`frames --start <t>`). Music under speech stays in the clip.")
    emit(rows, a.format)


def _stack_cycles(path, start, end, period, offset):
    """Per-sample median of every full loop cycle, aligned to the first (+-50 ms).
    Narration or effects over any one cycle drop out."""
    rate = 44100
    seek = ["-ss", str(start or 0)] + (["-to", str(end)] if end is not None else [])
    st = array.array("h", subprocess.run(["ffmpeg", "-loglevel", "error", *seek, "-i", str(path), "-vn", "-ac", "2",
                                          "-ar", str(rate), "-f", "s16le", "-"], capture_output=True, timeout=900).stdout)
    mono = [(st[i] + st[i + 1]) / 2 for i in range(0, len(st) - 1, 2)]
    L, p0 = round(period * rate), round(offset * rate)
    ref = mono[p0:p0 + min(L, 3 * rate)]  # 3 s is enough to place the others
    placed, skipped, k = [(0, p0)], 0, 1
    while True:
        per = (placed[-1][1] - p0) / placed[-1][0] if len(placed) > 1 else L  # refined as cycles get placed
        at = p0 + round(k * per)
        if at + L - int(0.05 * rate) > len(mono):
            break
        # wider at first, a 40 ms-frame period drifts ~20 ms/cycle
        pos, sc = _align(ref, mono, at, int((0.05 + (0.025 * k if len(placed) == 1 else 0)) * rate))
        if pos is None or pos + L > len(mono):
            break
        if sc >= 0.3:
            placed.append((k, pos))
        else:  # something drowns the music in this cycle
            skipped += 1
        k += 1
    if len(placed) > 1:
        L = round((placed[-1][1] - p0) / placed[-1][0])
    # music restarts off-grid each game, round or menu,
    # so search the envelope for the opening, then place exactly
    hop = rate // 50
    env = [math.sqrt(sum(v * v for v in mono[i:i + hop]) / hop) for i in range(0, len(mono) - hop, hop)]
    renv = env[p0 // hop: (p0 + len(ref)) // hop]
    found = [p for _, p in placed]
    if len(renv) > 10:
        scores = [(_ncc(renv, env[j:j + len(renv)]), j) for j in range(0, len(env) - len(renv))]
        tried = []
        for sc_, j in sorted(scores, reverse=True):
            if sc_ < 0.6:
                break
            if any(abs(j * hop - f) < 0.8 * L for f in found) or any(abs(j * hop - t) < rate for t in tried):
                continue  # 1 try/s near a miss, placed cycles claim their length
            tried.append(j * hop)
            pos, sc = _align(ref, mono, j * hop, int(0.06 * rate))
            if pos is not None and sc >= 0.3 and pos + L <= len(mono) and all(abs(pos - f) >= 0.8 * L for f in found):
                found.append(pos)
                placed.append((None, pos))
    skipped = max(0, skipped - (len(placed) - len([1 for k_, _ in placed if k_ is not None])))
    starts = sorted(p for _, p in placed if p + L <= len(mono))
    cyc = [st[2 * p: 2 * (p + L)] for p in starts]
    m = len(cyc)
    if m == 1:
        med = list(cyc[0])
    elif m % 2:
        med = [sorted(v)[m // 2] for v in zip(*cyc)]
    else:
        med = [(lambda s_: (s_[m // 2 - 1] + s_[m // 2]) // 2)(sorted(v)) for v in zip(*cyc)]
    return med, rate, {"cycles_stacked": m, "cycle_starts_s": [round((start or 0) + p / rate, 3) for p in starts],
                       "cycles_skipped": skipped, "period_refined_s": round(L / rate, 4)}


def cmd_loop(a):
    _need("ffmpeg")
    path, meta = resolve(a.src)
    start, end = _secs(a.start), _secs(a.end)
    x = _pcm(path, start, end)
    # coarse pitch spectrogram (24 notes, 40 ms frames).
    # pitch tells a melody from the same rhythm
    xs = x[::2]  # 11 kHz is plenty for melody
    sr2, hop = SR / 2, int(SR / 2 * 0.04)
    freqs = [110 * 2 ** (i / 4) for i in range(24)]  # 110 Hz .. ~6 kHz in quarter-octave steps
    ks = [2 * math.cos(2 * math.pi * f / sr2) for f in freqs]
    frames = []
    for i in range(0, len(xs) - hop, hop):
        seg = xs[i:i + hop]
        row = []
        for k in ks:
            s1 = s2 = 0.0
            for v in seg:
                s1, s2 = v + k * s1 - s2, s1
            row.append(math.log1p(max(0.0, s1 * s1 + s2 * s2 - k * s1 * s2) / 1e6))
        nrm = math.sqrt(sum(v * v for v in row)) or 1
        frames.append([v / nrm for v in row])
    lo, hi = int(a.min_s / 0.04), min(int(a.max_s / 0.04), len(frames) // 2)
    if hi <= lo:
        sys.exit(f"window too short: give at least {2 * a.min_s:.0f}s (two repeats of the shortest loop)")
    cache = {}
    def score(lag_):
        if lag_ not in cache:
            n = len(frames) - lag_
            cache[lag_] = sum(sum(p * q for p, q in zip(frames[i], frames[i + lag_])) for i in range(n)) / n
        return cache[lag_]
    # start below the minimum, a peak needs 2 lags each side
    scores = [(score(lag_), lag_) for lag_ in range(max(1, lo - 3), hi + 1)]
    # real peaks only, range edges score high near a peak
    peaks_ = [(s_, l_) for i, (s_, l_) in enumerate(scores) if 2 <= i < len(scores) - 2
              and s_ >= max(v for v, _ in scores[i - 2:i + 3])] or scores
    best = max(peaks_)[0]
    # shortest near-best lag, since 2x and 3x also match
    best, lag = next((s_, l_) for s_, l_ in peaks_ if s_ >= best - 0.01)
    base = sum(s_ for s_, _ in scores) / len(scores)
    confidence = (best - base) / max(1e-6, 1 - base)
    y0, y2 = score(lag - 1), score(lag + 1)  # parabola for the period between 40 ms frames
    den = y0 - 2 * best + y2
    period = (lag + (0.5 * (y0 - y2) / den if den < 0 else 0.0)) * 0.04
    peaks = None
    if confidence < 0.3:
        # speech or effects dilute this, check 2x and 3x peaks
        def peak(l_):
            if l_ + 2 > len(frames) - 50:  # need 2 s of overlap to score at all
                return None
            top = max(range(max(1, l_ - 2), l_ + 3), key=score)
            span = max(8, lag // 4)
            around = sorted(score(l2) for l2 in range(max(1, l_ - span), min(len(frames) - 50, l_ + span) + 1, 2)
                            if abs(l2 - l_) > 4)
            if len(around) < 4:
                return None
            med = around[len(around) // 2]
            return top if score(top) >= around[-1] and (score(top) - med) / max(1e-6, 1 - med) > 0.1 else None
        hits = [peak(m * lag) for m in (1, 2, 3)]
        if hits[0] and hits[1]:
            peaks = [round(h * 0.04, 2) for h in hits if h]
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    seg_start = (start or 0) + a.offset
    if confidence < a.min_confidence and not peaks:  # no repetition, don't save a fake loop
        print(json.dumps({"period_s": None, "confidence": round(confidence, 2), "out": None,
                          "verdict": "no loop found: the stretch holds speech, effects or non-repeating music. Pick a "
                                     "stretch of plain music (a menu, a pause, a gap between commentary lines found "
                                     "with `community.py yt --timed`), or a longer one."}, indent=1))
        return
    res = {"period_s": round(period, 2), "match": round(best, 3), "confidence": round(confidence, 2), "out": str(out),
           "locator": _loc(meta, seg_start)}
    if a.stack:
        med, rate, info = _stack_cycles(path, start, end, period, a.offset)
        res.update(info)
        wav = out if out.suffix.lower() == ".wav" else Path(tempfile.mkdtemp()) / "loop.wav"
        _write_wav(wav, med, rate, 2)
        if wav != out:
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), str(out)], check=True, timeout=300)
        if info["cycles_stacked"] < 3:
            res["stack_note"] = (f"only {info['cycles_stacked']} full cycle(s) in the window: a median needs 3+ to "
                                 "outvote what lies on top; give a longer window")
    else:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(seg_start), "-t", f"{period:.3f}", "-i",
                        str(path), "-vn", str(out)], check=True, timeout=300)
    if peaks:
        res["repeats"] = "peaks at " + ", ".join(f"{p_:g}" for p_ in peaks) + " s"
        res["note"] = ("the overall score is weak (speech or effects over the music), but the period recurs at its "
                       "multiples: a real loop")
    elif confidence < 0.3:
        res["warning"] = "weak repetition: the stretch may hold effects or speech, or the loop is longer than --max-s"
    res["next"] = ("listen to the loop played twice back to back; if the seam clicks, nudge --offset to a beat. "
                   "BPM and key can be confirmed by ear against the video."
                   + ("" if a.stack else " Narration over it? --stack takes the median of every cycle in the window."))
    print(json.dumps(res, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--format", choices=["jsonl", "json", "md"], default="jsonl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("get", help="download a video once into dossier/media/")
    s.add_argument("url")
    s.add_argument("--size", "--height", dest="height", type=int, default=720,
                   help="short side in px (default 720: 1280x720 landscape, 720x1280 portrait); --height is the old name")
    s.add_argument("--force", action="store_true", help="replace the local copy (e.g. to get it again at another --size)")
    s.set_defaults(fn=cmd_get)
    s = sub.add_parser("scenes", help="every distinct screen with timestamps, plus a contact sheet")
    s.add_argument("src")
    s.add_argument("--out", help="default dossier/screens/scenes/<video id or file name>/")
    s.add_argument("--rate", type=float, default=4, help="samples per second (default 4)")
    s.add_argument("--same", type=float, default=10.0, help="thumbnail distance (0-255) under which screens are the same")
    s.add_argument("--still", type=float, default=3.0, help="distance under which the screen counts as holding still")
    s.add_argument("--hold", type=int, default=2, help="samples a screen must hold still to count")
    s.add_argument("--cut", type=float, default=30.0, help="jump between samples that counts as a hard cut")
    s.add_argument("--min-gap", type=float, default=0.5, help="seconds between hard-cut picks")
    s.add_argument("--moving", type=float, default=4.0,
                   help="seconds of continuous change (gameplay, scrolling) after which one sample is kept")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--height", type=int, help="scale frames to this height (default: the source's own pixels)")
    s.add_argument("--region", help="x,y,w,h of the game window (source pixels, or at --height): detection and the "
                                    "sheet look only there")
    s.add_argument("--cols", type=int, default=6)
    s.set_defaults(fn=cmd_scenes)
    s = sub.add_parser("gif", help="frames and per-frame delays of a GIF/APNG/WebP")
    s.add_argument("src")
    s.add_argument("--out", required=True)
    s.add_argument("--frames", action="store_true", help="also list every frame")
    s.set_defaults(fn=cmd_gif)
    s = sub.add_parser("sprite", help="cut moving objects out of frames as transparent PNGs")
    s.add_argument("images", nargs="+", help="frames (a burst from `community.py frames --fps`) or a folder")
    s.add_argument("--out", required=True)
    s.add_argument("--region", help="x,y,w,h: the play field only (skip HUD and borders)")
    s.add_argument("--bg", default="auto", help="auto (median of frames), #hex key colour, or a clean frame")
    s.add_argument("--tol", type=int, default=28, help="difference from the background that counts as sprite")
    s.add_argument("--step", type=int, default=2)
    s.add_argument("--min-pixels", type=int, default=40)
    s.add_argument("--max-objects", type=int, default=3)
    s.add_argument("--follow", help="x,y (frame coords) of the object to cut; it is then tracked frame to frame")
    s.add_argument("--jump", type=float, default=40, help="px an object may move between frames when following")
    s.add_argument("--soften", action="store_true", help="grow the mask 1px (keeps anti-aliased edges)")
    s.add_argument("--native-width", type=float, help="the original's logical screen width in px (gives the exact scale)")
    s.add_argument("--scale", type=float, help="video px per native px, if known")
    s.add_argument("--vector", action="store_true",
                   help="smooth (Flash/vector/3D) art: keep full resolution, no pixel or palette reduction")
    s.add_argument("--pose-tol", type=float, default=22, help="how different two takes may be and still be one pose")
    s.add_argument("--colors", type=int, default=16, help="palette size of the native-resolution sprites")
    s.add_argument("--list", action="store_true")
    s.set_defaults(fn=cmd_sprite)
    s = sub.add_parser("cut", help="cut static or smooth art out of frames (edge flood fill, full resolution)")
    s.add_argument("images", nargs="+", help="one or more frames showing the same thing in the same place")
    s.add_argument("--box", required=True, help="x,y,w,h around the object, with a margin of background")
    s.add_argument("--out", required=True)
    s.add_argument("--tol", type=int, default=24, help="colour step allowed between neighbouring background pixels")
    s.add_argument("--max-drift", type=int, default=150, help="how far the background may drift from the edge colour")
    s.add_argument("--seed", action="append", help="x,y of an enclosed background pocket to clear too (repeatable)")
    s.add_argument("--key", help="#hex: also clear every pixel near this colour (flat backgrounds, holes in the art)")
    s.add_argument("--plate", help="a clean background image (same framing, e.g. from `merge`): cut by difference "
                                   "from it, for patterned or busy backgrounds")
    s.add_argument("--drop-pointer", action="store_true",
                   help="remove a desktop mouse pointer (white arrow or hand, dark outline) caught in the cut")
    s.add_argument("--feather", action="store_true", help="soften the edge slightly")
    s.add_argument("--erode", type=int, default=1, help="px of edge fringe to drop (0 keeps every edge pixel)")
    s.add_argument("--keep-islands", action="store_true", help="keep detached pieces (default: largest piece only)")
    s.add_argument("--fill-holes", type=int, help="fill enclosed holes smaller than N px (--plate default: 0.5%% of "
                   "the box; 0 keeps every hole)")
    s.add_argument("--largest", action="store_true", help="--plate: keep only the largest piece (a figure, not text)")
    s.add_argument("--smooth", type=int, help="--plate: px to open and close the mask by (drops specks, shuts "
                   "cracks; default 1 per 250 px of the box's short side; 0 off)")
    s.add_argument("--trim", action="store_true", help="crop to what is left")
    s.add_argument("--width", type=int, help="resize to this width (the original's logical size)")
    s.set_defaults(fn=cmd_cut)
    s = sub.add_parser("merge", help="fill holes / make clean plates: per pixel, what most frames show")
    s.add_argument("images", nargs="+", help="frames of the same picture (same framing) or a folder")
    s.add_argument("--out", required=True)
    s.add_argument("--box", help="x,y,w,h to merge only this part")
    s.add_argument("--how", choices=["mode", "median", "sharpest"], default="mode",
                   help="mode: the colour seen most often (moving things vanish; best for pixel art and flat "
                        "backgrounds); median: average-ish; sharpest: the clearest single frame (smooth or animated art)")
    s.add_argument("--ignore", action="append", help="#hex of hole/cover colour to skip (repeatable)")
    s.add_argument("--tol", type=int, default=30)
    s.add_argument("--quant", type=int, default=12, help="colour bucket size for mode")
    s.add_argument("--scroll", choices=["auto"], help="side-scrolling background: track the camera's x and build a "
                                                      "panorama (frames must be in order)")
    s.add_argument("--band", help="y,h of the horizontal band that scrolls (with --scroll; default the whole frame)")
    s.add_argument("--layers", choices=["auto"], help="parallax: split the frame (or --band) into strips, group "
                                                      "strips scrolling at one speed into layers, one panorama each")
    s.add_argument("--strip", type=int, default=8, help="strip height in px for --layers")
    s.add_argument("--fps", type=float, help="frames per second of the burst, when the names carry no @MMmSS_ss "
                                             "time: gives speeds in px/s")
    s.add_argument("--dup-tol", type=float, default=1.5,
                   help="mean abs difference under which a frame repeats the previous one and is dropped")
    s.set_defaults(fn=cmd_merge)
    s = sub.add_parser("count", help="how many moving objects are on screen at once (per frame and overall)")
    s.add_argument("images", nargs="+", help="frames in order (a burst from `community.py frames --fps`) or a folder")
    s.add_argument("--region", required=True, help="x,y,w,h: the play field (a stretch where the screen does not scroll)")
    s.add_argument("--bg", default="auto", help="auto (per-pixel median of the frames) or a clean background frame")
    s.add_argument("--tol", type=int, default=28, help="difference from the background that counts as an object")
    s.add_argument("--min-area", type=int, default=60, help="smallest object in px")
    s.add_argument("--join", type=int, default=3, help="px gap bridged inside one object")
    s.add_argument("--exclude-static", action="store_true",
                   help="drop objects in the same place in most frames (the player, HUD readouts)")
    s.add_argument("--dup-tol", type=float, default=1.5, help="mean abs difference under which a frame is a repeat")
    s.set_defaults(fn=cmd_count)
    s = sub.add_parser("stage", help="trim borders and resize frames to the original stage size")
    s.add_argument("images", nargs="+")
    s.add_argument("--size", required=True, help="the original stage, e.g. 640x480 (SWF header, wiki, measured)")
    s.add_argument("--box", help="x,y,w,h of the stage inside the frame (when trimming is not enough)")
    s.add_argument("--tol", type=int, default=18)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_stage)
    s = sub.add_parser("tiles", help="puzzle boards: cut a lattice of cells and merge each tile type into one sprite")
    s.add_argument("images", nargs="+", help="frames of the board (same framing)")
    s.add_argument("--origin", required=True, help="x,y centre of the top-left cell")
    s.add_argument("--step", required=True, help="dx,dy between cell centres")
    s.add_argument("--cols", type=int, required=True)
    s.add_argument("--rows", type=int, required=True)
    s.add_argument("--size", required=True, help="w,h of one tile")
    s.add_argument("--odd-shift", type=float, default=0.0, help="x offset of odd rows (hex/bubble grids: dx/2)")
    s.add_argument("--snap", type=int, default=3, help="px each cell may move to centre on its tile")
    s.add_argument("--mask", choices=["square", "circle"], default="square")
    s.add_argument("--tol", type=float, default=22, help="how different two cells may be and be one tile type")
    s.add_argument("--empty", help="#hex of an empty cell's background, to skip empty cells")
    s.add_argument("--bg-plate", help="the board with no tiles (`footage.py merge` of frames, or an empty-board frame): "
                                      "cells that match it are empty")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_tiles)
    s = sub.add_parser("hud", help="one screen region over time: scores, timers, floaters")
    s.add_argument("src")
    s.add_argument("--region", required=True, help="x,y,w,h in the source video's pixels (or at --height if given)")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--fps", type=float, default=4)
    s.add_argument("--height", type=int, help="scale the video to this height first (default: source pixels)")
    s.add_argument("--same", type=float, default=3.0,
                   help="change needed to keep a new state: pixels must differ by 20x this in grey (lower keeps more)")
    s.add_argument("--cols", type=int, default=8)
    s.add_argument("--max-sheets", type=int, default=3, help="most contact sheets to write (more states are sampled "
                   "evenly; hud.json lists all)")
    s.add_argument("--measure", choices=["bar", "marker"],
                   help="per sample: a bar's filled length along the region's long axis, or a marker's position")
    s.add_argument("--fill", help="#hex of the bar/marker colour (default: the most saturated common colour)")
    s.add_argument("--fill-tol", type=int, default=90, help="colour distance (sum over RGB) still counted as fill")
    s.add_argument("--out", default="dossier/screens/hud")
    s.set_defaults(fn=cmd_hud)
    s = sub.add_parser("sfx", help="cut and group sound effects from gameplay audio")
    s.add_argument("src")
    s.add_argument("--out", default="dossier/media/sfx")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--ratio", type=float, help="effect level vs. the rolling background level (default 3, or 2 over "
                   "a steady music bed)")
    s.add_argument("--floor", type=float, default=400, help="minimum RMS to count (ignores hiss)")
    s.add_argument("--min-ms", type=int, default=40)
    s.add_argument("--max-ms", type=int, default=2500)
    s.add_argument("--pad-ms", type=int, default=30)
    s.add_argument("--similar", type=float, default=0.9, help="timbre similarity to group takes (0-1)")
    s.add_argument("--max", type=int, default=150)
    s.add_argument("--all-takes", action="store_true", help="save every take, not only the cleanest")
    s.add_argument("--events", help="label groups from known moments: '12.4:jump,15.0:coin,31.2:wrong answer'")
    s.add_argument("--sheet", action="store_true",
                   help="write sheet.png: each sound beside the video frame when it played and its spectrogram")
    s.add_argument("--region", help="x,y,w,h of the play field for the sheet's frames (source pixels)")
    s.add_argument("--video", help="the video to take the sheet's frames from (when SRC is a stem or a wav)")
    s.add_argument("--video-offset", type=float, default=0.0,
                   help="seconds to add to sound times to find them in --video (not needed for a stem whose "
                        "--offset or stems.json already makes the times source times)")
    s.add_argument("--minus-loop", help="a music loop (from `loop`) to subtract before detecting effects")
    s.add_argument("--offset", type=float,
                   help="seconds SRC starts into the source (a stem cut with `stems --start`): --start/--end and "
                        "reported times are then source times (default: read from stems.json beside the stem, else 0)")
    s.add_argument("--event-tol", type=float, default=0.5, help="seconds around each --events moment searched for "
                   "its sound (labels read off video frames often run a few frames early)")
    s.add_argument("--no-stems", action="store_true", help="use the full mix even when a `stems` split of this "
                   "recording exists (default: detect on its drums stem)")
    s.set_defaults(fn=cmd_sfx)
    s = sub.add_parser("audio-check", help="triage recordings by their audio (downloads audio only)")
    s.add_argument("sources", nargs="+", help="video URLs or local files")
    s.add_argument("--seconds", type=float, default=300, help="how much of each to analyse")
    s.set_defaults(fn=cmd_audio_check)
    s = sub.add_parser("common-audio", help="do two recordings share audio (is this music the game's own?)")
    s.add_argument("a", help="recording A (URL or file)")
    s.add_argument("b", help="recording B (URL or file)")
    s.add_argument("--a-start", help="search from here in A (mm:ss; default the start)")
    s.add_argument("--a-end", help="search up to here in A (default --a-start + --max-a)")
    s.add_argument("--window", type=float, default=5, help="seconds per window of A looked for in B (4-6 suits "
                   "edited uploads; longer for one long shared piece)")
    s.add_argument("--hop", type=float, help="seconds between windows (default half a window)")
    s.add_argument("--min-score", type=float, default=0.7, help="loudness-curve correlation a window needs to match")
    s.add_argument("--max-a", type=float, default=600, help="seconds of A to search")
    s.add_argument("--max-b", type=float, default=600, help="seconds of B to search")
    s.set_defaults(fn=cmd_common_audio)
    s = sub.add_parser("lines", help="cut narration/voice lines by caption timing (needs `community.py yt --timed`)")
    s.add_argument("src", help="the video (URL or the `get` copy), or a voice stem from `stems` (see --offset)")
    s.add_argument("--cues", required=True, help="<id>.cues.json from `community.py yt <id> --timed DIR`")
    s.add_argument("--match", help="regex: only cues whose text matches")
    s.add_argument("--pad", type=float, default=0.3)
    s.add_argument("--merge-gap", type=float, default=0.4, help="join cues closer than this into one line (0: off)")
    s.add_argument("--span", type=float, default=12, help="longest line, in seconds, when joining")
    s.add_argument("--max", type=int, default=400)
    s.add_argument("--offset", type=float,
                   help="seconds SRC starts into the source the cues were timed on (a stem cut with `stems --start`; "
                        "default: read from stems.json beside the stem, else 0)")
    s.add_argument("--out", default="dossier/media/lines")
    s.set_defaults(fn=cmd_lines)
    s = sub.add_parser("stems", help="split music, effects and voice apart (Demucs; optional install)")
    s.add_argument("src", help="a local audio/video file or a URL")
    s.add_argument("--start")
    s.add_argument("--end")
    s.add_argument("--out", default="dossier/media/stems")
    s.add_argument("--model", default="htdemucs")
    s.add_argument("--two-stems", help="split one stem from the rest, e.g. vocals or drums")
    s.set_defaults(fn=cmd_stems)
    s = sub.add_parser("loop", help="find and save one loop of background music")
    s.add_argument("src")
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--out", required=True, help="e.g. dossier/media/music/title-loop.wav")
    s.add_argument("--min-s", type=float, default=4)
    s.add_argument("--max-s", type=float, default=60)
    s.add_argument("--offset", type=float, default=0, help="seconds into the window where the saved loop starts")
    s.add_argument("--min-confidence", type=float, default=0.25)
    s.add_argument("--stack", action="store_true",
                   help="align every full cycle in the window and save their per-sample median (removes narration "
                        "or effects over any one cycle)")
    s.set_defaults(fn=cmd_loop)
    ws_arg = pop_ws_arg() if argv is None else None
    a = ap.parse_args(argv)
    if getattr(a, "empty", None):
        a.empty_rgb = tuple(int(a.empty.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    enter_ws(ws_arg)
    a.fn(a)


if __name__ == "__main__":
    from _common import use_heavy
    use_heavy("PIL")
    main()
