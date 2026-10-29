#!/usr/bin/env python3
"""Turn a screen's reference frame into build input, then measure the rebuild against it, round by round.

The design-to-code research from 2025-26 agrees on two things. Models lay out a screen far more faithfully
from a measured blueprint (blocks with coordinates, colours and the real assets) than from a picture
alone. And they only improve over rounds when each round gets a concrete difference to fix. These two
commands give both, for any screen the evidence shows, whether an app screen, a web page or a game's HUD.

  make FRAME --out DIR [--viewport WxH] [--depth 4] [--min 6]
        Cut the frame into blocks the way a layout is built (recursive cuts along gaps and colour edges).
        Writes blueprint.json (each block's box in frame pixels and in CSS pixels at the viewport, its
        dominant colour, and its kind, flat / text / image), blueprint.png (the frame with numbered boxes,
        so Read it), blocks/ (a crop per block, either recovered art you may ship or the reference for
        what you rebuild as live HTML) and scaffold.html (the blocks absolutely positioned at measured
        size). The scaffold is a starting layout to turn into the real screen, never a screenshot to ship.
  diff REF NOW --out DIR [--blueprint DIR/blueprint.json] [--round N]
        Compare the rebuild's screenshot (same framing) with the reference. Writes diff.png (where they
        differ) and onion.png (one over the other). Per block it reports where the rebuild's content sits
        relative to the original (offset in px), colour differences, and blocks with no match.
        Fix the top items, screenshot again and run the next round. The report keeps every round's score
        so you can see it converge. Stop after about four rounds, or when what's left has no evidence.
        The layout part only compares long straight edges (cards, bars, panels), so it still works when
        the content is stand-in (fictional posts and photos) or the reference is a straightened photo.

  rectify FRAME (--corners x1,y1,...,x4,y4 | --find-corners) --size 320x568 --out FILE
        Warps a screen photographed at an angle (App Store art of a phone in a hand, a review video
        filming the device) flat to the device's size. Do this first. Read the corners off the image
        (shots.py probe helps), or let --find-corners look for the bright screen quadrilateral. It prints
        the corners it used. Check the result and nudge them if needed.

Needs numpy and Pillow. `enhance.py setup` installs them in the resurrect cache and this script finds them.
Crops of text are only references, so re-set the text in the period font. Crops of art are recovered assets
(MANIFEST origin "recovered", locator = the frame).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # also after a re-exec into the heavy env
from _common import atomic_write, cache_dir, enter_ws, pop_ws_arg, reexec  # noqa: E402

HEAVY = cache_dir() / "heavy"
OVERLAY_MAX_H = 2400  # blueprint.png is enlarged up to this height


def _need_numpy():
    try:
        import numpy  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        hp = HEAVY / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if hp.exists() and Path(sys.prefix).resolve() != HEAVY.resolve():
            reexec(str(hp), __file__)
        sys.exit("needs numpy and Pillow: run `enhance.py setup` (or pip install numpy pillow)")


def _hex(c):
    return "#%02x%02x%02x" % tuple(int(v) for v in c[:3])


# ---------- rectify ----------

def cmd_rectify(a):
    """Warp an angled photo of a screen (store art, review videos) flat to the device's size."""
    _need_numpy()
    import numpy as np
    from PIL import Image
    found = None
    if a.corners:
        pts = [float(v) for v in a.corners.split(",")]
        if len(pts) != 8:
            sys.exit("--corners needs 8 numbers: top-left x,y, top-right x,y, bottom-right x,y, bottom-left x,y")
        src = list(zip(pts[0::2], pts[1::2]))
    elif a.find_corners:
        found = _find_screen(Image.open(a.frame).convert("RGB"))
        if not found:
            sys.exit(json.dumps({"corners": None, "from": a.frame, "verdict": (
                "no screen-shaped quadrilateral stands out from the background here (screen as dark as its "
                "surroundings, cut off by the frame, or several bright areas). Read the four corners off the image "
                "(shots.py probe helps) and pass them with --corners x1,y1,...,x4,y4 (top-left, top-right, "
                "bottom-right, bottom-left)")}, indent=1))
        src = found["corners"]
    else:
        sys.exit("rectify needs --corners x1,y1,...,x4,y4 (top-left, top-right, bottom-right, bottom-left) or "
                 "--find-corners")
    w, h = (int(v) for v in a.size.lower().split("x"))
    dst = [(0, 0), (w, 0), (w, h), (0, h)]
    # PIL maps output to input, so solve dst -> src
    A, B = [], []
    for (x, y), (u, v) in zip(dst, src):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        B += [u, v]
    coef = np.linalg.solve(np.array(A, float), np.array(B, float))
    im = Image.open(a.frame).convert("RGB")
    flat = im.transform((w, h), Image.PERSPECTIVE, tuple(coef), Image.BICUBIC)
    flat.save(a.out)
    res = {"out": a.out, "size": [w, h], "from": a.frame, "corners": [[round(x, 1), round(y, 1)] for x, y in src]}
    if found:
        res["corners_found"] = (f"--corners {','.join(f'{v:.0f}' for xy in src for v in xy)} (found automatically: "
                                f"{found['why']})")
    res["next"] = (f"Read {a.out}: edges straight and nothing cut off? Nudge the corners if not"
                   + (" (pass the printed --corners, edited)" if found else "") + ". Then `blueprint.py make` on it "
                   "(a photo of a screen is softer and tinted: compare layout and proportions, not exact colours).")
    print(json.dumps(res, indent=1))


def _components(mask):
    """Connected regions of a boolean mask (4-neighbour) as index arrays, largest first."""
    import numpy as np
    from collections import deque
    h, w = mask.shape
    lab = np.zeros(mask.shape, np.int32)
    flat_mask, flat_lab = mask.ravel(), lab.ravel()
    regions = []
    for start in np.flatnonzero(flat_mask):
        if flat_lab[start]:
            continue
        k = len(regions) + 1
        flat_lab[start] = k
        q, members = deque([start]), [start]
        while q:
            i = q.popleft()
            y, x = divmod(int(i), w)
            for j, ok in ((i - w, y > 0), (i + w, y < h - 1), (i - 1, x > 0), (i + 1, x < w - 1)):
                if ok and flat_mask[j] and not flat_lab[j]:
                    flat_lab[j] = k
                    q.append(j)
                    members.append(j)
        regions.append(np.array(members))
    return sorted(regions, key=len, reverse=True)


def _quad_area(c):
    return 0.5 * abs(sum(c[i][0] * c[(i + 1) % 4][1] - c[(i + 1) % 4][0] * c[i][1] for i in range(4)))


def _quad(filled):
    """Corners of a roughly four-sided region, or None if too small, at the edge, or another shape."""
    import numpy as np
    h, w = filled.shape
    ys, xs = np.nonzero(filled)
    if len(xs) < 0.04 * h * w:
        return None
    if xs.min() == 0 or ys.min() == 0 or xs.max() == w - 1 or ys.max() == h - 1:
        return None  # touches the edge: background, or a cut-off screen
    s_, d_ = xs + ys, xs - ys
    ext = [(xs[s_.argmin()], ys[s_.argmin()]), (xs[d_.argmax()], ys[d_.argmax()]),
           (xs[s_.argmax()], ys[s_.argmax()]), (xs[d_.argmin()], ys[d_.argmin()])]
    area = _quad_area(ext)
    if area < 1 or not 0.85 <= len(xs) / area <= 1.2:
        return None  # not a quadrilateral
    # fit a line to each side, away from the corners
    inner = filled.copy()
    inner[1:-1, 1:-1] = (filled[1:-1, 1:-1] & filled[:-2, 1:-1] & filled[2:, 1:-1] & filled[1:-1, :-2]
                         & filled[1:-1, 2:])
    by, bx = np.nonzero(filled & ~inner)
    P = np.stack([bx + 0.5, by + 0.5], 1).astype(float)
    E = np.array(ext, float) + 0.5
    lines = []
    for i in range(4):
        p0, p1 = E[i], E[(i + 1) % 4]
        d = p1 - p0
        L = float(np.hypot(*d)) or 1.0
        t = ((P - p0) @ d) / (L * L)
        dist = np.abs((P - p0) @ np.array([-d[1], d[0]]) / L)
        sel = P[(t > 0.12) & (t < 0.88) & (dist < max(3.0, 0.04 * L))]
        if len(sel) < 5:
            return E, len(xs) / area
        m = sel.mean(0)
        _, _, vt = np.linalg.svd(sel - m)
        lines.append((m, vt[0]))
    out = []
    for i in range(4):  # corner i joins the sides before and after it
        (m1, v1), (m2, v2) = lines[(i - 1) % 4], lines[i]
        A = np.array([v1, -v2]).T
        if abs(np.linalg.det(A)) < 1e-6:
            return E, len(xs) / area
        t = np.linalg.solve(A, m2 - m1)
        out.append(m1 + t[0] * v1)
    out = np.array(out)
    return (out if max(np.hypot(*(out - E).T)) < 0.1 * max(h, w) else E), len(xs) / area


def _find_screen(im, side=320):
    """The screen in a device photo: the largest four-sided region brighter than (or unlike) its
    surroundings. Works on a copy at most `side` px across; corners come from lines fitted to its edges."""
    import numpy as np
    k = max(im.width, im.height) / side
    small = im.resize((max(1, round(im.width / k)), max(1, round(im.height / k))), 3) if k > 1 else im
    k = im.width / small.width
    arr = np.asarray(small).astype(float)
    lum = arr @ [0.299, 0.587, 0.114]
    h, w = lum.shape
    border = np.concatenate([arr[0], arr[-1], arr[:, 0], arr[:, -1]])
    unlike = np.abs(arr - np.median(border, axis=0)).sum(-1)

    def otsu(v):
        hist, edges = np.histogram(v, bins=64)
        p = hist / max(1, hist.sum())
        mids = (edges[:-1] + edges[1:]) / 2
        best, t = -1.0, mids[len(mids) // 2]
        for i in range(1, 64):
            w0, w1 = p[:i].sum(), p[i:].sum()
            if w0 < 1e-6 or w1 < 1e-6:
                continue
            m0, m1 = (p[:i] * mids[:i]).sum() / w0, (p[i:] * mids[i:]).sum() / w1
            if w0 * w1 * (m0 - m1) ** 2 > best:
                best, t = w0 * w1 * (m0 - m1) ** 2, edges[i]
        return t

    def filled(region):
        """The region with its holes (dark screen content) filled in."""
        reg = np.zeros(h * w, bool)
        reg[region] = True
        out = np.ones(h * w, bool)
        for o in _components(~reg.reshape(h, w)):
            ys, xs = np.divmod(o, w)
            if ys.min() == 0 or xs.min() == 0 or ys.max() == h - 1 or xs.max() == w - 1:
                out[o] = False
        return out.reshape(h, w)

    def grow(m, r):
        for _ in range(r):
            g = m.copy()
            g[1:] |= m[:-1]
            g[:-1] |= m[1:]
            g[:, 1:] |= m[:, :-1]
            g[:, :-1] |= m[:, 1:]
            m = g
        return m

    for why, mask in (("brighter than its surroundings", lum > otsu(lum)),
                      ("unlike the colour around the frame's edge", unlike > otsu(unlike))):
        regions = _components(mask)
        if not regions:
            continue
        core = filled(regions[0])
        # grow into darker screen parts like a title bar
        ring = grow(core, 4) & ~grow(core, 1)
        cands = [core]
        if ring.any():
            px_ = arr[ring]
            bins, inv, counts = np.unique((px_ // 16).astype(int), axis=0, return_inverse=True, return_counts=True)
            bez = px_[inv.ravel() == counts.argmax()].mean(0)  # commonest colour around it, bezel or background
            dist = np.abs(px_ - bez).sum(-1)
            spread = float(np.median(dist[dist < 64]))
            near = (np.abs(arr - bez).sum(-1) > max(40.0, 3 * spread)) | core
            seed = int(np.flatnonzero(core)[0])
            reg = next((r for r in _components(near) if seed in set(r.tolist())), None)
            if reg is not None:
                cands.insert(0, filled(reg))
        for reg in cands:
            q = _quad(reg)
            if q:
                corners, share = q
                found = {"corners": [(float(x * k), float(y * k)) for x, y in corners],
                         "why": f"the largest region {why}, {share:.0%} of its quadrilateral"}
                inner = _nested_screen(arr, reg, otsu)
                if inner:  # found the phone itself, the screen is inside it
                    ic, ishare = inner
                    return {"corners": [(float(x * k), float(y * k)) for x, y in ic],
                            "why": f"a screen inside the device region ({ishare:.0%} of its quadrilateral)",
                            "outer_corners": found["corners"]}
                return found
    return None


def _nested_screen(arr, outer, otsu):
    """The screen inside a found device region: the largest part unlike its rim (the bezel),
    filling 25-95% of it. Returns (corners, share) or None."""
    import numpy as np
    h, w = outer.shape
    rim = outer & ~_erode(outer, 3)
    if rim.sum() < 20:
        return None
    bez = np.median(arr[rim], axis=0)
    dist = np.abs(arr - bez).sum(-1)
    inside = _erode(outer, 2)
    if not inside.any():
        return None
    t = max(40.0, float(otsu(dist[inside])))
    mask = inside & (dist > t)
    comps = _components(mask)
    if not comps:
        return None
    reg = np.zeros(h * w, bool)
    reg[comps[0]] = True
    reg = reg.reshape(h, w)
    # grow into screen parts near the bezel colour (title bar, HUD)
    rim_d = dist[rim]
    noise = float(np.median(rim_d)) if rim_d.size else 10.0  # median, rim edge pixels blend with the table
    near = inside & ((dist > max(12.0, 3 * noise)) | reg)
    seed = int(np.flatnonzero(reg)[0])
    grown = next((c for c in _components(near) if seed in set(c.tolist())), None)
    if grown is not None:
        reg = np.zeros(h * w, bool)
        reg[grown] = True
        reg = reg.reshape(h, w)
    # fill holes like the outer search does
    holes = np.ones(h * w, bool)
    for o in _components(~reg):
        ys, xs = np.divmod(o, w)
        if ys.min() == 0 or xs.min() == 0 or ys.max() == h - 1 or xs.max() == w - 1:
            holes[o] = False
    reg = holes.reshape(h, w) & outer
    ratio = reg.sum() / max(1, outer.sum())
    if not 0.25 <= ratio <= 0.95:
        return None
    return _quad(reg)


def _erode(m, r):
    for _ in range(r):
        e = m.copy()
        e[1:] &= m[:-1]
        e[:-1] &= m[1:]
        e[:, 1:] &= m[:, :-1]
        e[:, :-1] &= m[:, 1:]
        m = e
    return m


# ---------- make ----------

def _cuts(prof, min_gap):
    """Cut positions at runs of 'quiet' positions (a gap or a colour edge)."""
    cuts, run = [], 0
    for i, q in enumerate(prof):
        if q:
            run += 1
        else:
            if run >= min_gap:
                cuts.append(i - run // 2 - 1)
            run = 0
    return cuts


def _split(arr, box, axis, min_size):
    """Cut a box along one axis, at flat runs and at sharp edges between flat regions."""
    import numpy as np
    x0, y0, x1, y1 = box
    sub = arr[y0:y1, x0:x1].astype(float)
    lines = sub if axis == 0 else sub.transpose(1, 0, 2)  # rows for axis 0, else columns
    std = lines.std(axis=1).mean(axis=1)          # variation along each line
    flat = std < 10
    cuts = set(_cuts(flat, 3 if axis == 0 else 8))  # column gaps must beat word gaps
    # Edge line: a sharp one-pixel change along most of the line.
    # Photo edges (a horizon) are soft, spread over several pixels.
    # Max over channels: grey bar on dark green differs in colour only.
    d = np.abs(np.diff(lines, axis=0)).max(axis=2)
    if len(d) > 3:
        crisp = (d[1:-1] > 20) & (d[:-2] < 8) & (d[2:] < 8)
        share = crisp.mean(axis=1)
        for i in np.where(share > 0.6)[0]:
            cuts.add(int(i) + 2)
    n = len(flat)
    edges = sorted(c for c in cuts if min_size <= c <= n - min_size)
    parts, start = [], 0
    for c in edges + [n]:
        if c - start >= min_size:
            seg = (x0, y0 + start, x1, y0 + c) if axis == 0 else (x0 + start, y0, x0 + c, y1)
            # drop flat background segments only if thin gaps
            parts.append(seg)
            start = c
    return parts if len(parts) > 1 else [box]


def _segment(arr, box, depth, min_size, axis=0, out=None, level=0):
    out = [] if out is None else out
    x0, y0, x1, y1 = box
    if depth == 0 or (level > 0 and _kind(arr[y0:y1, x0:x1]) == "text" and (y1 - y0) < 0.3 * arr.shape[0]):
        out.append((box, level))  # a paragraph or label stays one block
        return out
    parts = _split(arr, box, axis, min_size)
    if len(parts) == 1:
        parts = _split(arr, box, 1 - axis, min_size)
        axis = 1 - axis
        if len(parts) == 1:
            out.append((box, level))
            return out
    for p in parts:
        _segment(arr, p, depth - 1, min_size, 1 - axis, out, level + 1)
    return out


def _kind(block):
    import numpy as np
    b = block.reshape(-1, 3).astype(int)
    if b.std(axis=0).mean() < 8:
        return "flat"
    q = {tuple(v) for v in (b[:: max(1, len(b) // 4000)] // 32)}
    g = np.abs(np.diff(block.mean(axis=2), axis=1))
    edge = (g > 40).mean()
    return "text" if len(q) <= 12 and edge > 0.04 else "image"


def cmd_make(a):
    _need_numpy()
    import numpy as np
    from PIL import Image, ImageDraw
    im = Image.open(a.frame).convert("RGB")
    arr = np.asarray(im)
    H, W = arr.shape[:2]
    vw, vh = (int(v) for v in a.viewport.lower().split("x")) if a.viewport else (W, H)
    kx, ky = vw / W, vh / H
    out = Path(a.out)
    (out / "blocks").mkdir(parents=True, exist_ok=True)
    leaves = _segment(arr, (0, 0, W, H), a.depth, a.min)
    blocks = []
    for n, ((x0, y0, x1, y1), level) in enumerate(leaves, 1):
        sub = arr[y0:y1, x0:x1]
        flat = sub.reshape(-1, 3)
        # dominant colour: commonest colour family (pooled shades),
        # then its commonest exact colour, so black stays #000000
        bins, inv, counts = np.unique(flat // 8, axis=0, return_inverse=True, return_counts=True)
        fam = flat[inv.ravel() == counts.argmax()]
        vals, counts = np.unique(fam, axis=0, return_counts=True)
        dom = vals[counts.argmax()]
        kind = _kind(sub)
        crop = f"blocks/b{n:02d}.png"
        Image.fromarray(sub).save(out / crop)
        blocks.append({"id": f"B{n}", "box_px": [x0, y0, x1 - x0, y1 - y0],
                       "css": {"left": round(x0 * kx, 1), "top": round(y0 * ky, 1),
                               "width": round((x1 - x0) * kx, 1), "height": round((y1 - y0) * ky, 1)},
                       "colour": _hex(dom), "kind": kind, "crop": crop, "depth": level})
    # numbered overlay for reading
    vis = im.copy()
    d = ImageDraw.Draw(vis)
    for b in blocks:
        x, y, w, h = b["box_px"]
        d.rectangle((x, y, x + w - 1, y + h - 1), outline=(255, 0, 255))
        d.text((x + 2, y + 1), b["id"], fill=(255, 0, 255))
    scale = max(1, min(900 // W, OVERLAY_MAX_H // H)) if W < 900 else 1  # keeps a tall strip readable
    vis.resize((W * scale, H * scale), Image.NEAREST).save(out / "blueprint.png")
    palette = {}
    for b in blocks:
        palette[b["colour"]] = palette.get(b["colour"], 0) + b["box_px"][2] * b["box_px"][3]
    pal = [c for c, _ in sorted(palette.items(), key=lambda kv: -kv[1])][:12]
    bp = {"frame": str(a.frame), "frame_size": [W, H], "viewport": [vw, vh], "blocks": blocks, "palette": pal}
    atomic_write(out / "blueprint.json", json.dumps(bp, indent=1))
    # scaffold: flat blocks as colour, others show their crop
    divs = []
    for b in blocks:
        c = b["css"]
        style = f"left:{c['left']}px;top:{c['top']}px;width:{c['width']}px;height:{c['height']}px;"
        if b["kind"] == "flat":
            style += f"background:{b['colour']};"
        else:
            style += f"background:url({b['crop']}) center/100% 100% no-repeat;"
        note = {"text": "re-set as live text in the period font", "image": "recovered art or a live element",
                "flat": "plain colour"}[b["kind"]]
        divs.append(f'  <div class="b {b["kind"]}" id="{b["id"]}" title="{b["id"]}: {note}" style="{style}"></div>')
    (out / "scaffold.html").write_text(
        "<!doctype html>\n<meta charset=utf-8>\n<!-- blueprint scaffold: measured block positions from "
        f"{Path(a.frame).name}. Turn each block into the real element; replace text crops with live text. -->\n"
        f"<style>body{{margin:0}}.screen{{position:relative;width:{vw}px;height:{vh}px;overflow:hidden}}"
        ".b{position:absolute;box-sizing:border-box}</style>\n"
        f'<div class="screen">\n' + "\n".join(divs) + "\n</div>\n", encoding="utf-8")
    kinds = {}
    for b in blocks:
        kinds[b["kind"]] = kinds.get(b["kind"], 0) + 1
    print(json.dumps({"blocks": len(blocks), "kinds": kinds, "palette": pal, "out": str(out),
                      "next": (f"Read {out}/blueprint.png and blueprint.json. Build the screen from the measured "
                               "boxes and colours (scaffold.html is the starting layout), ship art crops as "
                               "recovered assets where restoration allows, re-set text crops as live text. Then "
                               "screenshot the rebuild with the same framing and run `blueprint.py diff`.")},
                     indent=1))


# ---------- diff ----------

def _ncc(a, b):
    import numpy as np
    a, b = a - a.mean(), b - b.mean()
    den = np.sqrt((a * a).sum() * (b * b).sum())
    return float((a * b).sum() / den) if den > 1e-6 else (1.0 if np.abs(a - b).mean() < 4 else 0.0)


def _ncc_at(blk, img, x, y, min_share=0.5):
    """_ncc of blk against the block-sized window of img at (x, y), clipped to the picture.
    Returns -1 if under min_share of the window overlaps."""
    import numpy as np
    h, w = blk.shape
    H, W = img.shape
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0 or (x1 - x0) * (y1 - y0) < min_share * h * w:
        return -1.0
    part = blk[y0 - y:y1 - y, x0 - x:x1 - x]
    if (x1 - x0, y1 - y0) != (w, h) and float(np.std(part)) < 1:
        return -1.0  # a blank margin alone would match any blank area
    return _ncc(part, img[y0:y1, x0:x1])


def _blur(a, r):
    """Box blur of radius r, run twice (near-Gaussian), edges clamped."""
    import numpy as np
    for _ in range(2):
        for ax in (0, 1):
            p = np.pad(a, [(r + 1, r) if i == ax else (0, 0) for i in range(2)], mode="edge")
            c = np.cumsum(p, axis=ax)
            hi = np.take(c, range(2 * r + 1, c.shape[ax]), axis=ax)
            lo = np.take(c, range(0, c.shape[ax] - 2 * r - 1), axis=ax)
            a = (hi - lo) / (2 * r + 1)
    return a


def _lines(img, axis, min_share=0.15, thr=18):
    """Long straight edges (card, bar and panel borders) as [(position, start, end)].
    axis 1 = vertical lines at x, axis 0 = horizontal at y. Content is ignored, since it may be stand-in."""
    import numpy as np
    g = np.abs(np.diff(img, axis=axis))  # axis 1 = column-to-column change -> vertical edges
    if g.ndim == 3:
        g = g.max(axis=2)  # any channel, so same-brightness colour edges count
    strong = g > thr
    if axis == 1:
        strong = strong.T  # rows of `strong` are now image columns
    n_len = strong.shape[1]
    found = []
    for pos in range(strong.shape[0]):
        row = strong[pos]
        best, run, start, bs = 0, 0, 0, 0
        for i, v in enumerate(row):
            if v:
                if run == 0:
                    start = i
                run += 1
                if run > best:
                    best, bs = run, start
            else:
                run = 0 if run < 2 else run - 1  # allow a 1 px gap in soft photo edges
        if best >= min_share * n_len:
            found.append((pos + 1, bs, bs + best))
    merged = []
    for p_, a_, b_ in found:  # neighbouring positions are the same line
        if merged and p_ - merged[-1][0] <= 2:
            q = merged[-1]
            if (b_ - a_) > (q[2] - q[1]):
                merged[-1] = (p_, a_, b_)
        else:
            merged.append((p_, a_, b_))
    return merged


def _layout(R, N, tol=4):
    lr, ln = R, N
    out = {}
    for axis, name, coord in ((1, "vertical", "x"), (0, "horizontal", "y")):
        a_, b_ = _lines(lr, axis), _lines(ln, axis)
        missing = [l for l in a_ if not any(abs(l[0] - m[0]) <= tol for m in b_)]
        extra = [m for m in b_ if not any(abs(m[0] - l[0]) <= tol for l in a_)]
        span = "y" if coord == "x" else "x"
        for l in missing:
            near = min(b_, key=lambda m: abs(m[0] - l[0]), default=None)
            hint = f"; the rebuild's nearest is at {coord}={near[0]}" if near and abs(near[0] - l[0]) < 60 else ""
            out.setdefault("fix", []).append(f"original has a {name} edge at {coord}={l[0]} ({span} {l[1]}-{l[2]}) "
                                             f"that the rebuild lacks{hint}")
        for m in extra:
            out.setdefault("fix", []).append(f"rebuild has a {name} edge at {coord}={m[0]} ({span} {m[1]}-{m[2]}) "
                                             "the original does not")
        out[name] = {"original": [l[0] for l in a_], "rebuild": [m[0] for m in b_]}
    return out


def cmd_diff(a):
    _need_numpy()
    import numpy as np
    from PIL import Image
    ref = Image.open(a.ref).convert("RGB")
    now = Image.open(a.now).convert("RGB").resize(ref.size, Image.LANCZOS)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    R, N = np.asarray(ref).astype(float), np.asarray(now).astype(float)
    H, W = R.shape[:2]
    diff = np.abs(R - N).mean(axis=2)
    Image.fromarray(np.clip(diff * 3, 0, 255).astype(np.uint8)).save(out / "diff.png")
    Image.blend(ref, now, 0.5).save(out / "onion.png")
    match = round(1 - float(diff.mean()) / 255, 4)
    if a.blueprint:
        bp = json.loads(Path(a.blueprint).read_text(encoding="utf-8"))
        sx, sy = W / bp["frame_size"][0], H / bp["frame_size"][1]
        blocks = [dict(b, box=[round(b["box_px"][0] * sx), round(b["box_px"][1] * sy),
                               round(b["box_px"][2] * sx), round(b["box_px"][3] * sy)]) for b in bp["blocks"]]
    else:
        leaves = _segment(np.asarray(ref), (0, 0, W, H), 4, 6)
        blocks = [{"id": f"B{n}", "box": [x0, y0, x1 - x0, y1 - y0], "kind": _kind(np.asarray(ref)[y0:y1, x0:x1])}
                  for n, ((x0, y0, x1, y1), _) in enumerate(leaves, 1)]
    lr, ln = R.mean(axis=2), N.mean(axis=2)
    blurred = {}
    items = []
    for b in blocks:
        x, y, w, h = b["box"]
        if w < 4 or h < 4:
            continue
        rb = lr[y:y + h, x:x + w]
        here = float(np.abs(R[y:y + h, x:x + w] - N[y:y + h, x:x + w]).mean())
        rc, nc = R[y:y + h, x:x + w].reshape(-1, 3).mean(0), N[y:y + h, x:x + w].reshape(-1, 3).mean(0)
        dcol = float(np.abs(rc - nc).mean())
        best = (_ncc(rb, ln[y:y + h, x:x + w]), 0, 0)
        reach = max(6, int(0.2 * max(w, h)))
        step = max(1, reach // 12)
        if b.get("kind") != "flat":
            # Coarse pass on blurred images so near misses still score,
            # then every px around the best 3. Windows may overhang the
            # edge (a bar shifted sideways), then only the overlap counts.
            if step not in blurred:
                blurred[step] = (_blur(lr, step), _blur(ln, step)) if step > 1 else (lr, ln)
            br, bn = blurred[step]
            rbb = br[y:y + h, x:x + w]
            coarse = []
            for dy in range(-reach, reach + 1, step):
                for dx in range(-reach, reach + 1, step):
                    coarse.append((_ncc_at(rbb, bn, x + dx, y + dy), dx, dy))
            coarse.sort(reverse=True)
            tried = set()
            for _, cdx, cdy in coarse[:3]:
                for dy in range(max(-reach, cdy - step), min(reach, cdy + step) + 1):
                    for dx in range(max(-reach, cdx - step), min(reach, cdx + step) + 1):
                        if (dx, dy) in tried or (dx, dy) == (0, 0):
                            continue
                        tried.add((dx, dy))
                        s = _ncc_at(rb, ln, x + dx, y + dy)
                        if s > best[0]:
                            best = (s, dx, dy)
        sc, dx, dy = best
        problems = []
        if dcol > 18:
            problems.append(f"colour {_hex(nc)} where the original is {_hex(rc)}")
        if b.get("kind") != "flat":
            if sc < 0.5:
                problems.append(f"content does not match (best {sc:.2f}): missing, different or misdrawn")
            elif abs(dx) + abs(dy) >= 2:
                problems.append(f"content sits {abs(dx)} px {'left' if dx < 0 else 'right'} and {abs(dy)} px "
                                f"{'up' if dy < 0 else 'down'} of where it should")
        if problems:
            items.append({"block": b["id"], "kind": b.get("kind"), "box": [x, y, w, h], "difference": round(here, 1),
                          "match": round(sc, 2), "fix": "; ".join(problems)})
    items.sort(key=lambda i: -i["difference"] * i["box"][2] * i["box"][3])
    layout = _layout(R, N, a.tol)
    hist_p = out / "rounds.json"
    rounds = json.loads(hist_p.read_text(encoding="utf-8")) if hist_p.exists() else []
    rounds.append({"round": a.round or len(rounds) + 1, "pixel_match": match, "blocks_to_fix": len(items),
                   "layout_edges_to_fix": len(layout.get("fix", []))})
    atomic_write(hist_p, json.dumps(rounds, indent=1))
    trend = [r["pixel_match"] for r in rounds]
    res = {"pixel_match": match, "rounds": trend, "layout_to_fix": layout.get("fix", [])[:15],
           "edges": {k: v for k, v in layout.items() if k != "fix"}, "blocks_checked": len(blocks), "to_fix": items[:12],
           "diff": str(out / "diff.png"), "onion": str(out / "onion.png")}
    if len(trend) >= 2 and trend[-1] < trend[-2] - 0.005:
        res["warning"] = "worse than the previous round: undo the last change or fix one block at a time"
    if len(trend) >= 4 and trend[-1] - trend[-4] < 0.01:
        res["note"] = "four rounds without real gain: what is left may be content the evidence does not settle; stop here"
    res["next"] = ("open diff.png and onion.png with Read, fix the listed blocks (biggest first), screenshot the "
                   "rebuild with the same framing and run diff again with the same --out")
    print(json.dumps(res, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("rectify", help="a screen photographed at an angle, warped flat to the device size")
    s.add_argument("frame")
    s.add_argument("--corners", help="x,y of top-left, top-right, bottom-right, bottom-left")
    s.add_argument("--find-corners", action="store_true",
                   help="look for the bright screen quadrilateral and print the corners used")
    s.add_argument("--size", required=True, help="the device's screen size, e.g. 320x568")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_rectify)
    s = sub.add_parser("make", help="measured blocks, crops and a scaffold from a reference frame")
    s.add_argument("frame")
    s.add_argument("--out", required=True)
    s.add_argument("--viewport", help="WxH in CSS px the screen is rebuilt at (default: the frame's size)")
    s.add_argument("--depth", type=int, default=4, help="how many levels of cuts")
    s.add_argument("--min", type=int, default=6, help="smallest block side in frame px")
    s.set_defaults(fn=cmd_make)
    s = sub.add_parser("diff", help="the rebuild against the reference: what to fix this round")
    s.add_argument("ref")
    s.add_argument("now")
    s.add_argument("--out", required=True)
    s.add_argument("--blueprint")
    s.add_argument("--round", type=int)
    s.add_argument("--tol", type=int, default=4, help="px an edge may be off (more for photographed references)")
    s.set_defaults(fn=cmd_diff)
    _need_numpy()  # re-exec into the heavy env before args are rewritten
    ws_arg = pop_ws_arg()
    a = ap.parse_args()
    enter_ws(ws_arg)
    a.fn(a)


if __name__ == "__main__":
    main()
