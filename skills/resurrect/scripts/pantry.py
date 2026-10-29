#!/usr/bin/env python3
"""Discontinued foods and drinks. Recover the formula from what the label still tells us.

A packaged food's label is a leaked spec. Ingredients are listed in descending order by
weight, and the nutrition panel fixes how much fat, sugar, protein and salt the mix holds.
Given per-100g values for each ingredient, the proportions are constrained enough to solve.

Subcommands
  hunt NAME [--brand B] [--years 1991-1998] [--aka "Other Name"] [--credits ARTICLE_URL ...]
                                 Finds where an old product's own label still exists. Prints the searches
                                 that turn up photos of the box or pack (collectors on Flickr, auction
                                 listings, collector blogs, forum threads) with every spelling of the name.
                                 Run these before any database, since pre-2010 labels mostly survive as photos.
  label QUERY | --barcode CODE   Open Food Facts: ingredients text, nutrition per 100 g, images.
                                 Discontinued products usually keep their entries.
  food QUERY [--offline]         USDA FoodData Central: per-100 g nutrients for one ingredient
                                 (uses $USDA_API_KEY, else DEMO_KEY, which allows ~30 calls/hour.
                                 Falls back to a cached offline SR Legacy table when rate-limited.)
  retail URL                     Reads a retailer's product page (Instacart, Walmart, Target...) for the
                                 maker's ingredient statement and nutrition in its embedded JSON.
  solve RECIPE.json              Fit ingredient grams per 100 g of product to the label target,
                                 keeping label order (w1 >= w2 >= ...). Writes the fitted grams back.
  match RECIPE.json [--out F]    Compare the recipe's computed nutrition with the label target and
                                 check label order. Writes codex/label-match.json by default.

RECIPE.json (written by the agent; every number cites evidence)
{
  "basis": "per 100 g of finished product",
  "target": {"kcal": 500, "fat": 25, "carbs": 62, "sugars": 3, "protein": 7, "sodium_mg": 600,
             "serving_g": 28, "evidence": ["E0012"]},   # serving_g lets match allow for label rounding
  "ingredients": [                       # label order, first = heaviest
    {"name": "corn masa", "grams": null, "per100g": {"kcal": 365, "fat": 4.7, "carbs": 76,
     "sugars": 0.6, "protein": 9.4, "sodium_mg": 5}, "source": "USDA FDC 168937", "evidence": ["E0020"]},
    {"name": "salt", "grams": 1.2, "fixed": true, "per100g": {"sodium_mg": 38758}, ...},
    {"name": "paprika", "grams": null, "min_grams": 0.5, ...}   # present at least this much (default 0.05)
  ],
  "cook_loss": 0.0,                      # fraction of raw weight lost (frying, baking), capped by water
  "order": "label"                       # or "unknown" when no ingredient list survives
}
For restaurant items and other per-sandwich data, set "basis": "serving". Targets are then per serving,
ingredients with "grams" + "per100g" are fitted, and known components go in with "per_serving":
{...} (the maker's own component nutrition) and no grams. The report's fitted_share says how much of
the item the fit really decided.
"part_order": {"blue cheese": "unknown"} turns the order check off for one part whose list did not survive.
"g_per_tsp" on an ingredient overrides the spoon density table used for batch measures.
"two_percent_or_less": true marks ingredients after "Contains 2% or less of". Each is capped at 2 g per 100 g,
with no order among them.
"omitted": true keeps a label ingredient on record but out of the mix and the order check (EDTA, preservatives
left out at home). "batch_g": 1200 makes `match` print every ingredient scaled to the batch. Mark fixed
amounts you sized by hand to hit a number with "sized_to": "sodium" so the fit counts them as tuned.
For filled or layered products, give each ingredient a "part" ("dough", "filling"). Label order is
checked within each part, in list order. "max_grams" caps an ingredient. "target.source" names
where the numbers came from (the maker's label vs. a calorie database) and shows on the scoreboard.
Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import USER_AGENT, atomic_write, emit, enter_ws, fetch_json, log, pop_ws_arg  # noqa: E402

NUTRIENTS = ("kcal", "fat", "carbs", "sugars", "protein", "sodium_mg", "fiber", "satfat", "chol_mg")  # not water, it cooks off
OFF_FIELDS = {"kcal": "energy-kcal_100g", "fat": "fat_100g", "carbs": "carbohydrates_100g",
              "sugars": "sugars_100g", "protein": "proteins_100g", "fiber": "fiber_100g",
              "satfat": "saturated-fat_100g", "sodium_mg": "sodium_100g", "chol_mg": "cholesterol_100g"}
FDC_IDS = {1051: "water", 1008: "kcal", 2047: "kcal", 1004: "fat", 1005: "carbs", 2000: "sugars", 1003: "protein",
           1093: "sodium_mg", 1079: "fiber", 1258: "satfat", 1253: "chol_mg"}


# ------------------------------------------------------------------ sources

def _off_row(p: dict) -> dict:
    n = p.get("nutriments") or {}
    per = {}
    for k, f in OFF_FIELDS.items():
        v = n.get(f)
        if isinstance(v, (int, float)):
            per[k] = round(v * 1000, 1) if k in ("sodium_mg", "chol_mg") else round(v, 2)  # OFF stores g
    code = p.get("code", "")
    return {"name": p.get("product_name") or "", "brand": p.get("brands") or "", "barcode": code,
            "ingredients": (p.get("ingredients_text_en") or p.get("ingredients_text") or "")[:1500],
            "per100g": per, "serving": p.get("serving_size") or "",
            "image": p.get("image_front_url") or "", "label_image": p.get("image_nutrition_url") or "",
            "ingredients_image": p.get("image_ingredients_url") or "",
            "last_modified": p.get("last_modified_t"),
            **({"all_images": p["all_images"]} if p.get("all_images") else {}),
            "url": f"https://world.openfoodfacts.org/product/{code}"}


def cmd_label(a):
    fields = ("product_name,brands,code,ingredients_text,ingredients_text_en,nutriments,serving_size,"
              "image_front_url,image_nutrition_url,image_ingredients_url,last_modified_t")
    if a.barcode:
        data = fetch_json(f"https://world.openfoodfacts.org/api/v2/product/{a.barcode}.json?fields={fields},images")
        prods = [data["product"]] if data and data.get("product") else []
        for p in prods:  # all uploaded photos, the label is often untagged
            code = str(p.get("code") or a.barcode)
            path = "/".join([code[0:3], code[3:6], code[6:9], code[9:]]) if len(code) > 8 else code
            raw = [(k, v) for k, v in (p.get("images") or {}).items() if k.isdigit()]
            p["all_images"] = [{"url": f"https://images.openfoodfacts.org/images/products/{path}/{k}.jpg",
                                "uploaded": __import__("datetime").datetime.fromtimestamp(
                                    int(v.get("uploaded_t") or 0)).strftime("%Y-%m-%d")}
                               for k, v in sorted(raw, key=lambda kv: int(kv[0]))]
        if prods and prods[0].get("all_images"):
            log(f"{len(prods[0]['all_images'])} uploaded photos: open each with Read (shots.py fetch-image), the "
                "ingredient list and panel are often on an untagged one")
    else:
        if not a.query:
            sys.exit("give a QUERY or --barcode")
        prods = []
        try:  # search-a-licious takes ~1 s, the legacy CGI is slow
            data = fetch_json("https://search.openfoodfacts.org/search?" + urllib.parse.urlencode(
                {"q": a.query, "page_size": a.n, "fields": fields}), max_retries=1)
            prods = [dict(h, brands=", ".join(h["brands"]) if isinstance(h.get("brands"), list)
                          else h.get("brands")) for h in (data or {}).get("hits", [])]
            words = [w.lower() for w in a.query.split() if len(w) > 2]
            score = lambda h: sum(w in (str(h.get("product_name", "")) + " " + str(h.get("brands", ""))).lower()
                                  for w in words)  # noqa: E731
            prods = sorted((h for h in prods if score(h)), key=score, reverse=True)
            if not prods:
                log(f"no product named like that: the search returned {(data or {}).get('count', 0)} fuzzy results, "
                    "none with the query words. It may never have been in Open Food Facts: try a retailer listing "
                    "(`pantry.py retail <url>`, Instacart/Walmart/Target pages keep discontinued items) or the maker's "
                    "archived site.")
            else:
                log(f"{len(prods)} products mention the query words")
        except (RuntimeError, FileNotFoundError, ValueError) as e:
            log(f"search.openfoodfacts.org failed ({e}); trying the legacy search")
        words_all = a.query.split()
        while not prods and len(words_all) > 2:  # "Doritos Collisions Hot Wings Blue Cheese" -> fewer words
            words_all = words_all[:-1]
            try:
                data = fetch_json("https://search.openfoodfacts.org/search?" + urllib.parse.urlencode(
                    {"q": " ".join(words_all), "page_size": a.n, "fields": fields}), max_retries=1)
                cand = [dict(h, brands=", ".join(h["brands"]) if isinstance(h.get("brands"), list) else h.get("brands"))
                        for h in (data or {}).get("hits", [])]
                prods = [h for h in cand if all(w.lower() in (str(h.get("product_name", "")) + " " +
                                                              str(h.get("brands", ""))).lower() for w in words_all)]
                if prods:
                    log(f"found with fewer words: {' '.join(words_all)!r}")
            except (RuntimeError, FileNotFoundError, ValueError):
                break
        if not prods:
            q = {"search_terms": a.query, "search_simple": 1, "json": 1, "page_size": a.n, "fields": fields}
            data = fetch_json("https://world.openfoodfacts.org/cgi/search.pl?" + urllib.parse.urlencode(q),
                              max_retries=1)
            prods = (data or {}).get("products", [])
    emit([_off_row(p) for p in prods], a.format)


SR_LEGACY = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip"


def _sr_index() -> list[dict]:
    """USDA SR Legacy (7,793 generic foods, 6 MB, fetched once) as an index. No key, no rate limit."""
    import csv
    import io
    import zipfile
    from _common import cache_dir
    idx_path = cache_dir() / "usda-sr-legacy-v2.json"  # v2 adds cholesterol
    if idx_path.exists():
        return json.loads(idx_path.read_text(encoding="utf-8"))
    import urllib.request
    log("downloading USDA SR Legacy (6 MB, once)...")
    req = urllib.request.Request(SR_LEGACY, headers={"User-Agent": USER_AGENT})
    data = urllib.request.urlopen(req, timeout=300).read()
    z = zipfile.ZipFile(io.BytesIO(data))
    names = {n.rsplit("/", 1)[-1]: n for n in z.namelist()}
    foods = {r["fdc_id"]: r["description"] for r in csv.DictReader(io.TextIOWrapper(z.open(names["food.csv"]), "utf-8"))}
    per: dict = {}
    for r in csv.DictReader(io.TextIOWrapper(z.open(names["food_nutrient.csv"]), "utf-8")):
        k = FDC_IDS.get(int(r["nutrient_id"])) if r.get("nutrient_id", "").isdigit() else None
        if k and r["fdc_id"] in foods and r.get("amount"):
            per.setdefault(r["fdc_id"], {}).setdefault(k, float(r["amount"]))
    idx = [{"fdc_id": int(f), "name": d, "per100g": per.get(f, {})} for f, d in foods.items()]
    atomic_write(idx_path, json.dumps(idx))
    return idx


SYNONYMS = {  # everyday words -> USDA's
    "dutched": "alkali", "dutch": "alkali", "dutch-processed": "alkali", "powdered": "powdered", "confectioners": "powdered",
    "icing": "powdered", "all-purpose": "all-purpose", "ap": "all-purpose", "flour": "flour", "shortening": "shortening",
    "margarine": "margarine", "cornstarch": "cornstarch", "baking soda": "leavening agents, baking soda",
    "soda": "baking soda", "brown": "brown", "corn syrup": "syrups, corn", "hfcs": "high-fructose",
    "milk powder": "dry milk", "nonfat": "nonfat", "whey": "whey", "lecithin": "lecithin", "vanilla": "vanilla",
}


def _food_offline(query: str, n: int) -> list[dict]:
    q = query.lower().replace(",", " ")
    for k, v in SYNONYMS.items():
        if " " in k and k in q:
            q = q.replace(k, v)
    words = [SYNONYMS.get(w, w) for w in q.split() if len(w) > 1]
    words = [x for w in words for x in w.replace(",", " ").split()]
    hits, partial = [], []
    for f in _sr_index():
        name = f["name"].lower()
        got = sum(1 for w in words if w in name)
        if got == len(words):
            hits.append((len(name), f))
        elif words and got >= max(1, (len(words) + 1) // 2):
            partial.append((-got, len(name), f))
    hits.sort(key=lambda h: h[0])  # shortest full match is the most generic
    if not hits and partial:
        partial.sort(key=lambda h: (h[0], h[1]))
        log("no food has every word: closest partial matches (check them)")
        hits = [(0, f) for _, _, f in partial]
    return [{"name": f["name"], "fdc_id": f["fdc_id"], "type": "SR Legacy (offline)", "brand": "",
             "per100g": f["per100g"], "source": f"USDA FDC {f['fdc_id']}",
             "url": f"https://fdc.nal.usda.gov/food-details/{f['fdc_id']}/nutrients"} for _, f in hits[:n]]


def cmd_food(a):
    key = os.environ.get("USDA_API_KEY", "DEMO_KEY")
    from _common import cache_dir
    import time as _t
    marker = cache_dir() / "usda-quota"
    if a.offline or (key == "DEMO_KEY" and marker.exists()
                     and float(marker.read_text(encoding="utf-8") or 0) > _t.time()):
        if not a.offline:
            log("USDA DEMO_KEY quota is spent (remembered): answering from the offline SR Legacy table instead. "
                "Set USDA_API_KEY (free at api.data.gov/signup) for branded foods.")
        rows = _food_offline(a.query, a.n)
        if not rows:
            log("no SR Legacy food has all those words: use fewer, more generic words ('cocoa dutched', 'shortening')")
        emit(rows, a.format)
        return
    q = {"query": a.query, "pageSize": a.n, "api_key": key}
    if a.data_type:
        q["dataType"] = a.data_type
    try:
        data = fetch_json("https://api.nal.usda.gov/fdc/v1/foods/search?" + urllib.parse.urlencode(q))
    except RuntimeError as e:
        if "429" in str(e):
            marker.write_text(str(_t.time() + 3600), encoding="utf-8")
            log("USDA rate limit: answering from the offline SR Legacy table instead")
            emit(_food_offline(a.query, a.n), a.format)
            return
        raise
    out = []
    for f in (data or {}).get("foods", []):
        per = {}
        for n in f.get("foodNutrients", []):
            k = FDC_IDS.get(n.get("nutrientId"))
            if k and k not in per and isinstance(n.get("value"), (int, float)):
                if k == "kcal" and str(n.get("unitName", "")).upper() == "KJ":
                    continue
                per[k] = n["value"]
        out.append({"name": f.get("description"), "fdc_id": f.get("fdcId"),
                    "type": f.get("dataType"), "brand": f.get("brandOwner") or "",
                    "per100g": per, "source": f"USDA FDC {f.get('fdcId')}",
                    "url": f"https://fdc.nal.usda.gov/food-details/{f.get('fdcId')}/nutrients"})
    emit(out, a.format)


def cmd_retail(a):
    """Ingredients and nutrition from a retailer page's embedded JSON.

    Retailers (Instacart, Walmart, Target, Kroger, Amazon) often keep discontinued listings.
    Record what's found as `seen` evidence with the URL."""
    import html as _h
    import re
    from _common import fetch
    try:
        body = fetch(a.url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                                                  "(KHTML, like Gecko) Chrome/126 Safari/537.36",
                                     "Accept-Language": "en-US,en"}).decode("utf-8", "replace")
    except (RuntimeError, FileNotFoundError) as e:
        sys.exit(f"could not fetch the page ({e}); try its Wayback capture: wayback.py fetch <url> <ts>")
    text = _h.unescape(body).replace("\\u0026", "&").replace("\\n", " ")
    ingredients = []
    for m in re.finditer(r'"(?:ingredients?|ingredientStatement|ingredients_text|ingredientsText|ingredient_statement)"'
                         r'\s*:\s*"((?:[^"\\]|\\.){15,4000})"', text, re.I):
        v = re.sub(r"<[^>]+>", " ", m.group(1)).replace('\\"', '"').strip()
        if v not in ingredients:
            ingredients.append(v)
    for m in re.finditer(r"(?i)ingredients?\s*:?\s*</[^>]+>\s*<[^>]+>([^<]{30,3000})<", body):  # plain HTML panels
        v = _h.unescape(m.group(1)).strip()
        if v not in ingredients:
            ingredients.append(v)
    nutrition = {}
    keys = {"calories": "kcal", "totalFat|total_fat|fatContent|fat": "fat", "saturatedFat|saturated_fat|"
            "saturatedFatContent": "satfat", "sodium|sodiumContent": "sodium_mg", "totalCarbohydrate|"
            "total_carbohydrate|carbohydrateContent|carbohydrates": "carbs", "sugars|sugarContent|totalSugars": "sugars",
            "protein|proteinContent": "protein", "dietaryFiber|fiberContent|fiber": "fiber",
            "servingSize|serving_size": "serving"}
    for pat, k in keys.items():
        m = re.search(rf'"(?:{pat})"\s*:\s*"?([0-9.]+\s*(?:m?g|kcal|cal)?[^",}}]{{0,20}})"?', text, re.I)
        if m:
            nutrition[k] = m.group(1).strip()
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    out = {"url": a.url, "title": _h.unescape(title.group(1)).strip() if title else "",
           "ingredient_statements": ingredients[:5], "nutrition": nutrition}
    if not ingredients and not nutrition:
        out["note"] = ("nothing embedded found: the page may need a browser (`shots.py capture <url>` then read it), "
                       "or the listing is gone (try its Wayback capture)")
    print(json.dumps(out, indent=1, ensure_ascii=False))


# ------------------------------------------------------------------ maths

BASIS = {"mode": "100g"}  # "serving" = per serving (restaurant items)


def mix(ings: list[dict], grams: list[float], cook_loss: float = 0.0) -> dict:
    """Nutrition per 100 g of finished product, or per serving when basis is "serving".

    Cooking loses water, concentrating the rest. Fixed components add their own totals."""
    if BASIS["mode"] == "serving":
        out = {}
        for k in NUTRIENTS:
            vals = [i["per_serving"].get(k, 0.0) if i.get("per_serving") else g * i.get("per100g", {}).get(k, 0.0) / 100
                    for i, g in zip(ings, grams)]
            if any(k in (i.get("per_serving") or i.get("per100g") or {}) for i in ings):
                out[k] = sum(vals)
        return out
    total = sum(grams) or 1.0
    finished = total * (1 - cook_loss)
    out = {}
    for k in NUTRIENTS:
        if any(k in i.get("per100g", {}) for i in ings):
            out[k] = sum(g * i.get("per100g", {}).get(k, 0.0) / 100 for i, g in zip(ings, grams)) * 100 / finished
    return out


def _weights(target: dict) -> dict:
    # relative error, floored so 0.1 g sugar can't dominate
    floor = {"kcal": 20, "sodium_mg": 40}
    return {k: 1.0 / max(abs(v), floor.get(k, 1.0)) ** 2 for k, v in target.items() if k in NUTRIENTS}


def loss(ings, grams, target, cook_loss=0.0) -> float:
    got = mix(ings, grams, cook_loss)
    w = _weights(target)
    return sum(w[k] * (got.get(k, 0.0) - target[k]) ** 2 for k in w)


def project_order(v: list[float]) -> list[float]:
    """Closest non-increasing sequence (pool adjacent violators), clipped at 0."""
    blocks = []  # [sum, count]
    for x in v:
        blocks.append([x, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] < blocks[-1][0] / blocks[-1][1]:
            s, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    out = []
    for s, c in blocks:
        out += [max(0.0, s / c)] * c
    return out


MIN_GRAMS = 0.001  # listed means present. at_minimum flags unsizable ones


UNORDERED_PARTS: set = set()  # parts whose own order was lost (recipe "part_order")


def groups(ings: list[dict]) -> list[list[int]]:
    """Index lists ordered by weight, one per `part` (dough, filling...).

    `per_serving` components and parts marked "unknown" in "part_order" are left out."""
    out = {}
    for i, x in enumerate(ings):
        if x.get("per_serving") or x.get("part", "") in UNORDERED_PARTS:
            continue
        out.setdefault(x.get("part", ""), []).append(i)
    return list(out.values())


def feasible(g: list[float], ings: list[dict], fixed: dict, total: float | None, ordered: bool = True) -> list[float]:
    """Pull grams back within label order, fixed amounts and min/max, summing to total."""
    lo = [float(x.get("min_grams", MIN_GRAMS)) for x in ings]
    hi = [float(x["max_grams"]) if x.get("max_grams") is not None else float("inf") for x in ings]
    free = [i for i in range(len(g)) if i not in fixed]
    parts = groups(ings)
    for _ in range(50):
        g = list(g)
        if ordered:
            for idx in parts:
                for i, v in zip(idx, project_order([g[i] for i in idx])):
                    g[i] = v
        for i, v in fixed.items():
            g[i] = v
        for idx in parts:
            for k, i in enumerate(idx):
                if i in fixed:
                    continue
                if ordered:  # fixed items bound their neighbours
                    above = [g[j] for j in idx[k + 1:] if j in fixed]
                    below = [g[j] for j in idx[:k] if j in fixed]
                    g[i] = max(g[i], max(above, default=0.0))
                    if below:
                        g[i] = min(g[i], min(below))
                g[i] = min(max(g[i], lo[i]), hi[i])
        if total is not None:  # per-100 g recipes must add up, per-serving needn't
            room = total - sum(g[i] for i in fixed)
            cur = sum(g[i] for i in free) or 1.0
            g = [min(max(g[i] * room / cur, lo[i]), hi[i]) if i in free else g[i] for i in range(len(g))]
        ok_order = not ordered or all(g[a] + 1e-6 >= g[b] for idx in parts for a, b in zip(idx, idx[1:]))
        if ok_order and (total is None or abs(sum(g) - total) < 1e-3 * total):
            break
    return g


def solve(ings, target, cook_loss=0.0, iters=6000, ordered=True, starts=5) -> list[float]:
    """Best fit from several starts (even, carrier-light, random).

    Projected gradient descent can stall in a corner, e.g. one carrier taking a whole seasoning."""
    import random
    rnd = random.Random(7)
    results = []
    for k in range(max(1, starts)):
        init = None
        if k == 1:  # label order says first-listed is heaviest
            init = [1.0 / (j + 1) for j in range(len(ings))]
        elif k > 1:
            init = [rnd.uniform(0.2, 1.0) for _ in ings]
        g = _solve_once(ings, target, cook_loss, iters // max(1, starts) + 400, ordered, init)
        results.append((loss(ings, g, target, cook_loss), g))
    return min(results, key=lambda r: r[0])[1]


def _solve_once(ings, target, cook_loss=0.0, iters=6000, ordered=True, init=None) -> list[float]:
    n = len(ings)
    fixed = {i: float(x.get("grams") or 0) for i, x in enumerate(ings)
             if x.get("per_serving") or (x.get("fixed") and x.get("grams") is not None)}
    raw_total = None if BASIS["mode"] == "serving" else 100.0 / (1 - cook_loss)
    g = [float(x.get("grams") or (raw_total or 10.0 * n) / n) for x in ings]
    if init is not None:  # new starting shape, same total
        tot = sum(g) or 1.0
        g = [tot * w / (sum(init) or 1) if i not in fixed else g[i] for i, w in enumerate(init)]
    g = feasible(g, ings, fixed, raw_total, ordered)
    step = 0.5
    best, best_l = list(g), loss(ings, g, target, cook_loss)
    for _ in range(iters):
        base = loss(ings, g, target, cook_loss)
        grad = []
        for i in range(n):
            h = 1e-4 * max(1.0, g[i])
            gi = list(g)
            gi[i] += h
            grad.append((loss(ings, gi, target, cook_loss) - base) / h)
        cand = feasible([gi - step * d for gi, d in zip(g, grad)], ings, fixed, raw_total, ordered)
        cl = loss(ings, cand, target, cook_loss)
        if cl < base:
            g, step = cand, step * 1.1
            if cl < best_l:
                best, best_l = list(cand), cl
        else:
            step *= 0.5
            if step < 1e-9:
                break
    return [round(x, 2) for x in best]


def label_tolerance(k: str, t: float, serving_g: float | None) -> float:
    """Half the US label rounding step for this nutrient, per 100 g.

    '3 g' protein per 28 g serving means 2.5-3.5 g, so +-1.8 g per 100 g."""
    if not serving_g:
        return 0.0
    per_serving = t * serving_g / 100
    step = {"kcal": 5 if per_serving <= 50 else 10, "fat": 0.5 if per_serving < 5 else 1,
            "satfat": 0.5 if per_serving < 5 else 1, "sodium_mg": 5 if per_serving <= 140 else 10,
            "carbs": 1, "sugars": 1, "protein": 1, "fiber": 1,
            "chol_mg": 2 if per_serving < 2 else 5}.get(k, 0)
    return step / 2 * 100 / serving_g


def report(ings, grams, target, cook_loss=0.0, ordered=True) -> dict:
    got = mix(ings, grams, cook_loss)
    rows, errs = [], []
    serving = target.get("serving_g")
    if BASIS["mode"] == "serving":
        serving = 100  # already per serving, round as is
    if target.get("kcal_method", "label") == "label" and all(k in got for k in ("fat", "carbs", "protein")):
        # labels use 9/4/4 factors. USDA's food-specific ones
        # run a few percent low even when macros match.
        got = dict(got, kcal_usda=got.get("kcal"), kcal=9 * got["fat"] + 4 * got["carbs"] + 4 * got["protein"])
    for k, t in target.items():
        if k not in NUTRIENTS:
            continue
        v = got.get(k, 0.0)
        denom = max(abs(t), {"kcal": 20, "sodium_mg": 40}.get(k, 1.0))
        e = max(0.0, abs(v - t) - label_tolerance(k, t, serving)) / denom
        errs.append(min(1.0, e))
        rows.append({"nutrient": k, "label": t, "recipe": round(v, 1), "rel_error": round(e, 3)})
    order_breaks = [f"{ings[a]['name']} ({grams[a]} g) < {ings[b]['name']} ({grams[b]} g)"
                    for idx in (groups(ings) if ordered else []) for a, b in zip(idx, idx[1:])
                    if grams[a] + 1e-6 < grams[b]]
    water = [i.get("per100g", {}).get("water") for i in ings]
    warnings = []
    if cook_loss and all(w is not None for w in water):
        available = sum(g * w / 100 for g, w in zip(grams, water))
        if cook_loss * sum(grams) > available + 1e-6:
            warnings.append(f"cook_loss {cook_loss} drives off {cook_loss * sum(grams):.1f} g but the mix only "
                            f"holds {available:.1f} g of water: impossible, lower it")
    elif cook_loss:
        warnings.append("cook_loss is unchecked: add per100g.water to every ingredient (pantry.py food shows it)")
    score = round(max(0.0, 1 - sum(errs) / max(1, len(errs))), 3)
    free = [i for i in ings if not i.get("fixed") and not i.get("per_serving")]
    n_constraints = sum(1 for k in target if k in NUTRIENTS)
    vecs = {}
    for i in free:  # identical rows can't be told apart, split is arbitrary
        key = tuple(sorted((k, round(v, 3)) for k, v in i.get("per100g", {}).items()))
        vecs.setdefault(key, []).append(i["name"])
    for names in vecs.values():
        if len(names) > 1:
            warnings.append(f"{', '.join(names)} share one nutrient row: the fit cannot tell them apart, so "
                            "their split is a design choice; fix it from evidence or merge them")
    kcal_all = mix(ings, grams, cook_loss).get("kcal")
    if kcal_all and BASIS["mode"] == "serving":
        free_kcal = sum(g * i.get("per100g", {}).get("kcal", 0) / 100 for i, g in zip(ings, grams)
                        if not i.get("fixed") and not i.get("per_serving"))
        fitted_share = round(free_kcal / kcal_all, 3)
    else:
        fitted_share = round(sum(g for i, g in zip(ings, grams) if not i.get("fixed")) / (sum(grams) or 1), 3)
    tuned = [i["name"] for i in ings if i.get("fixed") and (i.get("design") or i.get("sized_to"))]
    if len(free) + len(tuned) >= n_constraints:
        warnings.append(f"underdetermined: {len(free)} fitted + {len(tuned)} hand-sized ingredients vs {n_constraints} "
                        "nutrient numbers, so many recipes fit equally well and a high label_match proves little. Fix "
                        "amounts from evidence, add min/max_grams, or merge similar ingredients.")
    if n_constraints <= 3:
        warnings.append(f"only {n_constraints} panel numbers survive: the match checks little; say so with the score")
    at_floor = [i["name"] for i, g in zip(ings, grams)
                if not i.get("fixed") and not i.get("per_serving") and g <= float(i.get("min_grams", MIN_GRAMS)) + 1e-6]
    return {"label_match": score, "order_ok": not order_breaks, "order_breaks": order_breaks,
            "order_checked": ordered, "warnings": warnings, "basis": BASIS["mode"],
            "fitted_share": fitted_share,  # how much the fit decided
            **({"note_fitted": "most of the product is fixed by known components, so label_match mainly tests "
                               "the fitted part"} if fitted_share < 0.3 else {}),
            "target_source": target.get("source", ""), "target_evidence": target.get("evidence", []),
            **_target_kind(target),
            "nutrients": rows, "grams": {i["name"]: g for i, g in zip(ings, grams)},
            "panel_numbers": n_constraints,
            "at_minimum": at_floor,
            "note": "label_match = 1 - mean relative error beyond label rounding (target.serving_g), "
                    "each capped at 1." + (" Ingredients at their minimum: the label numbers do not "
                    "constrain them; set min_grams or fixed amounts from flavour evidence." if at_floor else "")}


def _target_kind(target: dict) -> dict:
    """Flag a category average (a USDA food-type entry) as a stand-in for the label.

    Matching it only shows a typical fruit snack. Say so, and only after hunting for the label."""
    src = str(target.get("source", "")).lower()
    if target.get("kind") == "category" or any(w in src for w in ("usda", "category", "fdc", "typical", "average")):
        out = {"target_kind": "category stand-in"}
        if not target.get("searched"):
            out["target_warning"] = ("the target is a category average, not this product's label, and target.searched "
                                     "does not say where the label was hunted for. Run `pantry.py hunt` and look at "
                                     "every pack photo first; report this match as 'against a category average'.")
        else:
            out["target_warning"] = ("against a category average, not this product's label (searched: "
                                     f"{target['searched']}): report it that way")
        return out
    return {"target_kind": "product label"}


def _variants(name: str) -> list[str]:
    import re
    words = re.findall(r"[A-Za-z0-9]+", name)
    joined = "".join(words)
    out = [name, " ".join(words), "-".join(words), joined]
    if len(words) == 1 and len(joined) > 6:  # Fruitalicious -> Fruit-alicious, at a known word
        for cut in range(3, len(joined) - 2):
            a, b = joined[:cut], joined[cut:]
            if a.lower() in ("soda", "fruit", "choco", "super", "mega", "magic", "cheese", "corn", "pop", "mini"):
                out += [f"{a}-{b}", f"{a} {b}", f"{a}-{b.capitalize()}"]
    seen, res = set(), []
    for v in out:
        if v.lower() not in seen:
            seen.add(v.lower())
            res.append(v)
    return res


def _credits(url: str) -> dict:
    """Photo credits and collectors' links in an article about the product."""
    import html as _html
    import re
    from _common import fetch
    try:
        body = fetch(url)
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    except Exception as e:  # noqa: BLE001
        return {"url": url, "error": str(e)[:120]}
    creds = {_html.unescape(m.strip()) for m in re.findall(r'credit[^>]*>([^<]{3,120})<', text)}
    creds |= {_html.unescape(m.strip()) for m in re.findall(r'(?:Photo|Image)s?(?: by|:)\s*([^<"]{3,80})', text)}
    stock = ("shutterstock", "getty", "istock", "alamy", "adobe stock", "unsplash", "static media")
    people = sorted(c for c in creds if c and not any(w in c.lower() for w in stock))
    links = sorted(set(re.findall(r'https?://(?:www\.)?(?:flickr\.com/photos/[^"\s<]+|instagram\.com/[^"\s<]+|'
                                  r'facebook\.com/[^"\s<]+|[a-z0-9-]+\.blogspot\.com/[^"\s<]*)', text)))[:10]
    import urllib.parse as _up
    site = _up.urlparse(url).netloc.replace("www.", "").split(".")[0].lower()
    def account(link):
        parts = link.lower().replace("www.", "").split("/")
        return parts[3] if len(parts) > 3 else ""
    links = [x for x in links if not site or site not in account(x)]  # drop the publication's own accounts
    return {"url": url, "collector_credits": people, "collector_links": links}


def cmd_hunt(a):
    names = []
    for n in [a.name] + (a.aka or []):
        names += [v for v in _variants(n) if v not in names]
    q = " OR ".join(f'"{n}"' for n in names[:4])
    brand = f' {a.brand}' if a.brand else ""
    first = names[0]
    searches = [
        ("the box or pack itself (plain words: search engines index photo sites poorly with site:)",
         f"{first}{brand} box back ingredients"),
        ("every spelling at once", f"{q} fruit snacks OR candy OR cereal OR chips box"),
        ("collector photo archives: Flickr (packaging collectors shoot every side of a box, often flattened, "
         "panels included), Dinosaur Dracula, Mr. Breakfast (cereal), Collecting Candy",
         f"{first}{brand} flickr OR \"dinosaur dracula\" OR mrbreakfast OR \"collecting candy\""),
        ("auction and resale listings with photos of the back (eBay, Worthpoint, Etsy)",
         f"{first}{brand} vintage box ebay OR worthpoint OR etsy"),
        ("the nutrition panel quoted anywhere, including period calorie-counter books (`archive_search.py fulltext`)",
         f"{first} \"nutrition facts\" OR \"per pouch\" OR calories"),
        ("ads and commercials: shape, colours, claims (magazine scans: `archive_search.py fulltext`)",
         f"{first}{brand} {a.years or ''} commercial OR ad".replace("  ", " ")),
        ("people who kept one: photos, taste, texture", f"{first} reddit OR forum OR instagram"),
    ]
    if a.served:  # never packaged, so ask the people who made it
        searches = [
            ("former employees: the people who made it remember the dough, sauce, cheese, pans and bake (forum threads "
             "and the comments under retrospectives; a 403 or challenge page is not 'not found': `wayback.py fetch` "
             "the archived copy, page by page)",
             f"\"{first}\"{brand} \"I worked at\" OR \"used to make\" OR \"former employee\" OR \"when I worked\""),
            ("training videos and manuals (the build, step by step; read captions before downloading)",
             f"{first}{brand} training video OR manual OR \"spec chart\""),
            ("copycat and pizza or baking forums where ex-staff answer (pizzamaking.com, city-data, Reddit)",
             f"{first}{brand} recipe copycat forum"),
            ("menus and ads: varieties, sizes, words (newspaper scans: `archive_search.py fulltext`)",
             f"{first}{brand} {a.years or ''} menu OR ad OR commercial".replace("  ", " ")),
            ("nutrition per serving in period calorie books (`archive_search.py fulltext`)",
             f"{first}{brand} calories OR nutrition"),
            ("people who ate it: texture, taste, size", f"{first} reddit OR forum OR \"remember\""),
        ]
    for ed in a.edition or []:
        searches.append((f"the {ed} edition's own box: editions of a line usually share one base recipe, so any "
                         "edition's panel is evidence for all (say which edition it came from)",
                         f"{first} {ed}{brand} box"))
    out = {"name_variants": names, "searches": [{"for": w, "query": qq} for w, qq in searches]}
    if not a.edition and not a.served:
        out["editions_next"] = ("when fronts, ads or retrospectives name other editions of the line (a later flavour "
                                "pack, a co-branded or seasonal edition), rerun with --edition for each and look at "
                                "every side of those boxes too")
    if a.credits:
        out["credits"] = [_credits(u) for u in a.credits]
        out["credits_next"] = ("each named collector or linked account owns photos of the pack: search their name "
                               "with the product, open their posts or albums, and Read the side and back panels")
    out["next"] = ("Run each search (WebSearch), open every page that shows the pack, and Read the photos. Articles "
                   "about the product credit the collectors whose photos they use: pass them with --credits URL ... "
                   "to list those collectors, then search each one. An ingredient list or nutrition panel on a pack "
                   "photo is the product's own label (seen, authority artifact). Record every place you looked as the "
                   "label question's `searched`. Only when no photo shows the panel, fall back to a category average "
                   "(USDA) with target.kind \"category\" and target.searched, and call it that everywhere.")
    print(json.dumps(out, indent=1))


G_PER_TSP = {  # rough densities for spoon measures (grams per level teaspoon)
    "salt": 6.0, "sugar": 4.2, "powdered sugar": 2.5, "brown sugar": 4.6, "flour": 2.6, "masa": 2.6, "cornstarch": 2.7,
    "cocoa": 2.5, "baking soda": 4.6, "baking powder": 4.0, "milk powder": 2.1, "buttermilk powder": 2.6,
    "cheese powder": 2.4, "maltodextrin": 1.6, "msg": 4.0, "monosodium glutamate": 4.0, "garlic powder": 3.1,
    "onion powder": 2.4, "paprika": 2.3, "cayenne": 1.8, "chili": 2.7, "pepper": 2.3, "citric acid": 3.6,
    "tomato powder": 2.8, "yeast": 3.0, "spice": 2.0, "oil": 4.5, "shortening": 4.3, "butter": 4.7, "water": 4.9,
    "vanilla": 4.2, "extract": 4.2, "honey": 7.0, "syrup": 6.5, "mustard": 5.0, "mayonnaise": 4.6,
}


def _kitchen(ing: dict, grams: float) -> dict:
    """Grams plus a spoon or cup measure (`g_per_tsp`, else the rough table)."""
    name = ing["name"].lower()
    gpt = ing.get("g_per_tsp") or next((v for k, v in sorted(G_PER_TSP.items(), key=lambda kv: -len(kv[0]))
                                         if k in name), None)
    out = {"g": round(grams, 1)}
    if gpt:
        tsp = grams / gpt
        out["measure"] = (f"{tsp / 48:.2g} cup" if tsp >= 24 else f"{tsp / 3:.2g} Tbsp" if tsp >= 3 else
                          f"{tsp:.2g} tsp" if tsp >= 0.125 else "a pinch")
    return out


def _load_recipe(path):
    r = json.loads(Path(path).read_text(encoding="utf-8"))
    BASIS["mode"] = "serving" if r.get("basis", "").startswith("per serving") or r.get("basis") == "serving" else "100g"
    if not r.get("ingredients") or not r.get("target"):
        sys.exit("recipe needs `target` and `ingredients` (see --help)")
    # left out at home (EDTA, preservatives), kept on record only
    for i in r["ingredients"]:  # each <= 2% of the product, any order (21 CFR 101.4)
        if i.get("two_percent_or_less") and BASIS["mode"] == "100g":
            i["max_grams"] = min(float(i.get("max_grams") or 2.0), 2.0)
            i.setdefault("part", (i.get("part") or "") + " (2% or less)")
    UNORDERED_PARTS.clear()
    UNORDERED_PARTS.update(i["part"] for i in r["ingredients"] if i.get("two_percent_or_less"))
    UNORDERED_PARTS.update(p for p, o in (r.get("part_order") or {}).items() if o == "unknown")
    r["omitted_names"] = [i["name"] for i in r["ingredients"] if i.get("omitted")]
    r["ingredients"] = [i for i in r["ingredients"] if not i.get("omitted")]
    return r


def cmd_solve(a):
    r = _load_recipe(a.recipe)
    ings, cl = r["ingredients"], float(r.get("cook_loss") or 0)
    ordered = r.get("order", "label") != "unknown"
    grams = solve(ings, r["target"], cl, ordered=ordered)
    for i, g in zip(ings, grams):
        if not i.get("fixed"):
            i["grams"] = g
    r["solved"] = True
    atomic_write(Path(a.recipe), json.dumps(r, indent=2, ensure_ascii=False))
    print(json.dumps(report(ings, grams, r["target"], cl, ordered), indent=2))


def cmd_match(a):
    r = _load_recipe(a.recipe)
    ings = r["ingredients"]
    if any(i.get("grams") is None for i in ings):
        sys.exit("some ingredients have no grams; run `solve` first or fill them in")
    rep = report(ings, [float(i["grams"]) for i in ings], r["target"], float(r.get("cook_loss") or 0),
                 r.get("order", "label") != "unknown")
    rep["omitted"] = r.get("omitted_names", [])
    if r.get("batch_g"):  # every ingredient scaled to the batch
        tot = sum(float(i["grams"]) for i in ings) or 1
        rep["batch"] = {i["name"]: _kitchen(i, float(i["grams"]) * float(r["batch_g"]) / tot) for i in ings}
    out = Path(a.out) if a.out else None
    if out is None:
        try:
            from ledger import find_ws
            out = find_ws(None) / "codex" / "label-match.json"
        except SystemExit:
            out = None
    if out:
        atomic_write(out, json.dumps(rep, indent=2))
        rep["written"] = str(out)
    print(json.dumps(rep, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--format", choices=["jsonl", "json", "md"], default="jsonl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("hunt", help="the searches that find photos of an old product's box and label")
    s.add_argument("name")
    s.add_argument("--brand")
    s.add_argument("--years", help="e.g. 1991-1998")
    s.add_argument("--aka", action="append", help="another name or spelling (repeatable)")
    s.add_argument("--served", action="store_true",
                   help="a dish made in restaurants or kitchens, never packaged: searches for the people who made it")
    s.add_argument("--edition", action="append", help="another edition of the line, e.g. a later flavour pack (repeatable)")
    s.add_argument("--credits", nargs="+", help="articles about the product: list the collectors credited for photos")
    s.set_defaults(fn=cmd_hunt)
    s = sub.add_parser("label")
    s.add_argument("query", nargs="?")
    s.add_argument("--barcode")
    s.add_argument("--n", type=int, default=5)
    s.set_defaults(fn=cmd_label)
    s = sub.add_parser("retail", help="ingredients and nutrition from a retailer's product page (embedded JSON)")
    s.add_argument("url")
    s.set_defaults(fn=cmd_retail)
    s = sub.add_parser("food")
    s.add_argument("query")
    s.add_argument("--n", type=int, default=3)
    s.add_argument("--data-type", help="Foundation | SR Legacy | Branded | Survey (FNDDS)")
    s.add_argument("--offline", action="store_true", help="USDA SR Legacy from a cached download: no key, no limit")
    s.set_defaults(fn=cmd_food)
    s = sub.add_parser("solve")
    s.add_argument("recipe")
    s.set_defaults(fn=cmd_solve)
    s = sub.add_parser("match")
    s.add_argument("recipe")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_match)
    ws_arg = pop_ws_arg() if argv is None else None  # --ws may come before or after the subcommand
    a = ap.parse_args(argv)
    enter_ws(ws_arg)  # relative paths are workspace paths
    try:
        a.fn(a)
    except RuntimeError as e:
        log(f"error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
