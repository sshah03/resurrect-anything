#!/usr/bin/env python3
"""Internet Archive item search: manuals, magazine scans, press kits, screenshots, videos.

Documents, images and video download by default. Program files and disk images need `download
--original`. They're original material, so they get parsed and run in a sandbox, never natively (law 7).

Subcommands
  search QUERY            Search archive.org items (advancedsearch). Lucene syntax allowed.
  files IDENTIFIER        List an item's files with download URLs.
  download ID FILENAME    Download one file (size-capped) into a folder.
  fulltext QUERY          Search the scanned text of every book and magazine.
  grep ID PHRASE          Where a phrase occurs inside one item (page numbers for `pages`).
  pages ID [--first --last]  Page images of a scanned manual/magazine, to read with Read.
  inside ID CONTAINER     Manuals, readmes and help files inside a disc image or zip (no program files).

Examples
  archive_search.py search "Google Reader" --mediatype texts    (several words are searched as the phrase)
  archive_search.py fulltext '"Doom Valley"' --chars 1500        (paragraphs from the top items' text)
  archive_search.py grep softalkv3n04dec1982 "Doom Valley"       (leaf = the index `pages --first` takes)
  archive_search.py search '"Starlight Arcade" AND mediatype:(texts OR image OR movies)'
  archive_search.py files some-item-id --ext pdf,jpg,png,mp4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (atomic_write, emit, enter_ws, fetch, fetch_json, log, pop_ws_arg, refuse_binary,  # noqa: E402
                     strip_html)

ALLOWED_EXT = {"pdf", "txt", "djvu", "epub", "htm", "html", "md", "rtf", "doc", "docx",
               "jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "svg",
               "mp4", "webm", "ogv", "mov", "avi", "mkv", "mpg", "mpeg", "mp3", "ogg", "wav",
               "srt", "vtt", "json", "xml", "csv"}

FIELDS = ["identifier", "title", "date", "year", "mediatype", "collection", "downloads", "description"]


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v if v is not None else ""


def _plain_words(q: str) -> list[str]:
    """Query words without Lucene operators, field names or quotes."""
    q = re.sub(r"\b\w+:(\([^)]*\)|\"[^\"]*\"|\S+)", " ", q)  # mediatype:texts, collection:(a OR b)
    return [w.lower() for w in re.findall(r"[^\W_]+(?:'[^\W_]+)?", q) if len(w) > 2 and w not in ("AND", "OR", "NOT")]


def _auto_phrase(q: str) -> str:
    """Quote a bare multi-word name, else "Doom Valley" matches every "doom" or "valley" (crawl dumps).
    Queries with quotes, fields or operators are left alone."""
    if '"' in q or re.search(r"\w:|[()*?~^]|\b(AND|OR|NOT)\b", q) or len(q.split()) < 2:
        return q
    return f'"{q.strip()}"'


def _short_collection(coll) -> str:
    """First three collections plus a count. Some items are in thousands."""
    coll = coll if isinstance(coll, list) else [coll] if coll else []
    return ",".join(coll[:3]) + (f" +{len(coll) - 3} more" if len(coll) > 3 else "")


def cmd_search(a):
    q = a.query if a.any else _auto_phrase(a.query)
    if q != a.query:
        log(f"searching the phrase {q} (--any searches the words anywhere)")
    if a.mediatype:
        q = f"({q}) AND mediatype:({a.mediatype})"
    if a.collection:
        q = f"({q}) AND collection:({a.collection})"
    params = [("q", q), ("rows", str(a.rows)), ("page", str(a.page)), ("output", "json")]
    params += [("fl[]", f) for f in FIELDS]
    if a.sort:
        params.append(("sort[]", a.sort))
    data = fetch_json("https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params))
    resp = (data or {}).get("response", {})
    log(f"{resp.get('numFound', 0)} items match" + ("" if resp.get("numFound") else
        ": `fulltext` searches inside the scans, where old names usually are (--any loosens this search)"))
    out = []
    for d in resp.get("docs", []):
        out.append({
            "identifier": d.get("identifier"),
            "title": strip_html(str(_first(d.get("title"))))[:140],
            "date": str(_first(d.get("date")) or _first(d.get("year")))[:10],
            "mediatype": _first(d.get("mediatype")),
            "collection": _short_collection(d.get("collection")),
            "downloads": d.get("downloads", 0),
            "url": f"https://archive.org/details/{d.get('identifier')}",
            "description": strip_html(str(_first(d.get("description"))))[:300],
        })
    words = _plain_words(a.query)
    if out and words and not any(all(w in (r["title"] + " " + r["description"]).lower() for w in words) for r in out):
        # fuzzy matches (crawl dumps, ROM sets): keep a few titles
        log(f"warning: no result's title or description holds all of {' '.join(words)}; these are fuzzy matches "
            "(shown: the top 5 titles only). Try another spelling, `fulltext`, or --mediatype texts.")
        out = [{"identifier": r["identifier"], "title": r["title"], "mediatype": r["mediatype"]} for r in out[:5]]
    emit(out, a.format)


def cmd_files(a):
    data = fetch_json(f"https://archive.org/metadata/{urllib.parse.quote(a.identifier)}")
    if not data:
        log("item not found")
        sys.exit(2)
    exts = {e.strip().lower().lstrip(".") for e in a.ext.split(",")} if a.ext else None
    out = []
    for f in data.get("files", []):
        name = f.get("name", "")
        if exts and name.rsplit(".", 1)[-1].lower() not in exts:
            continue
        if not a.derivatives and f.get("source") == "derivative":
            continue
        out.append({
            "name": name,
            "format": f.get("format"),
            "size_mb": round(int(f.get("size", 0) or 0) / 1e6, 2),
            "source": f.get("source"),
            "md5": f.get("md5"),
            "url": f"https://archive.org/download/{a.identifier}/{urllib.parse.quote(name)}",
        })
    md = data.get("metadata", {})
    log(f"{md.get('title')} | {md.get('date')} | {len(out)} files listed")
    emit(out, a.format)


def cmd_download(a):
    url = f"https://archive.org/download/{a.identifier}/{urllib.parse.quote(a.filename)}"
    ext = a.filename.rsplit(".", 1)[-1].lower() if "." in a.filename else ""
    if ext not in ALLOWED_EXT and not a.original:
        log(f"refusing .{ext or '?'} without --original: program files and disk images are fetched only on "
            "purpose (to run in an emulator or to port), into dossier/originals/")
        sys.exit(4)
    refuse_binary(url, original=a.original)
    body = fetch(url, max_bytes=a.max_mb * 1024 * 1024)
    refuse_binary(url, body, original=a.original)
    out = Path("dossier/originals" if a.original else a.out)
    out.mkdir(parents=True, exist_ok=True)
    dest = out / a.filename.replace("/", "__")  # keep inner paths so three README.TXT files don't collide
    dest.write_bytes(body)
    meta = {"source_url": url, "item": f"https://archive.org/details/{a.identifier}",
            "path": str(dest), "bytes": len(body), "sha1": hashlib.sha1(body).hexdigest()}
    atomic_write(Path(str(dest) + ".meta.json"), json.dumps(meta, indent=2))
    emit([meta], a.format)


DOC_EXT = ("pdf", "txt", "hlp", "doc", "rtf", "htm", "html", "md", "wri", "jpg", "jpeg", "gif", "png", "bmp")


def cmd_inside(a):
    """List documents inside a disc image or archive on archive.org without downloading it.
    Program files are hidden to keep them out of the main context (law 2)."""
    import html as _html
    import re
    url = f"https://archive.org/download/{urllib.parse.quote(a.identifier)}/{urllib.parse.quote(a.container)}/"
    body = fetch(url, max_bytes=20 * 1024 * 1024).decode("utf-8", errors="replace")
    out = []
    for href, name in re.findall(r'<a href="([^"]+)">([^<]+)</a>', body):
        name = _html.unescape(name)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext not in DOC_EXT or (a.ext and ext not in a.ext.split(",")):
            continue
        out.append({"inner": name, "ext": ext,
                    "download": f"archive_search.py download {a.identifier} '{a.container}/{name}'",
                    "url": "https:" + href if href.startswith("//") else href})
    log(f"{len(out)} documents/images inside {a.container} (program files hidden)")
    emit(out[: a.limit], a.format)


FTS_URL = "https://archive.org/services/search/beta/page_production/?"


def _fts(query: str, n: int) -> list[dict]:
    """Full-text search hits. Snippets are fixed at ~110 chars (no highlight-size param works),
    so longer text comes from the item's text layer (`_inside`)."""
    q = {"service_backend": "fts", "user_query": query, "hits_per_page": str(n)}
    data = fetch_json(FTS_URL + urllib.parse.urlencode(q))
    return (data or {}).get("response", {}).get("body", {}).get("hits", {}).get("hits", [])


def _hl(t: str) -> str:
    return re.sub(r"\{\{\{|\}\}\}", "**", re.sub(r"\s+", " ", t)).strip()


def _phrases(query: str) -> list[str]:
    """The quoted phrases of a full-text query, or its plain words as one phrase."""
    quoted = re.findall(r'"([^"]+)"', query)
    return quoted or [" ".join(re.findall(r"[^\W_]+", re.sub(r"\b\w+:\S+|\b(AND|OR|NOT)\b", " ", query)))]


def _norm(t: str) -> str:
    """Text for comparing words: hyphens joined, markup, punctuation and case dropped."""
    t = re.sub(r"</?IA_FTS_MATCH>|\{\{\{|\}\}\}|\*\*", "", t)
    t = re.sub(r"(\w)- +(\w)", r"\1\2", t)
    return " ".join(re.findall(r"[^\W_]+", t.lower()))


def _walk_tails(text: str) -> list[list[str]]:
    """Phrases to search for the words after a snippet, best first. Apostrophes stay ("can't" is one
    word to the search). Other punctuation ("site.com", "weird—the") can match nothing, so words after it go next."""
    words = [w for w in (re.sub(r"^\W+|\W+$", "", t) for t in re.sub(r"\*\*", "", text).split()) if w]
    tails = [words[-4:]]
    odd = [i for i, w in enumerate(words) if re.search(r"[^\w'’]", w)]
    if odd and odd[-1] >= len(words) - 4:
        after = words[odd[-1] + 1:]
        tails += [after[-4:]] if len(after) >= 2 else []
    tails.append(words[-3:])
    out = []
    for i, t in enumerate(tails):  # fallbacks can't contain a punctuated word
        if (len(t) >= 2 and len(" ".join(t)) >= 6 and t not in out
                and (i == 0 or not any(re.search(r"[^\w'’]", w) for w in t))):
            out.append(t)
    return out


def _walk(identifier: str, text: str, steps: int, notes: list | None = None) -> str:
    """Read past a snippet in a lending-only item by searching its last words as a phrase and
    appending what follows, 20-40 characters per step. `notes` gets how far it got and why it stopped."""
    start, done, why = len(text), 0, ""
    for _ in range(steps):
        tails = _walk_tails(text)
        if not tails:
            why = "the snippet ends without searchable words"
            break
        have, grown = _norm(text), None
        for tail in tails[:3]:  # one request each
            try:
                hits = _fts(f'"{" ".join(tail)}" AND identifier:{identifier}', 10)
            except (RuntimeError, FileNotFoundError, PermissionError) as e:
                why = f"the search failed ({e}); rerun later"
                break
            for h in hits:
                for t in (h.get("highlight") or {}).get("text") or []:
                    m = re.search(r"\{\{\{(.*?)\}\}\}", t, re.S)
                    if not m:
                        continue
                    if not have.endswith(_norm(t[: m.end()])[-40:]):  # only if the words before it match ours
                        continue
                    after = re.sub(r"\s+", " ", t[m.end():]).rstrip()
                    if _norm(after):  # real words, not just closing punctuation
                        grown = after
                        break
                if grown:
                    break
            if grown:
                break
        if not grown:
            why = why or f"no hit continues '...{' '.join(tails[0])}' (the scan may end there, or OCR differs)"
            break
        last = list(re.finditer(r"[^\W_]+", text))[-1]
        text = text[: last.end()] + grown  # the rest, punctuation included, comes from the hit
        done += 1
    if notes is not None:
        notes.append(f"+{len(text) - start} chars in {done}/{steps} steps" + (f"; stopped: {why}" if why else ""))
    return text


def cmd_fulltext(a):
    """Search the TEXT of every scanned book and magazine on archive.org, not just titles.
    Each hit names the item; `grep` finds its pages and `pages` shows them."""
    try:
        hits = _fts(a.query, a.n)
    except (RuntimeError, FileNotFoundError, PermissionError) as e:
        sys.exit(f"archive.org full-text search failed ({e}). It fails under load or on long queries: wait a "
                 "minute and retry with fewer words, a quoted phrase, or `grep <identifier>` on an item you know.")
    rows, closed = [], 0
    for i, h in enumerate(hits):
        f, hl = h.get("fields", {}), h.get("highlight", {})
        ident = f.get("identifier")
        snippets = [_hl(t) for t in (hl.get("text") or [])][: a.per_hit]
        row = {"identifier": ident, "title": f.get("title"), "year": f.get("year") or f.get("date"),
               "url": f"https://archive.org/details/{ident}"}
        if (a.chars > 150 or a.walk) and i < a.expand:  # longer text: matching paragraphs from the text layer
            doc = re.sub(r"_(hocr_searchtext\.txt\.gz|djvu\.txt)$", "", f.get("filename") or "") or ident
            walk = False
            try:
                found = []
                for ph in _phrases(a.query)[:2]:
                    found += _inside(ident, doc, f'"{ph}"', a.chars)
                if found:
                    snippets = [m["text"] for m in found][: a.per_hit]
                    row["leaves"] = [m["leaf"] for m in found][: a.per_hit]
                walk = not found  # OCR split the phrase, but the snippets can still walk
            except PermissionError:
                row["lending_only"] = walk = True
                closed += 1
            except (RuntimeError, FileNotFoundError) as e:
                log(f"{ident}: text layer unavailable ({e}); backend snippets kept" + (", walked" if a.walk else ""))
                walk = True
            if walk and a.walk:
                row["walk"] = []
                snippets = [_walk(ident, t, a.walk, row["walk"]) for t in snippets]
        cut = a.chars if a.chars and "walk" not in row else 0  # don't let --chars trim what the walk read
        rows.append({**row, "snippets": [t[:cut] if cut else t for t in snippets]})
    if a.walk and len(hits) > a.expand:
        log(f"walked the top {a.expand} items only (--expand {len(hits)} walks all; each step is 1-3 requests)")
    if not rows:
        log("no full-text hits: try fewer words, a distinctive phrase in quotes, or the author's name")
    if closed and not a.walk:
        log(f"{closed} item(s) are lending-only: their snippets stay ~110 characters (the backend's fixed size). "
            "--walk 10 reads ~300 characters further by chaining phrase searches; add AND identifier:<id> to the "
            "query to walk one item.")
    emit(rows, a.format)


def _leaf_map(identifier: str, doc: str, files: set) -> dict:
    """Text-layer leaf -> (`pages` index n, printed page). The renderer (/page/nX) skips leaves not kept
    for reading (colour card, blank), shifting later pages. Uses page_numbers.json, else scandata.xml,
    else assumes they're equal."""
    base = f"https://archive.org/download/{urllib.parse.quote(identifier)}/"
    for name in (f"{doc}_page_numbers.json", f"{identifier}_page_numbers.json"):
        if name in files:
            try:
                return leaf_map_from_page_numbers(fetch_json(base + urllib.parse.quote(name)) or {})
            except (RuntimeError, FileNotFoundError, ValueError):
                pass
    for name in (f"{doc}_scandata.xml", f"{identifier}_scandata.xml", "scandata.xml"):
        if name in files:
            try:
                return leaf_map_from_scandata(fetch(base + urllib.parse.quote(name)).decode("utf-8", "replace"))
            except (RuntimeError, FileNotFoundError):
                pass
    return {}


def leaf_map_from_page_numbers(pn: dict) -> dict:
    return {int(p["leafNum"]): (n, str(p.get("pageNumber") or ""))
            for n, p in enumerate(pn.get("pages") or []) if str(p.get("leafNum", "")).isdigit()}


def leaf_map_from_scandata(xml: str) -> dict:
    kept = [int(num) for num, body in re.findall(r'<page leafNum="(\d+)"[^>]*>(.*?)</page>', xml, re.S)
            if not re.search(r"<addToAccessFormats>\s*false", body)]
    return {leaf: (n, "") for n, leaf in enumerate(kept)}


def _inside(identifier: str, doc: str, q: str, chars: int, meta: dict | None = None) -> list[dict]:
    """Matches of q in one text file of an item, with each page's `pages` index.
    Raises PermissionError if the item is lending-only."""
    meta = meta or fetch_json(f"https://archive.org/metadata/{urllib.parse.quote(identifier)}") or {}
    server, d = meta.get("server"), meta.get("dir")
    if not (server and d):
        raise FileNotFoundError("no such item")
    params = {"item_id": identifier, "doc": doc, "path": d, "q": q}
    try:
        res = fetch_json(f"https://{server}/fulltext/inside.php?" + urllib.parse.urlencode(params)) or {}
    except RuntimeError as e:
        if "403" in str(e):
            raise PermissionError(str(e))
        raise
    leaves = _leaf_map(identifier, doc, {f.get("name") for f in meta.get("files", [])})
    rows = []
    for m in res.get("matches", []):
        for par in m.get("par", [])[:1]:
            leaf = par.get("page")
            n, printed = leaves.get(leaf, (leaf if not leaves else None, ""))
            text = re.sub(r"\s+", " ", re.sub(r"</?IA_FTS_MATCH>", "**", m.get("text", ""))).strip()
            rows.append({"doc": doc, "leaf": n, "printed_page": printed or str(par.get("page_num") or ""),
                         "url": f"https://archive.org/details/{identifier}/page/n{n}" if n is not None else "",
                         "text": window(text, chars)})
    return rows


def window(text: str, chars: int) -> str:
    """At most `chars` characters of a paragraph, around its first match."""
    if not chars or len(text) <= chars:
        return text
    i = max(0, text.find("**") - chars // 4)
    i = text.rfind(" ", 0, i) + 1 if i else 0
    return ("..." if i else "") + text[i:i + chars]


def phrase_first(rows: list[dict], phrase: str) -> list[dict]:
    """Whole-phrase matches first, ignoring line-break hyphens and punctuation."""
    want = _norm(phrase)
    return sorted(rows, key=lambda r: (want not in _norm(r["text"]), r["leaf"] if r["leaf"] is not None else 1e9))


def cmd_grep(a):
    """Where a phrase occurs in one scanned item, as `pages` indexes plus printed page numbers."""
    meta = fetch_json(f"https://archive.org/metadata/{urllib.parse.quote(a.identifier)}") or {}
    docs = [f["name"][:-len("_djvu.txt")] for f in meta.get("files", []) if f["name"].endswith("_djvu.txt")]
    if not (meta.get("server") and meta.get("dir") and docs):
        sys.exit("no text layer for this item (no _djvu.txt): open its pages with `pages` and read them")
    multi = len(a.query.split()) > 1 and '"' not in a.query
    q = a.query if a.any or not multi else f'"{a.query}"'  # several words = that phrase, unless --any
    rows = []
    if len(docs) > a.docs:
        log(f"the item holds {len(docs)} text files; searching the first {a.docs} (--docs {len(docs)} searches all)")
    for doc in docs[: a.docs]:
        try:
            rows += _inside(a.identifier, doc, q, a.chars, meta)
        except PermissionError:
            sys.exit("this item is lending-only: its text layer is not public. Read the matches through "
                     f"`archive_search.py fulltext '\"{a.query.strip(chr(34))}\" AND identifier:{a.identifier}' "
                     "--chars 1500 --walk 10` (snippets of ~110 characters, walked further), or open pages with "
                     "`pages` if they render.")
        except (RuntimeError, FileNotFoundError) as e:
            log(f"{doc}: {e}")
    if multi:
        rows = phrase_first(rows, a.query.strip('"'))
        if not rows and not a.any:
            log("no exact-phrase match (OCR may split it): --any finds the words apart")
    if any(r["leaf"] is None for r in rows):
        log("a match with leaf null sits on a leaf left out of the page images (colour card, blank)")
    if len(rows) > a.limit:
        log(f"showing {a.limit} of {len(rows)} matches (--limit raises it)")
    if not rows:
        emit(rows, a.format)
        return
    log(f"{len(rows)} matches. Open them with `archive_search.py pages {a.identifier} --first <leaf> --last <leaf>` "
        "(leaf numbers here are what `pages` takes; url opens the same page in the book reader).")
    emit(rows[: a.limit], a.format)


def cmd_pages(a):
    """Page images of a scanned item, via archive.org's page renderer (no PDF tools needed)."""
    out = Path(a.out or f"dossier/docs/{a.identifier}")
    out.mkdir(parents=True, exist_ok=True)
    total, printed = None, {}
    try:
        meta = fetch_json(f"https://archive.org/metadata/{urllib.parse.quote(a.identifier)}") or {}
        total = int(meta.get("metadata", {}).get("imagecount") or 0) or None
        # printed numbers by /page/nX index (grep's "leaf")
        printed = {n: pp for n, pp in _leaf_map(a.identifier, a.identifier,
                                                {f.get("name") for f in meta.get("files", [])}).values() if pp}
    except (RuntimeError, ValueError, FileNotFoundError):
        pass
    rows = []
    for n in range(a.first, a.last + 1):
        url = f"https://archive.org/download/{urllib.parse.quote(a.identifier)}/page/n{n}_w{a.width}.jpg"
        try:
            body = fetch(url, max_bytes=8 * 1024 * 1024)
        except (FileNotFoundError, RuntimeError) as e:
            log(f"page n{n}: {e}")
            break
        if not body.startswith(b"\xff\xd8"):
            log(f"page n{n}: not an image; stopping")
            break
        dest = out / f"page-{n:03d}.jpg"
        dest.write_bytes(body)
        rows.append({"page": n, **({"printed_page": printed[n]} if n in printed else {}), "path": str(dest),
                     "source": f"https://archive.org/details/{a.identifier}/page/n{n}"})
    log(f"{len(rows)} pages in {out}" + (f" (the item has {total}; use --first/--last for more)" if total else "")
        + f"; open them with Read (or `shots.py sheet {out}` for an overview)")
    emit(rows, a.format)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--format", choices=["jsonl", "json", "md"], default="jsonl")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--mediatype", help="software | texts | movies | audio | image | web (OR-able)")
    s.add_argument("--collection")
    s.add_argument("--rows", type=int, default=25)
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--sort", default="downloads desc")
    s.add_argument("--any", action="store_true",
                   help="search a bare multi-word query as words anywhere (default: as the phrase)")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("files")
    s.add_argument("identifier")
    s.add_argument("--ext", help="comma list, e.g. pdf,jpg,png,mp4")
    s.add_argument("--derivatives", action="store_true", help="include IA-generated derivatives")
    s.set_defaults(fn=cmd_files)

    s = sub.add_parser("inside", help="documents inside a disc image/zip on archive.org (manuals, readmes)")
    s.add_argument("identifier")
    s.add_argument("container", help="e.g. POWERGOO.iso (from `files`)")
    s.add_argument("--ext", help="comma list, e.g. pdf,txt,hlp")
    s.add_argument("--limit", type=int, default=200)
    s.set_defaults(fn=cmd_inside)

    s = sub.add_parser("fulltext", help="search the text inside every scanned book and magazine on archive.org")
    s.add_argument("query")
    s.add_argument("--n", type=int, default=15)
    s.add_argument("--chars", type=int, default=0,
                   help="characters per snippet. The search backend's snippets are fixed at ~110 characters; above "
                        "150, the top --expand items are read from their own text layer instead (whole matching "
                        "paragraphs, cut at this length). Lending-only items keep the short snippets: see --walk")
    s.add_argument("--expand", type=int, default=3, help="items to read further with --chars or --walk (default 3)")
    s.add_argument("--walk", type=int, default=0, metavar="STEPS",
                   help="lending-only items: read on past each snippet by chaining phrase searches on its last "
                        "words, ~20-40 characters per step (1-3 requests each); each row's `walk` notes say how far "
                        "each snippet got and why it stopped")
    s.add_argument("--per-hit", type=int, default=5, help="snippets per item")
    s.set_defaults(fn=cmd_fulltext)
    s = sub.add_parser("grep", help="where a phrase occurs inside one scanned item (page numbers for `pages`)")
    s.add_argument("identifier")
    s.add_argument("query")
    s.add_argument("--chars", type=int, default=500, help="characters of text per match")
    s.add_argument("--any", action="store_true",
                   help="match the words anywhere (default: several words are one phrase); phrase matches first")
    s.add_argument("--limit", type=int, default=40, help="matches to print (default 40)")
    s.add_argument("--docs", type=int, default=25, help="text files to search when the item holds several")
    s.set_defaults(fn=cmd_grep)
    s = sub.add_parser("pages", help="page images of a scanned text item (no PDF tools needed)")
    s.add_argument("identifier")
    s.add_argument("--first", type=int, default=0)
    s.add_argument("--last", type=int, default=9)
    s.add_argument("--width", type=int, default=1000)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_pages)

    s = sub.add_parser("download")
    s.add_argument("identifier")
    s.add_argument("filename")
    s.add_argument("--out", default="dossier/docs")
    s.add_argument("--max-mb", type=int, default=200)
    s.add_argument("--original", action="store_true",
                   help="allow program files and disk images (iso, zip, exe, swf...) into dossier/originals/; "
                        "run them only in a browser emulator, never natively")
    s.set_defaults(fn=cmd_download)

    ws_arg = pop_ws_arg() if argv is None else None  # --ws can go before or after the subcommand
    a = ap.parse_args(argv)
    enter_ws(ws_arg)  # relative paths are workspace paths
    try:
        a.fn(a)
    except (RuntimeError, FileNotFoundError, PermissionError) as e:
        log(f"error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
