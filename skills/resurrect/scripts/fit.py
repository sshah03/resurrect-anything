#!/usr/bin/env python3
"""Work out the rules behind observed behaviour. Close to decompiling, for programs that are gone.

Readings come from footage (`footage.py hud`, `shots.py track`, frame counts), guides, reviews and
discussion. Put them in a CSV or JSONL table, one reading per row. A `source` or `evidence` column is
carried through, so every rule cites the readings it rests on.

  rule DATA --y COL [--x COL,COL] [--noise N]
        Which formula produces y from the x columns: constant, linear, two-variable, product, quadratic,
        every-N steps, a change of slope or a cap at some point, power, exponential. Each is fitted, then
        its constants are snapped to the round numbers designers use, as long as the fit still holds
        within the reading noise. Says which rules fit, which reading breaks each one, and the one
        reading that would tell the two best apart.
  motion DATA --t COL --y COL [--x COL] [--fps 30] [--events t1,t2]
        Positions over time (`shots.py track` output works directly). Finds gravity shared across every
        arc, when the jumps/flaps happened, whether an input SETS the vertical speed or ADDS to it, a
        terminal speed if there is one, and horizontal speed. Per second and per frame, snapped.
  table DATA --y COL --by COL[,COL]
        A lookup (enemy -> points, item -> effect): every key's value, and keys whose readings disagree.
  cutoffs DATA --x COL --label COL
        Ranks, medals, grades or levels by score: where each boundary lies (between which readings) and
        the roundest number in that gap.
  timing DATA --t COL [--label COL] [--fps 30]
        Event times (spawns, waves, blinks). Tells a fixed interval from a ramp or random within a range,
        and checks whether the labels repeat in a fixed order.

Every result is an inference. Record it as `inferred` with the readings' evidence as `derived_from`
(`--batch FILE` writes that ledger line for `ledger.py add-batch`). A rule fitted to five readings is
weaker than one fitted to fifty, so the count is always reported.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import enter_ws, jsonl_lines, pop_ws_arg  # noqa: E402

NICE = [1000, 500, 250, 100, 50, 25, 20, 10, 5, 2, 1, 0.5, 0.25, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.001]


# ---------- data ----------

def load(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"no such file: {path}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".jsonl", ".ndjson"):
        rows = [json.loads(line) for line in jsonl_lines(text) if line.strip()]
    elif p.suffix.lower() == ".json":
        d = json.loads(text)
        rows = d.get("points") or d.get("rows") or d.get("readings") if isinstance(d, dict) else d
    else:
        rows = list(csv.DictReader(jsonl_lines(text)))
    if not rows:
        sys.exit("no rows")
    return rows


def num(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        return v if math.isfinite(v) else None
    s = str(v).strip().replace(",", "")
    if ":" in s:  # m:ss or h:mm:ss
        try:
            parts = [float(x) for x in s.split(":")]
            v = sum(x * 60 ** i for i, x in enumerate(reversed(parts)))
        except ValueError:
            return None
    else:
        try:
            v = float(s)
        except (ValueError, OverflowError):
            return None
    return v if math.isfinite(v) else None  # "nan" and "inf" parse as floats, not readings


def cite(r):
    return r.get("evidence") or r.get("source") or r.get("locator")


def fmt(v):
    if v is None:
        return "?"
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.4g}"


# ---------- least squares (small, dependency-free) ----------

def lsq(X, y, fixed=None):
    """Coefficients minimising |X c - y|. `fixed` ({j: value}) pins some of them."""
    fixed = fixed or {}
    free = [j for j in range(len(X[0])) if j not in fixed]
    yy = [yi - sum(row[j] * v for j, v in fixed.items()) for row, yi in zip(X, y)]
    k = len(free)
    if k == 0:
        return [fixed[j] for j in range(len(X[0]))]
    A = [[sum(row[free[a]] * row[free[b]] for row in X) + (1e-9 if a == b else 0) for b in range(k)] for a in range(k)]
    B = [sum(row[free[a]] * v for row, v in zip(X, yy)) for a in range(k)]
    for i in range(k):  # Gauss-Jordan with partial pivoting
        p = max(range(i, k), key=lambda r: abs(A[r][i]))
        A[i], A[p], B[i], B[p] = A[p], A[i], B[p], B[i]
        piv = A[i][i] or 1e-12
        for r in range(k):
            if r != i:
                f = A[r][i] / piv
                A[r] = [x - f * z for x, z in zip(A[r], A[i])]
                B[r] -= f * B[i]
    sol = {free[i]: B[i] / (A[i][i] or 1e-12) for i in range(k)}
    return [fixed[j] if j in fixed else sol[j] for j in range(len(X[0]))]


def nice_values(v):
    """Round candidates for v, coarsest first."""
    out = []
    for step in NICE:
        r = round(v / step) * step
        if abs(r - v) <= max(abs(v) * 0.5, step):
            out.append(round(r, 6))
    return out


# ---------- rule ----------

class Model:
    def __init__(self, name, labels, funcs, hyper=None, render=None):
        self.name, self.labels, self.funcs, self.hyper = name, labels, funcs, hyper or {}
        self.render = render

    def design(self, xs):
        return [[f(x) for f in self.funcs] for x in xs]


def candidate_models(cols, xs):
    """Every rule shape, with scanned breakpoints, step sizes and exponents."""
    ms = [Model("constant", ["c"], [lambda x: 1.0])]
    for i, c in enumerate(cols):
        vals = sorted({x[i] for x in xs})
        lo, hi = vals[0], vals[-1]
        ms.append(Model(f"linear in {c}", ["c", c], [lambda x: 1.0, lambda x, i=i: x[i]]))
        if len(vals) >= 4:
            ms.append(Model(f"quadratic in {c}", ["c", c, f"{c}^2"],
                            [lambda x: 1.0, lambda x, i=i: x[i], lambda x, i=i: x[i] ** 2]))
        # a + b*floor(x/N), like a level every 10 lines
        if all(abs(v - round(v)) < 1e-9 for v in vals) and hi - lo >= 3:
            for N in range(2, int(min(hi - lo, 200)) + 1):
                ms.append(Model(f"steps of {N} in {c}", ["c", f"floor({c}/{N})"],
                                [lambda x: 1.0, lambda x, i=i, N=N: math.floor(x[i] / N)], {"N": N}))
        # slope change at k, covers caps and floors
        if len(vals) >= 5:
            ks = sorted({(a + b) / 2 for a, b in zip(vals, vals[1:])} | {v for v in vals[1:-1]})
            for k in ks:
                ms.append(Model(f"slope changes at {c}={fmt(k)}", ["c", c, f"max(0,{c}-{fmt(k)})"],
                                [lambda x: 1.0, lambda x, i=i: x[i], lambda x, i=i, k=k: max(0.0, x[i] - k)], {"k": k}))
        if lo > 0:
            for b in [x / 20 for x in range(-60, 61) if x and x != 20]:  # skip b = 1, that's just linear
                ms.append(Model(f"power of {c}", [f"{c}^{fmt(b)}", "c"],
                                [lambda x, i=i, b=b: x[i] ** b, lambda x: 1.0], {"b": b}))
        for r in [1 + x / 100 for x in range(-50, 101, 1) if x]:
            # cap r^x at 1e12, in logs (r ** 200000 overflows)
            if max(hi * math.log(r), lo * math.log(r)) < math.log(1e12):
                ms.append(Model(f"exponential in {c}", [f"{fmt(r)}^{c}", "c"],
                                [lambda x, i=i, r=r: r ** x[i], lambda x: 1.0], {"r": r}))
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = cols[i], cols[j]
            ms.append(Model(f"linear in {a} and {b}", ["c", a, b],
                            [lambda x: 1.0, lambda x, i=i: x[i], lambda x, j=j: x[j]]))
            ms.append(Model(f"product of {a} and {b}", ["c", f"{a}*{b}"],
                            [lambda x: 1.0, lambda x, i=i, j=j: x[i] * x[j]]))
            ms.append(Model(f"linear in {a}, {b} and their product", ["c", a, b, f"{a}*{b}"],
                            [lambda x: 1.0, lambda x, i=i: x[i], lambda x, j=j: x[j], lambda x, i=i, j=j: x[i] * x[j]]))
    return ms


def evaluate(m, xs, ys, fixed=None):
    try:
        X = m.design(xs)
        if any(isinstance(v, complex) or not math.isfinite(v) for row in X for v in row):
            return None
        c = lsq(X, ys, fixed)
        pred = [sum(a * b for a, b in zip(row, c)) for row in X]
        res = [p - y for p, y in zip(pred, ys)]
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
    if not all(math.isfinite(v) for v in [*c, *res]):
        return None  # huge x, squared, can overflow the solve
    return c, res


def formula(m, c, y):
    parts = []
    for lab, v in zip(m.labels, c):
        if abs(v) < 1e-12:
            continue
        if lab == "c":
            parts.append(fmt(v))
        elif abs(v - 1) < 1e-12:
            parts.append(lab)
        else:
            parts.append(f"{fmt(v)}*{lab}")
    s = " + ".join(parts) or "0"
    return f"{y} = " + s.replace("+ -", "- ")


def cmd_rule(a):
    rows = load(a.data)
    cols = a.x.split(",") if a.x else [k for k in rows[0] if k not in (a.y, "source", "evidence", "locator", "note")]
    data = []
    for r in rows:
        x = [num(r.get(c)) for c in cols]
        y = num(r.get(a.y))
        if y is None or any(v is None for v in x):
            continue
        data.append((x, y, r))
    if len(data) < 3:
        sys.exit(f"need at least 3 readings with numeric {a.y} and {', '.join(cols)}; have {len(data)}. "
                 "Non-numeric keys (enemy names): use `table`")
    xs, ys = [d[0] for d in data], [d[1] for d in data]
    if a.noise is not None:
        tol, tol_why = a.noise, "given"
    elif all(abs(v - round(v)) < 1e-9 for v in ys):
        tol, tol_why = 0.5, "readings are whole numbers (exact counters): a rule must reproduce them to within 0.5"
    else:
        tol = 0.02 * ((max(ys) - min(ys)) or abs(ys[0]) or 1)
        tol_why = "assumed 2% of the range: pass --noise with the reading precision (e.g. 1 px, 1 frame, 0.05 s)"
    n = len(ys)
    found = []
    for m in candidate_models(cols, xs):
        k = len(m.funcs) + len(m.hyper)
        if k >= n:
            continue  # with as many knobs as readings, anything fits
        ev = evaluate(m, xs, ys)
        if not ev:
            continue
        c, res = ev
        try:
            rss = sum(r * r for r in res)
        except OverflowError:
            continue
        if not math.isfinite(rss):
            continue
        found.append({"m": m, "c": c, "res": res, "worst": max(abs(r) for r in res), "k": k,
                      "bic": n * math.log(rss / n + 1e-12) + k * math.log(n)})
    # best of each shape, the scans make near-copies
    best = {}
    for f in found:
        key = f["m"].name.split(" at ")[0].split(" of ")[0] if f["m"].hyper else f["m"].name
        key = f["m"].name if not f["m"].hyper else key + "|" + ",".join(c for c in cols if c in f["m"].name)
        if key not in best or f["bic"] < best[key]["bic"]:
            best[key] = f
    fits = sorted([f for f in best.values() if f["worst"] <= tol], key=lambda f: (f["k"], f["worst"]))
    others = sorted([f for f in best.values() if f["worst"] > tol], key=lambda f: f["bic"])
    out_rules, seen = [], set()
    for f in fits:
        if len(out_rules) >= a.top:
            break
        m, c = f["m"], list(f["c"])
        fixed = {}
        for j in sorted(range(len(c)), key=lambda j: -abs(c[j])):  # snap the big constants first
            for v in nice_values(c[j]):
                ev = evaluate(m, xs, ys, {**fixed, j: v})
                if ev and max(abs(r) for r in ev[1]) <= tol:
                    fixed[j] = v
                    c, res = ev
                    break
        ev = evaluate(m, xs, ys, fixed)
        c, res = ev
        if formula(m, c, a.y) in seen:
            continue  # another shape snapped to the same rule
        seen.add(formula(m, c, a.y))
        reading = None
        if "k" in m.hyper and len(c) == 3:  # say so plainly if it's a cap or a start
            k_, before, after = m.hyper["k"], c[1], c[1] + c[2]
            col = m.labels[1]
            at = c[0] + c[1] * k_
            if abs(after) < 1e-9:
                reading = f"{a.y} grows with {col} until {col} = {fmt(k_)}, then stays at {fmt(at)}: {a.y} = min({fmt(at)}, {formula(m, c[:2] + [0], a.y).split(' = ')[1]})"
            elif abs(before) < 1e-9:
                reading = f"{a.y} stays at {fmt(c[0])} until {col} = {fmt(k_)}, then grows by {fmt(after)} per {col}"
            else:
                reading = f"{a.y} changes by {fmt(before)} per {col} up to {fmt(k_)}, then by {fmt(after)}"
        out_rules.append({"rule": formula(m, c, a.y), **({"reading": reading} if reading else {}),
                          "shape": m.name, "params": f["k"],
                          "worst_error": round(max(abs(r) for r in res), 4), "fits_all": True, "_m": m, "_c": c})
    near = []
    for f in others[: max(0, a.top - len(out_rules))]:
        i = max(range(n), key=lambda i: abs(f["res"][i]))
        near.append({"rule": formula(f["m"], f["c"], a.y), "shape": f["m"].name, "params": f["k"],
                     "worst_error": round(f["worst"], 3), "fits_all": False,
                     "breaks_on": {**{c: data[i][0][j] for j, c in enumerate(cols)}, a.y: ys[i],
                                   "predicted": round(ys[i] + f["res"][i], 3), "source": cite(data[i][2])}})
    result = {"readings": n, "y": a.y, "x": cols, "tolerance": tol, "tolerance_why": tol_why,
              "rules": [{k: v for k, v in r.items() if not k.startswith("_")} for r in out_rules] + near}
    if not out_rules:
        result["verdict"] = ("no rule shape here reproduces every reading within the tolerance: check the outlying "
                             "readings (misread, other version, a hidden variable such as difficulty or combo), add "
                             "the missing variable as a column, or widen --noise if the readings are approximate")
    elif len(out_rules) >= 2:
        result["verdict"] = (f"{len(out_rules)} rules fit every reading; the simplest is listed first. Prefer it "
                             "unless a source says otherwise, and settle it with the reading below")
        result["settle_with"] = _discriminate(out_rules[0], out_rules[1], cols, xs, a.y, tol)
    else:
        result["verdict"] = "one rule shape fits every reading"
    if n < 2 * (out_rules[0]["params"] if out_rules else 2) + 2:
        result["caution"] = f"only {n} readings: a few more, at values not yet seen, would make this much firmer"
    _batch(a, result, data, out_rules[0]["rule"] if out_rules else None)
    print(json.dumps(result, indent=1))


def _discriminate(r1, r2, cols, xs, y, tol):
    """The next reading to get: an unmeasured input where the top two rules differ beyond the noise."""
    measured = {tuple(x) for x in xs}
    grid = []
    for j in range(len(cols)):
        vals = sorted({x[j] for x in xs})
        lo, hi = vals[0], vals[-1]
        span = hi - lo or 1
        g = set(vals + [lo + span * t / 8 for t in range(0, 13)] + [(p + q) / 2 for p, q in zip(vals, vals[1:])])
        if all(abs(v - round(v)) < 1e-9 for v in vals):  # counts stay counts
            g = {float(round(v)) for v in g}
        grid.append(sorted(g))
    pts = [[]]
    for g in grid:
        pts = [p + [v] for p in pts for v in g][:5000]
    best = None
    for p in pts:
        if tuple(p) in measured:
            continue  # already measured, can't tell the rules apart
        try:
            a = sum(c * f(p) for c, f in zip(r1["_c"], r1["_m"].funcs))
            b = sum(c * f(p) for c, f in zip(r2["_c"], r2["_m"].funcs))
        except (OverflowError, ValueError, ZeroDivisionError, TypeError):
            continue
        if isinstance(a, complex) or isinstance(b, complex) or not (math.isfinite(a) and math.isfinite(b)):
            continue
        if abs(a - b) <= tol:
            continue
        if best is None or abs(a - b) > best[0]:
            best = (abs(a - b), p, a, b)
    if not best:
        return {"none": f"no input near the readings makes {r1['rule']} and {r2['rule']} differ by more than "
                        f"the tolerance ({fmt(tol)}): they agree here, so either serves; a source would settle it"}
    return {"look_for": {c: round(v, 3) for c, v in zip(cols, best[1])},
            r1["rule"]: round(best[2], 3), r2["rule"]: round(best[3], 3)}


def _batch(a, result, data, claim_rule):
    if not getattr(a, "batch", None) or not claim_rule:
        return
    ev = sorted({str(cite(d[2])) for d in data if cite(d[2])})
    item = {"tag": "inferred", "kind": "behavior", "claim": f"{claim_rule} (fitted to {len(data)} readings, "
            f"worst error {result['rules'][0]['worst_error']})", "derived_from": ev[:50],
            "feature": getattr(a, "feature", None) or result.get("y")}
    with open(a.batch, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(item) + "\n")
    result["batch"] = f"appended to {a.batch}: run ledger.py add-batch {a.batch}"


# ---------- motion ----------

def cmd_motion(a):
    rows = load(a.data)
    pts = sorted({(num(r.get(a.t)), num(r.get(a.y)), num(r.get(a.x)) if a.x else None)
                  for r in rows if num(r.get(a.t)) is not None and num(r.get(a.y)) is not None})
    if len(pts) < 5:
        sys.exit("need at least 5 timed positions")
    ts, ys = [p[0] for p in pts], [p[1] for p in pts]
    vs = [(ys[i + 1] - ys[i]) / ((ts[i + 1] - ts[i]) or 1e-9) for i in range(len(ts) - 1)]
    n = len(ts)

    # Shared g, per-arc start height and speed. With g fixed,
    # each arc is a line fit of y - g*u^2/2 on [1, u], so
    # per-arc sums make trying a new break O(1).
    binom = [[math.comb(k, j) for j in range(5)] for k in range(5)]

    def sums(lo, hi):
        """Sums of u^k (k <= 4), u^k * y (k <= 2) and y^2 over points lo..hi-1, u = time since ts[lo]."""
        out, acc = [], [0.0] * 9
        t0, y0 = (ts[lo], ys[lo]) if lo < n else (0.0, 0.0)
        out.append(list(acc))
        for i in range(lo, hi):
            u, y = ts[i] - t0, ys[i] - y0  # relative to arc start, keeps sums exact
            u2 = u * u
            acc = [acc[0] + 1, acc[1] + u, acc[2] + u2, acc[3] + u2 * u, acc[4] + u2 * u2,
                   acc[5] + y, acc[6] + u * y, acc[7] + u2 * y, acc[8] + y * y]
            out.append(acc)
        return out  # prefix sums, out[j] covers lo..lo+j-1

    def later(st, d):
        """The same sums with u measured from d later (u -> u - d)."""
        S, T = st[:5], st[5:8]
        S2 = [sum(binom[k][j] * (-d) ** (k - j) * S[j] for j in range(k + 1)) for k in range(5)]
        T2 = [sum(binom[k][j] * (-d) ** (k - j) * T[j] for j in range(k + 1)) for k in range(3)]
        return S2 + T2 + [st[8]]

    def part(st):
        """An arc's <q,q>, <q,y>, <y,y> with [1, u] projected out (q = u^2/2)."""
        m, su, su2, su3, su4, sy, suy, su2y, syy = st
        det = m * su2 - su * su
        if det > 1e-12 * max(1.0, m * su2):
            inv = (su2 / det, -su / det, m / det)
        else:  # a single point only gives a start height
            inv = (1 / m if m else 0.0, 0.0, 0.0)

        def proj(v, w):
            return v[0] * (inv[0] * w[0] + inv[1] * w[1]) + v[1] * (inv[1] * w[0] + inv[2] * w[1])
        zq, zy = (0.5 * su2, 0.5 * su3), (sy, suy)
        return (0.25 * su4 - proj(zq, zq), 0.5 * su2y - proj(zq, zy), syy - proj(zy, zy), inv, zq, zy)

    def solve(parts):
        qq, qy, yy = (sum(p[k] for p in parts) for k in range(3))
        g = qy / qq if qq > 1e-12 else 0.0
        rss = max(0.0, yy - g * qy)
        return g, rss

    def coef(p, g):
        inv, zq, zy = p[3], p[4], p[5]
        r0, r1 = zy[0] - g * zq[0], zy[1] - g * zq[1]
        return inv[0] * r0 + inv[1] * r1, inv[1] * r0 + inv[2] * r1  # start y (from the arc's start), speed

    def score(rss, nseg):
        return n * math.log(rss / n + 1e-9) + (2 * nseg + 1) * math.log(n)

    def arcs(cuts):
        """Fit shared gravity and per-arc start height and speed. cuts = indices where arcs begin."""
        bounds = [0] + sorted(cuts) + [n]
        parts = [part(sums(bounds[s_], bounds[s_ + 1])[-1]) for s_ in range(len(bounds) - 1)]
        g, _ = solve(parts)
        c, rss = [], 0.0
        for s_, p in enumerate(parts):
            y0, v0 = coef(p, g)
            y0 += ys[bounds[s_]]
            c += [y0, v0]
            for i in range(bounds[s_], bounds[s_ + 1]):  # direct residuals, exact where the sums cancel
                u = ts[i] - ts[bounds[s_]]
                rss += (y0 + v0 * u + 0.5 * g * u * u - ys[i]) ** 2
        return c + [g], rss, bounds, score(rss, len(parts))

    def kick(cuts, i):
        # an input must start moving up faster than before
        c, _, bounds, _ = arcs(cuts + [i])
        s_ = bounds.index(i)
        prev_v = c[2 * (s_ - 1) + 1] + c[-1] * (ts[i] - ts[bounds[s_ - 1]])
        return c[2 * s_ + 1] < min(0.0, prev_v)  # an input starts the object moving up

    def best_break(cuts):
        """Best new arc break that cuts the score by over 6 and looks like an input, or None."""
        bounds = [0] + sorted(cuts) + [n]
        nseg = len(bounds) - 1
        segs = [sums(bounds[s_], bounds[s_ + 1]) for s_ in range(nseg)]
        parts = [part(p[-1]) for p in segs]
        _, rss0 = solve(parts)
        bic = score(rss0, nseg)
        tot = [sum(p[k] for p in parts) for k in range(3)]
        best = None
        for s_ in range(nseg):
            b0, b1, pre = bounds[s_], bounds[s_ + 1], segs[s_]
            for i in range(max(3, b0 + 3), min(n - 2, b1 - 2)):
                left = part(pre[i - b0])
                right = part(later([x - y for x, y in zip(pre[-1], pre[i - b0])], ts[i] - ts[b0]))
                qq, qy, yy = (tot[k] - parts[s_][k] + left[k] + right[k] for k in range(3))
                g = qy / qq if qq > 1e-12 else 0.0
                b2 = score(max(0.0, yy - g * qy), nseg + 1)
                if not (b2 < bic - 6 and (best is None or b2 < best[0])):
                    continue
                prev_v = coef(left, g)[1] + g * (ts[i] - ts[b0])
                if coef(right, g)[1] < min(0.0, prev_v):  # an input starts the object moving up
                    best = (b2, i)
        return best
    # input = sudden upward jump in speed (screen y points down)
    if a.events:
        ev = sorted(num(x) for x in a.events.split(","))
        cuts = sorted({min(range(n), key=lambda i: abs(ts[i] - t)) for t in ev} - {0})
    else:
        dv = [vs[i + 1] - vs[i] for i in range(len(vs) - 1)]
        typical = sorted(abs(d) for d in dv)[len(dv) // 2] or 1e-9
        floor = max(4 * typical, 0.35 * abs(min(dv)) if dv else 0)
        cuts = []
        for i, d in enumerate(dv):
            if d < -floor and (not cuts or i + 1 - cuts[-1] >= 3):
                cuts.append(i + 1)
        # add breaks that clearly help, drop the rest
        for _ in range(30):
            best = best_break(cuts)
            if not best:
                break
            cuts = sorted(cuts + [best[1]])
        for cpos in list(cuts):
            rest = [x for x in cuts if x != cpos]
            if arcs(rest)[3] < arcs(cuts)[3] or not kick(rest, cpos):
                cuts = rest
    c, rss, bounds, _ = arcs(cuts)
    g = c[-1]
    rms = math.sqrt(rss / n)
    nseg = len(bounds) - 1
    starts = [ts[b] for b in bounds[:-1]]
    seg_of = [s_ for s_ in range(nseg) for _ in range(bounds[s_], bounds[s_ + 1])]
    ev_t = starts[1:]
    segs = [{"from_t": round(starts[s_], 4), "points": bounds[s_ + 1] - bounds[s_], "start_y": round(c[2 * s_], 2),
             "start_vy": round(c[2 * s_ + 1], 2)} for s_ in range(nseg)]
    out = {"points": n, "arcs": len(segs), "inputs_at": [round(t, 4) for t in ev_t],
           "gravity_px_s2": round(g, 2), "rms_px": round(rms, 3), "arcs_detail": segs}
    # does an input set the speed, or add to it?
    after = [sg["start_vy"] for sg in segs[1:]]
    before = []
    for s in range(1, len(segs)):
        p = segs[s - 1]
        before.append(p["start_vy"] + g * (segs[s]["from_t"] - p["from_t"]))
    if len(after) >= 2:
        sd = lambda v: math.sqrt(sum((x - sum(v) / len(v)) ** 2 for x in v) / len(v))  # noqa: E731
        delta = [x - b for x, b in zip(after, before)]
        out["input"] = ({"sets_vy_to": round(sum(after) / len(after), 2), "spread": round(sd(after), 2),
                         "rule": "each input sets the vertical speed to the same value, whatever the speed before"}
                        if sd(after) <= sd(delta) else
                        {"adds_to_vy": round(sum(delta) / len(delta), 2), "spread": round(sd(delta), 2),
                         "rule": "each input adds the same amount to the vertical speed"})
        k_ = "sets_vy_to" if "sets_vy_to" in out["input"] else "adds_to_vy"
        long_ = [sg["start_vy"] for sg in segs[1:] if sg["points"] >= 6]
        if out["input"]["spread"] > 0.2 * abs(out["input"][k_]):
            out["input"]["caution"] = ("the inputs disagree by more than 20%: short arcs (a few frames between taps) give "
                                       "noisy speeds. Trust arcs with 6+ points" +
                                       (f": their start speeds are {[round(v, 1) for v in long_]}" if long_ else "") +
                                       "; or track a steadier point of the sprite (its top edge)")
    # terminal speed: stops growing late in long arcs
    vmax_seen = max(vs)
    if g > 0 and len(ts) >= 8:
        best = None
        for frac in [x / 20 for x in range(8, 20)]:
            vm = vmax_seen * frac + 1e-9
            yy = []
            for s in range(nseg):
                idx = [i for i, ss in enumerate(seg_of) if ss == s]
                if not idx:
                    continue
                y0, v0 = c[2 * s], c[2 * s + 1]
                for i in idx:
                    u = ts[i] - starts[s]
                    tc = (vm - v0) / g if g else 1e9
                    yy.append((i, y0 + v0 * u + 0.5 * g * u * u if u <= tc or tc < 0
                               else y0 + v0 * tc + 0.5 * g * tc * tc + vm * (u - tc)))
            r2 = math.sqrt(sum((ys[i] - v) ** 2 for i, v in yy) / len(yy))
            if best is None or r2 < best[1]:
                best = (vm, r2)
        if best and best[1] < 0.7 * rms:
            out["terminal_vy_px_s"] = round(best[0], 1)
            out["rms_with_terminal_px"] = round(best[1], 3)
    if a.x:
        xs = [p[2] for p in pts if p[2] is not None]
        if len(xs) == len(ts):
            cx = lsq([[1.0, t] for t in ts], xs)
            out["vx_px_s"] = round(cx[1], 2)
    if a.fps:
        f = a.fps
        per = {"gravity_px_per_frame2": g / f / f}
        if "input" in out:
            k = "sets_vy_to" if "sets_vy_to" in out["input"] else "adds_to_vy"
            per[f"input_{k}_px_per_frame"] = out["input"][k] / f
        if "terminal_vy_px_s" in out:
            per["terminal_vy_px_per_frame"] = out["terminal_vy_px_s"] / f
        if "vx_px_s" in out:
            per["vx_px_per_frame"] = out["vx_px_s"] / f
        scale = a.scale or 1
        out["per_frame"] = {k: {"value": round(v / scale, 4), "round": nice_values(v / scale)[-3:]} for k, v in per.items()}
        gf = g / f / f / scale
        for k in list(out["per_frame"]):
            if k.startswith("input_sets"):  # per-frame stepping lands half a step off the curve
                v = out["per_frame"][k]["value"]
                out["per_frame"][k]["if_position_moves_before_speed_changes"] = round(v + gf / 2, 4)
                out["per_frame"][k]["if_speed_changes_before_position_moves"] = round(v - gf / 2, 4)
                out["per_frame"][k]["note"] = ("games step once per frame, which shifts the fitted launch speed by half "
                                               "a gravity step; whichever of the two is a round number is the original")
        out["per_frame_note"] = (f"at {fmt(f)} fps" + (f", divided by {fmt(scale)} video px per game px" if a.scale else
                                 " in video px: pass --scale (video px per game px) for the game's own units") +
                                 ". Flash and most 2D games update speed once per frame, so round numbers here "
                                 "are likely the original constants")
    out["next"] = ("rms is the fit's error in px: near the tracking precision means the motion model is right. "
                   "Use the per-frame numbers in the rebuild and check them with footdiff.py.")
    print(json.dumps(out, indent=1))


# ---------- table ----------

def cmd_table(a):
    rows = load(a.data)
    keys = a.by.split(",")
    groups = {}
    for r in rows:
        k = tuple(str(r.get(c, "")).strip() for c in keys)
        v = r.get(a.y)
        if v is None or v == "":
            continue
        groups.setdefault(k, []).append((num(v) if num(v) is not None else str(v).strip(), cite(r)))
    table, conflicts = [], []
    for k, vals in sorted(groups.items()):
        distinct = sorted({str(v) for v, _ in vals})
        numeric = all(isinstance(v, float) for v, _ in vals)
        agree = len(distinct) == 1 or (numeric and a.noise and max(v for v, _ in vals) - min(v for v, _ in vals) <= a.noise)
        row = {**dict(zip(keys, k)), a.y: vals[0][0] if agree else distinct, "readings": len(vals)}
        table.append(row)
        if not agree:
            conflicts.append({**dict(zip(keys, k)), "values": {d: [s for v, s in vals if str(v) == d] for d in distinct}})
    out = {"keys": keys, "y": a.y, "entries": table, "conflicts": conflicts,
           "verdict": ("every key has one value" if not conflicts else
                       f"{len(conflicts)} keys have readings that disagree: another variable (level, combo, version, "
                       "difficulty) is involved, or a reading is wrong. Add that variable to --by, or check the sources")}
    single = [t for t in table if t["readings"] == 1]
    if single:
        out["resting_on_one_reading"] = [dict((c, t[c]) for c in keys) for t in single]
    print(json.dumps(out, indent=1))


# ---------- cutoffs ----------

def cmd_cutoffs(a):
    rows = load(a.data)
    pts = sorted(((num(r.get(a.x)), str(r.get(a.label)).strip(), cite(r)) for r in rows
                  if num(r.get(a.x)) is not None and r.get(a.label) not in (None, "")),
                 key=lambda p: (p[0], p[1]))  # some rows lack a source, don't sort on it
    order = []
    for _, lab, _ in pts:
        if not order or order[-1] != lab:
            order.append(lab)
    bands = {}
    for x, lab, _ in pts:
        lo, hi = bands.get(lab, (x, x))
        bands[lab] = (min(lo, x), max(hi, x))
    clashes = [lab for lab in set(order) if order.count(lab) > 1]
    cuts = []
    for i in range(len(order) - 1):
        lo_lab, hi_lab = order[i], order[i + 1]
        lo, hi = bands[lo_lab][1], bands[hi_lab][0]
        guess = None
        for step in NICE:  # roundest number in the gap ("at least N")
            k = math.ceil((lo + 1e-9) / step) * step
            if lo < k <= hi:
                guess = k
                break
        cuts.append({"from": lo_lab, "to": hi_lab, "boundary_between": [lo, hi],
                     "roundest_guess": guess, "rule_guess": f"{a.x} >= {fmt(guess)} gives {hi_lab}" if guess is not None else None})
    out = {"readings": len(pts), "labels_in_order": order, "bands_seen": {k: list(v) for k, v in bands.items()},
           "cutoffs": cuts}
    if clashes:
        out["warning"] = (f"labels {clashes} appear in more than one stretch of {a.x}: the label depends on something "
                          "else as well (time, level), or a reading is wrong")
    out["next"] = ("each boundary is only known to lie in its gap; a reading inside a gap narrows it. Record the "
                   "guess as inferred and the gap as the bound (`searched`/`would_settle` in the ledger)")
    print(json.dumps(out, indent=1))


# ---------- timing ----------

def cmd_timing(a):
    rows = load(a.data)
    pts = sorted((num(r.get(a.t)), str(r.get(a.label)).strip() if a.label else None) for r in rows
                 if num(r.get(a.t)) is not None)
    ts = [p[0] for p in pts]
    if len(ts) < 4:
        sys.exit("need at least 4 event times")
    d = [b - a_ for a_, b in zip(ts, ts[1:])]
    n = len(d)
    mean = sum(d) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in d) / n)
    out = {"events": len(ts), "intervals": {"mean": round(mean, 4), "sd": round(sd, 4), "min": round(min(d), 4),
                                            "max": round(max(d), 4)}}
    # ramp: intervals shrinking or growing?
    mid = [(a_ + b) / 2 for a_, b in zip(ts, ts[1:])]
    c = lsq([[1.0, m] for m in mid], d)
    pred = [c[0] + c[1] * m for m in mid]
    ss_res = sum((x - p) ** 2 for x, p in zip(d, pred))
    ss_tot = sum((x - mean) ** 2 for x in d) or 1e-12
    r2 = 1 - ss_res / ss_tot
    jitter = a.noise if a.noise is not None else (1 / a.fps if a.fps else 0.05)
    if sd <= jitter:
        out["pattern"] = "fixed interval"
        out["interval"] = round(mean, 4)
        out["round"] = nice_values(mean)[-3:]
    elif r2 > 0.6 and n >= 5:
        out["pattern"] = "ramp"
        out["interval_at_t"] = f"{fmt(round(c[0], 4))} + {fmt(round(c[1], 5))} * t"
        out["r2"] = round(r2, 3)
    else:
        span = max(d) - min(d)
        out["pattern"] = "random within a range"
        out["range_estimate"] = [round(max(0.0, min(d) - span / max(1, n - 1)), 4), round(max(d) + span / max(1, n - 1), 4)]
        out["note"] = (f"{n} intervals: the true range is a little wider than the one seen (estimate above). "
                       "Uniform is the usual choice in Flash games (Math.random()*range + min)")
    if a.fps:
        out["in_frames"] = {"mean": round(mean * a.fps, 2), "min": round(min(d) * a.fps, 2), "max": round(max(d) * a.fps, 2)}
    if a.label:
        labs = [p[1] for p in pts]
        period = next((p for p in range(1, len(labs) // 2 + 1)
                       if all(labs[i] == labs[i + p] for i in range(len(labs) - p))), None)
        counts = {}
        for lab in labs:
            counts[lab] = counts.get(lab, 0) + 1
        out["labels"] = ({"repeats_every": period, "cycle": labs[:period]} if period else
                         {"repeats": False, "shares": {k: round(v / len(labs), 3) for k, v in sorted(counts.items())},
                          "note": "no fixed order: drawn at random, with these shares so far"})
    print(json.dumps(out, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("rule", help="which formula gives y from x")
    s.add_argument("data")
    s.add_argument("--y", required=True)
    s.add_argument("--x", help="comma-separated input columns (default: every other column)")
    s.add_argument("--noise", type=float, help="how far a reading may be off (1 px, 1 frame, 0.05 s)")
    s.add_argument("--top", type=int, default=4)
    s.add_argument("--batch", help="append the best rule as an inferred ledger item to this JSONL file")
    s.add_argument("--feature")
    s.set_defaults(fn=cmd_rule)
    s = sub.add_parser("motion", help="gravity, inputs, terminal speed from positions over time")
    s.add_argument("data")
    s.add_argument("--t", default="t")
    s.add_argument("--y", default="y")
    s.add_argument("--x")
    s.add_argument("--fps", type=float)
    s.add_argument("--scale", type=float, help="video px per game px")
    s.add_argument("--events", help="comma-separated input times, when known")
    s.set_defaults(fn=cmd_motion)
    s = sub.add_parser("table", help="a lookup from keys to values, with disagreements")
    s.add_argument("data")
    s.add_argument("--y", required=True)
    s.add_argument("--by", required=True)
    s.add_argument("--noise", type=float)
    s.set_defaults(fn=cmd_table)
    s = sub.add_parser("cutoffs", help="where ranks or grades change")
    s.add_argument("data")
    s.add_argument("--x", required=True)
    s.add_argument("--label", required=True)
    s.set_defaults(fn=cmd_cutoffs)
    s = sub.add_parser("timing", help="intervals between events, and label order")
    s.add_argument("data")
    s.add_argument("--t", required=True)
    s.add_argument("--label")
    s.add_argument("--fps", type=float)
    s.add_argument("--noise", type=float, help="timing precision in seconds (default 1 frame, or 0.05 s)")
    s.set_defaults(fn=cmd_timing)
    ws_arg = pop_ws_arg()
    a = ap.parse_args()
    enter_ws(ws_arg)  # DATA and --batch resolve inside the workspace
    a.fn(a)


if __name__ == "__main__":
    main()
