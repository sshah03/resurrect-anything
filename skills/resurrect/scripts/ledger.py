#!/usr/bin/env python3
"""Resurrection workspace and evidence ledger.

Every fact about the lost product goes in evidence.jsonl with an ID (E0001...), a tag
(seen | inferred | unknown) and a source. The Codex (spec) may only cite these IDs.

Subcommands
  init NAME [--type T] [--budget B]   Create resurrections/<slug>/ with a seeded question checklist.
  add ...                             Append one evidence item; prints its ID.
  add-batch FILE                      Merge a JSONL file (e.g. a subagent's inbox file); assigns IDs.
  edit ID --quote/--claim/--locator   Correct one item in place (history kept); `supersede` retires one.
  lead add|list|done                  Follow-up searches turned up while digging (the "second hop").
  question add|list                   The checklist of things the rebuild needs answered.
  round start|end                     Bracket a dig round; `end` reports yield and saturation.
  status                              Coverage, counts, saturation verdict, suggested next step.
  find TEXT                           Search the ledger.
  mourn                               Rank features by how much people miss them.
  packet FEATURE                      Everything the evidence says about one feature (or screen), plus the
                                      frames and files to open. Read it before building that feature.
  validate                            Schema and reference checks.
  pack                                Write pack/FACTS.md and check the pack stands alone (files README.md
                                      names exist, reference images are not blank, no contact details).
  check-spec FILE                     Every Codex line must cite [E####] or be marked [DESIGN]/[UNKNOWN].
  versions [propose|add|target]       The thing's versions over time, which evidence describes which
                                      version, and which version the rebuild targets.

Workspace: --ws PATH, else $RESURRECT_WS, else ./state.json, else the only folder in ./resurrections/.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import atomic_write, jsonl_lines, locate_ws, log_activity, slugify  # noqa: E402

TAGS = ("seen", "inferred", "unknown")
KINDS = ("identity", "feature", "ui", "flow", "content", "data", "behavior", "copy",
         "timeline", "sentiment", "legal", "asset", "tech", "business", "other",
         "verify")  # verify = a check on the rebuild (footdiff, visual), not the original
CONF = ("high", "medium", "low")
SOFTWARE = ("website", "webapp", "app", "game")
TYPES = SOFTWARE + ("recipe", "ruleset", "api", "place", "media", "other")
EXHIBIT_ONLY = ("media",)  # rebuilding would just copy the work, so exhibit it instead
BUDGETS = {"quick": 2, "standard": 6, "deep": 10}  # quick = two rounds (SKILL.md)
MIN_ROUNDS = 2  # the good evidence is usually in round 1's leads
FAMILIES = ("archive", "community", "media", "docs", "code", "web", "user")

Q_COMMON = [
    ("identity", "must", "What was its core job in one sentence, and what made it feel different?"),
    ("timeline", "must", "When did it launch, when did it disappear, and why?"),
    ("sentiment", "must", "Which features or moments do people miss most (the soul to rebuild first)?"),
    ("ui", "must", "What did it look, sound or feel like (colours, shapes, layout, type, packaging), and were there distinct eras or versions?"),
    ("legal", "must", "Who owns the name/assets now; was anything released under an open licence?"),
]
Q_SOFTWARE = [
    ("ui", "must", "What were the main screens/pages and how did you move between them?"),
    ("flow", "must", "What were the core loops/flows, step by step?"),
    ("copy", "should", "What exact labels, slogans, button text and microcopy did it use?"),
    ("behavior", "should", "What small behaviours, shortcuts, sounds or easter eggs did fans remember?"),
]
Q_TYPE = {
    "recipe": [
        ("data", "must", "What did the label or ingredient statement list, in order, and what did the nutrition panel say?"),
        ("behavior", "must", "How did it taste, smell, look and feel (colour, texture, size), in people's own words?"),
        ("flow", "should", "How was it made or served (process, cooking, assembly, temperature)?"),
        ("content", "should", "Which sizes, flavours or variants existed, and which one is being rebuilt?"),
    ],
    "ruleset": [
        ("behavior", "must", "What were setup, turn structure, scoring and the end condition?"),
        ("content", "must", "What components existed (cards, board, tokens) and how many of each?"),
        ("behavior", "should", "Which edge cases, official FAQs/errata or popular house rules existed?"),
    ],
    "api": [
        ("data", "must", "Which endpoints, parameters and response shapes existed (with real archived examples)?"),
        ("behavior", "must", "What errors, limits, auth and status codes did clients rely on?"),
        ("tech", "should", "Which clients or libraries called it, and what did they expect?"),
    ],
    "place": [
        ("ui", "must", "What was the layout or route through it, scene by scene, with timings?"),
        ("asset", "should", "What did it sound like (music, narration, effects)?"),
    ],
    "media": [
        ("content", "must", "What is known about its content (description, runtime, credits, release dates)?"),
        ("other", "must", "Which fragments survive publicly, where, and what is still missing?"),
    ],
    "website": [
        ("ui", "must", "What page templates existed (home, listing, article, profile...) and their layout grids?"),
        ("content", "must", "Which content was the site's own (keep the structure) vs. user-generated or personal (replace with placeholders)?"),
        ("tech", "should", "Which interactive widgets existed (menus, rollovers, forms, embeds) and how did they behave?"),
    ],
    "webapp": [
        ("data", "must", "What objects did users create and manage, and how were they related?"),
        ("flow", "must", "What happened on signup/first-run, and what did an empty state look like?"),
        ("business", "should", "What were the plans, limits or pricing?"),
    ],
    "app": [
        ("ui", "must", "What platform conventions did it follow (desktop menus/toolbars, mobile tabs/gestures)?"),
        ("data", "must", "What did users create, save or open (documents, files, settings)?"),
        ("behavior", "should", "What preferences/settings existed and what did they change?"),
    ],
    "game": [
        ("behavior", "must", "What were the rules, controls and win/lose conditions?"),
        ("behavior", "must", "What numbers drove it (speeds, scores, timers, difficulty curve)?"),
        ("content", "must", "What levels, stages, characters or items existed, in what order?"),
        ("asset", "should", "What did it sound like (music, effects) and how were sprites animated?"),
    ],
    "other": [],
}


BATCH_HELP = """One JSON object per line. Evidence:
  {"tag": "seen|inferred|unknown", "kind": "<identity|feature|ui|flow|content|data|behavior|copy|
   timeline|sentiment|legal|asset|tech|business|other>", "claim": "one specific, testable fact",
   "source": "URL or file path", "quote": "verbatim <=600 chars", "locator": "snapshot date +
   region | video id @ mm:ss | page no.", "captured": "YYYY-MM-DD of the source",
   "as_of": "YYYY[-MM[-DD]] or YYYY..YYYY: when the THING looked/worked like this (a 2014 video of the
   2009 version is as_of 2009; a review is as_of its date)", "version": "label, e.g. 2.4 or classic",
   "authority": "artifact|recording|firsthand|secondhand (default: from family and source)",
   for unknown: "searched": "where and how (sites, queries, dates)", "would_settle": "what would answer it",
   "questions": ["Q3"], "feature": "tag (required for seen/inferred sentiment)", "confidence": "high|medium|low",
   "derived_from": ["E0012", "@k1"], "reasoning": "for inferred", "family": "archive|community|
   media|docs|code|web|user", "key": "k1"}
`key` names a line; other lines can cite it as "@k1" in derived_from, or as "source": "@k1" to
reuse that line's source (its URL or path is filled in). Keys are kept in the workspace, so a later file
(e.g. the fixed *.rejected.jsonl) can cite an earlier file's keys; a key two files bound to different
items must be cited by its ID. Re-adding a line already merged is skipped and its key keeps its ID.
`feature` must not be one of the kind words (identity feature ui flow content data behavior copy timeline
sentiment legal asset tech business other verify): name the feature itself (map, title-screen).
seen needs source + (quote or locator); inferred needs derived_from or reasoning.
To correct a merged line's quote, claim or locator: `ledger.py edit E0012 --quote '...' --why '...'`.
Leads in the same file: {"type": "lead", "what": "query or URL", "why": "...", "family": "web",
  "from": "@k1"}"""


# ------------------------------------------------------------------ optional tools

def _chromium_installed() -> bool:
    """Is Playwright's Chromium installed? Checked without importing playwright (slow)."""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    home = Path.home()
    roots = [Path(env)] if env and env != "0" else [home / "Library/Caches/ms-playwright", home / ".cache/ms-playwright",
                                                      Path(os.environ.get("LOCALAPPDATA", home)) / "ms-playwright"]
    return any(any(r.glob("chromium*")) for r in roots if r.is_dir())


def _env_python():
    """The resurrect env's Python, for one-off code. Scripts switch to it, `python3 -c` doesn't."""
    try:
        from _common import heavy_python
        return heavy_python()
    except ImportError:
        return None


def missing_tools() -> list[str]:
    """Missing optional tools, each with the commands it breaks. Imports nothing heavy."""
    import importlib.util
    import shutil
    env = None
    try:
        from _common import heavy_python
        hp = heavy_python()
        env = Path(hp).parent.parent if hp else None
    except ImportError:
        pass

    def has(m):  # or in the resurrect env the scripts switch to
        return importlib.util.find_spec(m) is not None or bool(
            env and any(env.glob(f"[lL]ib/**/site-packages/{m}")))
    setup = "run `enhance.py setup` once"
    out = []
    if not has("PIL"):
        out.append(f"Pillow ({setup}): footage.py scenes/cut/gif/sprite/hud, shots.py sheet/probe/compare, "
                   "blueprint.py, inspect_swf.py lossless bitmaps, the pack's blank-image check")
    if not has("numpy"):
        out.append(f"numpy ({setup}): blueprint.py, enhance.py native/stack/upscale")
    if not has("playwright"):
        out.append(f"playwright ({setup}): shots.py capture/play/wayback/swf, verify.py, footdiff.py")
    elif not _chromium_installed():
        out.append(f"playwright's chromium ({setup}): shots.py capture, verify.py, footdiff.py")
    if not shutil.which("yt-dlp"):
        out.append(f"yt-dlp ({setup}): community.py yt/frames/yt-comments and discuss's video comments, "
                   "footage.py get and video URLs")
    gone = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if gone:
        out.append(f"{'/'.join(gone)} (brew or apt install ffmpeg): footage.py, community.py frames/audio, "
                   "inspect_swf.py video to .mp4")
    return out


# ------------------------------------------------------------------ workspace

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def find_ws(arg: str | None) -> Path:
    ws = locate_ws(arg)
    if ws is not None:
        _guard(ws)
        return ws
    if arg or os.environ.get("RESURRECT_WS"):
        sys.exit(f"no state.json in {arg or os.environ.get('RESURRECT_WS')}")
    root = Path("resurrections")
    dirs = [d for d in root.glob("*") if (d / "state.json").exists()] if root.exists() else []
    if not dirs:
        sys.exit("no workspace found; run `ledger.py init NAME` first")
    sys.exit("several workspaces found; pick one with `ledger.py use <slug>` or pass --ws: "
             + ", ".join(str(d) for d in dirs))


def _guard(ws: Path) -> None:
    """Pass the guard a digest of the inputs (ledger files, plus pack/ minus FACTS.md for `pack`),
    so it only counts true repeats."""
    argv = sys.argv
    if Path(argv[0]).name != "ledger.py" or argv[1:] == ["versions"]:
        return log_activity(ws)  # verify.py etc. come through here too. Bare `versions` never counts
    h = hashlib.sha1()
    try:
        for name in ("evidence.jsonl", "questions.json", "leads.jsonl"):
            if (ws / name).is_file():
                h.update((ws / name).read_bytes())
        if "pack" in argv[1:] and (ws / "pack").is_dir():
            for f in sorted((ws / "pack").rglob("*")):
                if f.is_file() and f.name != "FACTS.md":
                    s = f.stat()
                    h.update(f"{f.relative_to(ws)}:{s.st_size}:{s.st_mtime_ns}".encode())
    except OSError:
        pass
    sys.argv = [*argv, f"inputs={h.hexdigest()[:10]}"]
    try:
        log_activity(ws)
    finally:
        sys.argv = argv


def load_state(ws: Path) -> dict:
    return json.loads((ws / "state.json").read_text(encoding="utf-8"))


def save_state(ws: Path, st: dict) -> None:
    atomic_write(ws / "state.json", json.dumps(st, indent=2, ensure_ascii=False))


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(jsonl_lines(path.read_text(encoding="utf-8")), 1):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"warning: {path.name}:{i} is not valid JSON, skipped", file=sys.stderr)
    return out


def append_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def next_id(rows: list[dict], prefix: str) -> int:
    n = 0
    for r in rows:
        m = re.match(prefix + r"(\d+)$", str(r.get("id", "")))
        if m:
            n = max(n, int(m.group(1)))
    return n + 1


def log_line(ws: Path, text: str) -> None:
    with (ws / "log.md").open("a", encoding="utf-8") as f:
        f.write(f"- {now()} {text}\n")


# ------------------------------------------------------------------ evidence

def normalize(item: dict) -> dict:
    item = dict(item)
    for k in ("questions", "derived_from"):
        v = item.get(k)
        if isinstance(v, str):
            item[k] = [x.strip() for x in v.split(",") if x.strip()]
        elif v is None:
            item[k] = []
    item.setdefault("confidence", "medium")
    if item.get("tag") == "unknown" and not item.get("kind"):
        item["kind"] = "other"  # "searched, found nothing" needs no kind
    return item


AS_OF = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?(\.\.\d{4}(-\d{2}(-\d{2})?)?)?")
DESCRIBES = ("feature", "ui", "flow", "behavior", "copy", "content", "data", "asset")


def when(item: dict) -> tuple[str, str] | None:
    """Time span an item describes: as_of, else its source's date."""
    v = str(item.get("as_of") or item.get("captured") or "")[:21]
    if not v or not v[:4].isdigit():
        return None
    a, _, b = v.partition("..")
    return a, (b or a)


def _pad(d: str, end: bool) -> str:
    d = d[:10]
    return d + ("-12-31"[len(d) - 4:] if end else "-01-01"[len(d) - 4:]) if len(d) < 10 else d


def version_of(item: dict, versions: list[dict]) -> str | None:
    """Version an item describes: its own `version`, else by date."""
    if item.get("version") and (not versions or str(item["version"]) in {v["label"] for v in versions}):
        return str(item["version"])  # unknown labels fall back to the date
    w = when(item)
    if not w:
        return None
    a, b = _pad(w[0], False), _pad(w[1], True)
    # by overlap, so "2014-02" matches a version from 2014-02-03
    # (an item spanning two versions gets none)
    hits = [v["label"] for v in versions
            if _pad(v["from"], False) <= b and a <= _pad(v.get("to") or "9999", True)]
    if len(hits) > 1:  # touches two versions? take the one it overlaps most
        def days(d):
            y, m, dd = (int(x) for x in d.split("-"))
            return y * 372 + m * 31 + dd
        def overlap(v):
            lo = max(days(a), days(_pad(v["from"], False)))
            hi = min(days(b), days(_pad(v.get("to") or "9999", True)))
            return hi - lo
        ranked = sorted((overlap(v), v["label"]) for v in versions if v["label"] in hits)
        if len(ranked) >= 2 and ranked[-1][0] > ranked[-2][0] * 1.5:
            return ranked[-1][1]
        return None
    return hits[0] if len(hits) == 1 else None


def problems(item: dict, known_ids: set[str], qids: set[str]) -> list[str]:
    errs = []
    tag, kind = item.get("tag"), item.get("kind")
    if tag not in TAGS:
        errs.append(f"tag must be one of {TAGS}")
    if kind not in KINDS:
        errs.append(f"kind must be one of {KINDS}")
    if not str(item.get("claim", "")).strip():
        errs.append("claim is required")
    if item.get("confidence") not in CONF:
        errs.append(f"confidence must be one of {CONF}")
    if tag == "seen" and not item.get("source"):
        errs.append("seen evidence needs a source (URL or file path)")
    if tag == "seen" and not (item.get("quote") or item.get("locator")):
        errs.append("seen evidence needs a verbatim quote or a locator (where exactly it was seen)")
    if tag == "inferred" and not (item.get("derived_from") or item.get("reasoning")):
        errs.append("inferred evidence needs derived_from IDs or reasoning")
    if kind == "sentiment" and tag in ("seen", "inferred") and not item.get("feature"):
        errs.append("sentiment evidence needs a feature tag (what is being missed)")
    if str(item.get("source", "")).startswith("@"):
        errs.append(f"source {item['source']!r} is a batch key that no line defines; give a URL or file path")
    if str(item.get("feature", "")).strip().lower() in KINDS:
        errs.append(f"feature tag {item['feature']!r} is a kind name ({', '.join(KINDS)} are reserved for `kind`); "
                    "name the feature itself (e.g. hold-to-fly, title-screen, game-name)")
    for ref in item.get("derived_from", []):
        if ref not in known_ids:
            errs.append(f"derived_from references unknown ID {ref}")
    for q in item.get("questions", []):
        if q not in qids:
            errs.append(f"unknown question {q}")
    if item.get("authority") and item["authority"] not in AUTHORITY:
        errs.append(f"authority must be one of {AUTHORITY}")
    if item.get("as_of") and not AS_OF.fullmatch(str(item["as_of"])):
        errs.append("as_of must be YYYY, YYYY-MM, YYYY-MM-DD or a range YYYY[-MM]..YYYY[-MM]")
    if len(str(item.get("quote", ""))) > 600:
        errs.append("quote longer than 600 chars; excerpt the relevant part")
    return errs


def ingest(ws: Path, items: list[dict], strict: bool = True) -> list[str]:
    st = load_state(ws)
    ev = read_jsonl(ws / "evidence.jsonl")
    qids = {q["id"] for q in json.loads((ws / "questions.json").read_text(encoding="utf-8"))}
    known = {e["id"] for e in ev}
    seen_claims = {(e.get("claim", "").strip().lower(), e.get("source")): e["id"] for e in ev if e.get("id")}
    n = next_id(ev, "E")
    added, rejected = [], []
    # key -> E-id, kept in state.json so later files (retries) can cite it.
    # This file's own keys win. A key two earlier files bound differently
    # (two subagents both used "k1") is ambiguous and needs the ID.
    prior = dict(st.get("keys") or {})
    clash = {k: v for k, v in (st.get("key_clash") or {}).items()}
    keyed = {str(i.get("key")) for i in items if i.get("key")}
    local, failed = {}, {}  # this file's keys -> IDs, and rejected lines

    def cites(i):
        return [str(r) for r in list(i.get("derived_from") or []) + [i.get("source")] if str(r).startswith("@")]

    def lookup(ref):
        k = ref[1:]
        return local.get(k) if k in keyed else (None if k in clash else prior.get(k))
    sources = {e["id"]: e.get("source") for e in ev}
    pending = list(items)
    while pending:  # wait for the lines that define the keys we cite
        ready = [i for i in pending if not any(r[1:] in keyed and r[1:] not in local and r[1:] not in failed
                                               for r in cites(normalize(i)))]
        if not ready:  # key cycle, let them fail below
            ready = pending
        took = {id(i) for i in ready}
        pending = [i for i in pending if id(i) not in took]
        for raw in ready:
            line_no = raw.pop("_line", None)
            item = normalize(raw)
            item.pop("id", None)
            bkey = item.pop("key", None)
            claim = str(item.get("claim", ""))[:60]
            used = {r: lookup(r) for r in cites(item)}
            hint = "; ".join(f"use {v} for {r}" for r, v in used.items() if v)

            def reject(why):
                rejected.append((claim, why + (f" ({hint})" if hint else ""), line_no))
                if bkey:
                    failed[str(bkey)] = (line_no, why)
            missing = [r for r, v in used.items() if not v]
            if missing:
                why = []
                for r in missing:
                    k = r[1:]
                    if k in failed:
                        why.append(f"{r} is defined on line {failed[k][0]}, which was rejected ({failed[k][1].split(' (')[0][:100]}): "
                                   "fix that line and add both again")
                    elif k in keyed:
                        why.append(f"{r} is defined by a line that itself waits on this one (a cycle): cite an ID")
                    elif k in clash:
                        why.append(f"{r} names different items in earlier files ({', '.join(clash[k])}): cite the ID")
                    else:
                        why.append(f"{r} is not defined in this file or an earlier one: cite an evidence ID")
                reject("; ".join(why))
                continue
            item["derived_from"] = [used.get(str(r)) or r for r in item.get("derived_from", [])]
            src = str(item.get("source") or "")
            if src.startswith("@") and sources.get(used.get(src)):
                # "@manual" source: copy that line's URL or path, keep the link
                item["source_from"] = used[src]
                item["source"] = sources[used[src]]
            key = (str(item.get("claim", "")).strip().lower(), item.get("source"))
            if key in seen_claims:
                if bkey:  # a retry: its keys still name items already in the ledger
                    local[str(bkey)] = seen_claims[key]
                rejected.append((claim, f"duplicate claim+source (already {seen_claims[key]}; to correct its quote "
                                        f"or locator: `ledger.py edit {seen_claims[key]} --quote ...`)", line_no))
                continue
            errs = problems(item, known, qids)
            if errs:
                reject("; ".join(errs))
                if strict and len(items) == 1:
                    sys.exit("rejected: " + "; ".join(errs))
                continue
            item["id"] = f"E{n:04d}"
            item["round"] = st.get("round", 0)
            item["added"] = now()
            n += 1
            known.add(item["id"])
            seen_claims[key] = item["id"]
            if bkey:
                local[str(bkey)] = item["id"]
            sources[item["id"]] = item.get("source")
            added.append(item)
    append_jsonl(ws / "evidence.jsonl", added)
    unbounded = [a["id"] for a in added if a.get("tag") == "unknown" and not a.get("searched")]
    if unbounded:
        print(f"note: {len(unbounded)} unknown item(s) without `searched` ({', '.join(unbounded[:6])}): a not-found is only "
              "as good as the search behind it; say where you looked (and `would_settle`: what would answer it)",
              file=sys.stderr)
    undated = [a["id"] for a in added if a.get("kind") in DESCRIBES and a.get("tag") == "seen" and not when(a)]
    if undated:
        print(f"note: {len(undated)} item(s) describe the thing without a date ({', '.join(undated[:6])}"
              f"{'...' if len(undated) > 6 else ''}): add as_of (when it looked/worked like that) or captured, or "
              "versions cannot be told apart", file=sys.stderr)
    for r in rejected:
        print(f"rejected{' (line ' + str(r[2]) + ')' if len(r) > 2 and r[2] else ''}: {r[0]!r}: {r[1]}", file=sys.stderr)
    st_keys = load_state(ws)
    for k, v in local.items():
        if prior.get(k) and prior[k] != v:
            clash[k] = sorted(set(clash.get(k, [prior[k]])) | {v})
    st_keys["keys"] = {**prior, **local}
    if clash:
        st_keys["key_clash"] = clash
    save_state(ws, st_keys)
    ingest.local = {**prior, **local}
    ingest.rejected = rejected
    return [a["id"] for a in added]


# ------------------------------------------------------------------ coverage

PRIMARY = ("archive", "code", "docs", "media")  # the thing itself, its files, docs, footage
AUTHORITY = ("inference", "secondhand", "firsthand", "recording", "artifact")  # weakest to strongest


def authority(e: dict) -> str:
    """How directly an item touches the original: artifact > recording > firsthand > secondhand > inference.
    A claim only closes on comparable authority. Passing tests never upgrade it."""
    if e.get("authority") in AUTHORITY:
        return e["authority"]
    if e.get("tag") != "seen":
        return "inference"
    fam, src = e.get("family"), str(e.get("source", ""))
    if fam in ("code", "archive") or "originals/" in src or "web.archive.org" in src:
        return "artifact"
    if fam == "docs":
        return "artifact"  # the maker's own manual, help page or label
    if fam == "media" or re.search(r"youtu|vimeo|\.mp4|frames?/|scenes?/", src):
        return "recording"
    if fam == "user" or e.get("confidence") == "high":
        return "firsthand"
    return "secondhand"
MULTI_TENANT = {"youtube.com", "youtu.be", "m.youtube.com", "news.ycombinator.com", "reddit.com",
                "old.reddit.com", "archive.org", "github.com", "twitter.com", "x.com", "medium.com",
                "boardgamegeek.com", "fandom.com", "world.openfoodfacts.org", "fdc.nal.usda.gov"}


def _source_key(src: str | None) -> str:
    """One key per independent site, video or file (two pages of one capture are one)."""
    s = str(src or "").strip()
    m = re.match(r"https?://web\.archive\.org/web/\d+\w*/(?:https?://)?([^/]+)", s)
    if m:
        return "wayback:" + m.group(1).lower().removeprefix("www.")
    m = re.match(r"https?://([^/]+)(/[^?#]*)?", s)
    if m:
        host = m.group(1).lower().removeprefix("www.")
        if host in MULTI_TENANT:  # each video, thread or item is its own source
            return s.split("#")[0]
        return host
    return s.split("#")[0].split(",")[0]

def coverage(ws: Path) -> dict:
    qs = json.loads((ws / "questions.json").read_text(encoding="utf-8"))
    ev = read_jsonl(ws / "evidence.jsonl")
    by_q = collections.defaultdict(list)
    for e in ev:
        if e.get("superseded_by"):  # e.g. an old "not found" a later find overtook
            continue
        for q in e.get("questions", []):
            by_q[q].append(e)
    out = {}
    for q in qs:
        items = by_q.get(q["id"], [])
        tags = {e["tag"] for e in items}
        seen = [e for e in items if e["tag"] == "seen"]
        sources = {_source_key(e.get("source")) for e in items  # low-confidence sources don't count as corroboration
                   if e["tag"] != "unknown" and e.get("source") and e.get("confidence") != "low"}
        if q.get("status_override"):
            status = q["status_override"]
        elif seen and "unknown" in tags:
            status = "partial"  # found in part. Close it with `question set`
        elif seen and (len(sources) >= 2 or any(e.get("confidence") == "high" and e.get("family") in PRIMARY
                                                 for e in seen)):
            status = "answered"  # corroborated, or seen first-hand in a primary source
        elif seen:
            status = "thin"  # one medium/low source: build on it, keep digging
        elif "inferred" in tags and "unknown" in tags:
            status = "bounded"  # best inference plus what couldn't be confirmed
        elif "inferred" in tags:
            status = "partial"
        elif "unknown" in tags:
            status = "confirmed-unknown"
        else:
            status = "open"
        out[q["id"]] = {"status": status, "evidence": len(items), "priority": q["priority"],
                        "text": q["text"]}
    return out


def saturation(st: dict, cov: dict) -> dict:
    rounds = [r for r in st.get("rounds", []) if r.get("ended")]
    need = st.get("saturation", {}).get("consecutive", 2)
    low = [r.get("low_yield", False) for r in rounds]
    must_open = [q for q, c in cov.items()
                 if c["priority"] == "must" and c["status"] in ("open", "partial", "thin")]
    reasons = []
    if len(rounds) >= st.get("budget_rounds", 6):
        reasons.append("round budget reached")
    elif len(rounds) >= MIN_ROUNDS:
        if len(low) >= need and all(low[-need:]):
            reasons.append(f"{need} consecutive low-yield rounds")
        if not must_open:
            reasons.append("every must-have question corroborated, bounded or confirmed unknown")
    return {"saturated": bool(reasons), "reasons": reasons, "must_open": must_open,
            "rounds_done": len(rounds), "min_rounds": MIN_ROUNDS}


# ------------------------------------------------------------------ commands

def cmd_init(a):
    slug = a.slug or slugify(a.name)
    ws = Path(a.root) / slug
    if (ws / "state.json").exists():
        sys.exit(f"workspace exists: {ws} (resume it instead)")
    for d in ("dossier/raw", "dossier/screens", "dossier/docs", "dossier/inbox", "dossier/private",
              "pack/reference", "pack/assets", "codex", "app", "museum/shots"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    common = [q for q in Q_COMMON if not (q[0] == "ui" and a.type in ("api", "media"))]
    seed = common + (Q_SOFTWARE if a.type in SOFTWARE + ("other",) else []) + Q_TYPE.get(a.type, [])
    if a.budget == "quick":  # generic should-haves are filler on small targets
        seed = [q for q in seed if q[1] == "must"]
    qs = []
    for i, (area, prio, text) in enumerate(seed, 1):
        qs.append({"id": f"Q{i}", "area": area, "priority": prio, "text": text})
    atomic_write(ws / "questions.json", json.dumps(qs, indent=2))
    (ws / "evidence.jsonl").touch()
    (ws / ".gitignore").write_text("# downloaded originals (program files, disk images): keep out of git\n"
                                   "dossier/originals/\n"
                                   "# the restored original material belongs to its owners: kept out of git by default so a\n"
                                   "# pushed repo never redistributes it (remove this line only if you have the rights)\n"
                                   "app/original/\n"
                                   "# raw captures and recordings: other people's names, faces and comments, and big files\n"
                                   "dossier/private/\n"
                                   "dossier/raw/\n"
                                   "dossier/media/\n"
                                   "# the pack holds recovered originals and frames of other people's footage\n"
                                   "pack/assets/\n"
                                   "pack/reference/\n", encoding="utf-8")
    (ws / "leads.jsonl").touch()
    st = {"name": a.name, "slug": slug, "type": a.type, "created": now(), "phase": "intake",
          "round": 0, "rounds": [], "budget": a.budget, "budget_rounds": BUDGETS[a.budget],
          "saturation": {"consecutive": 2, "low_yield_threshold": a.low_yield},
          "codename": None, "codex_approved": False, "share_safe": bool(a.share_safe),
          **({"viewport": [int(v) for v in a.viewport.lower().split("x")]} if a.viewport else {})}
    save_state(ws, st)
    (ws / "log.md").write_text(f"# Resurrection log: {a.name}\n\n", encoding="utf-8")
    log_line(ws, f"workspace created (type={a.type}, budget={a.budget})")
    (Path(a.root) / ".active").write_text(slug, encoding="utf-8")
    gone = missing_tools()
    print(json.dumps({"workspace": str(ws.resolve()), "questions": len(qs), "active": True,
                      "optional_tools": ({"python": sys.executable, "missing": gone} if gone else "all found"),
                      **({"python_for_your_own_code": hp} if (hp := _env_python()) else {}),
                      "next": "Scripts now find this workspace from the project root and treat relative "
                              "paths as relative to it. Record facts as you find them with `ledger.py add-batch "
                              "<file>` (fields: `add-batch --help`); when the pack is ready, `ledger.py pack`."},
                     indent=2))


def cmd_stop(a):
    """Record a law-4 stop (it, or the part asked for, is alive). No workspace."""
    root = Path(a.root)
    root.mkdir(parents=True, exist_ok=True)
    f = root / "STOPPED.md"
    lines = [f"## {a.name} ({now()[:10]})", "", f"- Decision: not resurrected: {a.why}", f"- Where it lives: {a.where}"]
    lines += [f"- Evidence: {e}" for e in (a.evidence or [])]
    with f.open("a", encoding="utf-8") as fh:
        fh.write(("" if f.exists() and f.stat().st_size else "# Stopped under law 4 (only what is gone)\n\n")
                 + "\n".join(lines) + "\n\n")
    print(f"recorded in {f}")


def cmd_use(a):
    root = Path(a.root)
    if not (root / a.slug / "state.json").exists():
        sys.exit(f"no workspace {root / a.slug}")
    (root / ".active").write_text(a.slug, encoding="utf-8")
    print(f"active workspace: {root / a.slug}")


def cmd_add(a):
    ws = find_ws(a.ws)
    item = {k: v for k, v in {
        "tag": a.tag, "kind": a.kind, "claim": a.claim, "source": a.source, "locator": a.locator,
        "quote": a.quote, "captured": a.captured, "questions": a.questions, "feature": a.feature,
        "confidence": a.confidence, "derived_from": a.derived_from, "reasoning": a.reasoning,
        "family": a.family, "as_of": a.as_of, "version": a.version}.items() if v}
    ids = ingest(ws, [item])
    print(ids[0])


def cmd_add_batch(a):
    ws = find_ws(a.ws)
    fp = Path(a.file)
    if not fp.exists() and (ws / a.file).exists():
        fp = ws / a.file
    if not fp.exists():
        sys.exit(f"no such file: {a.file}")
    a.file = str(fp)
    raw_lines = jsonl_lines(fp.read_text(encoding="utf-8"))
    rows, bad_json = [], []
    for no, line in enumerate(raw_lines, 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            bad_json.append((no, line, f"invalid JSON: {e.msg} at column {e.colno}"))
            continue
        if isinstance(r, dict):
            r["_line"] = no
            rows.append(r)
        else:
            bad_json.append((no, line, "not a JSON object"))
    lead_rows = [r for r in rows if r.get("type") == "lead"]
    items = [{k: v for k, v in r.items() if not k.startswith("_") or k == "_line"} for r in rows if r.get("type") != "lead"]
    ids = ingest(ws, items, strict=False)
    local = getattr(ingest, "local", {})
    leads = read_jsonl(ws / "leads.jsonl")
    st = load_state(ws)
    new_leads = []
    for r in lead_rows:
        what = str(r.get("what", "")).strip()
        if not what or any(l["what"].lower() == what.lower() for l in leads + new_leads):
            continue
        frm = str(r.get("from", ""))
        new_leads.append({"id": f"L{next_id(leads + new_leads, 'L'):04d}", "what": what,
                          "family": r.get("family", "web"), "why": r.get("why", ""),
                          "from": local.get(frm[1:], frm) if frm.startswith("@") else frm,
                          "status": "open", "round": st.get("round", 0), "added": now()})
    append_jsonl(ws / "leads.jsonl", new_leads)
    print(json.dumps({"added": len(ids), "rejected": len(items) - len(ids) + len(bad_json), "leads_added": len(new_leads),
                      "first": ids[0] if ids else None, "last": ids[-1] if ids else None,
                      "keys": {k: v for k, v in local.items() if v in ids}}))
    rej = getattr(ingest, "rejected", [])
    if rej or bad_json:  # rejected lines mustn't vanish into the archived inbox
        why_by_line = {r[2]: r[1] for r in rej if len(r) > 2 and r[2]}
        for no, _, why in bad_json:
            why_by_line[no] = why
        bad = []
        for no, line in enumerate(raw_lines, 1):  # as written, so a sed fix still matches
            if no not in why_by_line:
                continue
            body, why = line.strip(), why_by_line[no]
            if body.startswith("{") and '"_why"' not in body and not any(no == b[0] for b in bad_json):
                body = body[:1] + "\"_why\": " + json.dumps(why, ensure_ascii=False) + ", " + body[1:].lstrip()
            elif any(no == b[0] for b in bad_json):
                print(f"  line {no}: {why}", file=sys.stderr)
            bad.append(body)
        rp = Path(a.file).with_suffix(".rejected.jsonl")
        rp.write_text("".join(b + "\n" for b in bad), encoding="utf-8")
        print(f"{len(bad)} rejected lines written to {rp} as you wrote them, each with its reason in `_why` (ignored "
              "on re-add): fix them and add-batch that file", file=sys.stderr)
    if a.archive_inbox and ids:
        done = Path(a.file).with_suffix(".merged.jsonl")
        Path(a.file).rename(done)
    log_line(ws, f"merged {len(ids)}/{len(items)} items from {a.file}")


def cmd_link(a):
    """Attach existing evidence to questions. Rewrites evidence.jsonl in place."""
    ws = find_ws(a.ws)
    qids = {q["id"] for q in json.loads((ws / "questions.json").read_text(encoding="utf-8"))}
    want = [q.strip() for q in a.questions.split(",") if q.strip()]
    bad = [q for q in want if q not in qids]
    if bad:
        sys.exit(f"unknown question(s): {', '.join(bad)}")
    ids = {e.strip() for e in a.evidence.split(",")}
    ev = read_jsonl(ws / "evidence.jsonl")
    hit = 0
    for e in ev:
        if e["id"] in ids:
            cur = set(e.get("questions", []))
            e["questions"] = sorted(cur - set(want) if a.remove else cur | set(want))
            hit += 1
    if hit != len(ids):
        sys.exit(f"found {hit} of {len(ids)} evidence IDs; nothing written")
    atomic_write(ws / "evidence.jsonl", "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in ev))
    print(f"{'unlinked' if a.remove else 'linked'} {hit} items {'from' if a.remove else 'to'} {', '.join(want)}")


def cmd_supersede(a):
    """Retire an item a later find overtook. It stays for the record but stops counting."""
    ws = find_ws(a.ws)
    ev = read_jsonl(ws / "evidence.jsonl")
    ids = {x.strip() for x in a.old.split(",")}
    known = {e["id"] for e in ev}
    if a.by not in known or not ids <= known:
        sys.exit(f"unknown evidence id(s): {', '.join(sorted((ids | {a.by}) - known))}")
    for e in ev:
        if e["id"] in ids:
            e["superseded_by"] = a.by
            if a.why:
                e["superseded_why"] = a.why
    atomic_write(ws / "evidence.jsonl", "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in ev))
    print(f"{', '.join(sorted(ids))} superseded by {a.by}")


def cmd_edit(a):
    """Fix an item's quote, claim or locator in place. IDs stay valid and history is kept
    (minus the old text with --forget, for personal data)."""
    ws = find_ws(a.ws)
    ev = read_jsonl(ws / "evidence.jsonl")
    if a.id.startswith("@"):  # batch key -> its ID
        st = load_state(ws)
        k = a.id[1:]
        if k in (st.get("key_clash") or {}):
            sys.exit(f"{a.id} names several items ({', '.join(st['key_clash'][k])}): give the ID")
        if k not in (st.get("keys") or {}):
            sys.exit(f"no key {a.id} in this workspace (`ledger.py find` looks items up by text)")
        a.id = st["keys"][k]
    e = next((x for x in ev if x.get("id") == a.id), None)
    if e is None:
        sys.exit(f"no evidence {a.id}")
    new = {k: v for k, v in (("quote", a.quote), ("claim", a.claim), ("locator", a.locator)) if v is not None}
    changed = {k: v for k, v in new.items() if e.get(k) != v}
    if not changed:
        sys.exit("nothing to change: give --quote, --claim or --locator with a new value")
    fixed = {**e, **changed}
    if not fixed.get("quote"):
        fixed.pop("quote", None)
    qids = {q["id"] for q in json.loads((ws / "questions.json").read_text(encoding="utf-8"))}
    errs = problems(fixed, {x["id"] for x in ev}, qids)
    claim_key = (str(fixed.get("claim", "")).strip().lower(), fixed.get("source"))
    if "claim" in changed and any(x is not e and (str(x.get("claim", "")).strip().lower(), x.get("source")) == claim_key
                                  for x in ev):
        errs.append("another item already has this claim and source")
    if errs:
        sys.exit("not edited: " + "; ".join(errs))

    def short(v):
        v = str(v or "")
        return repr(v if len(v) <= 100 else v[:97] + "...")
    # show the claim so a wrong ID gets noticed
    print(f"{a.id} {short(fixed['claim'] if a.forget else e.get('claim'))}")
    for k in sorted(changed):
        print(f"  {k}: " + ("(old text not shown)" if a.forget else short(e.get(k))) + f" -> {short(changed[k])}")
    if a.dry_run:
        print("dry run: nothing written")
        return
    entry = {"at": now(), "fields": sorted(changed), "why": a.why or ""}
    if not a.forget:
        entry["was"] = {k: e.get(k) for k in changed}
    fixed["edits"] = list(e.get("edits") or []) + [entry]
    ev[ev.index(e)] = fixed
    atomic_write(ws / "evidence.jsonl", "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in ev))
    log_line(ws, f"{a.id} edited ({', '.join(sorted(changed))})" + (f": {a.why}" if a.why else ""))
    print(f"{a.id}: {', '.join(sorted(changed))} updated" + ("; old text not kept" if a.forget else ""))


def cmd_lead(a):
    ws = find_ws(a.ws)
    path = ws / "leads.jsonl"
    leads = read_jsonl(path)
    st = load_state(ws)
    if a.action == "add":
        if any(l["what"].lower() == a.what.lower() for l in leads):
            print("duplicate lead, skipped", file=sys.stderr)
            return
        lead = {"id": f"L{next_id(leads, 'L'):04d}", "what": a.what, "family": a.family,
                "why": a.why or "", "from": a.from_ev or "", "status": "open",
                "round": st.get("round", 0), "added": now()}
        append_jsonl(path, [lead])
        print(lead["id"])
    elif a.action == "done":
        ids = set()
        for want in a.what.replace(",", " ").split():
            m = re.fullmatch(r"[Ll]?(\d+)", want)
            ids.add(f"L{int(m.group(1)):04d}" if m else want)
        hit = [l for l in leads if l["id"] in ids]
        if not hit:
            sys.exit(f"no lead {a.what} (ids look like L0003; `lead list --all`)")
        for l in hit:
            l["status"] = "dead" if a.dead else "done"
            l["closed"] = now()
        atomic_write(path, "".join(json.dumps(l, ensure_ascii=False) + "\n" for l in leads))
    else:
        rows = [l for l in leads if a.all or l["status"] == "open"]
        for l in rows:
            print(f"{l['id']} [{l['family']}] {l['what']}  ({l['why']})")
        if not rows:
            print("no open leads")


def cmd_question(a):
    ws = find_ws(a.ws)
    path = ws / "questions.json"
    qs = json.loads(path.read_text(encoding="utf-8"))
    if a.action == "add":
        q = {"id": f"Q{next_id(qs, 'Q')}", "area": a.area, "priority": a.priority, "text": a.text}
        qs.append(q)
        atomic_write(path, json.dumps(qs, indent=2))
        print(q["id"])
    elif a.action == "set":
        if not a.status or not a.why:
            sys.exit("question set QID --status n/a|confirmed-unknown|contested|partial|bounded|open --why '...'")
        for q in qs:
            if q["id"] == a.text:
                if a.status == "open":
                    q.pop("status_override", None)
                else:
                    q["status_override"], q["override_why"] = a.status, a.why
                break
        else:
            sys.exit(f"no question {a.text}")
        atomic_write(path, json.dumps(qs, indent=2))
        log_line(ws, f"question {a.text} -> {a.status}: {a.why}")
        print(f"{a.text}: {a.status}")
    else:
        cov = coverage(ws)
        ev = read_jsonl(ws / "evidence.jsonl")
        for q in qs:
            c = cov[q["id"]]
            why = ""
            if c["status"] == "partial":
                unk = [e["id"] for e in ev if q["id"] in e.get("questions", []) and e["tag"] == "unknown"]
                why = (f"  <- not-found items {', '.join(unk[:4])}: if that gap is accepted, `question set {q['id']} "
                       "--status bounded --why ...`" if unk else "  <- only inferred evidence so far")
            elif c["status"] == "thin":
                why = "  <- one source: corroborate, or accept with `question set ... --status bounded`"
            print(f"{q['id']:>4} {c['status']:<17} {q['priority']:<6} ({c['evidence']} ev) {q['text']}{why}")


def cmd_round(a):
    ws = find_ws(a.ws)
    st = load_state(ws)
    ev = read_jsonl(ws / "evidence.jsonl")
    cov = coverage(ws)
    if a.action == "start":
        if st["rounds"] and not st["rounds"][-1].get("ended"):
            sys.exit(f"round {st['round']} is still open; run `round end` first")
        st["round"] += 1
        if st["phase"] == "intake":
            st["phase"] = "dig"
        st["rounds"].append({"n": st["round"], "started": now(), "evidence_before": len(ev),
                             "status_before": {k: v["status"] for k, v in cov.items()},
                             "focus": a.focus or ""})
        save_state(ws, st)
        log_line(ws, f"round {st['round']} started. focus: {a.focus or '-'}")
        print(f"round {st['round']} started")
        return
    if not st["rounds"] or st["rounds"][-1].get("ended"):
        sys.exit("no open round")
    r = st["rounds"][-1]
    rank = {"open": 0, "confirmed-unknown": 1, "partial": 2, "thin": 3, "contested": 4, "bounded": 4,
            "answered": 5, "n/a": 5}
    improved = [q for q, c in cov.items()
                if rank[c["status"]] > rank[r["status_before"].get(q, "open")]]
    leads = read_jsonl(ws / "leads.jsonl")
    new_leads = [l for l in leads if l.get("round") == r["n"]]
    new_ev = len(ev) - r["evidence_before"]
    thr = st["saturation"]["low_yield_threshold"]
    r.update({"ended": now(), "new_evidence": new_ev, "questions_improved": improved,
              "new_leads": len(new_leads),
              "low_yield": (not improved) and new_ev < thr and len(new_leads) < 3})
    r.pop("status_before", None)
    save_state(ws, st)
    sat = saturation(st, cov)
    log_line(ws, f"round {r['n']} ended: +{new_ev} evidence, improved {improved or 'none'}, "
                 f"+{len(new_leads)} leads, low_yield={r['low_yield']}")
    print(json.dumps({"round": r["n"], "new_evidence": new_ev, "questions_improved": improved,
                      "new_leads": len(new_leads), "low_yield": r["low_yield"], **sat}, indent=2))


def cmd_status(a):
    ws = find_ws(a.ws)
    st = load_state(ws)
    ev = read_jsonl(ws / "evidence.jsonl")
    cov = coverage(ws)
    leads = read_jsonl(ws / "leads.jsonl")
    sat = saturation(st, cov)
    tags = collections.Counter(e["tag"] for e in ev)
    kinds = collections.Counter(e["kind"] for e in ev)
    sources = {e.get("source") for e in ev if e.get("source")}
    qstat = collections.Counter(c["status"] for c in cov.values())
    with_unknown = {q for q in sat["must_open"]
                    if any(e.get("tag") == "unknown" and q in e.get("questions", []) for e in ev)}
    if st["phase"] in ("intake", "dig"):
        nxt = (("Dig is saturated. " + (f"These must-haves already have a not-found item: `question set <Q> --status "
                f"bounded --why ...` for {', '.join(sorted(with_unknown))}. " if with_unknown else "")
                + (f"Log an `unknown` item (what you searched) for each other open must-have: "
                   f"{', '.join(q for q in sat['must_open'] if q not in with_unknown)}."
                   if set(sat["must_open"]) - with_unknown else "") if sat["must_open"]
                else "Dig is saturated: finish the pack (pack/README.md with the scope, reference/, assets/), run "
                     "`ledger.py pack`, then hand it to a fresh builder (references/briefs.md).") if sat["saturated"]
               else f"Keep digging. Open must-haves: {', '.join(sat['must_open']) or 'none'}; "
                    f"open leads: {sum(1 for l in leads if l['status'] == 'open')}")
    elif st["phase"] == "rebuild" and (st.get("packed") or {}).get("problems"):
        nxt = (f"Packed with {st['packed']['problems']} problem(s) at the last `ledger.py pack`: fix them (or say why "
               "not in pack/README.md), then a fresh builder works from the pack (references/briefs.md).")
    else:
        nxt = {"codex": "Finish the pack and run `ledger.py pack`; on a big target show the user pack/README.md first.",
               "rebuild": "A fresh builder works from the pack (references/briefs.md); then the fresh-eyes review.",
               "verify": "Fresh-eyes review from the pack only (references/briefs.md), then send the list to the builder.",
               "unveil": "Write app/README.md (original, recovered, reconstructed, unknown) and app/LEFT-OUT.md.",
               "done": "Finished. Offer next steps."}.get(st["phase"], f"phase: {st['phase']}")
    gone = missing_tools()
    print(json.dumps({
        "name": st["name"], "type": st["type"], "phase": st["phase"], "round": st["round"],
        "budget_rounds": st["budget_rounds"], "evidence": len(ev), "by_tag": dict(tags),
        "by_kind": dict(kinds), "distinct_sources": len(sources), "questions": dict(qstat),
        "open_leads": sum(1 for l in leads if l["status"] == "open"),
        "saturation": sat, "codex_approved": st.get("codex_approved"),
        "approval": st.get("approval"), **({"packed": st["packed"]} if st.get("packed") else {}),
        **({"missing_tools": gone} if gone else {}),
        "next": nxt, **_progress(ws, ev)}, indent=2))


def _verify_next(ws: Path) -> str:
    c = ws / "codex"
    if not (c / "verify-results.json").exists():
        return "`verify.py run --serve app`, then `verify.py visual` and Read the look sheets."
    if not (c / "LOOK.md").exists():
        return "Read every museum/shots/look-*.png and write codex/LOOK.md (quote each code, a verdict per band)."
    if not (c / "SCOREBOARD.md").exists():
        return "`verify.py score`; build or excuse what codex/UNUSED-EVIDENCE.md lists."
    return "Verified: fix what SCOREBOARD.md and UNUSED-EVIDENCE.md show, or `phase unveil`."


def _progress(ws: Path, ev: list) -> dict:
    """Runaway guard, part 2: script calls since app/, evidence, dossier/ or pack/ last changed."""
    act = ws / ".activity.jsonl"
    if not act.exists():
        return {}
    calls = [json.loads(x) for x in jsonl_lines(act.read_text(encoding="utf-8")) if x.strip()]
    marks = []
    for e in ev:
        try:
            marks.append(dt.datetime.fromisoformat(str(e.get("added"))).timestamp())
        except ValueError:
            pass
    for sub in ("app", "dossier", "pack"):  # originals, screens and assets count as progress too
        d = ws / sub
        if d.is_dir():
            marks += [f.stat().st_mtime for f in d.rglob("*") if f.is_file()]
    last = max(marks) if marks else 0
    since = sum(1 for c in calls if c.get("t", 0) > last)
    out = {"script_calls": len(calls), "calls_since_progress": since}
    if since >= 30:
        recent = collections.Counter(" ".join(c["cmd"][:2]) for c in calls if c.get("t", 0) > last).most_common(4)
        out["warning"] = (f"{since} script calls since the app, the evidence or the dossier last changed (mostly: "
                          + ", ".join(f"{k} x{v}" for k, v in recent) + "). Check you are not looping: build, "
                          "record what you found, or log the gap and move on.")
    return out


def cmd_viewport(a):
    """Set the viewport verify uses (the original's device or stage size)."""
    ws = find_ws(a.ws)
    st = load_state(ws)
    w, h = (int(v) for v in a.size.lower().split("x"))
    st["viewport"] = [w, h]
    save_state(ws, st)
    print(f"viewport {w}x{h}: verify.py run uses it unless --width/--height are given")


def cmd_approve(a):
    """Record the user's approval of the Codex. Only run this after the user said yes."""
    ws = find_ws(a.ws)
    st = load_state(ws)
    codex = ws / "codex" / "CODEX.md"
    if not codex.exists():
        sys.exit("no codex/CODEX.md yet")
    report, msgs = spec_report(ws, str(codex))
    if not report["ok"]:
        print("\n".join(msgs[:20]), file=sys.stderr)
        sys.exit("check-spec fails; fix the Codex before asking for approval (iron law 1)")
    st["codex_approved"] = True
    st["approval"] = {"by": a.by, "at": now(), "note": a.note or "",
                      "codex_sha1": __import__("hashlib").sha1(codex.read_bytes()).hexdigest()[:12]}
    if a.codename:
        st["codename"] = a.codename
    save_state(ws, st)
    log_line(ws, f"Codex approved by {a.by}" + (f": {a.note}" if a.note else ""))
    print(json.dumps(st["approval"]))


def cmd_phase(a):
    ws = find_ws(a.ws)
    st = load_state(ws)
    codex = ws / "codex" / "CODEX.md"
    appr = st.get("approval") or {}
    if (a.phase in ("rebuild", "verify", "unveil") and appr.get("codex_sha1") and codex.exists()
            and __import__("hashlib").sha1(codex.read_bytes()).hexdigest()[:12] != appr["codex_sha1"]):
        sys.exit("CODEX.md changed after it was approved. Show the user what changed and run `approve` again "
                 "(the approval covers a specific version).")
    if a.phase in ("rebuild", "verify") and st.get("type") in EXHIBIT_ONLY:
        sys.exit(f"type {st['type']} is exhibit-only: go from codex to unveil (references/beyond-software.md)")
    if a.phase == "codex" and st["phase"] in ("intake", "dig"):
        sat = saturation(st, coverage(ws))
        if not sat["saturated"]:
            print(f"warning: the dig is not saturated (open must-haves: {', '.join(sat['must_open']) or 'none'}; "
                  f"rounds {sat['rounds_done']}). Moving on anyway; say so in the Codex summary.", file=sys.stderr)
            log_line(ws, "moved to codex before saturation: " + ", ".join(sat["must_open"]))
    if a.phase == "codex" and len(st.get("versions", [])) >= 2 and not st.get("target_version"):
        print("warning: several versions are defined but none is the target: `ledger.py versions target ...`",
              file=sys.stderr)
    st["phase"] = a.phase
    if a.codename:
        st["codename"] = a.codename
    save_state(ws, st)
    log_line(ws, f"phase -> {a.phase}" + (f" (codename {a.codename})" if a.codename else ""))
    print(f"phase: {a.phase}")


def cmd_find(a):
    ws = find_ws(a.ws)
    pat = re.compile(a.text, re.I)
    for e in read_jsonl(ws / "evidence.jsonl"):
        blob = " ".join(str(e.get(k, "")) for k in ("claim", "quote", "feature", "source", "locator"))
        if pat.search(blob) and (not a.kind or e["kind"] == a.kind) and (not a.tag or e["tag"] == a.tag):
            print(f"{e['id']} [{e['tag']}/{e['kind']}] {e['claim']}  <{e.get('source', '')}>")


def feature_table(ev: list[dict]) -> list[dict]:
    """Feature tags ranked by sources missing them, then by sources documenting them."""
    agg = collections.defaultdict(lambda: {"missed_by": set(), "documented_by": set(), "quotes": [],
                                           "ids": [], "aliases": set(), "primary": set()})
    for e in ev:
        if not e.get("feature") or e.get("kind") in ("timeline", "identity", "legal", "business"):
            continue  # facts about it, not features to rebuild
        for raw in str(e["feature"]).split(","):
            tag = slugify(raw)
            if not tag:
                continue
            f = agg[tag]
            f["aliases"].add(raw.strip())
            f["ids"].append(e["id"])
            src = _source_key(e.get("source"))
            longing = e["kind"] == "sentiment" and re.search(
                r"(?i)\bmiss|loved?|wish|bring (it )?back|favou?rite|best|remember|nostalg|childhood|used to|great|"
                r"addict|fond|beloved|cherish|petition", " ".join(str(e.get(k, "")) for k in ("claim", "quote")))
            if e["kind"] == "sentiment" and not longing:
                continue  # a critic's "so-so" isn't missing it
            (f["missed_by"] if e["kind"] == "sentiment" else f["documented_by"]).add(src)
            if e["kind"] != "sentiment" and e.get("family") in PRIMARY and e.get("tag") == "seen":
                f["primary"].add(src)
            if e["kind"] == "sentiment" and e.get("quote") and len(f["quotes"]) < 3:
                f["quotes"].append(e["quote"][:200])
    rows = sorted(agg.items(), key=lambda kv: (len(kv[1]["missed_by"]), len(kv[1]["documented_by"])),
                  reverse=True)
    return [{"feature": k, "missed_by_sources": len(v["missed_by"]),
             "documented_by_sources": len(v["documented_by"]), "primary_sources": len(v["primary"]),
             "evidence": v["ids"],
             "aliases": sorted(v["aliases"] - {k}), "sample_quotes": v["quotes"]} for k, v in rows]


def cmd_packet(a):
    """One feature's evidence and files, to have open while building it."""
    ws = find_ws(a.ws)
    ev = [e for e in read_jsonl(ws / "evidence.jsonl") if not e.get("superseded_by")]
    want = slugify(a.feature)
    parity = {}
    pp = ws / "codex" / "parity.json"
    if pp.exists():
        parity = json.loads(pp.read_text(encoding="utf-8"))
    feat = next((f for f in parity.get("features", []) if f.get("id") == a.feature or slugify(f.get("name", "")) == want
                 or want in [slugify(t) for t in f.get("tags", [])]), None)
    tags = {want} | ({slugify(t) for t in feat.get("tags", [])} if feat else set())
    ids = set(feat.get("evidence", [])) if feat else set()
    sc_path = ws / "codex" / "screens.json"
    screens = json.loads(sc_path.read_text(encoding="utf-8")) if sc_path.exists() else []
    screen = next((x for x in screens if x.get("id") == a.feature or slugify(x.get("name", "")) == want), None)
    if screen and not feat:  # `packet S2`: one whole screen
        ids |= set(screen.get("evidence", []))
        tags.add(slugify(screen.get("name", "")))
    words = [w for w in a.feature.lower().replace("-", " ").split() if len(w) > 3]

    def ftags(e):  # "flap, controls" is two tags, as in `mourn`
        return {slugify(t) for t in str(e.get("feature", "")).split(",") if t.strip()}
    hits = [e for e in ev if e["id"] in ids or ftags(e) & tags
            or (a.grep and any(w in (e.get("claim", "") + " " + str(e.get("quote", ""))).lower() for w in words))]
    if not hits:
        sys.exit(f"no evidence for {a.feature!r}: try a feature id (F3), one of its tags, or --grep")
    images, videos, other = [], [], []
    IMG = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    VID = (".mp4", ".webm", ".mkv", ".mov", ".m4a", ".mp3", ".wav")

    project = ws.resolve().parent.parent  # resurrections/<slug> lives in the project root

    def resolve(cand):
        for p_ in ([Path(cand)] if cand.startswith("/") else [ws / cand, project / cand]):
            if p_.is_file():
                return str(p_.resolve())
        return None

    def found(v):
        """Local files a field names. Tries the whole value first (paths with spaces)."""
        out = []
        for cand in [v] + [p_ for p_ in v.replace(";", " ").replace(",", " ").split()]:
            cand = cand.strip()
            if not cand or cand.startswith("http"):
                continue
            f = resolve(cand)
            if f and f not in out:
                out.append(f)
        return out
    for e in hits:
        for k in ("locator", "file", "path", "frame", "then", "source"):
            for f in found(str(e.get(k, ""))):
                bucket = images if f.lower().endswith(IMG) else videos if f.lower().endswith(VID) else other
                if f not in bucket:
                    bucket.append(f)
    order = {"seen": 0, "inferred": 1, "unknown": 2}
    lines = [f"# Packet: {feat['id'] + ' ' + feat['name'] if feat else a.feature}", ""]
    if feat:
        lines.append(f"Priority {feat.get('priority', '?')}, tags {', '.join(feat.get('tags', []))}")
    if screens:
        hit_ids = {e["id"] for e in hits}
        scored = []
        for s_ in ([screen] if screen and not feat else screens):
            named = (feat and feat.get("id") in s_.get("features", [])) or slugify(s_.get("name", "")) in tags
            shared = len(hit_ids.intersection(s_.get("evidence", [])))
            if named or shared:
                scored.append((2 if named else 0, shared, s_))
        for _, _, s_ in sorted(scored, key=lambda t: (-t[0], -t[1]))[:2]:  # top 2 screens for this feature
            ref = s_.get("then") or s_.get("then_frames")
            lines.append(f"Screen {s_.get('id')} {s_.get('name')}: reference {ref or 'none yet'}")
            f = resolve(str(ref)) if ref else None
            if f and f not in images:
                images.insert(0, f)
            bp = ws / "codex" / "blueprints" / str(s_.get("id")) / "blueprint.png"
            if bp.is_file() and str(bp) not in images:
                images.insert(1, str(bp))
    lines.append("")
    for e in sorted(hits, key=lambda e: (order.get(e["tag"], 3), e["id"])):
        lines.append(f"- **{e['id']}** [{e['tag']}, {e.get('kind')}{', ' + e['as_of'] if e.get('as_of') else ''}"
                     f"{', v' + str(e['version']) if e.get('version') else ''}] {e.get('claim', '')}")
        if e.get("quote"):
            lines.append(f"  > {str(e['quote'])[:400]}")
        if e.get("locator") or e.get("source"):
            lines.append(f"  ({e.get('locator', '')} {e.get('source', '')})".rstrip())
        if e.get("tag") == "unknown" and e.get("would_settle"):
            lines.append(f"  would settle: {e['would_settle']}")
    if images:
        lines += ["", "## Open these before building (Read each image)", *[f"- {f}" for f in images[:20]]]
    if videos:
        lines += ["", "## Recordings (not readable as images: look at their sheets)",
                  *[f"- {f}  ->  footage.py scenes {f} --out <dir>, then Read the sheet" for f in videos[:8]]]
    if other:
        lines += ["", "## Other files", *[f"- {f}" for f in other[:8]]]
    lines += ["", "Build this feature so every `seen` line above holds, cite the IDs in its journey's `evidence` "
              "list, and say in codex/LEFT-OUT.md why any line is not built."]
    out = "\n".join(lines)
    if a.out:
        Path(a.out).write_text(out + "\n", encoding="utf-8")
        print(f"{len(hits)} items, {len(images)} images, {len(videos)} recordings -> {a.out}")
    else:
        print(out)


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\w-])(?:\+?1[ .-])?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-])")


def _contact_details(pack):
    """E-mails and phone numbers in the pack's text files, minus ones README.md keeps."""
    readme = pack / "README.md"
    kept = readme.read_text(errors="replace", encoding="utf-8") if readme.exists() else ""
    found = []
    for f in sorted(pack.rglob("*")):
        if not f.is_file() or f == readme or f.suffix.lower() not in {".md", ".txt", ".html", ".htm", ".csv", ".json"}:
            continue
        if f.stat().st_size > 5_000_000:
            continue
        text = f.read_text(errors="replace", encoding="utf-8")
        for m in [*EMAIL_RE.findall(text), *PHONE_RE.findall(text)]:
            low = m.lower()
            if (low.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".invalid", ".test", ".example"))
                    or re.search(r"@example\.(com|org|net)$", low) or m in kept):
                continue
            item = f"{m} in {f.relative_to(pack)}"
            if item not in found:
                found.append(item)
    return found


def _hidden_contacts(pack) -> list[str]:
    """E-mail addresses inside compressed Flash files, which the text scan can't see."""
    import lzma
    import zlib
    found = []
    for f in sorted(pack.rglob("*.swf")):
        try:
            b = f.read_bytes()
            body = (zlib.decompress(b[8:]) if b[:3] == b"CWS" else
                    lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA1,
                                                                    "dict_size": 1 << 24}]).decompress(b[17:])
                    if b[:3] == b"ZWS" else b)
        except (OSError, zlib.error, lzma.LZMAError):
            continue
        rx = rb"[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9-]{2,}(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,6}\b"  # ASCII only, it's binary
        for m in sorted({x.decode() for x in re.findall(rx, body)}):
            if not re.search(r"(?i)\.(png|jpe?g|gif|swf|as)$|@example\.", m):
                found.append(f"{m} in {f.relative_to(pack)}")
    return found


def cmd_pack(a):
    """Write pack/FACTS.md and check the pack stands alone (the builder sees nothing else)."""
    ws = find_ws(a.ws)
    pack = ws / "pack"
    pack.mkdir(exist_ok=True)
    ev = [e for e in read_jsonl(ws / "evidence.jsonl") if not e.get("superseded_by")]
    order = {"seen": 0, "inferred": 1, "unknown": 2}
    ev.sort(key=lambda e: (order.get(e.get("tag"), 3), e.get("kind") or "", e.get("id") or ""))
    out = [f"# Facts: {load_state(ws).get('name', ws.name)}", "",
           "What the research found. `seen` = observed in a source; `inferred` = worked out from sources (says from "
           "what); `unknown` = searched for and not found (says where). Anything not here is your design: mark it.", ""]

    def line(e, where_parts):
        where = "; ".join(x for x in where_parts if x)
        quote = f' "{e["quote"]}"' if e.get("quote") else ""
        return f"{e.get('id')} **[{e.get('tag')}, {e.get('kind')}]** {e.get('claim', '')}{quote}", where

    def extra(e, as_of=True):  # what differs between lines in a group
        return (as_of and e.get("as_of") and f"as of {e['as_of']}",
                e.get("derived_from") and "from " + ", ".join(e["derived_from"]),
                e.get("searched") and "searched: " + e["searched"])
    # facts from one place share source and locator,
    # so they get one heading, not a locator per bullet
    place = lambda e: (e.get("source") or "", e.get("locator") or "")  # noqa: E731
    groups = collections.Counter(place(e) for e in ev if e.get("source"))
    done = set()
    for e in ev:
        if e["id"] in done:
            continue
        p = place(e)
        if e.get("source") and groups[p] >= 3:
            members = [x for x in ev if place(x) == p]
            dates = {x.get("as_of") for x in members}
            one_date = len(dates) == 1 and None not in dates
            out.append(f"- **{len(members)} facts from** _{'; '.join(x for x in p if x)}"
                       + (f"; as of {members[0]['as_of']}" if one_date else "") + "_")
            for x in members:
                text, where = line(x, extra(x, not one_date))
                out.append(f"  - {text}" + (f" _({where})_" if where else ""))
                done.add(x["id"])
            continue
        text, where = line(e, (*p, *extra(e)))
        out.append(f"- {text}" + (f"  \n  _{where}_" if where else ""))
    (pack / "FACTS.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    files = lambda d: [f for f in (pack / d).rglob("*") if f.is_file()] if (pack / d).exists() else []  # noqa: E731
    ref, assets = files("reference"), files("assets")
    problems = []
    if not (pack / "README.md").exists():
        problems.append("pack/README.md is missing: say what each folder holds, the target version, and where the "
                        "evidence (footage, archive) can be found again")
    if not ref:
        problems.append("pack/reference/ is empty: the clearest reference for every screen, state or page goes there")
    if ev and not any(authority(e) == "firsthand" for e in ev):
        problems.append("no first-hand account (authority: firsthand) among the facts: people who made, sold or "
                        "used it often hold what nothing else shows (former staff, developers, players); search for "
                        "them, reading blocked pages through the archive, or record where you searched as unknown")
    if len(assets) > 150 and not (pack / "assets" / "INDEX.md").exists():
        problems.append(f"pack/assets/ holds {len(assets)} files and no INDEX.md: say what each file or folder is "
                        "and what it is for, so the builder opens only what it uses")
    mb = sum(f.stat().st_size for f in ref + assets) / 1e6
    if mb > 150:
        print(f"note: the pack is {mb:.0f} MB; keep bulk extractions in dossier/ unless the build uses them "
              "(every file the builder looks at costs it time)", file=sys.stderr)
    if any("private" in f.parts for f in ref + assets):
        problems.append("pack holds files from a private folder: people's content stays out of the pack")
    contacts = _contact_details(pack)
    if contacts:
        problems.append("pack holds contact details (" + "; ".join(contacts[:6]) + (" ..." if len(contacts) > 6 else "")
                        + "): nobody's e-mail, phone number or home address is restored, named creators included; "
                        "replace them (or, for a business's public address, name it in pack/README.md to keep it)")
    hidden_c = _hidden_contacts(pack)
    if hidden_c:  # keep the original as is, but the build mustn't show them
        print("note: original Flash files hold contact details inside them (" + "; ".join(hidden_c[:4])
              + (" ..." if len(hidden_c) > 4 else "") + "): keep the files unchanged, but say in pack/README.md that "
              "the build must never show them (check the screens that show credits or an ABOUT text)", file=sys.stderr)
    missing = _named_missing(ws, pack)
    if missing:
        problems.append("pack/README.md names files the pack does not hold: " + ", ".join(missing[:12])
                        + (" ..." if len(missing) > 12 else "") + ": add each one or fix the name; a file known to be lost "
                        "can stay named: mark it `name` (lost), or list it under an Unknowns/Lost heading")
    hidden = [e["id"] for e in ev if re.search(r"(?<![\w.-])dossier/", " ".join(str(e.get(k) or "") for k in
                                                                       ("source", "locator", "claim", "derived_from")))]
    hidden_md = [str(f.relative_to(pack)) for f in pack.rglob("*.md") if f.name != "FACTS.md"
                 and re.search(r"(?<![\w.-])dossier/", f.read_text(errors="replace", encoding="utf-8"))]
    if hidden or hidden_md:  # the builder can't open dossier paths, it only gets the pack
        problems.append("the pack points into dossier/, which the builder never sees: "
                        + (f"facts {', '.join(hidden[:10])}" + (" ..." if len(hidden) > 10 else "") if hidden else "")
                        + ("; " if hidden and hidden_md else "") + (", ".join(hidden_md[:6]) if hidden_md else "")
                        + ": copy what they cite into pack/reference/ (or pack/assets/) and point there, "
                        "or cite the original source (URL, video ID and time) instead")
    blank = _blank_images(ref)
    if blank:
        problems.append("blank reference images (one colour, nothing to build from): "
                        + ", ".join(str(f.relative_to(pack)) for f in blank[:12]) + (" ..." if len(blank) > 12 else "")
                        + ": re-capture them (a seek before the video drew, a page not yet loaded) or remove them")
    st = load_state(ws)
    if (pack / "README.md").exists() and ref:  # status should say packed, not "intake, round 0"
        st["packed"] = {"at": now(), "facts": len(ev), "problems": len(problems)}
        if st.get("phase") in ("intake", "dig", "codex"):
            st["phase"] = "rebuild"
            log_line(ws, f"packed ({len(ev)} facts, {len(problems)} problems): phase -> rebuild")
        save_state(ws, st)
    print(json.dumps({"facts": len(ev), "reference_files": len(ref), "asset_files": len(assets),
                      "problems": problems,
                      "next": "Hand the pack to a fresh builder (references/briefs.md), or, with no helper agents, "
                              "build from the pack yourself."}, indent=2))


PACK_NAMED = re.compile(r"`([^`\n]+)`")
LOST_WORDS = r"unknowns?|lost|missing|not (?:found|archived|recovered|surviving)|unrecovered|gone"
LOST_HEADING = re.compile(rf"\b({LOST_WORDS})\b", re.I)
LOST_AFTER = re.compile(rf"\s*[(\[:,\u2014-]*\s*({LOST_WORDS})\b", re.I)  # `vest.mov` (lost)
LOST_LEAD = re.compile(rf"^\s*(?:[-*+]\s+)?\**({LOST_WORDS})\**\s*:", re.I)  # - Lost: `a.mov`, `b.mov`
HEADING = re.compile(r"^\s*(#{1,6}\s+.+|\*\*[^*`]+\*\*:?|[A-Z][^`:]{0,60}:)\s*$")
REF_MEDIA = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".mp4", ".webm", ".mov", ".mp3", ".wav",
             ".ogg", ".m4a", ".pdf")


def _named_missing(ws: Path, pack: Path) -> list[str]:
    """Backticked files in pack/README.md that the pack lacks. Skips commands, globs, URLs, outside
    paths and files marked lost (Lost/Unknowns heading, "Lost:" line, `x.mov` (lost))."""
    readme = pack / "README.md"
    if not readme.exists():
        return []
    names = {f.name for f in pack.rglob("*") if f.is_file()}
    out, toks, lost = [], [], False
    for line in readme.read_text(errors="replace", encoding="utf-8").splitlines():
        if HEADING.match(line):  # a Lost section names those files on purpose
            lost = bool(LOST_HEADING.search(line))
            continue
        if lost or LOST_LEAD.match(line):
            continue
        toks += [m.group(1) for m in PACK_NAMED.finditer(line) if not LOST_AFTER.match(line, m.end())]
    for tok in toks:
        t = tok.strip().removeprefix("./")
        if not t or re.search(r"[*?<>{}$|]|://|\.\.|\s-{1,2}\w|\.py\b", t):
            continue
        if t.startswith("pack/"):
            t = t[5:]
        if t.split("/")[0] in ("reference", "assets") and len(t.split("/")) > 1:
            hit = (pack / t).exists()
        elif "/" not in t and t.lower().endswith(REF_MEDIA) and " " not in t:  # bare .txt/.md names are
            # often there for another reason (a site's index)
            hit = t in names
        else:
            continue
        if not hit and tok not in out:
            out.append(tok)
    return out


def _blank_images(files: list[Path]) -> list[Path]:
    """Reference images that are all one colour. Skipped without Pillow."""
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return []
    out = []
    for f in sorted(files)[:500]:
        if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            continue
        try:
            with Image.open(f) as im:
                im.draft("RGB", (256, 256))
                im = im.convert("RGB")
                im.thumbnail((256, 256))
                if max(ImageStat.Stat(im).stddev) < 1.5:
                    out.append(f)
        except Exception:  # unreadable images aren't this check's problem
            continue
    return out


def cmd_mourn(a):
    ws = find_ws(a.ws)
    rows = feature_table(read_jsonl(ws / "evidence.jsonl"))
    if rows and rows[0]["missed_by_sources"] <= 1:
        print("note: sentiment is sparse (the top feature is missed by at most one source); weigh "
              "documented_by_sources too before choosing the soul.", file=sys.stderr)
    if not any(r["missed_by_sources"] for r in rows):
        n_sent = sum(1 for e in read_jsonl(ws / "evidence.jsonl") if e.get("kind") == "sentiment")
        print((f"note: {n_sent} sentiment items, but none say people miss something (\"miss\", \"loved\", \"bring back\", "
               "\"used to\"); " if n_sent else "note: no sentiment items yet; ") +
              "ranking by documentation only. Search memory threads before choosing the soul, or say the thing is "
              "obscure and pick the soul from what it was for.", file=sys.stderr)
    print(json.dumps(rows, indent=2, ensure_ascii=False))


def cmd_validate(a):
    ws = find_ws(a.ws)
    ev = read_jsonl(ws / "evidence.jsonl")
    qids = {q["id"] for q in json.loads((ws / "questions.json").read_text(encoding="utf-8"))}
    ids, bad = set(), 0
    for e in ev:
        if e["id"] in ids:
            print(f"{e['id']}: duplicate id")
            bad += 1
        ids.add(e["id"])
    for e in ev:
        for err in problems(e, ids, qids):
            print(f"{e['id']}: {err}")
            bad += 1
    print(f"{len(ev)} items, {bad} problems")
    sys.exit(1 if bad else 0)


REF = re.compile(r"\bE\d{4}\b")
MARK = re.compile(r"\[(DESIGN|UNKNOWN)\b", re.I)


def spec_report(ws: Path, file: str) -> tuple[dict, list[str]]:
    ev = {e["id"]: e for e in read_jsonl(ws / "evidence.jsonl")}
    fp = Path(file)
    if not fp.is_absolute() and not fp.exists():
        fp = ws / file
    lines = fp.read_text(encoding="utf-8").splitlines()
    section, exempt, fenced = "", False, False
    ungrounded, dangling, msgs = [], [], []
    counts = collections.Counter()
    sep = re.compile(r"^\|[\s:|-]+\|?$")
    exempt_re = re.compile(r"(?i)\b(unknowns?|open questions?|provenance|appendix|how to read)\b")
    for n, line in enumerate(lines, 1):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            fenced = not fenced
            continue
        if fenced or not s or s.startswith("<!--"):
            continue
        h = re.match(r"^#{1,6}\s+(.*)", s)
        if h:
            section = h.group(1).strip()
            exempt = bool(exempt_re.search(section))
            continue
        if exempt or sep.match(s) or re.fullmatch(r"[-*_]{3,}", s):
            continue
        if re.match(r"^[-*]\s*Test:", s):
            continue  # pointers to journeys are not claims
        if s.startswith("|") and n < len(lines) and sep.match(lines[n].strip()):
            continue  # table header
        if not section:
            continue  # preamble before the first heading
        refs = REF.findall(s)
        marks = MARK.findall(s)
        for r in refs:
            if r not in ev:
                dangling.append((n, r))
            else:
                counts[ev[r]["tag"]] += 1
        for m in marks:
            counts[m.lower()] += 1
        if not refs and not marks:
            ungrounded.append((n, section, s[:110]))
    st_ = load_state(ws)
    target = (st_.get("target_version") or {}).get("label")
    targets = {t.strip() for t in str(target or "").split(",") if t.strip()}  # several sites, several eras
    off = []
    if target:  # other versions' evidence is fine if the line says so
        vers = st_.get("versions", [])
        order = {v["label"]: i for i, v in enumerate(sorted(vers, key=lambda v: v["from"]))}
        t_i = max([order.get(t, len(order)) for t in targets] or [len(order)])
        # newest version per feature, up to the target
        latest = {}
        for e in ev.values():
            v = version_of(e, vers)
            if e.get("feature") and v in order and order[v] <= t_i:
                latest[e["feature"]] = max(latest.get(e["feature"], -1), order[v])
        for n, line in enumerate(lines, 1):
            if re.search(r"(?i)\((?:from|as of|in) [^)]*\)|\[era\]|other version", line):
                continue
            for r in REF.findall(line):
                v = version_of(ev[r], vers) if r in ev else None
                if not v or v in targets or v not in order:
                    continue
                later = order[v] > t_i  # may describe something the target never had
                superseded = ev[r].get("feature") and order[v] < latest.get(ev[r]["feature"], -1)
                if later or superseded:  # older evidence still applies unless there's something newer
                    off.append((n, r, v))
    grounded = counts["seen"] + counts["inferred"]
    report = {
        "file": str(fp), "lines": len(lines), "citations": dict(counts),
        "ungrounded_lines": len(ungrounded), "dangling_refs": len(dangling),
        "grounding": round(grounded / max(1, grounded + counts["design"]), 3),
        "ok": not ungrounded and not dangling,
        "ungrounded_at": [n for n, _, _ in ungrounded][:40],
    }
    for n, sec, s in ungrounded[:60]:
        msgs.append(f"line {n} [{sec}]: no evidence ref or [DESIGN]/[UNKNOWN] mark: {s}")
    for n, r in dangling:
        msgs.append(f"line {n}: {r} does not exist in the ledger")
    for n, r, v in off[:30]:
        msgs.append(f"note line {n}: {r} describes version {v}, not the target {target}: find target-era evidence, "
                    f"or say so in the line, e.g. '(from {v})'")
    report["off_target_citations"] = len(off)
    return report, msgs


VERSION_RX = re.compile(r"(?i)\b(?:v(?:ersion)?\.?\s?)(\d+(?:\.\d+){0,3}[a-z]?)\b|\b(\d+\.\d+(?:\.\d+)?)\s+(?:update|release)\b")
CHANGE_RX = re.compile(r"(?i)\b(redesign(?:ed)?|relaunch(?:ed)?|new look|overhaul(?:ed)?|updated|removed|added|"
                       r"replaced|rebrand(?:ed)?|beta|launch(?:ed)?|shut(?:ting)? down|discontinued|sunset|classic)\b")


def cmd_versions(a):
    """List versions, or `propose` them from the ledger, `add` one, or `target` the one to rebuild."""
    ws = find_ws(a.ws)
    st = load_state(ws)
    ev = read_jsonl(ws / "evidence.jsonl")
    vers = st.setdefault("versions", [])
    if a.action == "add":
        if not a.label or not a.frm:
            sys.exit("versions add LABEL --from YYYY[-MM] [--to YYYY[-MM]] [--note ...]")
        vers[:] = [v for v in vers if v["label"] != a.label] + [{"label": a.label, "from": a.frm, "to": a.to or "",
                                                                 "note": a.note or "", "evidence": a.evidence or ""}]
        vers.sort(key=lambda v: v["from"])
        save_state(ws, st)
        print(f"version {a.label}: {a.frm}..{a.to or 'end'}")
        return
    if a.action == "target":
        if not a.label:
            sys.exit("versions target LABEL --why '...'")
        unknown = [x for x in a.label.split(",") if vers and x.strip() not in {v["label"] for v in vers}]
        if unknown:
            sys.exit(f"no version {', '.join(unknown)}; add it first (known: {', '.join(v['label'] for v in vers)}). "
                     "Several targets (two sites, two eras) are comma-separated.")
        st["target_version"] = {"label": a.label, "why": a.why or ""}
        save_state(ws, st)
        log_line(ws, f"target version -> {a.label}: {a.why or ''}")
        print(f"target version: {a.label}")
        return
    if a.action == "propose":
        marks = []
        years = collections.Counter()
        for e in ev:
            w = when(e)
            if w:
                years[w[0][:4]] += 1
            text = " ".join(str(e.get(k, "")) for k in ("claim", "quote", "locator"))
            for m in list(VERSION_RX.finditer(text))[:1]:  # one marker per item
                marks.append({"id": e["id"], "when": "..".join(w) if w else "?", "marker": "version " + (m.group(1) or m.group(2)),
                              "claim": e.get("claim", "")[:110]})
            for m in CHANGE_RX.finditer(text):
                if e.get("kind") in ("timeline", "feature", "ui", "behavior", "identity", "flow"):
                    marks.append({"id": e["id"], "when": "..".join(w) if w else "?", "marker": m.group(1).lower(),
                                  "claim": e.get("claim", "")[:110]})
                    break
        marks.sort(key=lambda m: m["when"])
        print(json.dumps({"dated_items_per_year": dict(sorted(years.items())),
                          "undated_descriptive_items": sum(1 for e in ev if e.get("kind") in DESCRIBES and not when(e))},
                         indent=1))
        for m in marks[: a.max]:
            print(f"{m['when']:<22} {m['marker']:<16} {m['id']}  {m['claim']}")
        print("\nDefine the iterations these markers imply (`versions add 'classic' --from 2006 --to 2009-05`), and "
              "fill gaps with sources made for it: release notes, changelogs, App Store version history "
              "(`wayback.py changes <store-url>`), dated captures (`wayback.py timeline`), dated reviews.")
        return
    # list
    target = (st.get("target_version") or {}).get("label")
    by_v = collections.defaultdict(list)
    feats = collections.defaultdict(set)
    for e in ev:
        v = version_of(e, vers) or ("(undated)" if not when(e) else "(between versions)")
        by_v[v].append(e["id"])
        if e.get("feature") and not v.startswith("("):
            feats[e["feature"]].add(v)
    rows = [{"version": v["label"], "span": f"{v['from']}..{v.get('to') or 'end'}", "items": len(by_v.get(v["label"], [])),
             "target": v["label"] == target, "note": v.get("note", "")} for v in vers]
    rows += [{"version": k, "items": len(by_v[k])} for k in ("(between versions)", "(undated)") if by_v.get(k)]
    print(json.dumps({"versions": rows, "target": st.get("target_version")}, indent=1))
    multi = {f: sorted(vs) for f, vs in feats.items() if len(vs) > 1}
    if multi:
        print("Features described in more than one version (check whether they changed):")
        for f, vs in sorted(multi.items()):
            print(f"  {f}: {', '.join(vs)}")
    if len(vers) >= 2 and not target:
        print("No target version yet: `versions target <label> --why ...` (default: the last full version before it "
              "died; the user may prefer the era people miss). Ask at the Codex checkpoint.")
    if not vers:
        print("No versions defined. `versions propose` suggests them from the ledger.")


def cmd_check_spec(a):
    ws = find_ws(a.ws)
    report, msgs = spec_report(ws, a.file)
    for m in msgs:
        print(m, file=sys.stderr)
    print(json.dumps(report, indent=2))
    sys.exit(0 if report["ok"] else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ws", help="workspace folder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.add_argument("name")
    s.add_argument("--type", choices=TYPES, default="other")
    s.add_argument("--budget", choices=list(BUDGETS), default="standard")
    s.add_argument("--slug")
    s.add_argument("--root", default="resurrections")
    s.add_argument("--share-safe", action="store_true",
                   help="clean-room mode for publishing: no original files in app/ (references/share-safe.md)")
    s.add_argument("--viewport", help="WxH the original was used at, e.g. 320x568 (iPhone 5), 640x480 (a Flash "
                                     "stage): verify.py run and visual use it by default. Not known yet (a Flash "
                                     "stage comes from the SWF)? Leave it out and set it later: `ledger.py viewport WxH`")
    s.add_argument("--low-yield", type=int, default=4,
                   help="a round adding fewer items than this, answering nothing new, is low-yield")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("add")
    s.add_argument("--tag", required=True, choices=TAGS)
    s.add_argument("--kind", required=True, choices=KINDS)
    s.add_argument("--claim", required=True)
    s.add_argument("--source")
    s.add_argument("--locator", help="where exactly: snapshot date + region, timestamp in video, page no.")
    s.add_argument("--quote", help="short verbatim excerpt")
    s.add_argument("--captured", help="date the source itself was made (YYYY-MM-DD)")
    s.add_argument("--as-of", dest="as_of", help="when the thing looked/worked like this (YYYY[-MM[-DD]] or a range a..b)")
    s.add_argument("--version", help="which iteration this describes (label)")
    s.add_argument("--questions", help="comma list, e.g. Q3,Q5")
    s.add_argument("--feature", help="feature tag, e.g. 'river-of-news'")
    s.add_argument("--confidence", choices=CONF, default="medium")
    s.add_argument("--derived-from", help="comma list of evidence IDs (for inferred)")
    s.add_argument("--reasoning", help="why the inference holds (for inferred)")
    s.add_argument("--family", choices=FAMILIES)
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("versions", help="iterations over time: propose, add, target, or list")
    s.add_argument("action", nargs="?", default="list", choices=["list", "propose", "add", "target"])
    s.add_argument("label", nargs="?")
    s.add_argument("--from", dest="frm", help="YYYY[-MM[-DD]]")
    s.add_argument("--to", help="YYYY[-MM[-DD]] (empty: until it died)")
    s.add_argument("--note")
    s.add_argument("--evidence", help="E-ids that define it (release notes, changelog)")
    s.add_argument("--why")
    s.add_argument("--max", type=int, default=60)
    s.set_defaults(fn=cmd_versions)

    s = sub.add_parser("add-batch", help="merge a JSONL file of evidence (and leads)",
                       description=BATCH_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("file")
    s.add_argument("--archive-inbox", action="store_true", help="rename file to *.merged.jsonl afterwards")
    s.set_defaults(fn=cmd_add_batch)

    s = sub.add_parser("link", help="attach existing evidence IDs to questions")
    s.add_argument("evidence", help="comma list, e.g. E0012,E0013")
    s.add_argument("questions", help="comma list, e.g. Q3,Q7")
    s.add_argument("--remove", action="store_true", help="unlink instead")
    s.set_defaults(fn=cmd_link)

    s = sub.add_parser("supersede", help="retire items a later find overtook (e.g. an early 'not found')")
    s.add_argument("old", help="evidence id(s), comma-separated")
    s.add_argument("--by", required=True, help="the evidence id that overtook them")
    s.add_argument("--why")
    s.set_defaults(fn=cmd_supersede)

    s = sub.add_parser("edit", help="correct an item's quote, claim or locator in place (history kept)")
    s.add_argument("id", help="evidence id, e.g. E0136, or the batch key it was added with, e.g. @k1")
    s.add_argument("--quote", help="the corrected quote ('' removes it; then a locator must remain)")
    s.add_argument("--claim")
    s.add_argument("--locator")
    s.add_argument("--why", help="what was wrong, e.g. 'phone number removed (law 3)'")
    s.add_argument("--forget", action="store_true", help="do not keep the old text in the item's history "
                                                         "(personal data being removed)")
    s.add_argument("--dry-run", action="store_true", help="show the item and the change without writing it")
    s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("lead")
    s.add_argument("action", choices=["add", "list", "done", "set"], help="set = done (accepted for symmetry with question set)")
    s.add_argument("what", nargs="?", default="", help="query/URL to chase (add) or lead ID (done)")
    s.add_argument("--family", choices=FAMILIES, default="web")
    s.add_argument("--why")
    s.add_argument("--from", dest="from_ev", help="evidence ID that revealed this lead")
    s.add_argument("--dead", action="store_true", help="mark as dead end")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_lead)

    s = sub.add_parser("viewport", help="set the device/stage size verify uses, e.g. 320x568")
    s.add_argument("size")
    s.set_defaults(fn=cmd_viewport)

    s = sub.add_parser("stop", help="record a law-4 stop: it is still available (no workspace needed)")
    s.add_argument("name")
    s.add_argument("--where", required=True, help="where the user can get it (URL, store, owner's archive)")
    s.add_argument("--why", required=True, help="one sentence")
    s.add_argument("--evidence", action="append", help="URL + what it showed (repeatable)")
    s.add_argument("--root", default="resurrections")
    s.set_defaults(fn=cmd_stop)

    s = sub.add_parser("use", help="make another workspace the active one")
    s.add_argument("slug")
    s.add_argument("--root", default="resurrections")
    s.set_defaults(fn=cmd_use)

    s = sub.add_parser("approve", help="record the user's approval of the Codex")
    s.add_argument("--by", required=True, choices=["user", "auto"],
                   help="user = the user said yes; auto = the user explicitly waived the checkpoint")
    s.add_argument("--note")
    s.add_argument("--codename")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("question")
    s.add_argument("action", choices=["add", "list", "set"])
    s.add_argument("text", nargs="?", default="", help="question text (add) or ID (set)")
    s.add_argument("--status", choices=["n/a", "confirmed-unknown", "contested", "partial", "bounded", "open"],
                   help="set: n/a = does not apply; confirmed-unknown = searched, not findable; "
                        "contested = sources disagree (the Codex must make a marked call); open = clear")
    s.add_argument("--why")
    s.add_argument("--area", choices=KINDS, default="other")
    s.add_argument("--priority", choices=["must", "should"], default="should")
    s.add_argument("--must", dest="priority", action="store_const", const="must", help="same as --priority must")
    s.set_defaults(fn=cmd_question)

    s = sub.add_parser("round")
    s.add_argument("action", choices=["start", "end"])
    s.add_argument("--focus", help="what this round targets")
    s.set_defaults(fn=cmd_round)

    s = sub.add_parser("phase")
    s.add_argument("phase", choices=["intake", "dig", "codex", "rebuild", "verify", "unveil", "done"])
    s.add_argument("--codename")
    s.set_defaults(fn=cmd_phase)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    s = sub.add_parser("find")
    s.add_argument("text")
    s.add_argument("--kind", choices=KINDS)
    s.add_argument("--tag", choices=TAGS)
    s.set_defaults(fn=cmd_find)

    sub.add_parser("mourn").set_defaults(fn=cmd_mourn)
    s = sub.add_parser("packet", help="everything about one feature, to read before building it")
    s.add_argument("feature", help="a parity feature id (F3), its name, or a feature tag")
    s.add_argument("--grep", action="store_true", help="also match the words in claims and quotes")
    s.add_argument("--out", help="write the packet to this file instead of printing it")
    s.set_defaults(fn=cmd_packet)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    sub.add_parser("pack", help="write pack/FACTS.md from the ledger and check the build pack").set_defaults(fn=cmd_pack)

    s = sub.add_parser("check-spec")
    s.add_argument("file")
    s.set_defaults(fn=cmd_check_spec)

    for sp in {id(v): v for v in sub.choices.values()}.values():
        sp.add_argument("--ws", default=argparse.SUPPRESS, help="workspace folder")
    a = ap.parse_args(argv)
    if a.cmd == "lead" and a.action == "set":
        a.action = "done"
    if a.cmd == "lead" and a.action in ("add", "done") and not a.what:
        ap.error("lead add/done needs a value")
    if a.cmd == "question" and a.action == "add" and not a.text:
        ap.error("question add needs text")
    a.fn(a)


if __name__ == "__main__":
    from _common import use_heavy
    use_heavy("PIL")
    main()
