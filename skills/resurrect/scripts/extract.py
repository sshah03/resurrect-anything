#!/usr/bin/env python3
"""Turn archived pages into a design + structure inventory for a modern facsimile.

Subcommands
  page FILE.html [--css FILE.css ...]   Layout skeleton, nav, forms, widgets, copy, design tokens,
                                        and a privacy scan (user-generated / personal content to
                                        replace with placeholders, never carry over).
  css FILE.css ...                      Design tokens: colours, fonts, sizes, widths, breakpoints.
  pdf FILE.pdf [--images DIR]           Text of a PDF manual (pypdf if installed, else a stdlib reader).
                                        --images saves its embedded pictures as files (pdfimages, PyMuPDF or
                                        pypdf when installed, else a stdlib reader for JPEG and plain images).
  templates DIR                         Group many archived pages into page templates by structure,
                                        so you rebuild ~5 templates instead of 500 pages.

Stdlib only. Input files are untrusted archive bytes, so they're parsed and never executed.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import zlib
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import atomic_write  # noqa: E402

LANDMARKS = {"header", "nav", "main", "aside", "footer", "section", "article", "form", "table",
             "frameset", "frame", "iframe", "marquee", "embed", "object", "canvas", "video", "audio"}
UGC_HINT = re.compile(
    r"(?<![a-z])(comments?|guestbook|gbook|guest-?book|repl(?:y|ies)|forum|thread|signature|sig|"
    r"user-?(?:name|info|profile)|member|author|byline|avatar|testimonials?|shoutbox|chat|"
    r"messages?|reviews?)(?![a-z])", re.I)
PROFILE_LINK = re.compile(r"(?i)friendid=|userid=|viewprofile|/profile[s]?/|/user[s]?/|/members?/|/u/|"
                          r"/people/|fuseaction=user|uid=|/~[a-z]")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
PHONE = re.compile(r"(?<!\d)(\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
COLOR = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\([^)]*\)")
VOID = {"br", "img", "hr", "input", "meta", "link", "area", "base", "col", "embed", "param",
        "source", "track", "wbr"}


class Inventory(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.title = ""
        self._in = None
        self.meta = {}
        self.headings: list[tuple[str, str]] = []
        self.links: list[tuple[str, str]] = []
        self.nav_links: list[tuple[str, str]] = []
        self.forms: list[dict] = []
        self.buttons: list[str] = []
        self.images: list[dict] = []
        self.scripts: list[str] = []
        self.styles: list[str] = []
        self.inline_css: list[str] = []
        self.legacy_colors: list[str] = []
        self.fonts: list[str] = []
        self.landmarks = collections.Counter()
        self.skeleton: list[str] = []
        self.ugc_regions: collections.Counter = collections.Counter()
        self.widgets = collections.Counter()
        self.text_chunks: list[str] = []
        self._link = None
        self._nav_depth = 0
        self._heading = None
        self._ugc_depth = 0
        self._ugc_text = 0
        self._style = False
        self.all_text: list[str] = []
        self._script = False
        self.layout_cells = 0

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        ident = (a.get("id", "") + " " + a.get("class", "")).strip()
        if tag not in VOID:
            self.stack.append(tag)
        depth = len(self.stack)
        if tag in LANDMARKS or (tag == "div" and a.get("id")) or tag == "table" and depth < 8:
            self.landmarks[tag] += 1
            if depth <= 7 and len(self.skeleton) < 120:
                label = tag + (f"#{a['id']}" if a.get("id") else "") + \
                    (f".{a['class'].split()[0]}" if a.get("class") else "")
                if tag == "table" and a.get("width"):
                    label += f"[w={a['width']}]"
                self.skeleton.append("  " * min(depth - 1, 6) + label)
        in_ugc = bool(self._ugc_depth) or bool(UGC_HINT.search(ident))
        if tag == "td" and (a.get("width") or a.get("valign") or a.get("colspan")):
            self.layout_cells += 1
        if tag == "nav" or re.search(r"nav|menu|sidebar|topbar", ident, re.I):
            self._nav_depth = depth
        if UGC_HINT.search(ident):
            self.ugc_regions[ident[:60]] += 1
            if not self._ugc_depth:
                self._ugc_depth = depth
        for attr in ("bgcolor", "color", "text", "link", "vlink", "alink"):
            if a.get(attr):
                self.legacy_colors.append(a[attr])
        if tag == "font" and a.get("face"):
            self.fonts.append(a["face"])
        if a.get("style"):
            self.inline_css.append(a["style"])
        if a.get("onmouseover") or a.get("onclick"):
            self.widgets["js-interaction"] += 1
        if tag == "title":
            self._in = "title"
        elif tag == "meta" and a.get("name") in ("description", "keywords", "generator"):
            self.meta[a["name"]] = a.get("content", "")[:200]
        elif tag == "a":  # profile links count even in user content (privacy)
            self._link = [a.get("href", ""), ""] if not in_ugc else None
            if in_ugc and PROFILE_LINK.search(a.get("href", "")):
                self.links.append(("", a.get("href", "")))
        elif tag in ("h1", "h2", "h3") and not in_ugc:
            self._heading = [tag, ""]
        elif tag == "form":
            self.forms.append({"action": a.get("action", ""), "method": a.get("method", "get"),
                               "fields": []})
        elif tag in ("input", "textarea", "select") and self.forms:
            t = a.get("type", tag)
            if t not in ("hidden",):
                self.forms[-1]["fields"].append(f"{t}:{a.get('name', '')}")
            if t in ("submit", "button") and a.get("value"):
                self.buttons.append(a["value"])
        elif tag == "img" and not in_ugc:
            self.images.append({"src": a.get("src", "")[:120], "alt": a.get("alt", "")[:80],
                                "w": a.get("width", ""), "h": a.get("height", "")})
        elif tag == "script":
            self._script = True
            if a.get("src"):
                self.scripts.append(a["src"])
        elif tag == "link" and "stylesheet" in a.get("rel", "").lower():
            self.styles.append(a.get("href", ""))
        elif tag == "style":
            self._style = True
        elif tag in ("embed", "object") and re.search(r"swf|flash", json.dumps(a), re.I):
            self.widgets["flash-embed"] += 1
        elif tag in ("marquee", "blink"):
            self.widgets[tag] += 1
        elif tag in ("frameset", "frame", "iframe"):
            self.widgets[tag] += 1
        elif tag == "button":
            self._in = "button"
        elif tag == "canvas":
            self.widgets["canvas"] += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in = None
        if tag == "button" and self._in == "button":
            self._in = None
        if tag == "style":
            self._style = False
        if tag == "script":
            self._script = False
        if tag == "a" and self._link:
            href, text = self._link
            text = " ".join(text.split())[:60]
            self.links.append((text, href))
            if self._nav_depth:
                self.nav_links.append((text, href))
            self._link = None
        if tag in ("h1", "h2", "h3") and self._heading:
            self.headings.append((self._heading[0], " ".join(self._heading[1].split())[:100]))
            self._heading = None
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                if t == tag:
                    break
        depth = len(self.stack)
        if self._nav_depth and depth < self._nav_depth:
            self._nav_depth = 0
        if self._ugc_depth and depth < self._ugc_depth:
            self._ugc_depth = 0

    def handle_data(self, data):
        if self._script:
            return
        if self._style:
            self.inline_css.append(data)
            return
        if self._in == "title":
            self.title += data
        elif self._in == "button":
            self.buttons.append(data.strip())
        if self._link is not None:
            self._link[1] += data
        if self._heading is not None:
            self._heading[1] += data
        t = data.strip()
        if t and len(self.all_text) < 5000:
            self.all_text.append(t)
        if t:
            if self._ugc_depth:
                self._ugc_text += len(t)
            elif len(self.text_chunks) < 400:
                self.text_chunks.append(t)


def css_tokens(css: str) -> dict:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    colors = collections.Counter(c.lower() for c in COLOR.findall(css))
    fonts = collections.Counter(f.strip().strip("'\"") for m in re.findall(r"font-family\s*:\s*([^;}]+)", css, re.I)
                                for f in m.split(",")[:1])
    sizes = collections.Counter(re.findall(r"font-size\s*:\s*([^;}]+)", css, re.I))
    widths = collections.Counter(re.findall(r"(?:max-)?width\s*:\s*(\d{3,4}px)", css, re.I))
    bps = sorted(set(re.findall(r"@media[^{]*?(\d{3,4})px", css)), key=int)
    radius = collections.Counter(re.findall(r"border-radius\s*:\s*([^;}]+)", css, re.I))
    return {
        "colors": colors.most_common(16),
        "fonts": fonts.most_common(8),
        "font_sizes": sizes.most_common(10),
        "container_widths": widths.most_common(6),
        "breakpoints_px": bps,
        "border_radius": radius.most_common(5),
        "uses_flex": "display:flex" in css.replace(" ", "") or "display: flex" in css,
        "uses_grid": "display:grid" in css.replace(" ", ""),
    }


def inventory(html: str, extra_css: list[str]) -> dict:
    p = Inventory()
    try:
        p.feed(html)
    except Exception as e:  # malformed 1990s markup is normal, keep whatever parsed
        print(f"warning: parse stopped early: {e}", file=sys.stderr)
    css = "\n".join(p.inline_css + extra_css)
    tokens = css_tokens(css)
    legacy = collections.Counter(c.lower() for c in p.legacy_colors)
    body_text = " ".join(p.all_text)
    emails = sorted(set(EMAIL.findall(body_text)))
    phones = sorted(set(PHONE.findall(body_text)))
    profile_links = sum(1 for _, href in p.links if PROFILE_LINK.search(href or ""))
    layout = ("frames" if p.widgets.get("frameset") else
              "table-layout" if p.landmarks.get("table", 0) and (p.layout_cells >= 2 or p.landmarks["table"] >= 2) else
              "grid/flex" if tokens["uses_flex"] or tokens["uses_grid"] else "flow")
    head = html.lstrip()[:300].lower()
    return {
        "title": " ".join(p.title.split()),
        "doctype": (head.split(">", 1)[0] + ">") if head.startswith("<!doctype") else
                   "none (quirks mode: keep it that way if users customised the page)",
        "meta": p.meta,
        "layout_style": layout,
        "skeleton": p.skeleton[:80],
        "landmarks": dict(p.landmarks),
        "headings": p.headings[:40],
        "nav": p.nav_links[:40],
        "link_count": len(p.links),
        "forms": p.forms[:10],
        "buttons": [b for b in p.buttons if b][:30],
        "images": p.images[:40],
        "scripts": p.scripts[:20],
        "stylesheets": p.styles[:10],
        "widgets": dict(p.widgets),
        "design_tokens": {**tokens, "legacy_html_colors": legacy.most_common(10),
                          "font_faces": collections.Counter(p.fonts).most_common(6)},
        "privacy": {
            "profile_links": profile_links,
            "user_generated_regions": p.ugc_regions.most_common(10) + (
                [(f"{profile_links} links to member profiles (friend grids, comment authors)", profile_links)]
                if profile_links >= 5 else []),
            "user_generated_chars": p._ugc_text,
            "emails_found": len(emails),
            "phone_numbers_found": len(phones),
            "advice": ("Contains user-generated or personal content. Rebuild these regions as "
                       "components with clearly fictional placeholder data; do not copy the text, "
                       "names, emails or avatars. Keep this raw file in dossier/private/ (git-ignored).")
                      if (p.ugc_regions or emails or phones or profile_links >= 5) else
                      "none detected (heuristic: still look for names, faces and comments yourself)",
        },
        "copy_sample": [PHONE.sub("[phone]", EMAIL.sub("[email]", t))
                        for t in p.text_chunks if 3 <= len(t) <= 120][:40],
    }


def pdf_text(path: str) -> str:
    """Text of a PDF via pypdf, else a stdlib reader for simple 1990s PDFs (FlateDecode, Tj/TJ).
    Scanned PDFs have no text, use page images."""
    try:
        from pypdf import PdfReader
        return "\n\n".join(f"--- page {i + 1} ---\n" + (pg.extract_text() or "")
                             for i, pg in enumerate(PdfReader(path).pages))
    except ImportError:
        pass
    import zlib
    data = Path(path).read_bytes()
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = m.group(1)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        if b"Tj" not in raw and b"TJ" not in raw:
            continue
        line = []
        for tok in re.finditer(rb"\((?:\\.|[^\\)])*\)|\b(?:Tj|TJ|T\*|Td|TD|ET)\b", raw):
            t = tok.group(0)
            if t.startswith(b"("):
                s = t[1:-1]
                s = re.sub(rb"\\([nrt()\\])", lambda x: {b"n": b"\n", b"r": b"", b"t": b" "}.get(x.group(1), x.group(1)), s)
                line.append(s.decode("latin-1"))
            elif t in (b"T*", b"Td", b"TD", b"ET"):
                if line:
                    out.append("".join(line))
                    line = []
        if line:
            out.append("".join(line))
    text = "\n".join(x for x in out if x.strip())
    if not text:
        text = ("(no extractable text: probably scanned or font-encoded. `pip install pypdf` for better "
                "extraction, or render pages to images and read them)")
    return text


def _png(w: int, h: int, rows: bytes, colors: int) -> bytes:
    """PNG bytes from PNG-filtered rows, 8 bits per channel."""
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    ctype = {1: 0, 3: 2}[colors]
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def _pdf_images_stdlib(path: str, out: Path, min_px: int, limit: int) -> tuple[list, collections.Counter]:
    """Image XObjects straight from the file: JPEG/JPEG 2000 as is, 8-bit Flate gray/RGB/CMYK as PNG.
    Named by object number, since pages aren't known here."""
    data = Path(path).read_bytes()
    if re.search(rb"/Encrypt\b", data[-4096:] + data[:4096]):
        raise ValueError("the PDF is encrypted: use pdfimages, PyMuPDF or pypdf")
    objs = {int(m.group(1)): m.start(3) for m in re.finditer(rb"(\d+)\s+(\d+)\s+obj\b(\s*)", data)}

    def val(d, key):
        m = re.search(rb"/" + key + rb"\s*(\d+\s+\d+\s+R|/\w+|\[[^\]]*\]|-?\d+)", d)
        return m.group(1) if m else None

    def deref_int(v):
        m = re.fullmatch(rb"(\d+)\s+\d+\s+R", v or b"")
        if m and int(m.group(1)) in objs:
            n = re.match(rb"\s*(\d+)", data[objs[int(m.group(1))]:objs[int(m.group(1))] + 40])
            return int(n.group(1)) if n else None
        return int(v) if v and v.lstrip(b"-").isdigit() else None

    def colors_of(cs):
        if cs is None:
            return None
        m = re.fullmatch(rb"(\d+)\s+\d+\s+R", cs)
        if m and int(m.group(1)) in objs:  # indirect: [/ICCBased n 0 R] or a name
            body = data[objs[int(m.group(1))]:objs[int(m.group(1))] + 400]
            if b"/Indexed" in body[:60]:
                return None
            ref = re.search(rb"/ICCBased\s+(\d+)\s+\d+\s+R", body)
            if ref and int(ref.group(1)) in objs:
                n = re.search(rb"/N\s+(\d)", data[objs[int(ref.group(1))]:objs[int(ref.group(1))] + 400])
                return int(n.group(1)) if n else None
            cs = body
        if b"Indexed" in cs or b"Separation" in cs or b"DeviceN" in cs:
            return None
        return 3 if b"RGB" in cs or b"CalRGB" in cs else 1 if b"Gray" in cs else 4 if b"CMYK" in cs else None

    rows, why = [], collections.Counter()
    masks = {int(x) for x in re.findall(rb"/SMask\s+(\d+)\s+\d+\s+R", data)}
    for num, start in sorted(objs.items()):
        head = data[start:start + 4096]
        si = head.find(b"stream")
        if si < 0 or not re.search(rb"/Subtype\s*/Image", head[:si]):
            continue
        if num in masks:
            why["transparency mask of another image"] += 1
            continue
        d = head[:si]
        w, h = deref_int(val(d, b"Width")), deref_int(val(d, b"Height"))
        if not w or not h:
            why["no size"] += 1
            continue
        if w < min_px and h < min_px:
            why[f"smaller than {min_px} px"] += 1
            continue
        if re.search(rb"/ImageMask\s+true", d):
            why["stencil mask"] += 1
            continue
        p = start + si + 6
        p += 2 if data[p:p + 2] == b"\r\n" else 1
        ln = deref_int(val(d, b"Length"))
        body = data[p:p + ln] if ln and data[p + ln:p + ln + 12].lstrip().startswith(b"endstream") \
            else data[p:data.find(b"endstream", p)].rstrip(b"\r\n")
        filters = re.findall(rb"/(\w+Decode|DCT|Fl)\b", val(d, b"Filter") or b"")
        try:
            if filters[:1] == [b"FlateDecode"] and len(filters) > 1:
                body, filters = zlib.decompress(body), filters[1:]
            if filters == [b"DCTDecode"]:
                ext, blob = ".jpg", body
            elif filters == [b"JPXDecode"]:
                ext, blob = ".jp2", body
            elif filters in ([b"FlateDecode"], []):
                raw = zlib.decompress(body) if filters else body
                colors = colors_of(val(d, b"ColorSpace"))
                if deref_int(val(d, b"BitsPerComponent")) != 8 or colors not in (1, 3, 4):
                    why["colour format this reader skips (indexed, 1-bit, 16-bit)"] += 1
                    continue
                pred = deref_int(val(d, b"Predictor")) or 1
                stride = w * colors
                if pred >= 10:  # PNG predictors mean the rows are already PNG-filtered
                    if colors == 4:
                        why["CMYK with PNG predictor"] += 1
                        continue
                    rows_ = raw[:h * (stride + 1)]
                elif pred == 1:
                    px = raw[:h * stride]
                    if colors == 4:  # naive CMYK to RGB
                        px = bytes(255 - min(255, px[i + k] + px[i + 3]) for i in range(0, len(px), 4) for k in range(3))
                        colors, stride = 3, w * 3
                    rows_ = b"".join(b"\0" + px[y * stride:(y + 1) * stride] for y in range(h))
                else:
                    why["TIFF predictor"] += 1
                    continue
                ext, blob = ".png", _png(w, h, rows_, colors)
            else:
                why["filter " + "+".join(f.decode() for f in filters)] += 1
                continue
        except (zlib.error, KeyError, ValueError) as e:
            why[f"unreadable ({e.__class__.__name__})"] += 1
            continue
        f = out / f"obj{num:05d}_{w}x{h}{ext}"
        f.write_bytes(blob)
        rows.append({"file": str(f), "width": w, "height": h, "object": num})
        if limit and len(rows) >= limit:
            break
    return rows, why


def pdf_images(path: str, out: Path, min_px: int = 48, limit: int = 0) -> dict:
    """Save embedded pictures with pdfimages, else PyMuPDF, else pypdf, else a stdlib reader."""
    out.mkdir(parents=True, exist_ok=True)
    rows, why, via = [], collections.Counter(), None
    if shutil.which("pdfimages"):
        via = "pdfimages (poppler)"
        r = subprocess.run(["pdfimages", "-all", "-p", path, str(out / "img")], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0:
            for f in sorted(out.glob("img-*")):
                m = re.match(r"img-(\d+)-(\d+)", f.name)
                rows.append({"file": str(f), "page": int(m.group(1)) if m else None})
        else:
            via = None
    if via is None:
        try:
            import fitz  # PyMuPDF
            via, doc, seen = "PyMuPDF", fitz.open(path), set()
            for pno, page in enumerate(doc, 1):
                for k, img in enumerate(page.get_images(full=True)):
                    if img[0] in seen:
                        continue
                    seen.add(img[0])
                    x = doc.extract_image(img[0])
                    if x["width"] < min_px and x["height"] < min_px:
                        why[f"smaller than {min_px} px"] += 1
                        continue
                    f = out / f"p{pno:04d}_{k}_{x['width']}x{x['height']}.{x['ext']}"
                    f.write_bytes(x["image"])
                    rows.append({"file": str(f), "page": pno, "width": x["width"], "height": x["height"]})
        except ImportError:
            pass
    if via is None:
        try:
            from pypdf import PdfReader
            via, seen = "pypdf", set()
            for pno, page in enumerate(PdfReader(path).pages, 1):
                try:  # list first so a shared image is decoded once
                    xo = page["/Resources"].get_object().get("/XObject")
                    xo = xo.get_object() if xo is not None else {}
                    names = []
                    for name in xo:
                        ref, obj = xo.raw_get(name), xo[name].get_object()
                        if obj.get("/Subtype") != "/Image":
                            continue
                        key = getattr(ref, "idnum", None) or id(obj)
                        if key in seen:
                            continue
                        seen.add(key)
                        if int(obj.get("/Width", 0)) < min_px and int(obj.get("/Height", 0)) < min_px:
                            why[f"smaller than {min_px} px"] += 1
                            continue
                        names.append(name)
                except Exception as e:  # noqa: BLE001 - one odd page must not stop the rest
                    why[f"page unreadable ({e.__class__.__name__})"] += 1
                    continue
                for k, name in enumerate(names):
                    try:
                        im = page.images[name]
                        pic = im.image
                        ext = Path(im.name).suffix or ".png"
                        size = f"_{pic.width}x{pic.height}" if pic is not None else ""
                        f = out / f"p{pno:04d}_{k}{size}{ext}"
                        f.write_bytes(im.data)
                        rows.append({"file": str(f), "page": pno, **({"width": pic.width, "height": pic.height}
                                                                      if pic is not None else {})})
                    except Exception as e:  # noqa: BLE001
                        why[f"image unreadable ({e.__class__.__name__}: needs Pillow?)"] += 1
                    if limit and len(rows) >= limit:
                        break
                if limit and len(rows) >= limit:
                    break
        except ImportError:
            via = None
    if via is None:
        via = "stdlib reader (JPEG, JPEG 2000, plain Flate images; no page numbers)"
        rows, why = _pdf_images_stdlib(path, out, min_px, limit)
    res = {"pdf": path, "out": str(out.resolve()), "via": via, "images": len(rows), "skipped": dict(why),
           "files": rows[:200]}
    if why and via.startswith("stdlib"):
        res["hint"] = ("some images were skipped by the stdlib reader: for every image with page numbers install "
                       "poppler (`brew install poppler` / `apt install poppler-utils`, gives pdfimages) or "
                       "`pip install pymupdf` (or `pip install pypdf pillow`)")
    atomic_write(out / "images.json", json.dumps(rows, indent=1))
    return res


def cmd_pdf(a):
    if a.images:
        res = pdf_images(a.file, Path(a.images), a.min_px, a.limit)
        if not res["images"]:
            print(f"no images extracted from {a.file}" + (f" (skipped: {res['skipped']})" if res["skipped"] else ""),
                  file=sys.stderr)
        print(json.dumps(res, indent=1))
        return
    t = pdf_text(a.file)
    print(t[: a.max] if a.max else t)


def structure_signature(html: str) -> str:
    p = Inventory()
    try:
        p.feed(html)
    except Exception:
        pass
    sk = [re.sub(r"\[w=.*?\]", "", s.strip()) for s in p.skeleton[:25]]
    return hashlib.sha1("|".join(sk).encode()).hexdigest()[:10]


def cmd_page(a):
    html = Path(a.file).read_text(errors="replace", encoding="utf-8")
    css = [Path(c).read_text(errors="replace", encoding="utf-8") for c in (a.css or [])]
    print(json.dumps(inventory(html, css), indent=2, ensure_ascii=False))


def cmd_css(a):
    css = "\n".join(Path(f).read_text(errors="replace", encoding="utf-8") for f in a.files)
    print(json.dumps(css_tokens(css), indent=2))


def cmd_templates(a):
    groups = collections.defaultdict(list)
    for f in sorted(Path(a.dir).rglob("*")):
        if f.is_file() and f.suffix.lower() in (".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".cgi", ""):
            if f.name.endswith(".meta.json"):
                continue
            try:
                groups[structure_signature(f.read_text(errors="replace", encoding="utf-8"))].append(f)
            except OSError:
                continue
    out = []
    for sig, files in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        inv = inventory(files[0].read_text(errors="replace", encoding="utf-8"), [])
        out.append({"template": sig, "pages": len(files), "example": str(files[0]),
                    "title_example": inv["title"], "layout_style": inv["layout_style"],
                    "others": [str(x) for x in files[1:6]]})
    print(json.dumps(out, indent=2, ensure_ascii=False))


def enter_ws(ws=None):
    from _common import enter_ws as _enter
    return _enter(ws)


def pop_ws_arg():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _common import pop_ws_arg as _pop
    return _pop()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("page")
    s.add_argument("file")
    s.add_argument("--css", action="append")
    s.set_defaults(fn=cmd_page)
    s = sub.add_parser("css")
    s.add_argument("files", nargs="+")
    s.set_defaults(fn=cmd_css)
    s = sub.add_parser("pdf", help="text of a PDF manual (pypdf if installed, else a stdlib fallback); --images DIR "
                                   "extracts its pictures instead")
    s.add_argument("file")
    s.add_argument("--max", type=int, default=0, help="characters of text to print (0: all)")
    s.add_argument("--images", metavar="DIR", help="save every embedded image (screenshots, photos) into DIR, with "
                                                   "images.json listing page and size")
    s.add_argument("--min-px", type=int, default=48, help="skip images smaller than this on both sides (bullets, icons)")
    s.add_argument("--limit", type=int, default=0, help="stop after this many images (0: all)")
    s.set_defaults(fn=cmd_pdf)
    s = sub.add_parser("templates")
    s.add_argument("dir")
    s.set_defaults(fn=cmd_templates)
    ws_arg = pop_ws_arg() if argv is None else None  # --ws can go before or after the subcommand
    a = ap.parse_args(argv)
    enter_ws(ws_arg)  # relative paths are workspace paths
    a.fn(a)


if __name__ == "__main__":
    main()
