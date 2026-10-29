#!/usr/bin/env python3
"""Wayback Machine helper for the resurrect skill.

Subcommands
  timeline URL            When was it alive? First/last good capture, monthly sample, death estimate.
  urls DOMAIN             Map the site: unique archived URLs, grouped by path section.
  find DOMAIN KEYWORD     Archived URLs under a domain containing a keyword (find the .swf, the bundle).
  text URL [TS]           Readable text of an archived page (articles, help pages, FAQs).
  links URL TS [TS...]    Links on several captures of list pages, merged with first/last dates (lost indexes).
  mirror URL TS           Restore archived pages plus every asset they use, links rewritten (exact sites).
  mirror --from-inventory FILE [--match RX]   Fetch listed files straight from their captures (no CDX lookups).
  alive URL               Is it alive now? DNS, status, final URL, title, tombstone words (with a network check).
  cdx URL                 Raw CDX query (all filters exposed) -> JSONL rows.
  fetch URL [TS]          Download the raw archived bytes (id_ replay, no toolbar) into a folder. Without TS it takes
                          the earliest capture for program/media files and the latest for pages, and prints the date.
  available URL [--ts]    Closest snapshot via the availability API.
  api DOMAIN              Find archived JSON/XML API responses under a host (backend evidence).

All output is JSONL on stdout (or --format md|json|csv). Progress goes to stderr.
To be polite, requests are rate limited (~24/min for CDX) and cached in ~/.cache/resurrect/ ($RESURRECT_CACHE).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import re  # noqa: E402
from _common import (TIMEOUT, OutOfTime, atomic_write, cache_dir, emit, enter_ws, fetch, fetch_json,  # noqa: E402
                     hard_stop, jsonl_lines, last_status, last_truncated, log, pop_ws_arg, refuse_binary, slugify)

CDX = "https://web.archive.org/cdx/search/cdx"
DEFAULT_FL = "timestamp,original,mimetype,statuscode,digest,length"


class _Deadline(BaseException):
    """Hard time limit. Not a TimeoutError, which fetch() retries, so the limit used to get slept past."""


def _use_alarm() -> bool:
    """signal.alarm works on macOS and Linux, main thread only. Windows doesn't have it."""
    import signal
    import threading
    return hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()


def _arm(seconds, alarm: bool | None = None) -> None:
    """Raise _Deadline after `seconds`. Uses signal.alarm, or _common.hard_stop on Windows (fires at the
    next request, retry or back-off, and caps waits on a silent server)."""
    import time as _t
    if alarm is None:
        alarm = _use_alarm()
    if not alarm:
        hard_stop(_t.time() + max(1, int(seconds)), _Deadline)
        return
    import signal

    def _hit(*_):
        raise _Deadline
    signal.signal(signal.SIGALRM, _hit)
    signal.alarm(max(1, int(seconds)))


def _disarm() -> None:
    hard_stop(None)
    if _use_alarm():
        import signal
        signal.alarm(0)


def cdx_query(url: str, *, match: str = "exact", fl: str = DEFAULT_FL, filters=(),
              collapse=(), frm: str | None = None, to: str | None = None,
              limit: int | None = 1000, follow: bool = False, max_pages: int = 20, deadline: float | None = None):
    """Yield CDX rows as dicts. Follows resumeKey pagination when follow=True. For deadline, see _common.fetch."""
    params = [("url", url), ("output", "json"), ("fl", fl)]
    if match and match != "exact":
        params.append(("matchType", match))
    for f in filters:
        params.append(("filter", f))
    for c in collapse:
        params.append(("collapse", c))
    if frm:
        params.append(("from", frm))
    if to:
        params.append(("to", to))
    if limit:
        params.append(("limit", str(limit)))
    if follow:
        params.append(("showResumeKey", "true"))

    resume = None
    for page in range(max_pages):
        q = list(params)
        if resume:
            q.append(("resumeKey", resume))
        full = CDX + "?" + urllib.parse.urlencode(q)
        log(f"cdx page {page + 1}: {url}")
        data = fetch_json(full, **({"deadline": deadline} if deadline else {}))
        rows, resume = parse_cdx_json(data)
        if not rows:
            return
        header, body = rows[0], rows[1:]
        for r in body:
            yield dict(zip(header, r))
        if not (follow and resume):
            return


def parse_cdx_json(data):
    """Split a CDX JSON payload into (rows_including_header, resume_key|None).

    With showResumeKey=true and more results remaining the payload ends with
    [] followed by [<key>].
    """
    if not data:
        return [], None
    resume = None
    if len(data) >= 2 and data[-2] == [] and len(data[-1]) == 1:
        resume = data[-1][0]
        data = data[:-2]
    return data, resume


def norm_url(u: str) -> str:
    """CDX prints http://host:80/path, but the replay endpoints want it without the default port."""
    return re.sub(r"^(https?://[^/:]+):(?:80|443)(?=/|$)", r"\1", u)


def replay_url(ts: str, original: str, raw: bool = True) -> str:
    original = norm_url(original)
    return f"https://web.archive.org/web/{ts}{'id_' if raw else ''}/{original}"


def fmt_ts(ts: str) -> str:
    ts = (ts or "").ljust(14, "0")
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"


# ---------------------------------------------------------------- commands

def content_collapse(good: list[dict]):
    """First month the page shrinks well below its usual size and stays there.
    Closure notices, parked domains and login shells still answer 200."""
    sizes = [int(r.get("length") or 0) for r in good]
    for i in range(3, len(good)):
        before = sorted(x for x in sizes[max(0, i - 12):i] if x)
        if not before:
            continue
        median = before[len(before) // 2]
        after = sizes[i:i + 4]
        recovers = any(x >= 0.7 * median for x in sizes[i + 1:])  # a temporary splash or error isn't a death
        if (sizes[i] and sizes[i] < 0.35 * median and not recovers
                and sum(1 for x in after if x < 0.35 * median) >= min(3, len(after))):
            return good[i], median
    return None, None


def frozen_since(good: list[dict], months: int = 12):
    """Start of the final run of identical content >= `months` captures long. Dead sites stop changing."""
    if not good:
        return None
    run_start = len(good) - 1
    while run_start > 0 and good[run_start - 1].get("digest") == good[-1].get("digest"):
        run_start -= 1
    return good[run_start] if len(good) - run_start >= months else None


def _months(ts: str) -> int:
    return int(ts[:4]) * 12 + int(ts[4:6])


def dark_gap(good: list[dict], months: int = 12):
    """Longest gap (>= `months`) with no healthy capture. What answers 200 after it is usually another site."""
    best = None
    for prev, nxt in zip(good, good[1:]):
        g = _months(nxt["timestamp"]) - _months(prev["timestamp"])
        if g >= months and (not best or g > best[2]):
            best = (prev, nxt, g)
    return best


# ---------------------------------------------------------------- owner changes (resale, parking)

# parking/for-sale tells: ?fp= &poru= &kwrf= params and their frameset,
# ww1..ww9.<domain> hops, sale/ad-feed words, "The Leading X Site on the Net"
PARKED_RX = re.compile(
    r"(?i)[?&](?:fp|poru|prvtof|kwrf|reqp|caf)=|click here to proceed|https?://ww\d+\.|"
    r"domain (?:is |may be )?(?:for sale|parked)|buy this domain|this domain (?:name )?(?:may be |is )?for sale|"
    r"parkingcrew|sedoparking|bodis\.com|above\.com/|hugedomains|afternic|dan\.com/|related searches|"
    r"sponsored listings|the leading [\w ]{2,40} site on the net|data-adblockkey|window\.park\b|/park\.js|"
    r"parklogic|domain ?parking")
CMS_MARKS = [("wordpress", r"/wp-content/|/wp-includes/|wordpress"), ("drupal", r"drupal"),
             ("joomla", r"joomla"), ("wix", r"wix\.com|wixstatic"), ("squarespace", r"squarespace"),
             ("blogger", r"blogger\.com|blogspot\.com"), ("shopify", r"shopify"), ("weebly", r"weebly"),
             ("ghost", r"ghost\.(?:io|org)"), ("godaddy", r"godaddy|websitebuilder")]
TITLE_STOP = {"the", "and", "for", "home", "page", "welcome", "www", "com", "net", "org", "official", "site",
              "index", "default", "main", "untitled", "document", "new"}


def page_fingerprint(html_: str, host: str = "") -> dict:
    """Title, CMS and parked status of a capture. Captures that differ usually mean a new owner."""
    t = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_[:200_000])
    import html as _html
    title = _html.unescape(re.sub(r"\s+", " ", t.group(1))).strip()[:120] if t else ""
    gen = re.search(r"(?is)<meta[^>]+name=[\"']?generator[\"']?[^>]*content=[\"']([^\"']+)", html_[:200_000]) or \
        re.search(r"(?is)<meta[^>]+content=[\"']([^\"']+)[\"'][^>]*name=[\"']?generator", html_[:200_000])
    probe = (gen.group(1) if gen else "") + " " + html_[:300_000]
    cms = next((name for name, rx in CMS_MARKS if re.search(rx, probe, re.I)), None)
    m = PARKED_RX.search(html_[:300_000])
    host = host.lower().removeprefix("www.")
    parked = (m.group(0).strip()[:40] if m else
              "title is the bare domain name" if host and title.lower().removeprefix("www.") == host else None)
    body = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>|<[^>]+>|&nbsp;", " ", html_[:300_000])
    empty = not title and not body.strip() and not re.search(r"(?i)<(i?frame|embed|object)\b", html_[:300_000])
    return {"title": title, "cms": cms, "generator": (gen.group(1).strip()[:40] if gen else None), "parked": parked,
            **({"empty": True} if empty else {})}


def _title_words(t: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (t or "").lower()) if w not in TITLE_STOP}


def same_site(fp: dict, orig: dict, gap_months: int = 0) -> bool:
    """Is this capture still the original owner's site? Parked never is. A new CMS counts only if the title
    kept its words (all, or most with no 12-month dark gap). Titles sharing no word are different sites."""
    if fp.get("parked"):
        return False
    if fp.get("empty") and orig.get("title"):  # a blank page replaced a real site
        return False
    a, b = _title_words(fp.get("title")), _title_words(orig.get("title"))
    sim = len(a & b) / len(a | b) if a and b else None
    if fp.get("cms") != orig.get("cms"):
        return sim is not None and (sim == 1 or sim >= 0.7 and gap_months < 12)
    return sim is None or sim > 0


ERROR_TITLE = re.compile(r"(?i)^\s*(\d{3}\b|error\b)|\b(not found|forbidden|bad gateway|service unavailable|"
                         r"internal server error|access denied)\b")


def site_eras(target: str, budget: float = 60, max_fetches: int = 12) -> dict | None:
    """Find where a domain's root page stops being its first site (parking, resale, a new owner's WordPress).
    Samples captures (one per content hash at most), then bisects.
    Returns {"original": {...}, "later": {...} | None, "cut_ts": ts | None}, or None."""
    import time as _t
    t0 = _t.time()
    root = target if "://" in target else "http://" + target.rstrip("/") + "/"
    host = urllib.parse.urlparse(root).hostname or ""
    try:
        rows = list(cdx_query(root, fl="timestamp,original,mimetype,digest,length",
                              filters=["statuscode:200"], collapse=["timestamp:6"], limit=1000))
    except (RuntimeError, FileNotFoundError) as e:
        log(f"era check: cdx failed ({e})")
        return None
    rows = [r for r in rows if "html" in (r.get("mimetype") or "")]
    if len(rows) < 2:
        return None
    fps, fetched = {}, [0]

    def fp_of(i):
        r = rows[i]
        d = r.get("digest") or r["timestamp"]
        if d not in fps:
            if fetched[0] >= max_fetches or _t.time() - t0 > budget:
                return None
            fetched[0] += 1
            try:
                body = fetch(replay_url(r["timestamp"], r["original"]), max_bytes=3_000_000)
                fps[d] = page_fingerprint(body.decode("utf-8", "replace"), host)
            except (RuntimeError, FileNotFoundError):
                fps[d] = None
        return fps[d]

    # skip parked and error pages. A later "404 Not Found"
    # would otherwise pass as the original
    oi = next((i for i in range(min(6, len(rows))) if fp_of(i) and fp_of(i).get("parked") is None
               and not ERROR_TITLE.search(fp_of(i)["title"])), None)
    if oi is None:
        return None
    orig = fp_of(oi)
    verdict = {}  # row index -> same site as the original?

    def judge(i):
        if i not in verdict:
            fp = fp_of(i)
            if fp is None:
                return None
            prev = max([j for j, v in verdict.items() if v and j < i] or [oi])  # gap since the original was last seen
            gap = _months(rows[i]["timestamp"]) - _months(rows[prev]["timestamp"])
            verdict[i] = same_site(fp, orig, gap)
        return verdict[i]
    idx = sorted({rows.index(r) for r in _spread(rows[oi:], 6)} | {oi})
    for i in idx:
        judge(i)
    same_digests = {rows[i].get("digest") for i, v in verdict.items() if v}

    def bounds():
        last = max([i for i, v in verdict.items() if v] +
                   [i for i, r in enumerate(rows) if r.get("digest") in same_digests])
        firsts = [i for i, v in verdict.items() if v is False and i > last]
        return last, (min(firsts) if firsts else None)
    lo, hi = bounds()
    while hi is not None and hi - lo > 1:  # bisect last-same / first-different
        mid = (lo + hi) // 2
        v = judge(mid)
        if v is None:
            break
        if v:
            same_digests.add(rows[mid].get("digest"))
        lo, hi = bounds()
    out = {"original": {"from": fmt_ts(rows[oi]["timestamp"]), "to": fmt_ts(rows[lo]["timestamp"]),
                        "title": orig["title"], **({"cms": orig["cms"]} if orig.get("cms") else {})},
           "later": None, "cut_ts": None, "captures_read": fetched[0]}
    if hi is not None:
        fp = fps.get(rows[hi].get("digest") or rows[hi]["timestamp"]) or {}
        gap = _months(rows[hi]["timestamp"]) - _months(rows[lo]["timestamp"])
        out["later"] = {"from": fmt_ts(rows[hi]["timestamp"]), "title": fp.get("title"),
                        **({"cms": fp["cms"]} if fp.get("cms") else {}),
                        **({"parked": fp["parked"]} if fp.get("parked") else {}),
                        **({"gap_months": gap} if gap >= 12 else {}),
                        "view": replay_url(rows[hi]["timestamp"], rows[hi]["original"], raw=False)}
        out["cut_ts"] = rows[hi]["timestamp"]
    return out


def era_note(eras: dict | None) -> str:
    """One stderr line: the original site's span, and any later site."""
    if not eras:
        return ""
    o, l_ = eras["original"], eras.get("later")
    s = f"original site: {o['from']}..{o['to']}" + (f" ({o['title']!r})" if o.get("title") else "")
    if l_:
        what = ("a parking/for-sale page" if l_.get("parked") else
                f"{l_['cms']} site" if l_.get("cms") and l_.get("cms") != o.get("cms") else "a different site")
        s += (f"; the domain later hosted a different site (from {l_['from'][:4]}: {what}"
              + (f", {l_['title']!r}" if l_.get("title") else "") + ")")
    return s


# stand-in pages: "coming soon", "promotion ended" and such
PLACEHOLDER_RX = re.compile(
    r"(?i)coming soon|under construction|be back soon|check back (?:soon|later)|site (?:is )?(?:down|offline)|"
    r"(?:promotion|offer|contest|campaign|sweepstakes|game|site) (?:has )?(?:now )?(?:ended|is over|expired|closed)|"
    r"no longer (?:available|active|running|online)")
PROGRAM_RX = re.compile(r"(?i)([\w-]+\.(?:swf|dcr|unity3d|jar|xap))\b")


def _era_check(r: dict, host: str) -> dict:
    """Programs one capture embeds, and whether it's parked or a placeholder."""
    raw = fetch(replay_url(r["timestamp"], r["original"]), max_bytes=2_000_000).decode("utf-8", "replace")
    fp = page_fingerprint(raw, host)
    text = re.sub(r"(?s)<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw))
    m = PLACEHOLDER_RX.search(text)
    return {"parked": fp["parked"], "placeholder": m.group(0) if m else None, "title": fp["title"],
            "programs": sorted({x.lower() for x in PROGRAM_RX.findall(raw)})}


def page_change(eras: list[dict], host: str):
    """First version that's no longer the thing: parked, a placeholder, or no longer embedding the program.
    Sizes miss an embed swapped for a notice, so this reads pages (about 12 + log2(n) cached fetches)."""
    if len(eras) < 2:
        return None
    seen = {}

    def check(i):
        if i not in seen:
            try:
                seen[i] = _era_check(eras[i], host)
            except (RuntimeError, FileNotFoundError):
                seen[i] = None
        return seen[i]
    idx = sorted(set(round(k * (len(eras) - 1) / 7) for k in range(8)))
    error = ERROR_TITLE

    def clean(c):
        return c and not c["parked"] and not c["placeholder"] and not error.search(c["title"])
    # baseline: first with a program, else first clean one
    # (sites often open with their own "coming soon" page)
    sampled = [i for i in sorted(set(idx) | set(range(min(5, len(eras))))) if clean(check(i))]  # + first 5 (short life)
    with_prog = [i for i in sampled if check(i)["programs"]]
    if not sampled:
        return None
    b = (with_prog or sampled)[0]
    base = check(b)

    def bad(i):
        c = check(i)
        if not c or error.search(c["title"]):
            return None  # archived error page, says nothing
        if c["parked"]:
            return "parked", c["parked"]
        if c["placeholder"]:
            return "placeholder", c["placeholder"]
        if base["programs"] and not c["programs"]:
            return "program_gone", ", ".join(base["programs"][:3]) + " no longer embedded"
        return None
    lo, hi = b, None
    for i in [i for i in idx if i > b]:
        if bad(i):
            hi = i
            break
        lo = i
    if hi is None:
        return None
    while hi - lo > 1:  # changes stick, so bisect for the first
        mid = (lo + hi) // 2
        if bad(mid):
            hi = mid
        else:
            lo = mid
    kind, detail = bad(hi)
    return {"date": fmt_ts(eras[hi]["timestamp"]), "kind": kind, "detail": detail, "title": seen[hi]["title"],
            "view": replay_url(eras[hi]["timestamp"], eras[hi]["original"], raw=False),
            "last_as_original": fmt_ts(eras[lo]["timestamp"]),
            "last_original_view": replay_url(eras[lo]["timestamp"], eras[lo]["original"], raw=False),
            "programs_before": base["programs"][:6], "_ts": eras[hi]["timestamp"],
            **({"before_launch": f"{fmt_ts(eras[0]['timestamp'])}: the first capture is itself a "
                f"{'parked page' if check(0)['parked'] else 'placeholder'} (a pre-launch page, or the domain's "
                "previous owner)"} if b > 0 and check(0) and not clean(check(0)) else {})}


def cmd_timeline(a):
    fl = "timestamp,original,statuscode,mimetype,digest,length"
    if a.died and not a.to:
        a.to = a.died
    rows = list(cdx_query(a.url, fl=fl, collapse=["timestamp:6"], frm=a.frm, to=a.to, limit=None, follow=True))
    if not rows:
        emit([{"url": a.url, "archived": False,
               "hint": "no captures of this exact URL. Try the bare domain or www. variant, the product's own path "
                       "(google.com/reader/ rather than google.com), or `find` with --prefix"}], a.format)
        return
    # healthy only, monthly (collapse runs after filter)
    good = list(cdx_query(a.url, fl=fl, filters=["statuscode:200"], collapse=["timestamp:6"],
                          frm=a.frm, to=a.to, limit=None, follow=True))
    last_good = good[-1] if good else None
    after = [r for r in rows if last_good and r["timestamp"][:6] > last_good["timestamp"][:6]]
    obituary = after[0] if after else None
    status_after = collections.Counter(r.get("statuscode") for r in after)
    # digest changes = design eras worth a look
    collapse, usual = content_collapse(good)
    frozen = frozen_since(good)
    gap = dark_gap(good)
    moved_during_gap = None
    if gap:  # all-redirect dark stretch = a move, not a death
        inside = [r for r in rows if gap[0]["timestamp"] < r["timestamp"] < gap[1]["timestamp"]]
        if inside and sum(1 for r in inside if str(r.get("statuscode", "")).startswith("3")) >= 0.8 * len(inside):
            moved_during_gap = {"from": fmt_ts(gap[0]["timestamp"]), "to": fmt_ts(gap[1]["timestamp"]),
                                "redirects": len(inside)}
            gap = None
    eras, prev = [], None
    for r in good:
        if r.get("digest") != prev:
            eras.append(r)
            prev = r.get("digest")
    changed = None if a.no_notice else page_change(eras, urllib.parse.urlparse(norm_url(a.url)).hostname or "")
    later = "after the page change above, so this is what came later (a notice, parking, a resale), not the death"
    if changed:
        ct = changed.pop("_ts")
        if moved_during_gap and ct[:6] <= moved_during_gap["from"].replace("-", "")[:6]:
            moved_during_gap = None  # parked/replaced before the gap: redirects aren't a move
    cut = min([r["timestamp"] for r in (collapse, frozen) if r] + ([gap[1]["timestamp"]] if gap else []) +
              ([ct] if changed else []) or ["99999999999999"])
    last_notice = None
    if good and not a.no_notice:  # last healthy capture is often the shutdown banner
        try:
            raw = fetch(replay_url(good[-1]["timestamp"], good[-1]["original"]), max_bytes=2_000_000)
            words = re.findall(r"(?i)(shut(?:ting)? down|discontinued|will be closing|no longer (?:available|"
                               r"be available)|end of life|archived state|thank you for|farewell)", raw.decode("utf-8", "replace"))
            if words:
                last_notice = {"date": fmt_ts(good[-1]["timestamp"]), "words": sorted({w.lower() for w in words})[:6],
                               "view": replay_url(good[-1]["timestamp"], good[-1]["original"], raw=False)}
        except (RuntimeError, FileNotFoundError):
            pass
    moved_to = None
    if obituary and str(obituary.get("statuscode", "")).startswith("3"):
        try:  # follow the redirect: a domain move isn't a death
            _, final = fetch(replay_url(obituary["timestamp"], obituary["original"]), want_url=True, max_bytes=500_000)
            m = re.search(r"/web/\d+\w*/(https?://[^/]+)", final or "")
            src = urllib.parse.urlparse(norm_url(obituary["original"])).hostname or ""
            if m and urllib.parse.urlparse(m.group(1)).hostname not in (src, "www." + src, src.removeprefix("www.")):
                moved_to = m.group(1)
        except (RuntimeError, FileNotFoundError):
            pass
    summary = {
        "url": a.url,
        "archived": True,
        "moved_to": ({"host": moved_to, "date": fmt_ts(obituary["timestamp"]),
                      "note": "the 'death' is a move: run timeline on the new host too"} if moved_to else None),
        "shutdown_notice_in_last_capture": last_notice,
        "first_capture": fmt_ts(rows[0]["timestamp"]),
        "last_capture": fmt_ts(rows[-1]["timestamp"]),
        "last_seen_alive": fmt_ts(last_good["timestamp"]) if last_good else None,
        "months_with_captures": len(rows),
        "status_after_last_alive": dict(status_after),
        "page_change": (dict(changed, note={
            "parked": "from here the domain is parked or for sale: the thing ended on or before this date",
            "placeholder": "from here the page stands in for the thing (read it: often the closure or a "
                           "'coming soon' that never came); the thing ended between the two dates",
            "program_gone": "from here the page stops embedding the program it ran: the app ended or was replaced "
                            "between the two dates. Read both"}[changed["kind"]] + ". Snapshots below stop before it.")
                        if changed else None),
        "content_collapse": (
            {"date": fmt_ts(collapse["timestamp"]), "bytes": int(collapse.get("length") or 0), "usual_bytes": usual,
             "view": replay_url(collapse["timestamp"], collapse["original"], raw=False),
             "note": later if changed and collapse["timestamp"] > ct else
                     "the page shrank and stayed small while still answering 200: likely the real death "
                     "(closure notice, parked domain, login wall). Read it; snapshots below stop before it."}
            if collapse else None),
        **({"moved_during_gap": dict(moved_during_gap, note="the stretch without healthy captures is all redirects: "
                                     "it moved (follow the redirects), it did not die")} if moved_during_gap else {}),
        "dark_gap": (
            {"last_healthy_before": fmt_ts(gap[0]["timestamp"]), "next_healthy": fmt_ts(gap[1]["timestamp"]),
             "months": gap[2], "view_before": replay_url(gap[0]["timestamp"], gap[0]["original"], raw=False),
             "note": later if changed and gap[0]["timestamp"] > ct else
                     "no healthy capture for this long: the site probably died at the start of the gap and "
                     "what answers afterwards is something else. Check both sides; snapshots stop at the gap."}
            if gap else None),
        "content_frozen_since": (
            {"date": fmt_ts(frozen["timestamp"]), "view": replay_url(frozen["timestamp"], frozen["original"], raw=False),
             "note": later if changed and frozen["timestamp"] > ct else
                     "identical content in every capture from here on: a living site changes, a closure notice "
                     "does not. Open it: it is often the shutdown page. Snapshots below stop before it."}
            if frozen else None),
        "first_capture_after_death": (
            {"date": fmt_ts(obituary["timestamp"]), "status": obituary.get("statuscode"),
             "view": replay_url(obituary["timestamp"], obituary["original"], raw=False),
             "note": "often a shutdown notice or redirect - read it, it is evidence"}
            if obituary else None),
        "suggested_snapshots": [
            {"date": fmt_ts(r["timestamp"]), "ts": r["timestamp"],
             "view": replay_url(r["timestamp"], r["original"], raw=False),
             "raw": replay_url(r["timestamp"], r["original"]),
             **({"after_death_signal": True} if r["timestamp"] >= cut else {})}
            for r in (_spread([e for e in eras if e["timestamp"] < cut], max(1, a.samples - 2)) +
                      ([] if changed and changed["kind"] == "parked" and cut == ct else  # no era for parking pages
                       _spread([e for e in eras if e["timestamp"] >= cut], 2)))
        ],
        "signals_note": ("death signals can also mark an owner change (a domain bought by a new product): "
                         "open captures on both sides before choosing the era"),
    }
    if a.format == "md":
        for k, v in summary.items():
            if k != "suggested_snapshots":
                print(f"- **{k}**: {json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}")
        print("\n**suggested snapshots**")
        for r in summary["suggested_snapshots"]:
            print(f"- {r['date']} ts={r['ts']} {r['view']}" + ("  (after a death signal)" if r.get("after_death_signal") else ""))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))


def _spread(items, n):
    """Pick n items evenly across a list, always including first and last."""
    if len(items) <= n:
        return items
    if n <= 1:
        return [items[-1]]
    step = (len(items) - 1) / (n - 1)
    return [items[round(i * step)] for i in range(n)]


def _loose(rx: str) -> str:
    """CDX filters have to match the whole field, so turn 'shockwave' into '.*shockwave.*'."""
    return rx if rx.startswith((".*", "^")) or rx.endswith((".*", "$")) else f".*{rx}.*"


def cmd_text(a):
    """Readable text of an archived page (WebFetch is often blocked on web.archive.org)."""
    from _common import strip_html
    a.url = norm_url(a.url)
    ts = a.ts or (closest_capture(a.url) or {}).get("timestamp")
    if a.ts:  # else Wayback replays 403/404 captures as content
        near = closest_capture(a.url, a.ts)
        if near and near["timestamp"] != a.ts:
            gap = abs(int(near["timestamp"][:4]) - int(str(a.ts)[:4]))
            if gap >= 1:
                log(f"note: the nearest status-200 capture is {near['timestamp']} ({gap} year(s) from {a.ts})")
            ts = near["timestamp"]
    if not a.ts and ts:
        log(f"note: no timestamp given, so this is the LATEST healthy capture ({fmt_ts(ts)}). After a death or a "
            "domain sale that is often a different site: pass a timestamp from the era you mean.")
    if not ts:
        log("no capture found")
        sys.exit(2)
    try:
        body, final = fetch(replay_url(ts, a.url), max_bytes=20 * 1024 * 1024, want_url=True)
    except FileNotFoundError:
        log(f"not archived at {ts}: {a.url}")
        sys.exit(2)
    html_ = body.decode("utf-8", errors="replace")
    frames = re.findall(r"<i?frame[^>]+src=[\"']?([^\"' >]+)", html_, re.I) if re.search(r"<frameset|<iframe", html_, re.I) else []
    for src in frames[:6]:  # 1990s framesets: the text lives in the frames
        furl = re.sub(r"^https?://web\.archive\.org/web/\d+[a-z_]*/", "", urllib.parse.urljoin(a.url, src))
        if not furl.startswith("http"):
            continue
        try:
            fb = fetch(replay_url(ts, furl), max_bytes=5 * 1024 * 1024)
            html_ += "\n<!-- frame " + furl + " -->\n" + fb.decode("utf-8", errors="replace")
        except (RuntimeError, FileNotFoundError):
            log(f"frame not archived: {furl}")
    if frames:
        log(f"followed {len(frames[:6])} frame(s): their text is included below")
    title = re.search(r"<title[^>]*>(.*?)</title>", html_, re.S | re.I)
    html_ = re.sub(r"<!--.*?-->", " ", html_, flags=re.S)
    html_ = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html_, flags=re.S | re.I)
    main = None if getattr(a, "whole", False) or frames else _main_block(html_)
    whole_chars = None
    if main:
        whole_chars = len(_dedupe(_block_lines(html_)))
        html_ = main
    text = "\n".join(_dedupe(_block_lines(html_)))
    if main:
        log(f"main article body only ({len(text)} of {whole_chars} chars; menus, sidebars and teasers left out): "
            "--whole for the whole page")
    if re.search(r"(?i)domain (is )?(for sale|unavailable|parked)|buy this domain|this domain may be for sale|"
                 r"parkingcrew|sedoparking|hugedomains", text[:3000]):
        log("warning: this capture looks like a parked or for-sale domain page, not the site. Pass a "
            "timestamp from when it was alive (see `timeline`).")
    m = re.search(r"/web/(\d{14})", final or "")
    if a.ts and m and abs(int(m.group(1)[:4]) - int(str(a.ts)[:4])) >= 1:
        log(f"WARNING: asked for {a.ts}, Wayback served {m.group(1)}: probably not the version you mean")
    emit([{"url": a.url, "timestamp": m.group(1) if m else ts, "view": replay_url(m.group(1) if m else ts, a.url, raw=False),
           "title": strip_html(title.group(1)) if title else "", "chars": len(text),
           "text": text[: a.max] if a.max else text}], a.format)


ARTICLE_RX = re.compile(r"""(?is)<(article|div|section|main|td)\b[^>]*?(?:\bitemprop=["']?articleBody|"""
                        r"""\b(?:class|id)=["'][^"']*\b(?:field-name-body|field--name-body|node-content|node__content|"""
                        r"""entry-content|post-body|post-content|article-body|article-content|story-body|"""
                        r"""content-body|blog-post|storycontent)\b)[^>]*>|<article\b[^>]*>""")


def _element_at(html_: str, start: int, tag: str) -> str:
    """The element opening at `start`, through its matching close tag."""
    depth, rx = 0, re.compile(rf"(?is)<(/?){tag}\b[^>]*>")
    for m in rx.finditer(html_, start):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return html_[start:m.end()]
    return html_[start:]


def _main_block(html_: str) -> str | None:
    """The marked article body (Drupal, WordPress, Blogger, news) if it holds most of the prose, else None.
    Teasers and sidebars repeat the opening and bury the rest."""
    best, best_n = None, 0
    for m in ARTICLE_RX.finditer(html_):
        tag = (m.group(1) or "article").lower()
        el = _element_at(html_, m.start(), tag)
        n = sum(len(l) for l in _block_lines(el))
        if n > best_n:
            best, best_n = el, n
    return best if best_n >= 400 else None


def _block_lines(h: str) -> list[str]:
    h = re.sub(r"</(p|div|li|h[1-6]|tr|br|td|dd|dt|article|section|blockquote)>|<br\s*/?>", "\n", h, flags=re.I)
    return [l.strip() for l in strip_html_lines(h) if l.strip()]


def _dedupe(lines: list[str]) -> list[str]:
    """Drop repeated long lines (teasers, og summaries, print copies)."""
    seen, out = set(), []
    for l in lines:
        k = re.sub(r"\W+", " ", l).strip().lower()
        if len(k) >= 60 and k in seen:
            continue
        seen.add(k)
        out.append(l)
    return out


def strip_html_lines(h: str):
    import html as _html
    h = re.sub(r"<[^>]+>", lambda m: "\n" * m.group(0).count("\n") or " ", h)  # tags that span lines too
    for line in h.split("\n"):
        yield re.sub(r"\s+", " ", _html.unescape(line))


def _page_text(html_: str) -> list[str]:
    html_ = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html_, flags=re.S | re.I)
    html_ = re.sub(r"</(p|div|li|h[1-6]|tr|br|td|dd|dt)>|<br\s*/?>", "\n", html_, flags=re.I)
    return [l.strip() for l in strip_html_lines(html_) if l.strip()]


def cmd_changes(a):
    """How a page changed: one row per distinct capture, with versions, dates and added/dropped lines.
    Good on app store listings, changelogs, release notes, help pages and homepages."""
    import difflib
    a.url = norm_url(a.url)
    rows = list(cdx_query(a.url, match="exact", fl="timestamp,original,digest,statuscode",
                          filters=["statuscode:200"], collapse=["digest"], frm=a.frm, to=a.to, limit=2000))
    if not rows:
        sys.exit("no healthy captures (try the URL with/without www, a trailing slash, or `cdx` with --match prefix)")
    if len(rows) > a.max:  # keep first and last, spread the rest evenly
        step = (len(rows) - 1) / (a.max - 1)
        rows = [rows[round(i * step)] for i in range(a.max)]
    vrx = re.compile(a.pattern, re.I) if a.pattern else re.compile(
        r"(?i)\b(?:version|ver\.?|v)\s?(\d+(?:\.\d+){1,3}[a-z]?)\b|\b(\d+\.\d+(?:\.\d+)?)\s*\((?:\w+ \d{1,2}, )?\d{4}\)")
    out, prev = [], []
    for r in rows:
        try:
            body = fetch(replay_url(r["timestamp"], r["original"]), max_bytes=8 * 1024 * 1024)
        except (RuntimeError, FileNotFoundError) as e:
            out.append({"date": fmt_ts(r["timestamp"]), "error": str(e)[:120]})
            continue
        html_ = body.decode("utf-8", errors="replace")
        lang = (re.search(r"<html[^>]*\blang=[\"']?([a-zA-Z]{2})", html_) or [None, ""])[1].lower()
        first_lang = getattr(cmd_changes, "lang", None)
        if first_lang is None:
            cmd_changes.lang = lang
        elif lang and first_lang and lang != first_lang:
            continue  # other-language capture, not a product change
        lines = _page_text(html_)
        found = []
        for l in lines:
            for m in vrx.finditer(l):
                v = next((g for g in m.groups() if g), m.group(0)) if m.groups() else m.group(0)
                if v not in found:
                    found.append(v)
        news = [l for l in lines if re.search(r"(?i)what'?s new|new in (this )?version|release notes|changelog|"
                                               r"updated?:|bug fixes|improvements", l)]
        sm = difflib.SequenceMatcher(None, prev, lines, autojunk=False)
        added = [l for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag in ("insert", "replace") for l in lines[j1:j2]]
        removed = [l for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag in ("delete", "replace") for l in prev[i1:i2]]
        out.append({"date": fmt_ts(r["timestamp"]), "ts": r["timestamp"], "versions": found[:8],
                    "whats_new": [n[:160] for n in news[:4]],
                    "added": [l[:140] for l in added if len(l) > 14][: a.lines],  # short lines are menus and chrome
                    "removed": [l[:140] for l in removed if len(l) > 14][: a.lines] if prev else [],
                    "view": replay_url(r["timestamp"], r["original"], raw=False)})
        prev = lines
    log(f"{len(out)} distinct captures of {a.url}. Each version or visible change is a dated marker: record the ones "
        "that matter as evidence with as_of = the capture date, then `ledger.py versions add`.")
    emit(out, a.format)


def link_key(u: str) -> str:
    """One key per linked page: no scheme, www., port, fragment or trailing index.html; %7E as ~."""
    u = re.sub(r"^https?://web\.archive\.org/web/\d+[a-z_]*/", "", u.strip())
    p = urllib.parse.urlsplit(u if "://" in u else "http://" + u)
    host = (p.hostname or "").lower().removeprefix("www.")
    path = re.sub(r"(?i)/(index|default)\.s?html?$", "/", p.path.replace("%7E", "~").replace("%7e", "~")) or "/"
    return host + path.rstrip("/") + (f"?{p.query}" if p.query else "")


def extract_links(html_: str, base: str) -> list[tuple[str, str]]:
    """(text, absolute href) per <a>/<area> link, minus script, mail and # links. Text falls back to alt."""
    import html as _html
    out = []
    for m in re.finditer(r"(?is)<a\b([^>]*)>(.*?)(?=</a\s*>|<a\b)|<area\b([^>]*)>", html_):
        attrs = m.group(1) if m.group(1) is not None else m.group(3) or ""
        h = re.search(r"""(?is)\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", attrs)
        if not h:
            continue
        href = _html.unescape(next(g for g in h.groups() if g is not None)).strip()
        if not href or href.lower().startswith(("mailto:", "javascript:", "#", "news:", "tel:")):
            continue
        inner = m.group(2) or ""
        text = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", inner))).strip()
        if not text:
            alt = re.search(r"""(?is)\balt\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", inner or attrs)
            text = _html.unescape(next((g for g in alt.groups() if g is not None), "")).strip() if alt else ""
        absu = urllib.parse.urljoin(base, href)
        absu = re.sub(r"^https?://web\.archive\.org/web/\d+[a-z_]*/", "", absu)
        if absu.startswith("http"):
            out.append((text[:160], urllib.parse.urldefrag(absu)[0]))
    return out


def merge_links(found) -> list[dict]:
    """found: (source_url, ts, [(text, href)...]) per capture. One row per linked page with first/last
    listed dates, capture count, link texts and, if several pages list it, per-source first..last."""
    rows = {}
    for src, ts, links in sorted(found, key=lambda f: f[1]):
        d = fmt_ts(ts)
        for text, href in links:
            k = link_key(href)
            r = rows.setdefault(k, {"url": href, "text": text, "first": d, "last": d, "seen": 0, "_texts": [],
                                    "_src": {}, "_caps": set()})
            r["last"] = max(r["last"], d)
            r["first"] = min(r["first"], d)
            if (src, ts) not in r["_caps"]:
                r["_caps"].add((src, ts))
                r["seen"] += 1
            if text and text not in r["_texts"]:
                r["_texts"].append(text)
            if not r["text"]:
                r["text"] = text
            s_ = r["_src"].setdefault(link_key(src), [d, d])
            s_[0], s_[1] = min(s_[0], d), max(s_[1], d)
    out = []
    for r in sorted(rows.values(), key=lambda r: (r["first"], link_key(r["url"]))):
        texts, srcs = r.pop("_texts"), r.pop("_src")
        r.pop("_caps")
        if len(texts) > 1:
            r["other_texts"] = [t for t in texts if t != r["text"]][:3]
        if len(srcs) > 1:
            r["sources"] = {k: f"{v[0]}..{v[1]}" for k, v in srcs.items()}
        out.append(r)
    return out


def cmd_links(a):
    """Rebuild a lost index from period link lists ("New Pages", a Yahoo category, a webring hub).
    Args: URL TS [TS...] [URL2 TS...]. A URL with no TS uses its latest healthy capture."""
    pairs, cur, used = [], None, set()
    for x in a.items:
        if re.fullmatch(r"\d{4,14}", x):
            if cur is None:
                sys.exit("links: give the page URL before its timestamps (links URL TS [TS...] [URL2 TS...])")
            pairs.append((cur, x))
            used.add(cur)
        else:
            if cur and cur not in used:
                pairs.append((cur, None))
            cur = norm_url(x if "://" in x else "http://" + x)
    if cur and cur not in used:
        pairs.append((cur, None))
    found = []
    for url, ts in pairs:
        if ts is None:
            ts = (closest_capture(url) or {}).get("timestamp")
            if not ts:
                log(f"no healthy capture of {url}: skipped")
                continue
        try:
            body, final = fetch(replay_url(ts, url), max_bytes=10 * 1024 * 1024, want_url=True)
        except (RuntimeError, FileNotFoundError) as e:
            log(f"{url} @ {ts}: not read ({str(e)[:80]})")
            continue
        m = re.search(r"/web/(\d{14})", final or "")
        served = m.group(1) if m else ts
        if abs(_months(served) - _months(str(ts).ljust(14, "0"))) >= 12:
            log(f"note: asked {ts} for {url}, Wayback served {served}: dated as served")
        links = extract_links(body.decode("utf-8", "replace"), url)
        if a.same_host:
            h = link_key(url).split("/")[0]
            links = [l for l in links if link_key(l[1]).split("/")[0] == h]
        if a.match:
            links = [l for l in links if re.search(a.match, l[1], re.I)]
        log(f"{fmt_ts(served)} {url[:90]}: {len(links)} links")
        found.append((url, served, links))
    rows = merge_links(found)
    log(f"{len(rows)} distinct linked pages from {len(found)} capture(s); first/last = the dates a list showed them")
    emit(rows, a.format)


GEO_MIRRORS = ["https://www.oocities.org/{path}", "http://www.reocities.com/{path}",
               "https://geocities.restorativland.org/{path}"]


def cmd_known(a):
    """Every page a small site is known to have had: captured, captured only as an error, or only linked.
    GeoCities paths also check outside mirrors. Each file lists its capture eras (one per content hash),
    so a redesign can't hide an early original. --max-seconds caps the run and prints what it has."""
    import time as _t
    t0 = _t.time()
    base = norm_url(a.url if "://" in a.url else "http://" + a.url).rstrip("/")
    host_path = re.sub(r"^https?://", "", base)
    def canon(u):  # www., ~/%7E and index.html variants are one page
        u = re.sub(r"^https?://(www\.)?", "", norm_url(u)).split("#")[0]
        u = u.replace("%7E", "~").replace("%7e", "~")
        return re.sub(r"/(index\.html?|default\.html?)$", "/", u)
    rows, stopped = [], None
    variants = {host_path, re.sub(r"^www\.", "", host_path), "www." + re.sub(r"^www\.", "", host_path)}
    variants |= {v.replace("~", "%7E") for v in variants}
    fl = "timestamp,original,statuscode,mimetype,length,digest"
    _arm(a.max_seconds)
    try:
        for v in sorted(variants):
            # urlkey pass first: collapse=digest alone can drop an address
            # that matches its neighbour (mr.t/ vs mr.t/index.html)
            for q in ({"collapse": ["urlkey"]}, {"collapse": ["digest"], "filters": ["statuscode:200"]}):
                try:
                    rows += list(cdx_query(v, match="prefix", fl=fl, frm=a.frm, to=a.to, limit=5000, follow=True,
                                           max_pages=10, **q))
                except RuntimeError as e:
                    log(f"cdx {v}: {e}")
    except _Deadline:
        stopped = "while listing captures"
    _disarm()
    seen_rows, uniq = set(), []
    for r in sorted(rows, key=lambda r: r["timestamp"]):  # oldest first: earliest real capture wins
        if (r["original"], r["timestamp"]) not in seen_rows:
            seen_rows.add((r["original"], r["timestamp"]))
            uniq.append(r)
    pages, eras, addrs = {}, collections.defaultdict(dict), collections.defaultdict(set)
    for r in uniq:
        u = norm_url(r["original"]).split("#")[0]
        key = canon(u)
        addrs[key].add(u)
        kind = _file_class(u, r.get("mimetype", ""))
        st = str(r.get("statuscode", ""))
        ln = int(r.get("length") or 0) if str(r.get("length") or "").isdigit() else 0
        cur = pages.setdefault(key, {"url": u, "kind": kind, "status": st, "ts": r["timestamp"], "bytes": ln})
        if st == "200" and (cur["status"] != "200" or (cur.get("bytes", 0) < 1200 <= ln)):
            cur.update(url=u, status="200", ts=r["timestamp"], bytes=ln)  # prefer a real capture over a stub
        if a.died and r["timestamp"][:4] > str(a.died)[:4]:
            cur["after_death"] = True
        if st == "200":  # one era per distinct content
            d = r.get("digest") or r["timestamp"]
            e = eras[key].get(d)
            if e is None:
                eras[key][d] = e = {"first": fmt_ts(r["timestamp"]), "last": fmt_ts(r["timestamp"]),
                                    "ts": r["timestamp"], "url": u, "bytes": ln}
                if a.died and r["timestamp"][:4] > str(a.died)[:4]:
                    e["after_death"] = True
            e["last"] = fmt_ts(r["timestamp"])
            if u not in (e["url"], *e.get("also_at", [])):
                e.setdefault("also_at", []).append(u)
    for key, cur in pages.items():
        if cur["kind"] == "page" and cur["status"] == "200" and 0 < cur.get("bytes", 0) < 1200:
            cur["stub"] = True  # tiny: likely a "closed" notice or redirect script
        es = sorted(eras.get(key, {}).values(), key=lambda e: e["ts"])
        if es and (cur["kind"] == "page" or len(es) > 1):
            cur["eras"] = es
            if len(es) > 1:
                cur["first_ts"] = es[0]["ts"]
        if len(addrs[key]) > 1:
            cur["addresses"] = sorted(addrs[key])  # e.g. mr.t/ and mr.t/index.html
    linked = {}
    read = 0
    left = a.max_seconds - (_t.time() - t0)
    if stopped is None and left >= 1:
        _arm(left)
        try:
            for u, p in list(pages.items()):  # links on captured pages name pages that existed
                if p["status"] != "200" or p["kind"] != "page" or read >= a.read or \
                        _t.time() - t0 > a.max_seconds / 2:
                    continue
                read += 1
                try:
                    body = fetch(replay_url(p["ts"], p["url"]), max_bytes=3 * 1024 * 1024).decode("utf-8", errors="replace")
                except (RuntimeError, FileNotFoundError):
                    continue
                for m in re.finditer(r"(?is)\b(?:href|src|background)\s*=\s*[\"']?([^\"'\s>]+)", body):
                    ref = m.group(1)
                    if ref.lower().startswith(("mailto:", "javascript:", "#")) or re.fullmatch(r"(?i)https?:?/*", ref):
                        continue  # malformed links ("http", "http:/") aren't pages
                    absu = norm_url(urllib.parse.urljoin(p["url"], ref)).split("#")[0]
                    k = canon(absu)
                    same_host = canon(absu).split("/")[0] == canon(base).split("/")[0]
                    if k in pages:
                        continue
                    if canon(absu).startswith(canon(base)) or (same_host and _file_class(absu, "") in ("image", "media")):
                        linked.setdefault(k, (absu, p["url"]))  # a shared /img/ outside the root counts too
            for k, (u, frm_page) in linked.items():
                pages.setdefault(k, {"url": u, "kind": _file_class(u, ""), "status": "never captured",
                                     "linked_from": frm_page})
            if "geocities.com" in host_path:  # outside mirrors of the 2009 Archive Team rescue
                probes = 0
                for p in pages.values():
                    if p["status"] == "200" or p["kind"] not in ("page", "media") or probes >= 40:
                        continue
                    probes += 1
                    path = re.sub(r"^(?:https?://)?(?:www\.)?geocities\.com/", "", p["url"])
                    for tmpl in GEO_MIRRORS:
                        murl = tmpl.format(path=path)
                        try:
                            fetch(murl, max_bytes=200_000, max_retries=0)
                            p["mirror"] = murl
                            break
                        except (RuntimeError, FileNotFoundError):
                            continue
        except _Deadline:
            stopped = "while reading pages for links"
        _disarm()
        for k, (u, frm_page) in linked.items():  # links found before a stop still count
            pages.setdefault(k, {"url": u, "kind": _file_class(u, ""), "status": "never captured",
                                 "linked_from": frm_page})
    elif stopped is None:
        stopped = "before reading pages for links"
    if stopped:
        log(f"stopped at the {a.max_seconds}s limit {stopped}: the list below is partial (raise --max-seconds, "
            "or narrow with --from/--to)")
    out = sorted(pages.values(), key=lambda p: (p["kind"] != "page", p["status"] != "200", p["url"]))
    counts = collections.Counter(("captured" if p["status"] == "200" else "error capture" if p["status"] not in
                                  ("never captured",) else "never captured") + f" {p['kind']}" for p in out)
    log("known files: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) +
        ". Restore the captured ones (`mirror`), recover the rest from mirrors, copies and quotes, and list what "
        "nothing holds as 'known to have existed'. bytes = compressed WARC record size (fetch reports the file's).")
    if a.format == "md":  # eras whole, one compact first..last:bytes item each
        out = [dict(p, **({"eras": md_eras(p["eras"], p["url"])} if p.get("eras") else {}),
                    **({"addresses": " ".join(p["addresses"])} if p.get("addresses") else {})) for p in out]
    emit(out[: a.max], a.format, keep=("eras", "addresses"))


def md_eras(es, main_url: str = "") -> str:
    """Eras as 1999-03-02..2001-07-14:4123 (bytes = compressed WARC size), † after death, @addr if elsewhere."""
    return "<br>".join(f"{e['first']}..{e['last']}:{e.get('bytes', 0)}" + ("†" if e.get("after_death") else "")
                       + (f" @{e['url'].split('://')[-1]}" if main_url and e["url"] != main_url else "") for e in es)


def cmd_alive(a):
    """Is it alive now? DNS, status, final URL, title, tombstone words. A control host rules out "no network"."""
    import socket
    import urllib.request
    host = urllib.parse.urlparse(a.url if "://" in a.url else "http://" + a.url).hostname
    out, live_fp = {"url": a.url, "host": host}, None
    try:
        out["dns"] = socket.gethostbyname(host)
    except OSError as e:
        out["dns"] = f"does not resolve ({e.__class__.__name__})"
    out["network"] = "NO NETWORK: results below say nothing about the target"
    for control in ("https://example.com/", "https://archive.org/"):  # one control host may refuse or be blocked
        try:
            urllib.request.urlopen(urllib.request.Request(control, method="HEAD"), timeout=10)
            out["network"] = "ok"
            break
        except Exception:
            continue
    if not str(out["dns"]).startswith("does not"):
        try:
            req = urllib.request.Request(a.url if "://" in a.url else "http://" + a.url,
                                         headers={"User-Agent": "Mozilla/5.0 (resurrect liveness check)"})
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read(400_000).decode("utf-8", errors="replace")
                out.update({"status": r.status, "final_url": r.geturl()})
                out["network"] = "ok"  # the target itself answered
        except urllib.error.HTTPError as e:
            body = ""
            out.update({"status": e.code, "final_url": e.geturl()})
        except Exception as e:
            body = ""
            out["status"] = f"no response ({e.__class__.__name__})"
        t = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        import html as _html
        out["title"] = _html.unescape(re.sub(r"\s+", " ", t.group(1))).strip()[:120] if t else ""
        # tombstones say it up front, so check the lead only
        lead = out["title"] + "\n" + "\n".join(_page_text(body))[:2500]
        words = re.findall(r"(?i)\b(archived state|has been archived|discontinued|shut(?:ting)? down|no longer "
                           r"(?:available|supported)|end of life|retired|sunset|we(?:'|’)re closing|farewell|goodbye|"
                           r"domain (?:is )?for sale|parked)\b", lead)
        out["tombstone_words"] = sorted({w.lower() for w in words})[:10]
        live_fp = page_fingerprint(body, host or "") if body else None
        if live_fp and live_fp.get("parked"):
            out["tombstone_words"] = sorted(set(out["tombstone_words"]) | {"parked-domain page"})
    final_host = urllib.parse.urlparse(str(out.get("final_url") or "")).netloc.lower()
    asked_host = urllib.parse.urlparse(a.url if "://" in a.url else "http://" + a.url).netloc.lower().removeprefix("www.")
    if final_host and asked_host and final_host.removeprefix("www.") != asked_host and \
            not final_host.endswith("." + asked_host):
        out["redirects_to"] = final_host
        out["tombstone_words"] = sorted(set(out.get("tombstone_words") or []) |
                                        {f"redirects to {final_host}: a different product or site?"})
    if re.match(r"ww[0-9]+\.", final_host) or re.search(r"park|sedo|bodis|above\.com|domain(s)?for", final_host):
        out["tombstone_words"] = sorted(set(out.get("tombstone_words") or []) | {"parked-domain redirect"})
    eras = None
    if not getattr(a, "no_archive", False) and out.get("network") == "ok" and isinstance(out.get("status"), int) \
            and out["status"] < 400 and live_fp:
        _arm(60)  # is what answers now the archived original's site?
        try:
            eras = site_eras(a.url, budget=45)
        except _Deadline:
            pass
        _disarm()
    if eras and not live_fp.get("parked"):
        o = eras["original"]
        later = eras.get("later") or {}
        tw, ow = _title_words(live_fp.get("title")), _title_words(o.get("title"))
        if not same_site(live_fp, o) and (later or (tw and ow and not tw & ow)):
            out["different_site"] = {"archived_original": f"{o['from']}..{o['to']}" + (f" {o['title']!r}" if o.get("title")
                                                                                    else ""),
                                     "now": out.get("title") or "(no title)",
                                     **({"changed_hands": later["from"]} if later.get("from") else {})}
            out["tombstone_words"] = [w for w in out.get("tombstone_words") or [] if not w.startswith(("retired", "sunset",
                                      "farewell", "goodbye", "discontinued", "shut"))]
    out["verdict"] = ("no network: cannot tell" if out["network"] != "ok" else
                      "gone (host does not resolve)" if str(out["dns"]).startswith("does not") else
                      f"gone at this address (final status {out.get('status')})"
                      if isinstance(out.get("status"), int) and out["status"] >= 400 else
                      f"no response ({out.get('status')}) while the network works: likely gone"
                      if not isinstance(out.get("status"), int) else
                      "answers, but the domain now hosts a different site"
                      + (f" (from {out['different_site']['changed_hands'][:4]})" if out["different_site"].get("changed_hands")
                         else "") + ": the original is gone here; this is neither the original nor its tombstone"
                      if out.get("different_site") else
                      "answers, but reads like a tombstone: read it" if out.get("tombstone_words") else
                      "answers: check it is the same product (owner, era), not a squatter or successor")
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_find(a):
    """Archived URLs under a domain containing a keyword. Quickest way to a game's SWF or a JS bundle."""
    kw = a.keyword if a.case else "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else c for c in a.keyword)
    filters = [f"original:{_loose(kw)}"]  # CDX rejects (?i), so spell out both cases
    if a.mime:
        filters.append(f"mimetype:{_loose(a.mime)}")
    if not a.any_status:
        filters.append("statuscode:200")
    dom = re.sub(r"^https?://", "", a.domain)
    has_path = "/" in dom.rstrip("/")  # e.g. home.att.net/~natehall: prefix-query the folder
    target, match = ((dom.rstrip("/") + a.prefix, "prefix") if a.prefix else
                     (dom, "prefix") if has_path else (dom.rstrip("/"), "domain"))
    import time as _t
    t0, rows = _t.time(), []
    _arm(a.max_seconds)  # hard limit, incl. slow CDX pages and backoff
    try:
        for tgt in [target] + ([target.replace("~", "%7E")] if "~" in target else []):
            if rows:
                break  # %7E spelling only if ~ found nothing
            for r in cdx_query(tgt, match=match, fl="timestamp,original,mimetype,length",
                               filters=filters, collapse=["urlkey"], frm=a.frm, to=a.to, limit=a.limit,
                               follow=True, max_pages=a.pages):
                rows.append(r)
                if _t.time() - t0 > a.max_seconds:
                    log(f"stopped after {a.max_seconds}s with {len(rows)} results: narrow with --prefix or --from/--to")
                    break
    except _Deadline:
        log(f"stopped at the {a.max_seconds}s limit with {len(rows)} results. Big domains: list the paths first "
            f"(`wayback.py urls {a.domain} --group --to <death year>`), then `find --prefix /<path>/`, or use a "
            "subdomain as DOMAIN.")
    except RuntimeError as e:
        _disarm()
        log(f"error: {e}\nBig domains time out on a whole-domain scan. Narrow it: --prefix /games/ (or a "
            "subdomain such as media1.example.com as DOMAIN), --from/--to the product's years, or --mime.")
        sys.exit(1)
    _disarm()
    log(f"{len(rows)} matching URL(s) under {target} ({match} match"
        + (f", mime {a.mime}" if a.mime else "") + (f", {a.frm}-{a.to}" if a.frm or a.to else "")
        + "). bytes = archived (compressed) size.")
    emit([{"date": fmt_ts(r["timestamp"]), "ts": r["timestamp"], "url": r["original"],
           "mime": r.get("mimetype"), "bytes": r.get("length"),
           "view": replay_url(r["timestamp"], r["original"], raw=False)} for r in rows], a.format)


def cmd_urls(a):
    filters = ["statuscode:200"]
    if a.mime:
        filters.append(f"mimetype:{_loose(a.mime)}")
    target = a.domain.rstrip("/") + (a.prefix or "")
    match = "prefix" if a.prefix else a.match
    rows = list(cdx_query(target, match=match, fl="timestamp,original,mimetype",
                          filters=filters, collapse=["urlkey"], frm=a.frm, to=a.to, limit=a.limit,
                          follow=True, max_pages=a.pages))
    if a.group:
        groups = collections.Counter()
        examples = {}
        for r in rows:
            p = urllib.parse.urlparse(r["original"])
            seg = "/" + (p.path.strip("/").split("/")[0] if p.path.strip("/") else "")
            k = f"{p.netloc}{seg}"
            groups[k] += 1
            examples.setdefault(k, r["original"])
        out = [{"section": k, "urls": n, "example": examples[k]} for k, n in groups.most_common()]
        emit(out, a.format)
    else:
        emit([{"date": fmt_ts(r["timestamp"]), "ts": r["timestamp"], "url": r["original"],
               "mime": r.get("mimetype")} for r in rows], a.format)


FILE_CLASSES = [
    ("program", (".swf", ".dcr", ".dir", ".air", ".jar", ".exe", ".msi", ".dmg", ".apk", ".ipa", ".xap", ".unity3d",
                 ".zip", ".rar", ".7z", ".cab", ".sit", ".hqx", ".iso", ".wasm"),
     ("shockwave", "flash", "java-archive", "zip", "octet-stream", "x-director", "air")),
    ("media", (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".mid", ".midi", ".flv", ".f4v", ".mp4", ".mov", ".wmv", ".avi",
               ".webm", ".m4v"), ("audio", "video")),
    ("document", (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".rtf"), ("pdf", "msword", "powerpoint", "rtf")),
    ("data", (".xml", ".json", ".txt", ".csv", ".plist", ".dat", ".amf", ".js", ".css"),
     ("xml", "json", "javascript", "text/plain", "text/css")),
    ("image", (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".bmp", ".webp"), ("image",)),
]


def _file_class(url: str, mime: str, length: str | int | None = None) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    if "html" in (mime or ""):
        return "page"  # cgi-bin/Answer.exe?q=... served pages, not programs
    # web-app clients: Closure/j2cl modules (/_/js/, /_/scs/) and big bundles
    if re.search(r"/_/(js|scs)/|/static/js/|\.bundle\.js$|[._-]min\.js$|app\.[0-9a-f]{6,}\.js$", path) or \
            ("javascript" in (mime or "") and str(length or "0").isdigit() and int(length or 0) > 150_000):
        return "program"
    ext = "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    for name, exts, mimes in FILE_CLASSES:
        if ext in exts:
            return name
    for name, exts, mimes in FILE_CLASSES:
        if any(m in (mime or "") for m in mimes):
            return name
    return "page"


def _ext_rx(exts) -> str:
    """CDX regex for URLs ending in these extensions (query allowed), both cases spelled out: CDX rejects (?i)."""
    alts = "|".join("".join(f"[{c}{c.upper()}]" if c.isalpha() else c for c in e.lstrip(".")) for e in sorted(exts))
    return rf".*\.({alts})(\?.*)?$"


PROGRAM_MEDIA_EXT = {e for name, exts, _ in FILE_CLASSES if name in ("program", "media") for e in exts}


def uncaptured_from_rows(rows, captured_urls=()) -> list[dict]:
    """Program/media URLs whose every capture is HTML, a redirect or an error.
    Parked domains answer /game.swf with a page, which doesn't count."""
    def key(u):  # ignore query string (?session_id=...) and doubled slashes
        p = urllib.parse.urlparse(norm_url(u))
        return (re.sub(r"^www\.", "", (p.hostname or "")) + re.sub(r"/{2,}", "/", p.path)).replace(
            "%7E", "~").replace("%7e", "~")
    by = collections.defaultdict(list)
    for r in rows:
        if Path(urllib.parse.urlparse(norm_url(r["original"])).path.lower()).suffix in PROGRAM_MEDIA_EXT:
            by[key(r["original"])].append(r)
    done = {key(u) for u in captured_urls}
    out = []
    for k, rs in sorted(by.items()):
        if k in done or any(str(r.get("statuscode")) == "200" and "html" not in (r.get("mimetype") or "")
                            and r.get("mimetype") != "warc/revisit" for r in rs):
            continue
        variants = len({r["original"] for r in rs})
        rs.sort(key=lambda r: r["timestamp"])
        what = collections.Counter(f"{'html' if 'html' in (r.get('mimetype') or '') else r.get('mimetype')} "
                                   f"{r.get('statuscode')}" for r in rs)
        path = urllib.parse.urlparse(norm_url(rs[0]["original"])).path
        out.append({"kind": _file_class(path, ""), "status": "known, never captured", "url": norm_url(rs[0]["original"]),
                    "first": fmt_ts(rs[0]["timestamp"]), "last": fmt_ts(rs[-1]["timestamp"]),
                    "captures": ", ".join(f"{n}x {w}" for w, n in what.most_common(3))
                    + (f" ({variants} query-string variants)" if variants > 1 else "")})
    return out


def known_uncaptured(domain: str, rows, frm=None, to=None) -> list[dict]:
    """All captures of program/media URLs under the domain, any status. Not collapsed: collapse only
    compares neighbours, so two URLs captured the same month would merge."""
    allrows = list(cdx_query(domain, match="domain", fl="timestamp,original,mimetype,statuscode",
                             filters=[f"original:{_ext_rx(PROGRAM_MEDIA_EXT)}"], frm=frm, to=to, limit=10000))
    real = [r["original"] for r in rows if "html" not in (r.get("mimetype") or "")]
    return uncaptured_from_rows(allrows, real)


PATTERN_MIN = 12  # numbered series this big collapse to one row


def _num_pattern(url: str):
    """('/vids/clip_N.swf', 17) from the last number in the file name, else None."""
    p = urllib.parse.urlparse(norm_url(url))
    d, _, name = p.path.rpartition("/")
    ms = list(re.finditer(r"\d+", name))
    if not ms or p.query:
        return None
    m = ms[-1]
    return f"{d}/{name[:m.start()]}N{name[m.end():]}", int(m.group(0))


def _runs(nums, cap: int = 10) -> str:
    """[0,1,2,3,4,17,40,41] -> '0-4, 17, 40-41' (at most `cap` runs, then '+k more')."""
    runs = []
    for n in sorted(nums):
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    txt = ", ".join(f"{a_}-{b_}" if a_ != b_ else str(a_) for a_, b_ in runs[:cap])
    return txt + (f" +{len(runs) - cap} more" if len(runs) > cap else "")


def collapse_patterns(rows: list[dict], min_n: int = PATTERN_MIN) -> tuple[list[dict], list[dict]]:
    """Inventory rows -> (summary rows, other rows). Numbered series (/vids/clip_5.swf .. clip_338.swf)
    become one row with count, numbers, gaps, dates and sizes. A series with one digest (or size) is
    flagged: probably ONE placeholder file served under every name."""
    groups = collections.defaultdict(list)
    for r in rows:
        k = _num_pattern(r.get("url") or "") if r.get("url") and not r.get("status") else None
        if k:
            groups[(r.get("kind"), (r.get("host") or "").removeprefix("www."), k[0])].append((k[1], r))
    big = {key: m for key, m in groups.items() if len(m) >= min_n}
    in_big = {id(r) for m in big.values() for _, r in m}
    by_digest = {}  # digest -> an ungrouped file with that content
    for r in rows:
        if id(r) not in in_big and r.get("digest"):
            by_digest.setdefault(r["digest"], r["url"])
    out = []
    for (kind, host, pat), members in sorted(big.items(), key=lambda kv: -len(kv[1])):
        members.sort(key=lambda x: x[0])
        nums = [n for n, _ in members]
        lo = nums[0] if nums[0] in (0, 1) or nums[0] > 10 else 0
        gaps = sorted(set(range(lo, nums[-1] + 1)) - set(nums))
        rs = [r for _, r in members]
        sizes = sorted(int(r["bytes"]) for r in rs if str(r.get("bytes") or "").isdigit())
        digests = {r.get("digest") for r in rs}
        row = {"kind": kind, "host": rs[0].get("host"), "pattern": pat, "files": len(rs),
               "N": f"{nums[0]}..{nums[-1]}", **({"gaps": _runs(gaps)} if gaps else {}),
               "dates": f"{min(r['date'] for r in rs)}..{max(r['date'] for r in rs)}",
               "bytes": (f"{sizes[0]}..{sizes[-1]}" if sizes and sizes[0] != sizes[-1] else
                         str(sizes[0]) if sizes else None),
               "example": norm_url(rs[0]["url"]), "ts": rs[0]["ts"]}
        one = (len(digests) == 1 and None not in digests) or (None in digests and sizes and sizes[0] == sizes[-1]
                                                              and len(sizes) == len(rs))
        if one:
            same = by_digest.get(rs[0].get("digest")) if rs[0].get("digest") else None
            row["one_file"] = ("probably one file served under every name (" +
                               ("same digest" if None not in digests else "same size") + " on all "
                               f"{len(rs)}" + (f", identical to {same}" if same else "") +
                               "): check one hash before treating them as distinct")
        out.append(row)
    return out, [r for r in rows if id(r) not in in_big]


def cmd_inventory(a):
    """Every archived file under a domain and its subdomains, by kind (programs, media, documents, data).
    Dead services often survive on subdomains (application., media., static., cdn.).
    Files first captured after a resale or parking break are left out unless --all-eras. Program/media
    URLs only ever captured as HTML, a redirect or an error are listed as "known, never captured"."""
    import time as _t
    t0 = _t.time()
    eras = None
    if not a.to and not getattr(a, "no_eras", False):
        _arm(min(75, a.max_seconds / 3))
        try:
            eras = site_eras(a.domain.rstrip("/"), budget=min(60, a.max_seconds / 4))
        except _Deadline:
            log("era check stopped at its time limit: later owners are not separated (pass --to YYYY)")
        _disarm()
    cut = ((eras or {}).get("cut_ts") or "")[:8]  # whole day: parking crawls grab favicon.ico seconds earlier
    _arm(max(10, a.max_seconds - (_t.time() - t0)))
    rows = []
    try:
        for r in cdx_query(a.domain.rstrip("/"), match="domain", fl="timestamp,original,mimetype,length,digest",
                           filters=["statuscode:200"] + (["!mimetype:text/html"] if a.assets_only else []),
                           collapse=["urlkey"], frm=a.frm, to=a.to, limit=a.limit, follow=True, max_pages=a.pages):
            rows.append(r)
    except _Deadline:
        log(f"stopped at the {a.max_seconds}s limit after {len(rows)} URLs: the listing below is partial. "
            + ("Big domains: retry with --assets-only (skips HTML pages, the bulk of a big site), or `inventory "
               "<subdomain>` per host, or `find <host> <word>`." if not a.assets_only else
               "Try one subdomain at a time, or `find <host> <word> --prefix /<path>/`."))
    except RuntimeError as e:
        log(f"error after {len(rows)} URLs: {e}")
    _disarm()
    later = [r for r in rows if cut and r["timestamp"] >= cut]
    if later and not getattr(a, "all_eras", False):
        rows = [r for r in rows if r["timestamp"] < cut]
    left = a.max_seconds - (_t.time() - t0)
    uncaptured = []
    if left >= 5:
        _arm(left)
        try:
            uncaptured = known_uncaptured(a.domain.rstrip("/"), rows, a.frm, a.to)
            for u in uncaptured:
                if cut and u["first"].replace("-", "") >= cut:
                    u["note"] = "every capture is after the domain changed hands: the later site answered this path"
        except _Deadline:
            log("stopped at the time limit while checking program/media URLs for real captures")
        except RuntimeError as e:
            log(f"known-but-uncaptured check failed: {e}")
        _disarm()
    n_pages = sum(1 for r in rows if _file_class(r["original"], r.get("mimetype", "")) == "page")
    a.pages_list = a.list_pages or n_pages <= 150  # small site (fan pages): list every page
    n_images = sum(1 for r in rows if _file_class(r["original"], r.get("mimetype", ""), r.get("length")) == "image")
    images_list = getattr(a, "images", False) or n_images <= 150  # sprites and art, every one is evidence
    by_host = collections.defaultdict(collections.Counter)
    files = collections.defaultdict(list)
    everything = []  # for --save: every row, listed or not
    for r in rows:
        cls = _file_class(r["original"], r.get("mimetype", ""), r.get("length"))
        host = urllib.parse.urlparse(r["original"]).netloc.split(":")[0]
        by_host[host][cls] += 1
        row = {"kind": cls, "host": host, "date": fmt_ts(r["timestamp"]), "ts": r["timestamp"],
               "url": r["original"], "mime": r.get("mimetype"), "bytes": r.get("length"),
               **({"later_site": True} if cut and r["timestamp"] >= cut else {}),
               **({"digest": r["digest"]} if r.get("digest") else {})}
        everything.append(row)
        if cls in ("program", "media", "document") or (cls == "data" and a.data) or (cls == "page" and a.pages_list) \
                or (cls == "image" and images_list):
            files[cls].append(row)
    leads = query_leads(rows)
    summary = [{"host": h, **dict(c)} for h, c in sorted(by_host.items(), key=lambda kv: -sum(kv[1].values()))]
    log(f"{len(rows)} archived URLs under {a.domain} and its subdomains (status 200, one per address, first "
        "capture's date; bytes = compressed WARC record size)")
    if eras:
        log(era_note(eras) + (f". Listing files first captured before {fmt_ts(cut)}; {len(later)} later files left "
                              "out (--all-eras lists them, marked later_site)" if cut and not getattr(a, "all_eras", False)
                              else ""))
    print(json.dumps({"hosts": summary, **({"eras": {k: v for k, v in eras.items() if k != "captures_read"}}
                                            if eras else {})}, indent=1))
    listed = [f for cls in ("program", "media", "document", "image", "data", "page") for f in files.get(cls, [])]
    order = {c: i for i, c in enumerate(("program", "media", "document", "image", "data", "page"))}
    full = sorted(everything, key=lambda f: (order.get(f["kind"], 9), f["url"]))
    if leads:
        log("the program URLs name these in their query strings (FlashVars: where the movie loads its parts and data "
            "from), and this inventory holds nothing under them: " + "; ".join(leads[:10])
            + ". Run `inventory` or `cdx --match prefix` on each before calling the parts lost.")
    patterns, listed = collapse_patterns(listed)
    if patterns:
        log(f"{sum(p_['files'] for p_ in patterns)} files in {len(patterns)} numbered series are summarised as one row "
            "each (pattern, N range, gaps)" + ("; a one_file flag means every name holds the same bytes"
                                               if any(p_.get("one_file") for p_ in patterns) else ""))
    if uncaptured:
        log(f"{len(uncaptured)} program/media URL(s) are KNOWN but never captured as the file (every capture is an "
            "HTML page, a redirect or an error): the original may not survive in the archive. Look for copies "
            "elsewhere (mirrors, portals, fan archives) before concluding it is lost.")
        listed = uncaptured + listed
    listed = patterns + listed
    if patterns or len(listed) > a.max_files or len(full) > len(listed) or getattr(a, "save", None):
        save = Path(a.save) if getattr(a, "save", None) else (
            cache_dir() / "inventory" / f"{slugify(a.domain)}{'-to' + str(a.to) if a.to else ''}.jsonl")
        try:
            save.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(save, "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in full))
            log(f"full list ({len(full)} files, one row each with its ts): {save}. `wayback.py mirror --from-inventory "
                f"{save} [--match REGEX] --original` fetches rows straight from their captures (no CDX lookups)")
        except OSError as e:
            log(f"could not save the full list ({e})")
    unlisted = {cls: sum(c[cls] for c in by_host.values()) for cls in ("image", "data", "page")
                if sum(c[cls] for c in by_host.values()) and not files.get(cls)}
    if unlisted:
        flags = {"image": "--images", "data": "--data", "page": "--list-pages"}
        log("counted but not listed: " + ", ".join(f"{n} {k} (add {flags[k]})" for k, n in unlisted.items())
            + (f"; listing stops at --max-files {a.max_files}" if len(listed) > a.max_files else ""))
    elif len(listed) > a.max_files:
        log(f"{len(listed)} files to list; only the first {a.max_files} are shown (raise --max-files)")
    progs = [f for f in files.get("program", []) if not f.get("later_site")]
    if progs:
        span = f"{min(f['date'] for f in progs)[:4]}-{max(f['date'] for f in progs)[:4]}"
        log(f"{len(progs)} PROGRAM files captured ({span}: clients, games, installers, archives). "
            + ("Captured before the domain changed hands, so likely the original's own: "
               if cut else "Check their dates against the site's life (`timeline`) before calling them the original: ")
            + "fetch with `wayback.py fetch <url> <ts> --original`, inspect (`inspect_swf.py`, unzip -l), "
            "extract their art, sounds and text; never run them natively.")
    n_later = sum(1 for cls in ("program", "media") for f in files.get(cls, []) if f.get("later_site"))
    if n_later:
        log(f"{n_later} program/media files are from the later site (marked later_site): not the original.")
    media = [f for f in files.get("media", []) if not f.get("later_site")]
    if media:
        log(f"{len(media)} media files captured (sounds, music, video): likely the original's own audio and footage.")
    emit([{k: v for k, v in f.items() if k != "digest"} for f in listed[: a.max_files]], a.format)


def query_leads(rows: list[dict]) -> list[str]:
    """Hosts/folders named in program URL query strings (?component_base=/rs/swf/) that the listing lacks."""
    paths = {urllib.parse.urlparse(r["original"]).path.lower() for r in rows}
    urls = {re.sub(r"^https?://(www\.)?", "", r["original"].lower()) for r in rows}
    hosts = {urllib.parse.urlparse(r["original"]).hostname for r in rows  # a robots.txt alone doesn't count as content
             if not urllib.parse.urlparse(r["original"]).path.lower().endswith("/robots.txt")}
    out = []
    for r in rows:
        u = urllib.parse.urlparse(r["original"])
        if not u.query or not re.search(r"(?i)\.(swf|dcr|unity3d|jar|xap)$", u.path):
            continue
        for k, v in urllib.parse.parse_qsl(u.query):
            v = v.strip()
            if re.fullmatch(r"(?i)(https?://)?([a-z0-9-]+\.)+[a-z]{2,6}(/\S*)?", v):
                pu = urllib.parse.urlparse(v if "//" in v else "http://" + v)
                h, folder = pu.hostname or "", pu.path if pu.path.endswith("/") else pu.path.rsplit("/", 1)[0] + "/"
                if h and h not in hosts:
                    lead = f"{k}={v} (host {h})"
                elif folder != "/" and not any(u.startswith(re.sub(r"^www\.", "", h) + folder.lower()) for u in urls):
                    lead = f"{k}={v} (folder {folder})"
                else:
                    continue
                if lead not in out:
                    out.append(lead)
            elif re.fullmatch(r"/[\w./-]*", v) and v != "/":
                folder = v if v.endswith("/") else v.rsplit("/", 1)[0] + "/"
                lead = f"{k}={v} (path {folder})"
                if not any(p.startswith(folder.lower()) for p in paths) and lead not in out:
                    out.append(lead)
    return out


def cmd_cdx(a):
    rows = cdx_query(a.url, match=a.match, fl=a.fl, filters=a.filter or [],
                     collapse=a.collapse or [], frm=a.frm, to=a.to,
                     limit=a.limit, follow=a.all, max_pages=a.pages)
    emit(rows, a.format)


def closest_capture(url: str, ts: str | None = None) -> dict | None:
    """Nearest healthy capture via CDX. The archive.org/wayback/available API often returns 429."""
    params = [("url", url), ("output", "json"), ("fl", "timestamp,original,statuscode"),
              ("filter", "statuscode:200"), ("limit", "1")]
    if ts:
        params += [("closest", ts), ("sort", "closest")]
    else:
        params[-1] = ("limit", "-1")  # latest capture
    rows, _ = parse_cdx_json(fetch_json(CDX + "?" + urllib.parse.urlencode(params)))
    if len(rows) < 2 and not url.endswith("?"):
        # CDX keys "page?" and "host:80/page" apart from "page"
        for alt in (url + "?", re.sub(r"^(https?://[^/]+)", r"\1:80", url)):
            alt_params = [(k, alt if k == "url" else v) for k, v in params]
            rows, _ = parse_cdx_json(fetch_json(CDX + "?" + urllib.parse.urlencode(alt_params)))
            if len(rows) >= 2:
                break
    if len(rows) < 2:
        return None
    r = dict(zip(rows[0], rows[1]))
    return {"available": True, "timestamp": r["timestamp"], "date": fmt_ts(r["timestamp"]),
            "url": replay_url(r["timestamp"], r["original"], raw=False), "status": r["statuscode"]}


def cmd_available(a):
    emit([closest_capture(a.url, a.ts) or {"url": a.url, "available": False}], a.format)


EXT_ALIASES = {".jpeg": ".jpg", ".jpe": ".jpg", ".midi": ".mid", ".kar": ".mid", ".htm": ".html", ".svgz": ".svg"}
MAGIC_EXT = [(b"FWS", ".swf"), (b"CWS", ".swf"), (b"ZWS", ".swf"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"),
             (b"\xff\xd8\xff", ".jpg"), (b"PK\x03\x04", ".zip"), (b"PK\x05\x06", ".zip"), (b"MThd", ".mid"),
             (b"ID3", ".mp3"), (b"%PDF", ".pdf"), (b"FLV\x01", ".flv"), (b"OggS", ".ogg"), (b"BM", ".bmp"),
             (b"Rar!", ".rar"), (b"7z\xbc\xaf", ".7z"), (b"\x1f\x8b", ".gz"), (b"XFIR", ".dir"), (b"RIFX", ".dir")]
SAME_MAGIC = {".zip": (".jar", ".docx", ".xlsx", ".pptx", ".odt", ".apk", ".xpi", ".epub", ".air", ".xap", ".ipa", ".swc"),
              ".dir": (".dcr", ".dxr", ".cst", ".cct", ".cxt"), ".gz": (".tgz", ".svgz"), ".ogg": (".oga", ".ogv")}
HTML_TAGS = re.compile(rb"<(!doctype\s+html|html|head|body|title|frameset|meta|table|center|font|p|br|img|a|div|"
                       rb"script|h[1-6])[\s>/]", re.I)


def _bom_text(body: bytes) -> bytes | None:
    """BOM-prefixed text re-encoded as UTF-8, else None. Check before magic: UTF-16 FF FE looks like MPEG."""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if body.startswith(bom):
            return body[len(bom):len(bom) + 16000].decode(enc, "replace").encode("utf-8")
    return None


def sniff_ext(body: bytes, url_path: str = "") -> str | None:
    """Extension from the bytes, or None. Names lie (splash.swf served as HTML, getimage.php serving a GIF)."""
    suffix = Path(url_path).suffix.lower()
    text = _bom_text(body)
    if text is not None:  # BOM means text, never a binary format
        body = text
    else:
        for m, e in MAGIC_EXT:
            if body.startswith(m):
                if e == ".bmp" and (len(body) < 26 or body[14] not in (12, 40, 56, 64, 108, 124)):
                    continue  # "BM" is also how plain text can start
                return suffix if suffix in SAME_MAGIC.get(e, ()) else e  # .jar is a zip, .dcr is Director
        if body[:4] == b"RIFF" and body[8:12] in (b"WAVE", b"AVI "):
            return ".wav" if body[8:12] == b"WAVE" else ".avi"
        if len(body) > 2 and body[0] == 0xFF and body[1] & 0xE0 == 0xE0 and body[1] & 0x06 and body[1] & 0x18 != 0x08:
            return ".mp3"  # MPEG frame sync, layer/version not reserved (JPEG FF D8, AAC FF F1 fail)
    head = body[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<?xml", b"<rss", b"<feed", b"<svg")) or (head.startswith(b"<") and suffix == ".xml"):
        if HTML_TAGS.search(body[:2000]) and not head.startswith((b"<rss", b"<feed", b"<svg")) and \
                re.search(rb"(?i)<html|<!doctype\s+html", body[:2000]):
            return ".html"  # XHTML
        return ".svg" if b"<svg" in head[:400] else ".xml"
    if HTML_TAGS.search(body[:4000]) and head.startswith((b"<", b"<!--")):
        return ".html"
    return None


def pick_ext(body: bytes, url_path: str) -> tuple[str, bool]:
    """(extension, is_real_html). Bytes first, then the URL's extension, then a .js/.css/.json/.txt/.bin guess."""
    suffix = Path(url_path).suffix
    sniffed = sniff_ext(body, url_path)
    if sniffed:
        keep = suffix if suffix and EXT_ALIASES.get(suffix.lower(), suffix.lower()) == sniffed else sniffed
        return keep, sniffed == ".html"
    body = _bom_text(body) or body  # UTF-16 text counts as text, not .bin
    head = body[:200].lstrip().lower()
    if suffix and len(suffix) <= 6:
        return suffix, False
    if head[:1] in (b"{", b"["):
        return ".json", False
    if re.search(rb"function\s*\(|=>|var |const |\}\);", body[:4000]):
        return ".js", False  # script without a .js name (Closure/j2cl modules)
    if re.search(rb"[.#][\w-]+\s*\{[^}]*:", body[:4000]):
        return ".css", False
    if head.startswith(b"<"):
        return ".html", True  # tag soup with nothing better to call it
    text = body[:4000]
    return (".txt" if text and sum(c < 9 or 13 < c < 32 for c in text) < len(text) / 100 else ".bin"), False


def _same_ext(a_: str, b_: str) -> bool:
    a_, b_ = a_.lower(), b_.lower()
    return EXT_ALIASES.get(a_, a_) == EXT_ALIASES.get(b_, b_)


def default_capture(url: str) -> tuple[str | None, str]:
    """(timestamp, why) when `fetch` gets no ts. Program/media files: the EARLIEST healthy capture (later
    ones are often re-releases or parked pages). Anything else: the latest."""
    if _file_class(url, "") in ("program", "media"):
        try:
            rows = list(cdx_query(url, fl="timestamp,original,digest", filters=["statuscode:200", "!mimetype:text/html"],
                                  limit=2000))
        except RuntimeError as e:
            log(f"cdx {url}: {e}")
            rows = []
        if rows:
            first, last = rows[0]["timestamp"], rows[-1]["timestamp"]
            vers = len({r.get("digest") for r in rows})
            return first, (f"the EARLIEST of {len(rows)} healthy captures, {fmt_ts(first)}..{fmt_ts(last)}, "
                           f"{vers} distinct version(s)")
    c = closest_capture(url)
    return (c["timestamp"], "the LATEST healthy capture") if c else (None, "")


def cmd_fetch(a):
    a.url = norm_url(a.url)
    ts, why = a.ts, "the ts given"
    if not ts:
        ts, why = default_capture(a.url)
        if not ts:
            log("no snapshot available")
            sys.exit(2)
    url = replay_url(ts, a.url, raw=not a.toolbar)
    try:
        refuse_binary(a.url, original=a.original)
        fetch.last_status = 200
        body, final = fetch(url, max_bytes=a.max_mb * 1024 * 1024, want_url=True, keep_errors=True)
        status = getattr(fetch, "last_status", 200)
        truncated = last_truncated()
        refuse_binary(a.url, body, original=a.original)
        if a.original and status != 200:
            log(f"refusing to save an archived HTTP {status} page as an original: find a 200 capture "
                "(`cdx <url> --filter statuscode:200`), often with the original query string")
            sys.exit(2)
    except FileNotFoundError:
        log(f"not archived at {ts}: {a.url}")
        sys.exit(2)
    except PermissionError as e:
        log(str(e))
        sys.exit(4)
    m = re.search(r"/web/(\d{14})", final or "")
    if m and m.group(1) != ts:
        gap = abs(int(m.group(1)[:4]) - int(ts[:4]))
        log(f"{'WARNING' if gap >= 1 else 'note'}: Wayback served the nearest capture {m.group(1)} instead of {ts}"
            + (f" ({gap} year(s) away: probably not the version you want; pick a timestamp from `cdx`)" if gap >= 1 else ""))
        ts = m.group(1)
    out = Path(a.out if a.out != "dossier/raw" else "dossier/originals" if a.original else
               "dossier/private" if a.private else a.out)
    want_file = out.suffix and len(out.suffix) <= 6 and not out.is_dir()  # --out x.pdf means that file
    (out.parent if want_file else out).mkdir(parents=True, exist_ok=True)
    p = urllib.parse.urlparse(a.url)
    name = slugify(p.netloc + p.path) or "index"
    if len(p.netloc + p.path) > 55 or p.query:  # slugify cuts at 60 chars, hash keeps names unique
        name = name + "_" + hashlib.sha1((p.path + "?" + p.query).encode()).hexdigest()[:8]
    suffix = Path(p.path).suffix
    ext, is_html = pick_ext(body, p.path)  # bytes win over names
    if is_html and suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".swf", ".dcr", ".mp3", ".mid", ".wav",
                                      ".pdf", ".zip", ".jar", ".flv"):
        log(f"WARNING: asked for a {suffix} file but this capture is an HTML page (often a parked or squatter "
            "domain answering every path, or an error page). Pick a capture from before the death: "
            f"`wayback.py cdx {a.url} --to <death year>`")
    if status != 200 and (is_html or body[:200].lstrip().startswith(b"<")):
        ext = ".html"  # archived error page, whatever the URL's extension
    elif suffix and not is_html and not _same_ext(ext, suffix):
        log(f"note: content is {ext[1:].upper()}, not {suffix}: saved as {ext}")
    dest = out if want_file else out / f"{ts}_{name}{ext}"
    dest.write_bytes(body)
    if status != 200:
        log(f"note: the archived response itself was HTTP {status}; saved as evidence")
    log(f"capture used: {fmt_ts(ts)} (ts {ts}; {why}). Pass a ts for another era.")
    meta = {"source_url": a.url, "timestamp": ts, "date": fmt_ts(ts), "replay": url, "archived_status": status,
            "view": replay_url(ts, a.url, raw=False), "path": str(dest),
            "bytes": len(body), "sha1": hashlib.sha1(body).hexdigest(), "sha256": hashlib.sha256(body).hexdigest()}
    if truncated:
        meta["truncated"] = "the response ended early: this file is incomplete (the archived copy may itself be cut short)"
    atomic_write(Path(str(dest) + ".meta.json"), json.dumps(meta, indent=2))
    emit([meta], a.format)


def cmd_api(a):
    """Archived JSON/XML responses under a host, the nearest thing to a recorded backend."""
    target = a.domain.rstrip("/") + (a.prefix or "")
    log("tip: on a busy host pass --prefix /path/ (and --limit); full-host scans can take minutes")
    rows = cdx_query(target, match="prefix" if a.prefix else "host",
                     fl="timestamp,original,mimetype,length",
                     filters=["mimetype:.*(json|xml|javascript|protobuf|rss).*"] +
                             ([] if a.errors else ["statuscode:200"]),
                     collapse=["urlkey"], limit=a.limit, follow=a.pages > 1, max_pages=a.pages)
    out = []
    for r in rows:
        out.append({"date": fmt_ts(r["timestamp"]), "ts": r["timestamp"], "url": r["original"],
                    "mime": r.get("mimetype"), "bytes": r.get("length"),
                    "raw": replay_url(r["timestamp"], r["original"])})
    emit(out, a.format)


# ---------------------------------------------------------------- mirror (restore a site from the archive)

ASSET_ATTRS = re.compile(
    r"""(?is)(?:\b(?:src|href|background|data|value|poster|action)\s*=\s*)(["']?)([^"'\s>]+)\1""")
CSS_URLS = re.compile(r"""(?is)url\(\s*(["']?)([^"')]+)\1\s*\)|@import\s+(["'])([^"']+)\3""")
PAGE_EXT = (".html", ".htm", ".shtml", ".php", ".asp", ".aspx", ".jsp", ".cfm", "")
SKIP_SCHEMES = ("mailto:", "javascript:", "data:", "#", "about:", "tel:")


def _local_path(url: str) -> str:
    """Archive URL -> stable local path (host/path, query hashed into the name, pages as .html)."""
    p = urllib.parse.urlparse(url)
    path = p.path or "/"
    if path.endswith("/"):
        path += "index.html"
    stem, dot, ext = path.rpartition(".")
    if not dot or "/" in ext:
        stem, ext = path, ""
    if p.query:
        stem += "__" + hashlib.sha1(p.query.encode()).hexdigest()[:8]
    if not ext or ext.lower() in ("php", "asp", "aspx", "jsp", "cfm", "shtml", "cgi", "pl"):
        ext = (ext + ".html") if ext else "html"
    host = (p.hostname or "site").lower().removeprefix("www.")
    return f"{host}{stem}.{ext}".replace("//", "/")


def _rel(from_local: str, to_local: str) -> str:
    import posixpath
    return posixpath.relpath(to_local, posixpath.dirname(from_local) or ".")


# what each linked extension should be. Anything else is a parking
# page, an error page or a later owner's file
EXPECT_KIND = {".gif": "image", ".jpg": "image", ".jpeg": "image", ".jpe": "image", ".png": "image", ".bmp": "image",
               ".ico": "image", ".webp": "image", ".mid": "midi", ".midi": "midi", ".kar": "midi", ".rmi": "midi",
               ".swf": "flash", ".dcr": "director", ".dir": "director", ".wav": "audio", ".mp3": "audio",
               ".ogg": "audio", ".au": "audio", ".aif": "audio", ".aiff": "audio", ".ra": "audio", ".ram": "audio",
               ".mod": "audio", ".avi": "video", ".mpg": "video", ".mpeg": "video", ".mov": "video", ".flv": "video",
               ".jar": "zip", ".class": "java", ".zip": "zip", ".pdf": "pdf"}
SNIFF_KIND = {".gif": "image", ".png": "image", ".jpg": "image", ".bmp": "image", ".mid": "midi", ".swf": "flash",
              ".dir": "director", ".dcr": "director", ".mp3": "audio", ".wav": "audio", ".ogg": "audio",
              ".avi": "video", ".flv": "video", ".zip": "zip", ".jar": "zip", ".pdf": "pdf"}


def asset_mismatch(url: str, body: bytes) -> str | None:
    """Why these bytes aren't the linked file, or None. Trust bytes, not mimetype: HTML as logo.gif is junk."""
    path = urllib.parse.urlparse(url).path
    ext = Path(path).suffix.lower()
    want = EXPECT_KIND.get(ext)
    if not want:
        return None
    got = sniff_ext(body, path)
    head = body[:2000]
    textual = bool(head) and not any(c < 9 or 13 < c < 32 for c in head)
    if got in (".html", ".xml") or (got is None and textual):
        t = re.search(rb"(?is)<title[^>]*>(.*?)</title>", body[:20000])
        title = re.sub(r"\s+", " ", t.group(1).decode("utf-8", "replace")).strip()[:60] if t else ""
        return (f"the capture is {'an HTML page' if got == '.html' else 'text'}"
                + (f" ({title!r})" if title else "") + f", not the {ext} the page linked (a parked domain, "
                "an error or a later site answering every path)")
    if got and SNIFF_KIND.get(got) and SNIFF_KIND[got] != want:
        return f"the capture is {got[1:].upper()} data, not the {ext} ({want}) the page linked"
    return None


def _ts_months(ts) -> float:
    ts = str(ts).ljust(14, "0")
    return int(ts[:4]) * 12 + int(ts[4:6]) + int(ts[6:8] or 0) / 31


NOT_FETCHED_TIME = "not fetched: out of time"


def _kkey(u: str) -> str:
    """One key per file, ignoring scheme, www. and default port."""
    return re.sub(r"^https?://(www\.)?", "", norm_url(u))


def read_list(path: str, match: str | None = None) -> list[tuple[str, str | None]]:
    """(url, ts) rows from `inventory --save`/`find`/`cdx` JSON(L), 'URL [TS]' lines or Wayback URLs.
    Pattern summary rows are skipped."""
    text = Path(path).read_text(errors="replace", encoding="utf-8")
    try:
        data = json.loads(text)
        items = data if isinstance(data, list) else [data]
    except ValueError:
        items = []
        for line in jsonl_lines(text):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                items.append(json.loads(line))
            except ValueError:
                items.append(line)
    out, skipped = [], 0
    for it in items:
        if isinstance(it, dict):
            u, ts = it.get("url") or it.get("original") or it.get("source_url"), it.get("ts") or it.get("timestamp")
            if not u:
                skipped += 1
                continue
        else:
            m = re.match(r"https?://web\.archive\.org/web/(\d{4,14})[a-z_]*/(.+)", str(it))
            parts = str(it).split()
            u, ts = (m.group(2), m.group(1)) if m else (parts[0], parts[1] if len(parts) > 1 else None)
        if match and not re.search(match, u, re.I):
            continue
        out.append((norm_url(u if "://" in u else "http://" + u), str(ts) if ts else None))
    if skipped:
        log(f"{skipped} row(s) without a url (pattern summaries) skipped: pass the full list that `inventory` saved")
    return list(dict.fromkeys(out))


def cached_inventory(host: str) -> dict:
    """_kkey -> [ts] for this host, from inventory lists in the cache."""
    known, host = {}, host.removeprefix("www.")
    for f in (cache_dir() / "inventory").glob("*.jsonl"):
        try:
            for line in jsonl_lines(f.read_text(encoding="utf-8")):
                r = json.loads(line)
                if r.get("url") and r.get("ts") and (r.get("host") or "").removeprefix("www.") == host:
                    known.setdefault(_kkey(r["url"]), []).append(r["ts"])
        except (OSError, ValueError):
            continue
    return known


def cmd_mirror(a):
    """Restore pages from one era plus every asset they use, fetched raw near the timestamp, links rewritten.
    MANIFEST.json records each file's source, and `missing` lists what the archive never had. Mask other
    people's content (law 3). Captures with the wrong bytes (HTML for a .gif) or dated after the death
    (--died, --drift) go to missing with the reason. Rerunning into the same folder merges MANIFEST.json."""
    import time as _t
    root = Path(a.out)
    listed = read_list(a.from_inventory, getattr(a, "match", None)) if getattr(a, "from_inventory", None) else []
    if not a.url and not listed:
        sys.exit("mirror needs URL TS, or --from-inventory FILE (rows with url and ts)" +
                 (" holding rows that match --match" if getattr(a, "from_inventory", None) else ""))
    if a.url and not a.ts:
        sys.exit("mirror URL needs the timestamp of the era to restore (YYYYMMDD...)")
    root.mkdir(parents=True, exist_ok=True)
    start = norm_url(a.url if "://" in a.url else "http://" + a.url) if a.url else None
    site_host = (urllib.parse.urlparse(start or listed[0][0]).hostname or "").removeprefix("www.")
    manifest, missing, done, checked, gated = {}, [], {}, {}, []
    cut_short = set()  # bodies that ended early, saved but marked incomplete
    queue = [(start, 0)] if start else []
    sp = urllib.parse.urlparse(start or "").path or "/"
    user_root = re.match(r"^/(?:~|%7[eE])[^/]+/", sp if sp.endswith("/") else sp.rsplit("/", 1)[0] + "/")
    a.user_root = user_root.group(0) if user_root else None
    if a.whole_host and a.user_root:
        log(f"warning: {start} is a ~user folder on a shared server: the whole host is every user's pages. "
            f"--whole-host is kept to {a.user_root} (pass the server root as the URL to crawl the host)")
    t0 = _t.time()
    deadline = t0 + a.max_seconds  # nothing starts after this, live downloads aren't cut
    # known captures (saved inventory, --from-inventory, this run's CDX)
    # fetched at exact ts: instant replay, no per-file CDX lookup
    known = cached_inventory(site_host) if site_host else {}
    if any(not ts for _, ts in listed) and not a.ts:  # bare URL: default capture (earliest for program files)
        listed = [(u, ts or default_capture(u)[0]) for u, ts in listed]
        listed = [(u, ts) for u, ts in listed if ts or log(f"no capture of {u}: skipped")]
    for u, ts in listed:
        if ts:
            known.setdefault(_kkey(u), []).insert(0, ts)
    if listed:
        log(f"{len(listed)} file(s) from {a.from_inventory}"
            + (f" matching {a.match!r}" if getattr(a, "match", None) else "") + ", fetched at their listed captures")
    mpath = root / "MANIFEST.json"
    prior = {}
    if mpath.exists():  # earlier mirror into this folder: keep its entries
        try:
            prior = json.loads(mpath.read_text(encoding="utf-8"))
            prior = prior if isinstance(prior, dict) else {}
        except (OSError, ValueError):
            mpath.replace(mpath.with_name("MANIFEST.json.bak"))
            log(f"warning: {mpath} was not valid JSON: kept as MANIFEST.json.bak, starting a new one")
    prior_files = prior.get("files") if isinstance(prior.get("files"), dict) else {}
    prior_missing = [m for m in (prior.get("missing") or []) if isinstance(m, dict)]
    runs = [r for r in (prior.get("runs") or []) if isinstance(r, dict)] or \
        ([{"requested_ts": prior.get("requested_ts"), "start": prior["start"]}] if prior.get("start") else [])
    runs.append({"requested_ts": a.ts, "start": start, **({"died": a.died} if a.died else {})})
    dropped = set()  # earlier entries now found to be junk
    if prior_files:
        log(f"adding to the existing {mpath} ({len(prior_files)} files from earlier runs are kept, and not fetched again)")
    # reusable earlier files (not pages/CSS, those were rewritten on disk)
    reused = [0]
    prior_saved = {v.get("url"): (k, v) for k, v in prior_files.items()
                   if isinstance(v, dict) and v.get("url") and v.get("kind") not in ("page", "stylesheet")}

    def write_manifest():  # after every page, so a stop keeps progress
        files = {k: v for k, v in prior_files.items() if k not in dropped}
        files.update(manifest)
        now = {v.get("url") for v in manifest.values()} | {m.get("url") for m in missing}
        atomic_write(mpath, json.dumps({"requested_ts": a.ts, "start": start, "runs": runs, "files": files,
                                        "missing": [m for m in prior_missing if m.get("url") not in now] + missing},
                                       indent=1))

    def pick_ts(url, near):
        """Known capture within a year of the page (exact ts is fast), else the page's ts."""
        want, cands = near or a.ts, known.get(_kkey(url))
        if not cands:
            return want
        if not want:
            return cands[0]
        best = min(cands, key=lambda t: abs(_ts_months(t) - _ts_months(want)))
        return best if abs(_ts_months(best) - _ts_months(want)) <= 12 else want

    def grab(url, near=None):
        """Fetch one raw file nearest `near` (default a.ts). Returns (bytes, served_ts) or (None, reason).
        Past the time limit it returns "not fetched: out of time" so a rerun picks it up."""
        url = norm_url(url)
        if url in done:
            return done[url]
        old = prior_saved.get(url)
        if old and (root / old[0]).is_file():  # saved by an earlier run, don't refetch
            done[url] = ((root / old[0]).read_bytes(), old[1].get("served_ts"))
            reused[0] += 1
            return done[url]
        if _t.time() > deadline:
            done[url] = (None, NOT_FETCHED_TIME)
            return done[url]
        try:
            refuse_binary(url, original=getattr(a, "original", False))  # same gate as `fetch`, program files need --original
            last_status(reset=True)
            body, final = fetch(replay_url(pick_ts(url, near), url), max_bytes=a.max_mb * 1024 * 1024, want_url=True,
                                keep_errors=True, deadline=deadline)
            status = last_status()
            if last_truncated():
                cut_short.add(url)
            if status != 200:
                done[url] = (None, f"archived as HTTP {status}")
            else:
                refuse_binary(url, body, original=getattr(a, "original", False))
                m = re.search(r"/web/(\d{14})", final or "")
                done[url] = (body, m.group(1) if m else (near or a.ts))
        except FileNotFoundError:
            done[url] = (None, "not archived")
        except PermissionError:
            gated.append(url)
            done[url] = (None, "program/archive file: not fetched without --original (law 2)")
        except OutOfTime:
            done[url] = (None, NOT_FETCHED_TIME)
        except RuntimeError as e:
            done[url] = (None, (f"not fetched: timed out ({e})" if re.search(r"(?i)timed? ?out", str(e)) else str(e))[:160])
        b, info = done[url]
        log(f"[{int(_t.time() - t0)}s] {'ok ' + str(len(b)) + ' B' if b is not None else info[:60]}: {url[:110]}")
        return done[url]

    def too_late(served, near) -> str | None:
        """Captures after --died, or --drift years after the page, belong to someone else."""
        if not served or not str(served)[:4].isdigit():
            return None
        y = int(str(served)[:4])
        if a.died and y > int(str(a.died)[:4]):
            return f"captured {y}, after the site died ({str(a.died)[:4]})"
        if near and a.drift and y - int(str(near)[:4]) > a.drift:
            return (f"captured {y}, {y - int(str(near)[:4])} years after the page ({str(near)[:4]}): probably a "
                    "later site's file (--drift 0 accepts it)")
        return None

    lookups = [0]

    def other_capture(url, near, bad_ts):
        """Nearest healthy non-HTML capture before the death. One CDX query."""
        if lookups[0] >= a.max_lookups or _t.time() - t0 > a.max_seconds:
            return None
        lookups[0] += 1
        try:
            rows = list(cdx_query(url, fl="timestamp,original,statuscode,mimetype",
                                  filters=["statuscode:200", "!mimetype:text/html"],
                                  to=str(a.died)[:4] if a.died else None, limit=500, deadline=deadline))
        except (RuntimeError, FileNotFoundError) as e:
            log(f"cdx {url}: {e}")
            return None
        rows.sort(key=lambda r: abs(_ts_months(r["timestamp"]) - _ts_months(near or a.ts)))
        for r in [r for r in rows if r["timestamp"] != bad_ts][:3]:
            if too_late(r["timestamp"], near):
                continue
            try:
                body = fetch(replay_url(r["timestamp"], r["original"]), max_bytes=a.max_mb * 1024 * 1024,
                             deadline=deadline)
            except (RuntimeError, FileNotFoundError):
                continue
            if not asset_mismatch(url, body):
                return body, r["timestamp"]
        return None

    def get_asset(url, near):
        """Checked capture nearest the page. (bytes, ts) or (None, reason)."""
        url = norm_url(url)
        if url in checked:
            return checked[url]
        body, info = grab(url, near)
        if body is not None:
            why = asset_mismatch(url, body) or too_late(info, near)
            if why:
                alt = other_capture(url, near, info)
                if alt:
                    log(f"note: {url}: the capture at {info} is not the file ({why}); using {alt[1]} instead")
                    body, info = alt
                else:
                    body, info = None, f"rejected the capture at {info}: {why}"
                    old = _local_path(url)
                    for k in [k for k, v in prior_files.items() if v.get("url") == url or k == old]:
                        dropped.add(k)  # junk an earlier run saved as the original
                        log(f"note: {k} from an earlier run was this rejected capture: removed from the manifest")
        checked[url] = (body, info)
        return checked[url]

    def save(url, body, served, kind):
        local = _local_path(url)
        cur = Path(local).suffix
        sniffed = sniff_ext(body, urllib.parse.urlparse(url).path)
        if kind not in ("stylesheet", "script") and sniffed and not _same_ext(sniffed, cur):
            local = local[: -len(cur)] + sniffed if cur else local + sniffed  # a.php?id=3 serving a GIF -> .gif
        dest = root / local
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        manifest[local] = {"origin": "original", "url": url, "served_ts": served, "kind": kind,
                           "bytes": len(body), "sha1": hashlib.sha1(body).hexdigest(),
                           **({"truncated": True} if url in cut_short else {})}
        dropped.discard(local)
        return local

    def assets_in_css(css_text, css_url, css_local, near):
        def one(m):  # rewrite each url() once (str.replace re-matched rewritten paths)
            ref = m.group(2) or m.group(4)
            if not ref or ref.startswith(SKIP_SCHEMES):
                return m.group(0)
            absu, frag = urllib.parse.urldefrag(urllib.parse.urljoin(css_url, ref.strip()))
            body, info = get_asset(absu, near)
            if body is None:
                missing.append({"url": absu, "referenced_by": css_url, "why": info, "page_ts": near})
                return m.group(0)
            local = save(absu, body, info, "css-asset")
            return m.group(0).replace(ref, _rel(css_local, local) + (f"#{frag}" if frag else ""))
        return CSS_URLS.sub(one, css_text)

    def prefetch(pairs):
        """Warm get_asset for (url, near) pairs, --parallel at a time (still rate limited per host)."""
        todo = [(u, n) for u, n in dict.fromkeys(pairs) if norm_url(u) not in checked]
        if getattr(a, "parallel", 1) <= 1 or len(todo) < 2:
            return
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, min(4, a.parallel))) as ex:
            for f in [ex.submit(get_asset, u, n) for u, n in todo]:
                f.result()

    try:
        if start:
            _mirror_pages(a, root, start, site_host, queue, done, manifest, missing, grab, get_asset, save,
                          assets_in_css, write_manifest, t0, too_late, prefetch)
        if listed:
            prefetch(listed)
            got = 0
            for u, ts in listed:
                body, info = get_asset(u, ts)
                if body is None:
                    missing.append({"url": u, "referenced_by": "(list)", "why": info, "page_ts": ts})
                else:
                    save(u, body, info, "listed")
                    got += 1
                    if got % 25 == 0:
                        write_manifest()
            log(f"restored {got} of {len(listed)} listed file(s)")
        if a.max_captured and start:
            _mirror_captured(a, start, manifest, get_asset, save, write_manifest, known, prefetch, deadline, missing)
    except (TimeoutError, _Deadline):
        log(f"stopped at the {a.max_seconds}s limit: the manifest lists what was restored so far")
    finally:
        # archive refuses under load: one more try per missing file
        retry, still = [m for m in missing if not re.search(r"not archived|rejected|--original|out of time",
                                                            str(m.get("why")))], []
        for m in retry[:40]:
            if _t.time() > deadline:
                still += [dict(x) for x in retry[retry.index(m):40]]  # out of time, report the rest as is
                break
            done.pop(norm_url(m["url"]), None)
            checked.pop(norm_url(m["url"]), None)
            try:
                body, info = get_asset(m["url"], m.get("page_ts"))
            except TimeoutError:
                body, info = None, m["why"]
            if body is not None:
                save(m["url"], body, info, "asset")
            else:
                still.append(dict(m, why=info))
        missing[:] = [m for m in missing if m not in retry[:40]] + still
        write_manifest()
    pages = sum(1 for v in manifest.values() if v.get("kind") == "page")
    far = [k for k, v in manifest.items() if a.ts and v.get("served_ts") and str(v["served_ts"])[:4].isdigit()
           and abs(int(str(v["served_ts"])[:4]) - int(a.ts[:4])) >= 2]
    rejected = sum(1 for m in missing if str(m.get("why", "")).startswith("rejected"))
    if gated:
        log(f"{len(set(gated))} program/archive file(s) not fetched (swf, exe, zip...): rerun with --original to keep "
            "them as original material (parsed, never run natively)")
    if reused[0]:
        log(f"{reused[0]} file(s) already saved by an earlier run were reused, not fetched again")
    late = {m["url"] for m in missing if str(m.get("why")).startswith(NOT_FETCHED_TIME)}
    stalled = {m["url"] for m in missing if str(m.get("why")).startswith("not fetched: timed out")}
    resume = None
    if late or stalled:
        from _common import LAUNCH_DIR, python_cmd, shell_join
        args = [str((LAUNCH_DIR / x).resolve()) if not x.startswith("-") and (LAUNCH_DIR / x).is_file() else x
                for x in sys.argv[1:]]  # make launch-relative file args absolute
        if "mirror" not in args:  # called from another script: rebuild the essentials
            args = ["mirror", *[x for x in (a.url, a.ts) if x], "--out", str(root.resolve()),
                    *(["--from-inventory", str(Path(a.from_inventory).resolve())] if listed else []),
                    *(["--match", a.match] if getattr(a, "match", None) else []),
                    *(["--original"] if getattr(a, "original", False) else []), "--max-seconds", str(a.max_seconds)]
        resume = shell_join([python_cmd(), str(Path(__file__).resolve()), *args])
        log(f"not fetched: {len(late)} out of time (--max-seconds {a.max_seconds}), {len(stalled)} timed out (no bytes "
            f"for {int(TIMEOUT)}s, after retries); listed in MANIFEST missing. `resume` below reruns it: saved files "
            "are skipped (raise --max-seconds if needed)")
    if cut_short:
        log(f"{len(cut_short)} file(s) came back cut short (kept, marked truncated in MANIFEST): the archived copy may "
            "itself be incomplete; look for another capture with `cdx`")
    print(json.dumps({"pages": pages, "files": len(manifest), "missing": len(missing),
                      **({"truncated": sorted(cut_short)[:20]} if cut_short else {}),
                      **({"not_fetched": {"out_of_time": len(late), "timed_out": len(stalled)}, "resume": resume}
                         if resume else {}),
                      "rejected_captures": rejected,
                      "manifest_files_total": len({**{k: 1 for k in prior_files if k not in dropped}, **manifest}),
                      "entry": str(root / _local_path(start)) if start else None, "manifest": str(mpath),
                      "served_far_from_ts": far[:10],
                      "next": "open the entry page, compare with the archived screenshot, and reconstruct only what is "
                              "listed as missing; mask other people's content (law 3)"}, indent=1))


def _mirror_captured(a, start, manifest, get_asset, save, write_manifest, known=None, prefetch=None, deadline=None,
                     missing=None):
    """Unlinked files scripts load at run time (sprites, sounds, level data): every non-page capture under
    the start folder (or host, with --whole-host), checked like any asset."""
    p = urllib.parse.urlparse(start)
    folder = p.path.rsplit("/", 1)[0] + "/" if not a.whole_host else (getattr(a, "user_root", None) or "/")
    prefix = f"{p.hostname}{folder}"
    saved = {v.get("url") for v in manifest.values()}
    try:
        rows = list(cdx_query(prefix, match="prefix", fl="timestamp,original,statuscode,mimetype",
                              filters=["statuscode:200", "!mimetype:text/html"], collapse=["urlkey"],
                              to=str(a.died)[:4] if a.died else None, limit=5000, deadline=deadline))
    except (RuntimeError, FileNotFoundError) as e:
        log(f"captured files under {prefix}: cdx failed ({e}); rerun later to add them")
        return
    for r in rows:  # first captures known now, so no per-file lookup
        if known is not None:
            known.setdefault(_kkey(r["original"]), []).append(r["timestamp"])
    todo = [r["original"] for r in rows if norm_url(r["original"]) not in saved and "?" not in r["original"]]
    if len(todo) > a.max_captured:
        log(f"{len(todo)} captured files under {prefix} that no page links to; fetching the first {a.max_captured} "
            "(--max-captured raises it)")
    if prefetch:
        prefetch([(u, a.ts) for u in todo[: a.max_captured]])
    got = 0
    for url in todo[: a.max_captured]:
        body, info = get_asset(url, a.ts)
        if body is not None:
            save(url, body, info, "captured")
            got += 1
            if got % 25 == 0:
                write_manifest()
        elif missing is not None and str(info).startswith("not fetched"):  # listed, so a rerun picks it up
            missing.append({"url": url, "referenced_by": "(captured, unlinked)", "why": info, "page_ts": a.ts})
    log(f"added {got} captured files that no page links to (scripts load them at run time)")


def _mirror_pages(a, root, start, site_host, queue, done, manifest, missing, grab, get_asset, save, assets_in_css,
                  write_manifest, t0, too_late, prefetch=None):
    import time as _t
    sp = urllib.parse.urlparse(start).path or "/"
    start_dir = sp if sp.endswith("/") else sp.rsplit("/", 1)[0] + "/"
    pages = 0
    while queue and pages < a.max_pages:
        url, depth = queue.pop(0)
        body, info = grab(url)
        late = too_late(info, None) if body is not None else None
        if body is None or late:
            missing.append({"url": url, "referenced_by": "(page)", "why": info if body is None else
                            f"rejected the capture at {info}: {late}"})
            continue
        pages += 1
        page_local = _local_path(url)
        html_ = body.decode("utf-8", errors="replace")
        (root / page_local).parent.mkdir(parents=True, exist_ok=True)
        (root / page_local).write_bytes(body)  # save now so a stopped run keeps the page
        manifest[page_local] = {"origin": "original", "url": url, "served_ts": info, "kind": "page",
                                "bytes": len(body), "sha1": hashlib.sha1(body).hexdigest(),
                                "note": "as archived; references not yet rewritten"}
        write_manifest()

        def one(m):
            import html as _h
            ref = _h.unescape(m.group(2).strip())  # image-map and link hrefs carry &amp;
            if re.search(r"[\s'\"+\[\]{}()<>]|\\", ref) or (not re.search(r"[./]", ref)):
                return m.group(0)  # not a URL (script fragment, word, expression)
            attr = m.group(0).split("=", 1)[0].strip().lower()
            if not ref or ref.lower().startswith(SKIP_SCHEMES) or attr == "action":
                return m.group(0)  # form targets are behaviour, not files
            ext = Path(urllib.parse.urlparse(ref).path).suffix.lower()
            if attr == "value" and ext not in (".swf", ".dcr", ".flv", ".mp3", ".mid"):
                return m.group(0)  # <param value> holds Flash movies, <input value> text
            # #A..#Z are one page: fetch once, keep the fragment
            absu, frag = urllib.parse.urldefrag(norm_url(urllib.parse.urljoin(url, ref)))
            if not absu.startswith("http") or re.search(r"/(cgi-bin|_vti_bin|_vti_cnf|cgi)/", absu, re.I):
                return m.group(0)
            new = handle(m, ref, absu, f"#{frag}" if frag else "")
            return new if new is not None else m.group(0)

        def handle(m, ref, absu, frag):
            host = (urllib.parse.urlparse(absu).hostname or "").removeprefix("www.")
            path_ext = Path(urllib.parse.urlparse(absu).path).suffix.lower()
            is_page = path_ext in PAGE_EXT and m.group(0).lower().lstrip().startswith("href")
            if plan is not None:  # first pass only lists assets
                if not is_page and (a.offsite or host == site_host or host.endswith("." + site_host)
                                    or path_ext not in PAGE_EXT):
                    plan.append((absu, info))
                return None
            if is_page:
                apath = urllib.parse.urlparse(absu).path
                same_dir = apath.startswith(start_dir)
                in_scope = a.whole_host and (not a.user_root or apath.replace("%7E", "~").replace("%7e", "~")
                                             .startswith(a.user_root.replace("%7E", "~").replace("%7e", "~")))
                if host == site_host and depth < a.depth and (same_dir or in_scope):
                    target_local = _local_path(absu)
                    if absu not in [q[0] for q in queue] and absu not in done:
                        queue.append((absu, depth + 1))
                    return m.group(0).replace(ref, _rel(page_local, target_local) + frag)
                return None
            # fetch off-site badges, webring logos, counters. Only link off-site pages
            if not a.offsite and host != site_host and not host.endswith("." + site_host) and path_ext in PAGE_EXT:
                return None
            abody, ainfo = get_asset(absu, info)  # the capture nearest this page, checked
            if abody is None:
                missing.append({"url": absu, "referenced_by": url, "why": ainfo, "page_ts": info})
                return None
            kind = "stylesheet" if path_ext == ".css" else "script" if path_ext == ".js" else \
                "flash" if path_ext in (".swf", ".dcr") else "asset"
            local = save(absu, abody, ainfo, kind)
            if kind == "stylesheet":
                css = assets_in_css(abody.decode("utf-8", errors="replace"), absu, local, ainfo)
                (root / local).write_text(css, encoding="utf-8")
            return m.group(0).replace(ref, _rel(page_local, local) + frag)

        # inline scripts build URLs from strings ("src='+x+'"): leave them alone
        chunks = re.split(r"(?is)(<script\b[^>]*>.*?</script>)", html_)
        plan = [] if prefetch else None
        if plan is not None:  # first pass: list assets, prefetch a few at a time
            for c in chunks:
                if c.lower().startswith("<script"):
                    re.sub(r"(?is)^(<script\b[^>]*>)", lambda mm: ASSET_ATTRS.sub(one, mm.group(1)), c)
                else:
                    ASSET_ATTRS.sub(one, c)
            todo, plan = plan, None
            prefetch(todo)
        rewritten = "".join(
            (re.sub(r"(?is)^(<script\b[^>]*>)", lambda mm: ASSET_ATTRS.sub(one, mm.group(1)), c) if
             c.lower().startswith("<script") else ASSET_ATTRS.sub(one, c)) for c in chunks)
        rewritten = assets_in_css(rewritten, url, page_local, info)  # inline styles and <style> blocks
        (root / page_local).parent.mkdir(parents=True, exist_ok=True)
        (root / page_local).write_text(rewritten, encoding="utf-8")
        manifest[page_local] = {"origin": "original", "url": url, "served_ts": info, "kind": "page",
                                "bytes": len(body), "sha1": hashlib.sha1(body).hexdigest(),
                                "note": "references rewritten to local paths"}
        write_manifest()
        log(f"page {pages}: {url} ({len(manifest)} files so far, {int(_t.time() - t0)}s)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--format", choices=["jsonl", "json", "md", "csv"], default="jsonl")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("known", help="every page a small site is known to have had: captured, error, or only linked "
                                     "(www./bare, ~/%%7E and index.html spellings merged into one page)",
                       description="Every page a small site is known to have had. www./bare, ~/%7E and trailing "
                                   "index.html spellings are merged into one page (addresses lists them). eras: one per "
                                   "distinct content, first..last:bytes in --format md; bytes = compressed WARC record "
                                   "size, not the file's size (`fetch` reports real bytes).")
    s.add_argument("url", help="the site's root, e.g. geocities.com/Area51/Nebula/1234 or www.example.com")
    s.add_argument("--from", dest="frm")
    s.add_argument("--to")
    s.add_argument("--max", type=int, default=500)
    s.add_argument("--read", type=int, default=30, help="captured pages to read for links")
    s.add_argument("--died", help="YYYY: flag captures after this (parked-domain junk, closure stubs)")
    s.add_argument("--max-seconds", type=int, default=240, help="hard limit for the whole run, back-offs included")
    s.set_defaults(fn=cmd_known)

    s = sub.add_parser("links", help="links (text, href) on several captures of index/list pages, merged by page "
                                     "with first/last listed dates: rebuild a lost index (--format csv for a sheet)")
    s.add_argument("items", nargs="+", metavar="URL TS", help="URL TS [TS...] [URL2 TS...]: each page and the "
                                                              "timestamps to read it at (none = latest healthy)")
    s.add_argument("--same-host", action="store_true", help="only links to the listing page's own host")
    s.add_argument("--match", help="only hrefs matching this regex (e.g. the lost site's host or path)")
    s.set_defaults(fn=cmd_links)

    s = sub.add_parser("changes", help="a page's archived history: versions, What's New, lines added/removed")
    s.add_argument("url", help="App Store/Play listing, changelog, release notes, help or home page")
    s.add_argument("--from", dest="frm")
    s.add_argument("--to")
    s.add_argument("--max", type=int, default=24, help="captures to read (spread across the span)")
    s.add_argument("--lines", type=int, default=8, help="changed lines shown per capture")
    s.add_argument("--pattern", help="regex for version markers (default: version numbers)")
    s.set_defaults(fn=cmd_changes)

    s = sub.add_parser("inventory", help="every archived file under a domain and its subdomains, by kind "
                                         "(programs, media, documents): where surviving originals hide")
    s.add_argument("domain", help="e.g. pbskidsplay.org (subdomains included)")
    s.add_argument("--from", dest="frm")
    s.add_argument("--to", help="YYYY: up to the death (hides later squatters)")
    s.add_argument("--limit", type=int, default=5000, help="rows per CDX page")
    s.add_argument("--pages", type=int, default=10)
    s.add_argument("--max-seconds", type=int, default=180)
    s.add_argument("--max-files", type=int, default=400, help="files listed (programs first)")
    s.add_argument("--data", action="store_true", help="also list data files (xml, json, js, css, txt)")
    s.add_argument("--images", action="store_true", help="also list images (automatic when there are 150 or fewer)")
    s.add_argument("--list-pages", action="store_true", help="also list HTML pages (automatic when there are 150 or fewer)")
    s.add_argument("--assets-only", action="store_true", help="skip HTML pages in the CDX query (big domains)")
    s.add_argument("--all-eras", action="store_true",
                   help="also list files first captured after the domain changed hands (parking, resale), "
                        "marked later_site; by default they are left out")
    s.add_argument("--no-eras", action="store_true", help="skip the owner-change check (reads up to 12 root captures)")
    s.add_argument("--save", help="write the full file list (every file, with ts and digest) here as JSONL; by default "
                                  "it goes to the cache when series are summarised or the listing is cut")
    s.set_defaults(fn=cmd_inventory)

    s = sub.add_parser("timeline", help="lifespan + suggested snapshots for one URL")
    s.add_argument("url")
    s.add_argument("--from", dest="frm", help="YYYY: ignore captures before (e.g. a previous owner of the domain)")
    s.add_argument("--died", help="YYYY[MM]: known death date (big hosts): same as --to, and named in the output")
    s.add_argument("--to", help="YYYY: ignore captures after")
    s.add_argument("--samples", type=int, default=8, help="how many design-era snapshots to suggest")
    s.add_argument("--no-notice", action="store_true", help="read no captures (no shutdown-notice or page-change check): CDX only")
    s.set_defaults(fn=cmd_timeline)

    s = sub.add_parser("urls", help="unique archived URLs for a domain")
    s.add_argument("domain")
    s.add_argument("--prefix", help="path prefix, e.g. /help/")
    s.add_argument("--match", default="host", choices=["host", "domain", "prefix"])
    s.add_argument("--mime", help="mimetype regex, e.g. text/html")
    s.add_argument("--group", action="store_true", help="summarise by top-level path section")
    s.add_argument("--from", dest="frm", help="YYYY[MM[DD]]")
    s.add_argument("--to", help="YYYY[MM[DD]]: stop at the death date so domain squatters do not swamp the map")
    s.add_argument("--limit", type=int, default=5000)
    s.add_argument("--pages", type=int, default=10)
    s.set_defaults(fn=cmd_urls)

    s = sub.add_parser("find", help="archived URLs under a domain containing a keyword (e.g. a game's .swf)")
    s.add_argument("domain", help="e.g. foxkids.com (subdomains included), or a folder such as "
                                  "home.att.net/~natehall (a prefix query on that path: fast on huge hosts)")
    s.add_argument("keyword", help="regex fragment matched anywhere in the URL, e.g. cannon or '\\.swf'")
    s.add_argument("--mime", help="e.g. flash, javascript, json")
    s.add_argument("--case", action="store_true", help="case-sensitive keyword")
    s.add_argument("--prefix", help="only under this path, e.g. /play/v2/games/ (needed on huge domains)")
    s.add_argument("--max-seconds", type=int, default=90,
                   help="give up and report after this long (big domains: list hosts with `urls --group`, then --prefix)")
    s.add_argument("--from", dest="frm", help="YYYY[MM[DD]]: only captures from then (skip squatters)")
    s.add_argument("--to", help="YYYY[MM[DD]]: only captures until then (e.g. the death date)")
    s.add_argument("--any-status", action="store_true", help="include redirects and errors")
    s.add_argument("--limit", type=int, default=500)
    s.add_argument("--pages", type=int, default=5)
    s.set_defaults(fn=cmd_find)

    s = sub.add_parser("mirror", help="restore archived pages with all their assets, links rewritten (exact websites); "
                                      "or fetch a saved inventory list straight from its captures (--from-inventory)")
    s.add_argument("url", nargs="?", help="the page to start from (optional with --from-inventory)")
    s.add_argument("ts", nargs="?", help="timestamp of the era to restore (YYYYMMDD...)")
    s.add_argument("--from-inventory", metavar="FILE",
                   help="fetch the files listed here (the JSONL `inventory` saves, `find`/`cdx` output, or lines 'URL TS') "
                        "at their own captures: no CDX lookups. Program files still need --original")
    s.add_argument("--match", help="with --from-inventory: only rows whose URL matches this regex (e.g. 'vids/clip_')")
    s.add_argument("--parallel", type=int, default=3,
                   help="files fetched at once (1-4; each start still waits its turn at the archive's rate)")
    s.add_argument("--out", default="app/original", help="workspace folder to restore into")
    s.add_argument("--depth", type=int, default=0, help="follow same-site page links this many hops")
    s.add_argument("--max-pages", type=int, default=25)
    s.add_argument("--max-mb", type=int, default=30, help="per file")
    s.add_argument("--max-seconds", type=int, default=300,
                   help="time limit for the run, every request and retry included (what was restored by then is kept "
                        "in MANIFEST.json)")
    s.add_argument("--whole-host", action="store_true",
                   help="follow pages anywhere on the host, not only under the start folder (on a ~user URL: the "
                        "~user folder, since the host is a shared server)")
    s.add_argument("--original", action="store_true",
                   help="also fetch program/archive files (swf, dcr, exe, zip...), as `fetch --original` does; "
                        "without it they are listed as missing")
    s.add_argument("--offsite", action="store_true", help="also restore assets hosted on other domains (CDNs)")
    s.add_argument("--died", help="YYYY: the site's death; captures after it (parked domains, later owners) are "
                                  "rejected as missing, never saved as original")
    s.add_argument("--drift", type=int, default=3,
                   help="reject an asset captured more than this many years after the page using it (0 = off)")
    s.add_argument("--max-captured", type=int, default=300,
                   help="also fetch up to this many captured files under the start folder that no page links to "
                        "(assets scripts load at run time); 0 = off")
    s.add_argument("--max-lookups", type=int, default=40,
                   help="CDX lookups for a better capture when the nearest one is rejected")
    s.set_defaults(fn=cmd_mirror)

    s = sub.add_parser("alive", help="is it alive now? DNS, status, final URL, title, tombstone words, and whether "
                                     "what answers is still the archived original's site")
    s.add_argument("url")
    s.add_argument("--no-archive", action="store_true",
                   help="skip comparing the live page with the archived original (a few Wayback reads)")
    s.set_defaults(fn=cmd_alive)

    s = sub.add_parser("text", help="readable text of an archived page (use instead of WebFetch on web.archive.org)")
    s.add_argument("url")
    s.add_argument("ts", nargs="?", help="timestamp (default: latest healthy capture)")
    s.add_argument("--max", type=int, default=0, help="truncate text (default: whole page)")
    s.add_argument("--whole", action="store_true", help="the whole page, not only its marked article body")
    s.set_defaults(fn=cmd_text)

    s = sub.add_parser("cdx", help="raw CDX query (www./bare and ~/%%7E spellings match as one address; "
                                   "length = compressed WARC record size, not the file's bytes: `fetch` reports those)",
                       description="Raw CDX query. The CDX server keys URLs canonically, so www.example.com and "
                                   "example.com, and /~user and /%7Euser, are one address. The length column is the "
                                   "compressed WARC record size, not the file's size (`fetch` reports real bytes).")
    s.add_argument("url")
    s.add_argument("--match", default="exact", choices=["exact", "prefix", "host", "domain"])
    s.add_argument("--fl", default=DEFAULT_FL)
    s.add_argument("--filter", action="append", help="[!]field:regex (repeatable)")
    s.add_argument("--collapse", action="append", help="field or field:N (repeatable)")
    s.add_argument("--from", dest="frm")
    s.add_argument("--to")
    s.add_argument("--limit", type=int, default=1000)
    s.add_argument("--all", action="store_true", help="follow resumeKey pagination")
    s.add_argument("--pages", type=int, default=20)
    s.set_defaults(fn=cmd_cdx)

    s = sub.add_parser("available", help="closest snapshot")
    s.add_argument("url")
    s.add_argument("--ts", help="YYYYMMDD[hhmmss] target")
    s.set_defaults(fn=cmd_available)

    s = sub.add_parser("fetch", help="download raw archived bytes")
    s.add_argument("url")
    s.add_argument("ts", nargs="?", help="14-digit timestamp (default: the earliest healthy capture for program and "
                                         "media files, the latest for anything else; the date used is printed)")
    s.add_argument("--out", default="dossier/raw", help="relative to the active workspace")
    s.add_argument("--toolbar", action="store_true", help="keep Wayback rewriting (default raw id_)")
    s.add_argument("--max-mb", type=int, default=50)
    s.add_argument("--private", action="store_true",
                   help="save to dossier/private/ (git-ignored): pages that hold other people's names or comments")
    s.add_argument("--original", action="store_true",
                   help="dirty room only: allow a program file (swf, js bundle, exe...) into "
                        "dossier/originals/ for fact inspection; never executed, never shipped")
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("api", help="archived JSON/XML/JS responses under a host")
    s.add_argument("domain")
    s.add_argument("--prefix", help="e.g. /api/ (strongly recommended on busy hosts)")
    s.add_argument("--limit", type=int, default=300)
    s.add_argument("--pages", type=int, default=1)
    s.add_argument("--errors", action="store_true", help="include 4xx/5xx responses (how it died is evidence)")
    s.set_defaults(fn=cmd_api)

    ws_arg = pop_ws_arg() if argv is None else None  # --ws may come before or after the subcommand
    a = ap.parse_args(argv)
    enter_ws(ws_arg)  # relative paths mean workspace paths, from any folder
    try:
        a.fn(a)
    except RuntimeError as e:
        log(f"error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
