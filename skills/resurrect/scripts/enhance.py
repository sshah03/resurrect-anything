#!/usr/bin/env python3
"""Make small or blurry recovered art usable at full size, without just stretching it.

Which command depends on what the art was:
  native IMG --out FILE [--native-width N] [--colors N] [--zoom 4]
        Pixel art seen through a scaled, blurred or compressed screenshot or video. Finds the original
        pixel grid (fractional scales too), reads one colour per original pixel and snaps them to a
        palette. The result IS the original sprite at its true size (scale it up with nearest-
        neighbour, like the game did). Exact, nothing invented.
  stack TAKES... --out FILE [--scale 2]
        Several takes of the same thing, like a static screen across frames or a sprite crossing the
        screen. Aligns them to a fraction of a pixel and merges them on a finer grid. More data gives real
        detail. With identical takes it only removes compression noise, and it tells you which happened.
  upscale IMG... --out DIR [--kind auto|cartoon|photo|pixel] [--scale 4]
        Smooth art (Flash, painted, 3D renders, photos) with an AI upscaler (Real-ESRGAN, which `setup`
        installs). Sharp outlines and flat colour come back, but the model INVENTS fine detail, so
        small text turns to gibberish and faces can drift. Every run writes review.png with the tiles
        where the result differs most from the source, for you to look at. Pixel art goes to `native`.
  vector IMG --out FILE.svg
        Trace flat-colour art (logos, simple shapes) to SVG, sharp at any size. Gradients flatten into
        bands, so use it only where the original was flat.
  setup [--audio]
        Install the optional tools into the resurrect cache (~/.cache/resurrect, or $XDG_CACHE_HOME/resurrect,
        or $RESURRECT_CACHE). That's the Real-ESRGAN binary (~50 MB, checked against a pinned SHA-256) and
        a Python environment with Pillow, numpy, Playwright and its Chromium, yt-dlp and vtracer (~400 MB in
        all). The other scripts switch to that environment on their own when the Python running them lacks
        a package, so nothing goes into the system's Python. The environment is made with uv, or else with
        a Python 3.12+ found on PATH (the pinned packages need it), and Chromium gets a test start. The
        upscaler comes last. If its download fails it's skipped and the reason is printed. --audio adds
        Demucs for `footage.py stems` (~600 MB).

Order of preference: the original file (law 2) > the largest source (cut from the 1080p video, not a
downscaled copy) > native/stack (recovered) > upscale/vector (enhanced). Record enhanced files in
app/original/MANIFEST.json with "origin": "recovered", the source, and "enhanced": "<command, model>".
Keep the unenhanced cut next to it. Never upscale text. Re-set it in the period font (rebuild-guide).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # needed again after re-exec into the heavy env
from _common import cache_dir, enter_ws, heavy_python, pop_ws_arg, reexec  # noqa: E402

HOME = cache_dir()
HEAVY = HOME / "heavy"
ESRGAN_DIR = HOME / "esrgan"
ESRGAN_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-{}.zip"
# refuse a swapped or corrupted download before unpacking it
ESRGAN_SHA256 = {
    "macos": "e0ad05580abfeb25f8d8fb55aaf7bedf552c375b5b4d9bd3c8d59764d2cc333a",
    "ubuntu": "e5aa6eb131234b87c0c51f82b89390f5e3e642b7b70f2b9bbe95b6a285a40c96",
    "windows": "abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d",
}
# pinned so new releases can't break installs or results
PKGS = ["numpy==2.5.4", "pillow==13.0.0", "vtracer==0.6.15", "playwright==1.64.1",
        "yt-dlp"]  # unpinned, video sites break old releases within months
AUDIO_PKGS = ["demucs==4.1.0", "soundfile==0.14.0"]
NUMPY_CMDS = ("native", "stack", "upscale")
GRID_MIN = 0.2  # pixel art in video measured 0.24-0.41, smooth Flash 0.04-0.16
AUTO_MIN_SCALE = 2.4  # resized smooth art fakes a ~2 px grid (ringing)
MODELS = {"cartoon": "realesrgan-x4plus-anime", "photo": "realesrgan-x4plus"}


def _need_numpy():
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        hp = heavy_python()
        if hp and Path(sys.prefix).resolve() != HEAVY.resolve():
            reexec(hp, __file__)
        sys.exit("needs numpy and Pillow: run `enhance.py setup` (or pip install numpy pillow)")


def _need_vtracer():
    try:
        import vtracer  # noqa: F401
    except ImportError:
        hp = heavy_python()
        if hp and Path(sys.prefix).resolve() != HEAVY.resolve():
            reexec(hp, __file__)
        sys.exit("needs vtracer: run `enhance.py setup` (or pip install vtracer)")


def _esrgan_bin() -> str | None:
    name = "realesrgan-ncnn-vulkan.exe" if os.name == "nt" else "realesrgan-ncnn-vulkan"
    for c in (os.environ.get("RESURRECT_ESRGAN"), str(ESRGAN_DIR / name), shutil.which("realesrgan-ncnn-vulkan")):
        if c and Path(c).exists():
            return c
    return None


def _sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]


# ---------- native: the original pixel grid ----------

def _period(g, lo, hi):
    """Period and phase of the boundary comb in a 1-D edge profile.

    Multiples (2p, 3p) cancel out but p/2 can score almost as high, so strong multiples win."""
    import numpy as np
    x = np.arange(len(g))
    tot = g.sum() or 1.0
    rows = []
    for p in np.arange(lo, hi, 0.005):
        z = (g * np.exp(-2j * np.pi * x / p)).sum() / tot
        rows.append((abs(z), p, np.angle(z)))
    s, p, ang = max(rows)
    # blur and ringing add half-way edges, so p/2 looks best.
    # A strong multiple of it is the real period.
    for k in (2, 3, 2):
        near = [r for r in rows if abs(r[1] - k * p) <= 0.02 * k * p]
        if near:
            m = max(near)
            if m[0] >= 0.6 * s:
                s, p, ang = m
    b = (-ang * p / (2 * np.pi)) % p  # boundaries sit between index b and b+1, every p
    return float(p), float(b), float(s)


def _profiles(arr):
    """Edge profiles per axis.

    'edges' (1st difference) peaks at pixel borders when the game zoomed sharply before the video scaled.
    'kinks' (2nd difference) peaks at centres when smoothed straight from the small original."""
    import numpy as np
    lum = arr[..., :3].astype(float) @ [0.299, 0.587, 0.114]
    if arr.shape[2] == 4:
        lum = lum * (arr[..., 3] / 255.0) + 255 * (1 - arr[..., 3] / 255.0) * 0.5
    def peaks(d, ax, lo):
        # count each edge once, or big outlines drown the grid
        d = np.moveaxis(d, ax, 1)
        m = (d[:, 1:-1] > d[:, :-2]) & (d[:, 1:-1] >= d[:, 2:]) & (d[:, 1:-1] > lo)
        return np.concatenate([[0], m.sum(0), [0]]).astype(float)
    out = {}
    for ax, name in ((1, "x"), (0, "y")):
        out[name] = {"edges": peaks(np.abs(np.diff(lum, axis=ax)), ax, 8),
                     "kinks": peaks(np.abs(np.diff(lum, n=2, axis=ax)), ax, 8)}
    return out, lum.shape


def _boundary(mode, b, p):
    # kinks sit at centres, one index late; make them borders
    return b if mode == "edges" else (b + 0.5 - p / 2) % p


MIN_SIDE = 7  # periods scanned: 1.5 px to a quarter of the side


def _too_small(w, h, native_w=None, native_h=None):
    return (not native_w and w < MIN_SIDE) or (not native_h and h < MIN_SIDE)


def _grid(arr, native_w=None, native_h=None, square=True):
    import numpy as np
    prof, (h, w) = _profiles(arr)
    best = None
    for mode in ("edges", "kinks"):
        gx, gy = prof["x"][mode], prof["y"][mode]
        px = w / native_w if native_w else _period(gx, 1.5, min(24, w / 4))[0]
        py = h / native_h if native_h else _period(gy, 1.5, min(24, h / 4))[0]
        if square and not (native_w and native_h):
            # square pixels, so one period for both axes.
            # Ringing favours p/2 and fractional grids favour 2p/3,
            # so multiples are candidates too. Largest near-best wins.
            if native_w or native_h:
                cands = [px if native_w else py]
            else:
                cands = [c * k for c in (px, py) for k in (1, 1.5, 2, 3) if c * k <= min(24, w / 4, h / 4)]

            def both(c):
                return min(_period_phase(gx, c)[1], _period_phase(gy, c)[1])
            scored = []
            for c in cands:
                if native_w or native_h:
                    scored.append((both(c), c))
                    continue
                fine = [c * (1 + f) for f in np.arange(-0.02, 0.0201, 0.0025)]
                scored.append(max((both(x), x) for x in fine))
            top = max(scored)[0]
            px = py = max(c for sc, c in scored if sc >= 0.7 * top)
        bx, sx = _period_phase(gx, px)
        by, sy = _period_phase(gy, py)
        cand = ((px, _boundary(mode, bx, px), sx), (py, _boundary(mode, by, py), sy), mode)
        if best is None or min(sx, sy) > min(best[0][2], best[1][2]):
            best = cand
    return best


def _period_phase(g, p):
    import numpy as np
    x = np.arange(len(g))
    z = (g * np.exp(-2j * np.pi * x / p)).sum() / (g.sum() or 1.0)
    return float((-np.angle(z) * p / (2 * np.pi)) % p), float(abs(z))


def _centres(n, p, b):
    """Centres of whole cells on one axis. A boundary between i and i+1 sits at i+1."""
    start = (b + 1) % p
    if start > 0.4 * p:  # keep a partial edge cell if mostly visible
        start -= p
    cs = []
    c = start + p / 2
    while c - p * 0.1 < n:
        if c - p / 2 >= -0.4 * p and c + p / 2 <= n + 0.4 * p:
            cs.append(c)
        c += p
    return cs


def _palette(cols, counts, tol):
    import numpy as np
    order = np.argsort(-counts)
    pal = []
    for i in order:
        c = cols[i]
        if not pal or min(np.abs(np.array(pal) - c).sum(axis=1)) > tol:
            pal.append(c)
    return np.array(pal)


def _between(c, pal, tol):
    """Is colour c within tol of a palette colour, or of the line between two of them (a blend)?"""
    import numpy as np
    if not len(pal):
        return False
    if np.abs(pal - c).sum(-1).min() < tol:
        return True
    A, B = pal[:, None], pal[None]
    t = np.clip(((c - A) * (B - A)).sum(-1) / (((B - A) ** 2).sum(-1) + 1e-9), 0, 1)
    return bool((np.abs(c - (A + t[..., None] * (B - A))).sum(-1) < tol).any())


def cmd_native(a):
    _need_numpy()
    import numpy as np
    from PIL import Image
    im = Image.open(a.image).convert("RGBA")
    arr = np.asarray(im)
    if _too_small(im.width, im.height, a.native_width, a.native_height):
        sys.exit(f"{a.image} is {im.width}x{im.height} px: too small to find a pixel grid in (it needs {MIN_SIDE}+ px "
                 "on each side). A picture this small is likely the native sprite already; if it is a zoomed "
                 "copy, pass --native-width and --native-height")
    (px, bx, sx), (py, by, sy), mode = _grid(arr, a.native_width, a.native_height, not a.non_square)
    info = {"source": a.image, "scale_x": round(px, 3), "scale_y": round(py, 3),
            "grid_strength": round(min(sx, sy), 2), "grid_seen_by": mode}
    if min(sx, sy) < GRID_MIN or min(px, py) < 1.6:
        info["verdict"] = ("no clear pixel grid: smooth art, or too blurred to read. Try `stack` over more takes, "
                           "or `upscale --kind cartoon`")
        print(json.dumps(info, indent=1))
        sys.exit(2)
    xs, ys = _centres(arr.shape[1], px, bx), _centres(arr.shape[0], py, by)
    rx, ry = max(0.5, px * a.inner / 2), max(0.5, py * a.inner / 2)
    cells = np.zeros((len(ys), len(xs), 4), float)
    for j, cy in enumerate(ys):
        y0, y1 = int(np.floor(cy - ry)), int(np.ceil(cy + ry))
        for i, cx in enumerate(xs):
            x0, x1 = int(np.floor(cx - rx)), int(np.ceil(cx + rx))
            blk = arr[max(0, y0):min(arr.shape[0], y1), max(0, x0):min(arr.shape[1], x1)].reshape(-1, 4)
            cells[j, i] = np.median(blk, axis=0) if len(blk) else 0
    alpha = cells[..., 3] >= 128
    rgb = cells[..., :3]
    # palette from core cells (2+ alike neighbours). Edge cells
    # carry blur and bleed, so they're snapped to it later.
    agree = np.zeros(alpha.shape, int)
    for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        nb = np.roll(rgb, (dy, dx), axis=(0, 1))
        ok = np.roll(alpha, (dy, dx), axis=(0, 1)) & (np.abs(rgb - nb).sum(-1) < a.tol)
        if dy:
            ok[0 if dy > 0 else -1, :] = False
        if dx:
            ok[:, 0 if dx > 0 else -1] = False
        agree += ok
    core = alpha & (agree >= 2)
    base = rgb[core] if core.sum() >= max(4, 0.2 * alpha.sum()) else rgb[alpha]
    pal = np.zeros((0, 3))
    if len(base):
        keys, inv, counts = np.unique((base // 4).astype(int), axis=0, return_inverse=True, return_counts=True)
        means = np.array([base[inv.ravel() == k].mean(axis=0) for k in range(len(keys))])
        pal = _palette(means, counts, a.tol)
        for _ in range(6):  # settle each colour on the mean of its cells
            lab = np.abs(base[:, None] - pal[None]).sum(-1).argmin(1)
            pal = np.array([base[lab == k].mean(0) if (lab == k).any() else pal[k] for k in range(len(pal))])
        if not a.keep_blends:  # fold away near-twins and blends, least used first
            while len(pal) > 2:
                use = np.bincount(np.abs(base[:, None] - pal[None]).sum(-1).argmin(1), minlength=len(pal))
                k = int(use.argmin())
                rest = np.delete(pal, k, axis=0)
                if not _between(pal[k], rest, 1.5 * a.tol):
                    break
                pal = rest
    # lone non-blend colours are real (an eye, a highlight)
    for c in rgb[alpha & ~core]:
        if len(pal) and np.abs(pal - c).sum(-1).min() <= 3 * a.tol:
            continue
        if not a.keep_blends and _between(c, pal, 1.5 * a.tol):
            continue
        pal = np.vstack([pal, c]) if len(pal) else c[None]
    if a.colors and len(pal) > a.colors:
        pal = pal[: a.colors]
    out = np.zeros((len(ys), len(xs), 4), np.uint8)
    if len(pal):
        d = np.abs(cells[..., None, :3] - pal[None, None]).sum(axis=-1)
        out[..., :3] = pal[d.argmin(axis=-1)].round().astype(np.uint8)
    out[..., 3] = np.where(alpha, 255, 0)
    res = Image.fromarray(out, "RGBA")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    res.save(a.out)
    zoom = Path(a.out).with_name(Path(a.out).stem + f"_x{a.zoom}.png")
    res.resize((res.width * a.zoom, res.height * a.zoom), Image.NEAREST).save(zoom)
    # scaled back up, the sprite should match the source
    back = np.asarray(res.resize(im.size, Image.BILINEAR)).astype(float)
    vis = (arr[..., 3] > 0) | (back[..., 3] > 0)
    err = float(np.abs(back[..., :3] - arr[..., :3])[vis].mean()) if vis.any() else 0.0
    info.update({"native_size": [res.width, res.height], "colours": int(len(pal)), "out": a.out, "zoomed": str(zoom),
                 "mean_error_vs_source": round(err, 1)})
    if min(px, py) < AUTO_MIN_SCALE and not (a.native_width or a.native_height):
        info["warning"] = ("a grid near 2 px is also what resizing smooth art leaves behind: check the zoomed result "
                           "is really pixel art (flat cells, few colours) before using it")
    if not (a.native_width or a.native_height):
        info["note"] = ("scale measured from the picture. If the game's logical size is known (stage size, a "
                        "wiki, a sprite sheet), pass --native-width to pin it")
    info["next"] = (f"open {zoom} with Read beside the source. Stray pixels at edges are compression; fix them by "
                    "hand at native size. Ship the native file and let the game scale it with nearest-neighbour "
                    "(CSS image-rendering: pixelated; canvas imageSmoothingEnabled = false).")
    print(json.dumps(info, indent=1))


# ---------- stack: several takes on a finer grid ----------

def _subshift(ref, img):
    """Sub-pixel offset of img from ref (cross-correlation peak, parabola fit)."""
    import numpy as np
    a, b = ref - ref.mean(), img - img.mean()
    win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1]))
    r = np.fft.ifft2(np.fft.fft2(a * win) * np.conj(np.fft.fft2(b * win))).real
    h, w = r.shape
    py, px = np.unravel_index(int(r.argmax()), r.shape)

    def fit(m, c, p):
        den = m - 2 * c + p
        return 0.0 if den == 0 else 0.5 * (m - p) / den
    dy = py + fit(r[(py - 1) % h, px], r[py, px], r[(py + 1) % h, px])
    dx = px + fit(r[py, (px - 1) % w], r[py, px], r[py, (px + 1) % w])
    dy, dx = (dy - h if dy > h / 2 else dy), (dx - w if dx > w / 2 else dx)
    conf = float(r.max() / (np.sqrt((a * a * win).sum() * (b * b * win).sum()) + 1e-9))
    return float(dy), float(dx), conf


def cmd_stack(a):
    _need_numpy()
    import numpy as np
    from PIL import Image
    ims = [np.asarray(Image.open(p).convert("RGBA")).astype(float) for p in a.takes]
    if len(ims) < 2:
        sys.exit("stack needs two or more takes of the same thing")
    h, w = min(i.shape[0] for i in ims), min(i.shape[1] for i in ims)
    ims = [i[:h, :w] for i in ims]
    f = a.scale
    lum = [i[..., :3] @ [0.299, 0.587, 0.114] for i in ims]
    shifts, dropped, used = [(0.0, 0.0)], 0, [ims[0]]  # the first take is the reference
    featureless = float(lum[0].std()) < 0.5  # flat colour, nothing to align on
    for im, l in zip(ims[1:], lum[1:]):
        if featureless:
            if float(l.std()) < 0.5 and abs(float(l.mean()) - float(lum[0].mean())) < 8:
                shifts.append((0.0, 0.0))
                used.append(im)
            else:
                dropped += 1
            continue
        dy, dx, conf = _subshift(lum[0], l)
        if conf < 0.3 or abs(dy) > h / 4 or abs(dx) > w / 4:
            dropped += 1  # a different picture
            continue
        shifts.append((dy, dx))  # first take's content sits at (y+dy, x+dx)
        used.append(im)
    # iterative back-projection: refine until, shifted and shrunk,
    # it reproduces every take. Identical takes just get denoised.
    hr = np.asarray(Image.fromarray(np.mean(used, axis=0).clip(0, 255).astype(np.uint8), "RGBA")
                    .resize((w * f, h * f), Image.BICUBIC)).astype(float)
    last, rounds = None, 0
    for rounds in range(1, a.iterations + 1):
        corr, resid = np.zeros_like(hr), 0.0
        for im, (dy, dx) in zip(used, shifts):
            sy, sx = int(round(dy * f)), int(round(dx * f))
            low = np.roll(hr, (-sy, -sx), axis=(0, 1)).reshape(h, f, w, f, 4).mean(axis=(1, 3))
            resid += float(np.abs(im - low).mean()) / len(used)
            err = np.repeat(np.repeat(im - low, f, axis=0), f, axis=1)
            corr += np.roll(err, (sy, sx), axis=(0, 1))
        if last is not None and resid > 0.85 * last:
            break  # only compression noise left, fitting it adds artefacts
        last = resid
        hr += corr / len(used)
    m = int(np.ceil(max(max(abs(d) for d in sh) for sh in shifts) * f)) + 1  # rolled-in edges
    res = Image.fromarray(hr.clip(0, 255).astype(np.uint8)[m:h * f - m, m:w * f - m], "RGBA")
    res.save(a.out)
    frac = {(round(dy * f) % f, round(dx * f) % f) for dy, dx in shifts}
    info = {"takes": len(ims), "used": len(shifts), "scale": f, "out": a.out, "trimmed_edge_px": m, "rounds": rounds,
            "shifts_px": [[round(dy, 2), round(dx, 2)] for dy, dx in shifts],
            "distinct_subpixel_positions": len(frac),
            "verdict": ("the takes sit at different sub-pixel positions: the finer grid holds real detail"
                        if len(frac) > 1 else
                        "every take sits on the same pixel grid: this removed compression noise only, no new detail. "
                        "Takes from a panning camera or a moving sprite give more")}
    if dropped:
        info["dropped"] = f"{dropped} takes did not line up with the first (a different picture?)"
    if featureless:
        info["verdict"] = ("the first take is one flat colour: there is nothing to align or recover; the result is that "
                           "colour at the finer size")
    elif len(used) == 1:
        info["verdict"] = ("no other take lined up with the first: the result is the first take alone, enlarged, with "
                           "no new detail. Check the takes show the same thing at the same size")
    print(json.dumps(info, indent=1))


# ---------- upscale: AI for smooth art ----------

def _review(src, res, out_png, tile=32):
    """Tiles the model changed most vs a plain stretch. Text and faces land here."""
    import numpy as np
    from PIL import Image, ImageDraw
    k = res.width / src.width
    big = src.convert("RGB").resize(res.size, Image.BICUBIC)
    A, B = np.asarray(big).astype(float), np.asarray(res.convert("RGB")).astype(float)
    T = int(tile * k)
    scores = []
    for y in range(0, res.height - T + 1, T):
        for x in range(0, res.width - T + 1, T):
            scores.append((float(np.abs(A[y:y + T, x:x + T] - B[y:y + T, x:x + T]).mean()), x, y))
    scores.sort(reverse=True)
    top = scores[:8]
    S = 256
    # whole picture first, both versions, at a readable width:
    # garbled text shows here even when its tiles barely change
    fw = max(S * 2 + 10, min(1024, res.width))
    th = round(res.height * fw / res.width)
    pairs_h = (S + 24) * len(top) + 10
    sheet = Image.new("RGB", (fw + 20, 2 * (th + 20) + pairs_h + 10), "white")
    d = ImageDraw.Draw(sheet)
    d.text((10, 4), "source, stretched (read every word here and below)", fill="black")
    sheet.paste(big.resize((fw, th), Image.LANCZOS), (10, 18))
    d.text((10, th + 24), "enhanced", fill="black")
    sheet.paste(res.convert("RGB").resize((fw, th), Image.LANCZOS), (10, th + 38))
    y0 = 2 * (th + 20) + 10
    for i, (sc, x, y) in enumerate(top):
        box = (x, y, x + T, y + T)
        sheet.paste(big.crop(box).resize((S, S), Image.NEAREST), (10, y0 + i * (S + 24) + 14))
        sheet.paste(res.convert("RGB").crop(box).resize((S, S), Image.NEAREST), (S + 20, y0 + i * (S + 24) + 14))
        d.text((10, y0 + i * (S + 24)), f"source x{k:g} at ({int(x / k)},{int(y / k)})   |   enhanced   (change {sc:.0f})",
               fill="black")
    sheet.save(out_png)
    # drift uses shrunk, blurred brightness on opaque pixels,
    # so lost noise and cleaned edges don't count
    from PIL import ImageFilter
    blur = ImageFilter.GaussianBlur(1.2)
    small = np.asarray(res.convert("L").resize(src.size, Image.BOX).filter(blur)).astype(float)
    orig = np.asarray(src.convert("L").filter(blur)).astype(float)
    solid = np.asarray(src.convert("RGBA").getchannel("A")) > 200
    drift = float(np.abs(small - orig)[solid].mean()) if solid.any() else float(np.abs(small - orig).mean())
    return round(drift, 1), [{"x": int(x / k), "y": int(y / k), "change": round(s, 1)} for s, x, y in top]


def _defringe(im, layers=2, tol=40):
    """Peel leftover background off a cut-out's edge, up to `layers` px deep.

    Clears colours common on the rim but rare inside (sky, grass). Outlines survive, their colour runs inside too."""
    import numpy as np
    from PIL import Image
    a = np.asarray(im.convert("RGBA")).copy()
    op = a[..., 3] > 0
    if op.all() or not op.any():
        return im, 0

    def ring(mask):
        pad = np.pad(mask, 1, constant_values=False)
        inner = pad[1:-1, 1:-1] & pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:]
        return mask & ~inner
    edge = ring(op)
    deep = op.copy()
    for _ in range(3):
        deep &= ~ring(deep)
    q = lambda c: tuple((c // 24).tolist())  # noqa: E731
    ring_c, deep_c = {}, {}
    for c in a[..., :3][edge]:
        ring_c[q(c)] = ring_c.get(q(c), 0) + 1
    for c in a[..., :3][deep]:
        deep_c[q(c)] = deep_c.get(q(c), 0) + 1
    ne, nd = max(1, edge.sum()), max(1, deep.sum())
    fringe = [np.array(k) * 24 + 12 for k, v in ring_c.items()
              if v / ne > 0.03 and deep_c.get(k, 0) / nd < 0.2 * v / ne]
    if not fringe:
        return im, 0
    fr = np.array(fringe, float)
    removed = 0
    for _ in range(layers):
        e = ring(a[..., 3] > 0)
        d = np.abs(a[..., :3][e][:, None].astype(float) - fr[None]).sum(-1).min(1)
        kill = np.zeros_like(e)
        kill[e] = d < tol
        if not kill.any():
            break
        a[..., 3][kill] = 0
        removed += int(kill.sum())
    return Image.fromarray(a, "RGBA"), removed


def cmd_upscale(a):
    _need_numpy()
    from PIL import Image, ImageFilter
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    binp = _esrgan_bin()
    report = []
    for p in a.images:
        src = Image.open(p)
        row = {"source": p, "source_sha256": _sha(p), "size": list(src.size)}
        if not a.keep_edges and "A" in src.convert("RGBA").getbands() and src.convert("RGBA").getextrema()[3][0] < 255:
            src, gone = _defringe(src.convert("RGBA"))
            if gone:
                row["defringed_px"] = gone
        kind = a.kind
        if kind in ("auto", "pixel"):
            import numpy as np
            if _too_small(*src.size):
                px = py = sx = sy = 0.0  # too small for a grid
            else:
                (px, _, sx), (py, _, sy), _ = _grid(np.asarray(src.convert("RGBA")))
            if kind == "pixel" or (min(sx, sy) >= GRID_MIN and min(px, py) >= AUTO_MIN_SCALE):
                row.update({"kind": "pixel", "route": f"enhance.py native {p} --out <file> (grid {px:.2f} x {py:.2f})",
                            "why": "pixel art is recovered at its true pixels, never AI-upscaled"})
                report.append(row)
                continue
            kind = "cartoon"
        dst = out / (Path(p).stem + f"_x{a.scale}.png")
        if binp:
            model = MODELS[kind]
            with tempfile.TemporaryDirectory() as td:
                tmp_in, tmp_out = Path(td) / "in.png", Path(td) / "out.png"
                src.convert("RGBA").save(tmp_in)
                r = subprocess.run([binp, "-i", str(tmp_in), "-o", str(tmp_out), "-n", model,
                                    "-m", str(Path(binp).parent / "models")], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
                if r.returncode or not tmp_out.exists():
                    sys.exit(f"Real-ESRGAN failed on {p}: {(r.stderr or r.stdout).strip()[-300:]}")
                res = Image.open(tmp_out)
                res.load()
            if a.scale != 4:
                res = res.resize((src.width * a.scale, src.height * a.scale), Image.LANCZOS)
            row["enhanced"] = f"Real-ESRGAN {model}"
        else:
            res = src.convert("RGBA").resize((src.width * a.scale, src.height * a.scale), Image.LANCZOS)
            res = res.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=2))
            row["enhanced"] = "Lanczos + sharpen (Real-ESRGAN not installed: run `enhance.py setup` for a real upscale)"
        if src.mode in ("RGBA", "LA", "P") and "A" in src.convert("RGBA").getbands():
            al = src.convert("RGBA").getchannel("A").resize(res.size, Image.BICUBIC)
            res = res.convert("RGBA")
            res.putalpha(al)  # model alpha can fringe, a smooth stretch is safer
        res.save(dst)
        keep = out / (Path(p).stem + "_source" + Path(p).suffix.lower())
        if Path(p).resolve() != keep.resolve():
            shutil.copyfile(p, keep)  # keep the original cut too, MANIFEST wants both
        row["source_copy"] = str(keep)
        rv = out / (Path(p).stem + "_review.png")
        drift, tiles = _review(src.convert("RGBA"), res, rv)
        row.update({"kind": kind, "out": str(dst), "review": str(rv), "drift_vs_source": drift, "most_changed": tiles})
        if drift > 10:
            row["warning"] = ("shrunk back and blurred, the result no longer matches the source: the model changed shapes "
                              "or tones here, so check its review sheet first")
        report.append(row)
    ranked = sorted((r for r in report if "drift_vs_source" in r), key=lambda r: -r["drift_vs_source"])
    print(json.dumps({"review_first": [f"{Path(r['source']).name} (drift {r['drift_vs_source']})" for r in ranked],
                      "results": report, "next": (
        "open each *_review.png with Read: left is the source stretched, right is the result, at the places the model "
        "changed most. Reject or patch any tile where it invented: text (re-set it in the period font instead), "
        "faces, logos, patterns. Record shipped files with \"enhanced\" in MANIFEST.json and keep the source cut.")},
        indent=1))


# ---------- vector ----------

def cmd_vector(a):
    _need_vtracer()
    import vtracer
    vtracer.convert_image_to_svg_py(a.image, a.out, colormode="color", hierarchical="stacked", mode="spline",
                                     filter_speckle=a.speckle, color_precision=a.precision, layer_difference=16,
                                     corner_threshold=60, length_threshold=4.0, splice_threshold=45, path_precision=3)
    print(json.dumps({"out": a.out, "bytes": Path(a.out).stat().st_size,
                      "note": "gradients become bands and soft edges get ragged: check it at full size against the "
                              "source. Trace the `upscale` output rather than a tiny cut for cleaner curves."}, indent=1))


# ---------- setup ----------

UV_URL = "https://docs.astral.sh/uv/getting-started/installation/"
MIN_PY = (3, 12)  # pinned numpy needs 3.12 (Pillow, yt-dlp 3.11, Playwright 3.10)
# quick launch to prove Chromium and its system libraries work
LAUNCH_TEST = ("from playwright.sync_api import sync_playwright\n"
               "with sync_playwright() as p:\n"
               "    b = p.chromium.launch(headless=True, args=['--mute-audio'])\n"
               "    b.new_page().set_content('<p>ok</p>')\n"
               "    b.close()\n")


def _py_version(cmd: list) -> tuple | None:
    """(major, minor) of the Python `cmd` runs, or None."""
    try:
        r = subprocess.run([*cmd, "-c", "import sys; print(*sys.version_info[:2])"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        major, minor = r.stdout.split()[:2]
        return int(major), int(minor)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _find_python(nt: bool | None = None) -> list | None:
    """A Python >= MIN_PY for the environment when uv is missing.

    Tries this one, then python3.14, 3.13, 3.12 on PATH (and `py` on Windows), then python3."""
    if sys.version_info[:2] >= MIN_PY:
        return [sys.executable]
    nt = os.name == "nt" if nt is None else nt
    cands = [[w] for n in ("python3.14", "python3.13", "python3.12", "python3", "python") if (w := shutil.which(n))]
    if nt and shutil.which("py"):
        cands[:0] = [["py", "-3.14"], ["py", "-3.13"], ["py", "-3.12"]]
    for c in cands:
        v = _py_version(c)
        if v and v >= MIN_PY:
            return c
    return None


def _install_esrgan() -> str:
    """Download, verify and unpack Real-ESRGAN. Raises RuntimeError on failure."""
    import http.client
    tag = {"Darwin": "macos", "Linux": "ubuntu", "Windows": "windows"}.get(platform.system())
    if not tag:
        raise RuntimeError(f"no Real-ESRGAN build for {platform.system()}")
    ESRGAN_DIR.mkdir(parents=True, exist_ok=True)
    z = ESRGAN_DIR / "r.zip"
    print(f"downloading Real-ESRGAN ({tag}, ~50 MB)", file=sys.stderr)
    try:
        req = urllib.request.Request(ESRGAN_URL.format(tag), headers={"User-Agent": "resurrect-skill setup"})
        with urllib.request.urlopen(req, timeout=60) as resp, z.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        got = hashlib.sha256(z.read_bytes()).hexdigest()
        if got != ESRGAN_SHA256[tag]:
            raise RuntimeError(f"the download's SHA-256 is {got}, not the pinned {ESRGAN_SHA256[tag]}; nothing was "
                               "unpacked. If it persists, the release asset changed and must be checked by hand")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(ESRGAN_DIR)
    except (OSError, zipfile.BadZipFile, http.client.HTTPException) as e:  # URLError and timeouts are OSErrors
        raise RuntimeError(f"{type(e).__name__}: {e}") from e
    finally:
        z.unlink(missing_ok=True)
    for junk in ("onepiece_demo.mp4", "input.jpg", "input2.jpg"):
        (ESRGAN_DIR / junk).unlink(missing_ok=True)
    b = ESRGAN_DIR / ("realesrgan-ncnn-vulkan.exe" if tag == "windows" else "realesrgan-ncnn-vulkan")
    if not b.exists():
        raise RuntimeError(f"the archive held no {b.name}")
    b.chmod(0o755)
    if tag == "macos":  # only after the checksum matched
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(ESRGAN_DIR)], capture_output=True)
    return str(b)


def _chromium_check(hp: str) -> str:
    """Test-start Chromium. Returns 'ok', or what failed and the fix."""
    try:
        r = subprocess.run([hp, "-c", LAUNCH_TEST], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        if not r.returncode:
            return "ok"
        why = ([x.strip() for x in (r.stderr or r.stdout).splitlines() if x.strip()] or ["?"])[-1][:300]
    except (OSError, subprocess.SubprocessError) as e:
        why = str(e)
    if platform.system() == "Linux":  # usually missing libraries (libnss3, libgbm...)
        return (f"Chromium did not start ({why}). It usually lacks system libraries: run "
                f"`sudo {hp} -m playwright install-deps chromium` (needs sudo), then try again")
    return f"Chromium did not start ({why}); screenshots and journeys (shots.py, verify.py) will fail until it does"


def cmd_setup(a):
    """Python environment first (every script needs it), then Real-ESRGAN if possible."""
    done = {}
    pkgs = PKGS + (AUDIO_PKGS if a.audio else [])
    uv = shutil.which("uv")
    need = ".".join(map(str, MIN_PY))
    hp = heavy_python()
    if hp and (_py_version([hp]) or (0, 0)) < MIN_PY:  # an old setup's env on too old a Python
        print(f"replacing the environment in {HEAVY}: its Python is older than {need}", file=sys.stderr)
        shutil.rmtree(HEAVY, ignore_errors=True)
        hp = None
    if not hp:
        if uv:
            cmd = [uv, "venv", "-q", "--python", need, str(HEAVY)]
        else:
            py = _find_python()
            if not py:
                sys.exit(f"the extras need Python {need} or newer (the pinned numpy does), and this is Python "
                         f"{sys.version.split()[0]} with no python3.12, 3.13 or 3.14 on PATH. Install uv "
                         f"({UV_URL}; it fetches a suitable Python by itself) or Python {need}+, then run "
                         "`enhance.py setup` again")
            cmd = [*py, "-m", "venv", str(HEAVY)]
            if py != [sys.executable]:
                print(f"making the environment with {' '.join(py)} (this Python is {sys.version.split()[0]}; the "
                      f"extras need {need}+)", file=sys.stderr)
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode or not heavy_python():
            shutil.rmtree(HEAVY, ignore_errors=True)  # a half-made env would look like a working one
            why = ((r.stderr or r.stdout).strip().splitlines() or ["no python in the new environment"])[-1]
            sys.exit(f"could not make the Python environment in {HEAVY} with `{' '.join(cmd[:-1])}` ({why}). "
                     + ("" if uv else "Its venv support may be missing (on Debian and Ubuntu: apt install "
                                      "python3-venv, or python3.12-venv to match); ")
                     + f"installing uv ({UV_URL}) avoids this; then run `enhance.py setup` again")
        hp = heavy_python()
    if uv:
        cmd = [uv, "pip", "install", "-q", "--python", hp, *pkgs]
    else:
        if subprocess.run([hp, "-m", "pip", "--version"], capture_output=True).returncode:
            sys.exit(f"the environment in {HEAVY} has no pip (uv made it, or its Python lacks ensurepip): install uv "
                     f"({UV_URL}), or delete {HEAVY} and run `enhance.py setup` again")
        cmd = [hp, "-m", "pip", "install", "-q", *pkgs]
    r = subprocess.run(cmd)
    if r.returncode:
        sys.exit(f"installing {' '.join(pkgs)} into {HEAVY} failed: see the installer's message above (a network "
                 "error, or no build of a package for this system). Run `enhance.py setup` again once that is fixed")
    r = subprocess.run([hp, "-m", "playwright", "install", "chromium"])
    if r.returncode:
        sys.exit(f"installing Playwright's Chromium failed (see above); run `{hp} -m playwright install chromium`")
    done["python"] = hp
    done["packages"] = pkgs + ["chromium (playwright)"]
    done["chromium_check"] = _chromium_check(hp)
    if done["chromium_check"] != "ok":
        print(done["chromium_check"], file=sys.stderr)
    # upscaler last, best effort. `upscale` falls back without it
    if _esrgan_bin():
        done["real_esrgan"] = _esrgan_bin()
    else:
        try:
            done["real_esrgan"] = _install_esrgan()
        except RuntimeError as e:
            done["real_esrgan"] = f"upscaler skipped: {e}; rerun `enhance.py setup` later to add it"
            print(done["real_esrgan"], file=sys.stderr)
    print(json.dumps(done, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("native", help="pixel art: the original pixels from a scaled, blurred picture")
    s.add_argument("image")
    s.add_argument("--out", required=True)
    s.add_argument("--native-width", type=float, help="the original's width in its own pixels (pins the scale)")
    s.add_argument("--native-height", type=float)
    s.add_argument("--colors", type=int, help="cap the palette (default: as many as differ by more than --tol)")
    s.add_argument("--non-square", action="store_true", help="the original's pixels were not square (measure each axis)")
    s.add_argument("--tol", type=float, default=48, help="RGB distance (sum) under which two colours are one")
    s.add_argument("--keep-blends", action="store_true", help="keep lone in-between colours (real shading, not blur)")
    s.add_argument("--inner", type=float, default=0.5, help="share of each cell sampled around its centre")
    s.add_argument("--zoom", type=int, default=4)
    s.set_defaults(fn=cmd_native)
    s = sub.add_parser("stack", help="several takes merged on a finer grid")
    s.add_argument("takes", nargs="+")
    s.add_argument("--out", required=True)
    s.add_argument("--scale", type=int, default=2)
    s.add_argument("--iterations", type=int, default=12, help="upper bound; stops early once only noise is left")
    s.set_defaults(fn=cmd_stack)
    s = sub.add_parser("upscale", help="smooth art with an AI upscaler, plus a review sheet")
    s.add_argument("images", nargs="+")
    s.add_argument("--out", required=True)
    s.add_argument("--kind", choices=["auto", "cartoon", "photo", "pixel"], default="auto")
    s.add_argument("--scale", type=int, default=4, choices=[2, 3, 4])
    s.add_argument("--keep-edges", action="store_true", help="do not peel background colours off a cut-out's edge")
    s.set_defaults(fn=cmd_upscale)
    s = sub.add_parser("vector", help="flat-colour art traced to SVG")
    s.add_argument("image")
    s.add_argument("--out", required=True)
    s.add_argument("--speckle", type=int, default=8)
    s.add_argument("--precision", type=int, default=6)
    s.set_defaults(fn=cmd_vector)
    s = sub.add_parser("setup", help="install Real-ESRGAN and the Python extras (Pillow, numpy, Playwright, yt-dlp) "
                                     "into the resurrect cache")
    s.add_argument("--audio", action="store_true", help="also Demucs, for footage.py stems")
    s.set_defaults(fn=cmd_setup)
    # re-exec first, while args and cwd are still the caller's
    cmd = next((x for x in sys.argv[1:] if x in (*NUMPY_CMDS, "vector", "setup")), None)
    if cmd in NUMPY_CMDS:
        _need_numpy()
    elif cmd == "vector":
        _need_vtracer()
    ws_arg = pop_ws_arg()
    a = ap.parse_args()
    enter_ws(ws_arg)
    a.fn(a)


if __name__ == "__main__":
    main()
