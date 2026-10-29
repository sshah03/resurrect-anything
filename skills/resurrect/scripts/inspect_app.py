#!/usr/bin/env python3
"""Mine an archived application package for the original's own assets and words. It only parses, so
nothing is ever executed or loaded as code (SKILL.md law 7).

Subcommands
  pe FILE.exe|.dll|.scr|.cpl [--out DIR]
        Windows program resources, 32- and 64-bit PE (and 16-bit NE from Windows 3.x): icons and cursors
        (rebuilt as .ico/.cur plus a PNG per image), bitmaps (BMP and PNG), menus (JSON trees: text, label,
        mnemonic, shortcut, id, flags, and the status-bar prompt and tooltip when a string has the same id,
        as in MFC programs), dialogs (caption, font, styles, and every control with class, text, id,
        position and size in dialog units), string tables, message tables, accelerators (with the menu
        command each runs), version info, toolbars (button ids with labels and tooltips), the manifest, and
        every other resource as a raw file. Also machine, link date, subsystem and imported DLLs.
  chm FILE.chm [--out DIR]
        HTML Help. Unpacks every file (topics, images, CSS, the .hhc contents and .hhk index) through an LZX
        decoder, reads the title and default topic from #SYSTEM, and writes toc.json (the contents tree),
        keywords.json and topics.json (each topic's title and plain text).
  hlp FILE.hlp [--out DIR]
        WinHelp 3.x/4.x. Gets the internal files (each saved raw), title, copyright and macros from |SYSTEM,
        topic titles (|TTLBTREE), index keywords (|KWBTREE), the .cnt contents file if one sits beside it,
        and topic text (LZ77 with old |Phrases or Hall phrase compression). Text only. Character formatting,
        table layout and hotspots are dropped, and |bmN pictures stay in WinHelp's own SHG/MRB format (use
        helpdeco or a WinHelp viewer for those).
  mac PATH [--out DIR] [--max-copy-mb N]
        A macOS app. PATH is an .app folder, a .zip (or .ipa) holding one, or a .dmg (on macOS mounted with
        `hdiutil attach -nobrowse -readonly` and detached after; elsewhere extract it with `7z x` first and
        pass the folder). From a zip only what is read is unpacked (program code and libraries stay packed).
        Info.plist (name, version, bundle id, minimum OS), every .strings, .stringsdict and .loctable as JSON
        per language, every .icns as a PNG per size (the app icon too: PNG, JPEG 2000, ARGB and old RLE
        entries), images and sounds from the whole bundle, frameworks included (copied under images/ and
        sounds/ keeping their folders; TIFF and PSD converted to PNG), compiled nibs and storyboards (class
        names, every string, UI labels) and menus/ (each menu as a tree: title, shortcut with modifiers,
        action, hidden/alternate items), data/ (every other plist, command set and .sdef as JSON, with
        NSKeyedArchiver and base64/bplist blobs inside decoded, e.g. presets), every asset catalogue (*.car,
        theme catalogues too) with its renditions written as PNG/PDF/SVG files by its own reader (old
        catalogues `assetutil` rejects included; on macOS also LZFSE/LZVN data; deepmap2 cannot be decoded)
        plus `assetutil --info` when present, Help Book folders (copied, with page titles; a help book that
        only redirects online says where), and the main executable's architectures, minimum OS and linked
        libraries from its Mach-O header. Prints a progress line every few seconds. A big bundle (500 MB)
        takes under a minute, but run it in the background if your tool has a short timeout.
  a2 IMAGE.dsk|.do|.po|.2mg [--out DIR]
        Apple II disk image. Reads the DOS 3.3 or ProDOS catalog (140K images in either sector order, found by
        looking, not by the extension; bigger ProDOS volumes and subdirectories too). Every file is saved raw,
        Applesoft and Integer BASIC programs detokenized to .bas.txt listings, text files as .txt (control
        characters shown as visible symbols), binary files as .bin with their load address, and 8K hi-res
        screens at $2000/$4000 as 280x192 PNGs (exact white-on-black bits, plus an approximate colour view;
        no Pillow needed). .woz/.nib (raw disk signal), DOS 3.2, Pascal, CP/M and boot-only disks are not
        read, and it says so.
  list ARCHIVE [--get GLOB] [--out DIR]
        What is inside a zip (self-extracting .exe zips too), tar or ISO 9660 image (Joliet names), with the
        likely program, help and installer files first; --get copies matching members out for the commands
        above. For other formats (7z, rar, cab, msi, dmg, Inno Setup, InstallShield, NSIS) it names the tool
        that opens them.

Everything is written under --out (default dossier/originals/<name>-inspect/) with index.json and
summary.json, and the summary is also printed as JSON. In restore mode these are the original's own assets
and words, so use them exactly. In share-safe mode take facts only (see references/share-safe.md).
Not covered: Classic Mac OS (pre-OS X) programs, whose resources live in a resource fork, .NET managed
resources (use ILSpy), and compressed installers (unpack them first, `list` says how).
Pillow is optional. Without it the PNG copies are skipped and everything else still works.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fnmatch
import html
import io
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import atomic_write, enter_ws, log, pop_ws_arg, slugify, strip_html  # noqa: E402


class Bad(Exception):
    """Input that is not what it claims to be, or is damaged."""


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


_PIL = None


def pil():
    """PIL.Image, or None if Pillow is missing (logged once)."""
    global _PIL
    if _PIL is None:
        try:
            from PIL import Image
            _PIL = Image
        except ImportError:
            _PIL = False
            log("note: Pillow is not installed for this Python, so PNG copies are skipped (pip install pillow)")
    return _PIL or None


def wjson(path: Path, obj) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(obj, indent=1, ensure_ascii=False, default=str))
    return str(path)


def safe_part(s) -> str:
    s = re.sub(r'[\x00-\x1f:*?"<>|\\/]+', "_", str(s)).strip()
    return s if s not in ("", ".", "..") else "_"


def safe_rel(name: str) -> Path:
    """Archive member name as a relative path that can't escape the output folder."""
    parts = [safe_part(p) for p in re.split(r"[\\/]+", name) if p not in ("", ".", "..")]
    return Path(*parts) if parts else Path("_")


def default_out(src: Path, out: str | None) -> Path:
    return Path(out) if out else Path("dossier/originals") / f"{slugify(src.name)}-inspect"


def rel(p: Path, base: Path) -> str:
    # forward slashes on every system. Windows can hand back a temp folder under its 8.3 short name
    # (RUNNER~1), so try the resolved paths too
    for a, b in ((p, base), (p.resolve(), base.resolve())):
        try:
            return a.relative_to(b).as_posix()
        except ValueError:
            pass
    return p.as_posix()


def looks_ui(s: str) -> bool:
    """True for text a user would see, not keys, paths, class names or format codes."""
    t = s.strip()
    if len(t) < 2 or not re.search(r"[^\W\d_]", t):
        return False
    if re.search(r"^[A-Za-z]:\\|\\\\|^(Software|SYSTEM|HKEY_\w+)\\|://|^\{?[0-9A-Fa-f]{8}-", t):
        return False
    if re.fullmatch(r"[\w.\-~]*\.[A-Za-z0-9]{1,4}", t) and " " not in t:
        return False  # a file name
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t) and ("_" in t or re.search(r"[a-z][A-Z]", t) or t.isupper() and len(t) > 4):
        return False  # an identifier or class name
    if re.fullmatch(r"[%\d\s.,:;x*?/\\\-+#()lsdiuXfcgp]+", t):
        return False  # a format string
    return True


# ---------------------------------------------------------------- Windows programs (PE and NE)

RT = {1: "cursor", 2: "bitmap", 3: "icon", 4: "menu", 5: "dialog", 6: "string", 7: "fontdir", 8: "font",
      9: "accelerator", 10: "rcdata", 11: "messagetable", 12: "group_cursor", 14: "group_icon", 16: "version",
      17: "dlginclude", 19: "plugplay", 20: "vxd", 21: "anicursor", 22: "aniicon", 23: "html", 24: "manifest",
      240: "dlginit", 241: "toolbar"}
MACHINES = {0x14c: "x86", 0x8664: "x64", 0x1c0: "ARM", 0x1c4: "ARMv7", 0xaa64: "ARM64", 0x200: "Itanium",
            0x162: "MIPS R3000", 0x166: "MIPS R4000", 0x169: "MIPS WCE", 0x184: "Alpha", 0x1f0: "PowerPC",
            0x1a2: "SH3", 0x1a6: "SH4", 0x1c2: "ARM Thumb"}
SUBSYS = {1: "native", 2: "GUI", 3: "console", 7: "POSIX", 9: "Windows CE GUI", 10: "EFI application"}
LANGS = {0: "neutral", 1033: "en-US", 2057: "en-GB", 1031: "de-DE", 1036: "fr-FR", 1034: "es-ES", 3082: "es-ES",
         1040: "it-IT", 1041: "ja-JP", 1042: "ko-KR", 2052: "zh-CN", 1028: "zh-TW", 1043: "nl-NL", 1046: "pt-BR",
         2070: "pt-PT", 1049: "ru-RU", 1053: "sv-SE", 1030: "da-DK", 1044: "nb-NO", 1035: "fi-FI", 1045: "pl-PL",
         1029: "cs-CZ", 1038: "hu-HU", 1032: "el-GR", 1055: "tr-TR", 1037: "he-IL", 1025: "ar-SA"}


def wsz(b, o):
    """UTF-16 NUL-terminated string at o -> (text, offset after it)."""
    e = o
    while e + 1 < len(b) and b[e:e + 2] != b"\0\0":
        e += 2
    return b[o:e].decode("utf-16le", "replace"), e + 2


def asz(b, o, enc="cp1252"):
    e = b.find(b"\0", o)
    e = len(b) if e < 0 else e
    return b[o:e].decode(enc, "replace"), e + 1


def rdsz(b, o, wide):
    return wsz(b, o) if wide else asz(b, o)


def link_date(stamp):
    if not 315532800 < stamp < 2524608000:  # 1980..2050. Zeroed or hashed stamps are useless
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def read_pe(d: bytes):
    """-> (info, resources, wide). Resources are dicts {type, name, lang, data}."""
    if len(d) < 64 or d[:2] not in (b"MZ", b"ZM"):
        raise Bad("not a Windows program: no MZ header")
    e = u32(d, 0x3c)
    if e + 64 > len(d):
        raise Bad("an MS-DOS program without a Windows header: DOS programs keep no resources")
    sig = d[e:e + 4]
    if sig[:2] == b"NE":
        return read_ne(d, e)
    if sig[:2] in (b"LE", b"LX"):
        raise Bad("an LE/LX file (VxD driver or OS/2 program): no Windows resources to read")
    if sig != b"PE\0\0":
        raise Bad("an MS-DOS program (MZ without a PE/NE header): DOS programs keep no resources")
    machine, nsec, stamp, _, _, optsz, chars = struct.unpack_from("<HHIIIHH", d, e + 4)
    opt = e + 24
    magic = u16(d, opt)
    if magic == 0x10b:
        bits, ddo, nrva_o = 32, opt + 96, opt + 92
    elif magic == 0x20b:
        bits, ddo, nrva_o = 64, opt + 112, opt + 108
    else:
        raise Bad(f"unknown PE optional-header magic {magic:#x} (damaged file?)")
    nrva = min(u32(d, nrva_o), 16)
    secs = []
    so = opt + optsz
    for i in range(min(nsec, 96)):
        if so + 40 * (i + 1) > len(d):
            break
        name, vsz, va, rawsz, rawptr = struct.unpack_from("<8sIIII", d, so + 40 * i)
        secs.append({"name": name.rstrip(b"\0").decode("latin-1"), "va": va, "vsize": vsz, "raw": rawptr, "rawsize": rawsz})

    def r2o(rva):
        for s in secs:
            if s["va"] <= rva < s["va"] + max(s["vsize"], s["rawsize"]):
                o = rva - s["va"] + s["raw"]
                if o < len(d):
                    return o
        if (not secs or rva < secs[0]["va"]) and rva < len(d):
            return rva
        raise Bad(f"address {rva:#x} lies outside every section (damaged or packed file)")

    def ddir(i):
        return struct.unpack_from("<II", d, ddo + 8 * i) if i < nrva and ddo + 8 * i + 8 <= len(d) else (0, 0)

    warnings = []
    info = {"format": "PE32+" if bits == 64 else "PE32", "bits": bits, "machine": MACHINES.get(machine, hex(machine)),
            "subsystem": SUBSYS.get(u16(d, opt + 68), u16(d, opt + 68)), "dll": bool(chars & 0x2000),
            "link_date": link_date(stamp), "sections": [s["name"] for s in secs]}
    if any(s["name"].upper().startswith("UPX") for s in secs):
        warnings.append("packed with UPX: most resources are compressed. `upx -d -o unpacked.exe FILE` unpacks it "
                        "without running it; then inspect the unpacked copy")
    if ddir(14)[0]:
        info["dotnet"] = True
        warnings.append(".NET program: its forms and strings live in managed .resources, not here; open it in ILSpy")
    imports = []
    irva = ddir(1)[0]
    if irva:
        try:
            o = r2o(irva)
            for k in range(1024):
                ent = d[o + 20 * k:o + 20 * k + 20]
                if len(ent) < 20 or ent == b"\0" * 20:
                    break
                imports.append(asz(d, r2o(u32(ent, 12)), "latin-1")[0])
        except (Bad, struct.error) as ex:
            warnings.append(f"import table unreadable: {ex}")
    info["imports"] = imports
    res = []
    rrva = ddir(2)[0]
    if not rrva:
        warnings.append("no resource section: this program keeps no icons, menus or dialogs as resources")
    else:
        try:
            res = walk_pe_rsrc(d, r2o(rrva), r2o, warnings)
        except Bad as ex:
            warnings.append(f"resource section unreadable: {ex}")
    info["warnings"] = warnings
    return info, res, True


def walk_pe_rsrc(d, base, r2o, warnings):
    out, seen = [], set()

    def label(v):
        if v & 0x80000000:
            o = base + (v & 0x7fffffff)
            return d[o + 2:o + 2 + 2 * u16(d, o)].decode("utf-16le", "replace")
        return v

    def walk(off, path):
        if len(path) > 2 or off in seen:  # type, name, lang. seen breaks loops in damaged files
            return
        seen.add(off)
        nn, ni = struct.unpack_from("<HH", d, base + off + 12)
        for i in range(min(nn + ni, 8192)):
            try:
                nm, ptr = struct.unpack_from("<II", d, base + off + 16 + 8 * i)
                p = path + [label(nm)]
                if ptr & 0x80000000:
                    walk(ptr & 0x7fffffff, p)
                else:
                    drva, size = struct.unpack_from("<II", d, base + ptr)
                    o = r2o(drva)
                    p += [0] * (3 - len(p))
                    out.append({"type": p[0], "name": p[1], "lang": p[2], "data": d[o:o + size]})
            except (struct.error, Bad, IndexError) as ex:
                warnings.append(f"resource entry {path + [i]} skipped: {ex}")

    try:
        walk(0, [])
    except struct.error as ex:
        warnings.append(f"resource directory cut short: {ex}")
    return out


def read_ne(d, e):
    """Resource table, module name and imported modules of a 16-bit Windows (3.x) executable."""
    rt = e + u16(d, e + 0x24)
    info = {"format": "NE (16-bit Windows)", "bits": 16, "machine": "x86 (16-bit)",
            "windows_version": "%d.%d" % (d[e + 0x3f], d[e + 0x3e]) if e + 0x40 <= len(d) else None,
            "dll": bool(u16(d, e + 0x0c) & 0x8000), "warnings": []}
    try:
        rn = e + u16(d, e + 0x26)
        info["module"] = d[rn + 1:rn + 1 + d[rn]].decode("latin-1")
        mods, imp = e + u16(d, e + 0x28), e + u16(d, e + 0x2a)
        info["imports"] = [d[imp + u16(d, mods + 2 * k) + 1:imp + u16(d, mods + 2 * k) + 1 + d[imp + u16(d, mods + 2 * k)]]
                           .decode("latin-1") for k in range(u16(d, e + 0x1e))]
    except (IndexError, struct.error):
        pass
    res = []
    if u16(d, e + 0x24) == u16(d, e + 0x26):
        info["warnings"].append("no resource table")
        return info, res, False
    try:
        shift = u16(d, rt)
        o = rt + 2

        def ne_id(v):
            return v & 0x7fff if v & 0x8000 else d[rt + v + 1:rt + v + 1 + d[rt + v]].decode("latin-1")

        while o + 8 <= len(d) and len(res) < 20000:
            tid = u16(d, o)
            if tid == 0:
                break
            cnt = u16(d, o + 2)
            o += 8
            for _ in range(cnt):
                off, ln, _, rid = struct.unpack_from("<HHHH", d, o)
                o += 12
                res.append({"type": ne_id(tid), "name": ne_id(rid), "lang": 0,
                            "data": d[off << shift:(off << shift) + (ln << shift)]})
    except (IndexError, struct.error) as ex:
        info["warnings"].append(f"resource table cut short: {ex}")
    return info, res, False


def split_label(t):
    """'&Save As...\\tCtrl+S' -> ('Save As...', 'S', 'Ctrl+S')"""
    main, _, sc = t.partition("\t")
    if not sc and "\b" in main:
        main, _, sc = main.partition("\b")
    m = re.search(r"&([^&])", main)
    return re.sub(r"&(.)", r"\1", main).strip(), (m.group(1).upper() if m else ""), sc.strip()


MF = {0x1: "grayed", 0x2: "disabled", 0x4: "bitmap", 0x8: "checked", 0x20: "menubarbreak", 0x40: "menubreak",
      0x100: "ownerdraw", 0x4000: "right-aligned"}


def menu_item(text, mid, flag_names):
    lab, mn, sc = split_label(text)
    it = {"text": text, "label": lab}
    if mn:
        it["mnemonic"] = mn
    if sc:
        it["shortcut"] = sc
    if mid is not None:
        it["id"] = mid
    if flag_names:
        it["flags"] = flag_names
    return it


def dec_menu(b, wide):
    if wide and u16(b, 0) == 1:
        return dec_menuex(b)
    count = [0]

    def items(o, depth):
        res = []
        while o + 2 <= len(b) and count[0] < 5000:
            count[0] += 1
            flags = u16(b, o)
            o += 2
            names = [n for bit, n in MF.items() if flags & bit]
            if flags & 0x10:
                text, o = rdsz(b, o, wide)
                sub, o = items(o, depth + 1) if depth < 8 else ([], o)
                it = menu_item(text, None, names)
                it["items"] = sub
            else:
                mid = u16(b, o)
                text, o = rdsz(b, o + 2, wide)
                it = {"separator": True} if flags & 0x800 or (not mid and not text) else menu_item(text, mid, names)
            res.append(it)
            if flags & 0x80:
                break
        return res, o

    return items(4 + (u16(b, 2) if wide else 0), 0)[0]


def dec_menuex(b):
    def items(o, depth):
        res = []
        while o + 14 <= len(b) and len(res) < 2000:
            typ, state, mid, info = struct.unpack_from("<IIIH", b, o)
            text, o = wsz(b, o + 14)
            o = (o + 3) & ~3
            sub = None
            if info & 1:
                sub, o = items(o + 4, depth + 1) if depth < 8 else ([], o + 4)
            if typ & 0x800:
                it = {"separator": True}
            else:
                names = [n for bit, n in ((0x3, "grayed"), (0x8, "checked"), (0x1000, "default")) if state & bit == bit]
                names += ["right-aligned"] if typ & 0x4000 else []
                it = menu_item(text, None if sub is not None else mid, names)
                if sub is not None:
                    it["items"] = sub
            res.append(it)
            if info & 0x80:
                break
        return res, o

    return items(4 + u16(b, 2), 0)[0]


CTRL_CLASS = {0x80: "BUTTON", 0x81: "EDIT", 0x82: "STATIC", 0x83: "LISTBOX", 0x84: "SCROLLBAR", 0x85: "COMBOBOX"}
BUTTON_KIND = {0: "pushbutton", 1: "defpushbutton", 2: "checkbox", 3: "autocheckbox", 4: "radiobutton", 5: "3state",
               6: "auto3state", 7: "groupbox", 8: "userbutton", 9: "autoradiobutton", 0xB: "ownerdraw",
               0xC: "splitbutton", 0xE: "commandlink"}
STATIC_KIND = {0: "left", 1: "center", 2: "right", 3: "icon", 4: "blackrect", 5: "grayrect", 6: "whiterect",
               7: "blackframe", 8: "grayframe", 9: "whiteframe", 0xB: "simple", 0xC: "leftnowordwrap",
               0xD: "ownerdraw", 0xE: "bitmap", 0xF: "enhmetafile", 0x10: "etchedhorz", 0x11: "etchedvert",
               0x12: "etchedframe"}
DLG_STYLE = {0x4: "3dlook", 0x40: "setfont", 0x80: "modalframe", 0x400: "control", 0x800: "center",
             0x10000: "maximizebox", 0x20000: "minimizebox", 0x40000: "thickframe", 0x80000: "sysmenu",
             0xC00000: "caption", 0x10000000: "visible", 0x40000000: "child", 0x80000000: "popup"}


def sz_or_ord(b, o, wide):
    if wide:
        w = u16(b, o)
        if w == 0:
            return "", o + 2
        if w == 0xffff:
            return {"resource": u16(b, o + 2)}, o + 4
        return wsz(b, o)
    if b[o] == 0:
        return "", o + 1
    if b[o] == 0xff:
        return {"resource": u16(b, o + 1)}, o + 3
    return asz(b, o)


def control(cls, text, cid, x, y, w, h, style, exstyle=0):
    if isinstance(cls, dict):
        cls = cls["resource"]
    if isinstance(cls, int):
        cls = CTRL_CLASS.get(cls, cls)
    c = {"class": cls, "text": text, "id": -1 if cid in (0xffff, 0xffffffff) else cid,
         "x": x, "y": y, "w": w, "h": h, "style": hex(style)}
    if isinstance(cls, str):
        if cls.upper() == "BUTTON":
            c["kind"] = BUTTON_KIND.get(style & 0xF, style & 0xF)
        elif cls.upper() == "STATIC":
            c["kind"] = STATIC_KIND.get(style & 0x1F, style & 0x1F)
    if not style & 0x10000000:
        c["hidden"] = True
    if style & 0x08000000:
        c["disabled"] = True
    if exstyle:
        c["exstyle"] = hex(exstyle)
    return c


def dec_dialog(b, wide):
    if not wide:
        return dec_dialog16(b)
    ex = b[2:4] == b"\xff\xff"
    if ex:
        _, _, _, exst, style, n, x, y, cx, cy = struct.unpack_from("<HHIIIHhhhh", b, 0)
        o = 26
    else:
        style, exst, n, x, y, cx, cy = struct.unpack_from("<IIHhhhh", b, 0)
        o = 18
    menu, o = sz_or_ord(b, o, True)
    cls, o = sz_or_ord(b, o, True)
    title, o = wsz(b, o)
    dlg = {"caption": title, "x": x, "y": y, "w": cx, "h": cy, "style": hex(style),
           "style_names": [nm for bit, nm in DLG_STYLE.items() if style & bit == bit]}
    if exst:
        dlg["exstyle"] = hex(exst)
    if menu:
        dlg["menu"] = menu
    if cls:
        dlg["class"] = cls
    if style & 0x48:  # DS_SETFONT / DS_SHELLFONT
        if ex:
            pt, wt, it_, _ = struct.unpack_from("<HHBB", b, o)
            o += 6
        else:
            pt, wt, it_ = u16(b, o), 400, 0
            o += 2
        face, o = wsz(b, o)
        dlg["font"] = {"face": face, "size": pt, "weight": wt, "italic": bool(it_)}
    ctrls = []
    for _ in range(min(n, 1000)):
        o = (o + 3) & ~3
        if o + 18 > len(b):
            break
        if ex:
            _, exs, st, x_, y_, w, h, cid = struct.unpack_from("<IIIhhhhI", b, o)
            o += 24
        else:
            st, exs, x_, y_, w, h, cid = struct.unpack_from("<IIhhhhH", b, o)
            o += 18
        c, o = sz_or_ord(b, o, True)
        t, o = sz_or_ord(b, o, True)
        o += 2 + u16(b, o)
        ctrls.append(control(c, t, cid, x_, y_, w, h, st, exs))
    dlg["controls"] = ctrls
    return dlg


def dec_dialog16(b):
    style, n, x, y, cx, cy = struct.unpack_from("<IBhhhh", b, 0)
    o = 13
    menu, o = sz_or_ord(b, o, False)
    cls, o = asz(b, o)
    title, o = asz(b, o)
    dlg = {"caption": title, "x": x, "y": y, "w": cx, "h": cy, "style": hex(style),
           "style_names": [nm for bit, nm in DLG_STYLE.items() if style & bit == bit]}
    if menu:
        dlg["menu"] = menu
    if cls:
        dlg["class"] = cls
    if style & 0x40:
        pt = u16(b, o)
        face, o = asz(b, o + 2)
        dlg["font"] = {"face": face, "size": pt}
    ctrls = []
    for _ in range(n):
        if o + 14 > len(b):
            break
        x_, y_, w, h, cid, st = struct.unpack_from("<hhhhHI", b, o)
        o += 14
        if b[o] & 0x80:
            c = b[o]
            o += 1
        else:
            c, o = asz(b, o)
        t, o = sz_or_ord(b, o, False)
        o += 1 + b[o]
        ctrls.append(control(c, t, cid, x_, y_, w, h, st))
    dlg["controls"] = ctrls
    return dlg


VK = {8: "Backspace", 9: "Tab", 13: "Enter", 19: "Pause", 27: "Esc", 32: "Space", 33: "PgUp", 34: "PgDn",
      35: "End", 36: "Home", 37: "Left", 38: "Up", 39: "Right", 40: "Down", 44: "PrtSc", 45: "Ins", 46: "Del",
      0x6A: "Num*", 0x6B: "Num+", 0x6D: "Num-", 0x6E: "Num.", 0x6F: "Num/", 0x91: "ScrollLock", 0xBA: ";",
      0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'"}


def vk_name(k):
    if k in VK:
        return VK[k]
    if 0x30 <= k <= 0x39 or 0x41 <= k <= 0x5A:
        return chr(k)
    if 0x70 <= k <= 0x87:
        return f"F{k - 0x6F}"
    if 0x60 <= k <= 0x69:
        return f"Num{k - 0x60}"
    return hex(k)


def dec_accel(b, wide):
    out, o, step = [], 0, (8 if wide else 5)
    while o + step <= len(b) and len(out) < 2000:
        f, k, cmd = struct.unpack_from("<HHH" if wide else "<BHH", b, o)
        o += step
        mods = "".join(m for bit, m in ((0x8, "Ctrl+"), (0x10, "Alt+"), (0x4, "Shift+")) if f & bit)
        if f & 1:
            key = vk_name(k)
        elif 0 < k < 32:
            key = "Ctrl+" + chr(k + 64)
        else:
            key = chr(k)
        out.append({"keys": mods + key, "command": cmd})
        if f & 0x80:
            break
    return out


def dec_strings(b, block, wide):
    out, o = {}, 0
    for k in range(16):
        if wide:
            n = u16(b, o)
            s = b[o + 2:o + 2 + 2 * n].decode("utf-16le", "replace")
            o += 2 + 2 * n
        else:
            n = b[o]
            s = b[o + 1:o + 1 + n].decode("cp1252", "replace")
            o += 1 + n
        if n:
            out[(block - 1) * 16 + k] = s
    return out


def dec_msgtable(b):
    out = {}
    for k in range(min(u32(b, 0), 4096)):
        lo, hi, o = struct.unpack_from("<III", b, 4 + 12 * k)
        for mid in range(lo, min(hi, lo + 100000) + 1):
            ln, fl = struct.unpack_from("<HH", b, o)
            if ln < 4:
                break
            out[mid] = b[o + 4:o + ln].decode("utf-16le" if fl & 1 else "cp1252", "replace").rstrip("\0\r\n ")
            o += ln
    return out


def dec_version(b, wide):
    def node(o):
        ln, vl = u16(b, o), u16(b, o + 2)
        typ = u16(b, o + 4) if wide else 0
        key, p = rdsz(b, o + (6 if wide else 4), wide)
        p = (p + 3) & ~3
        end = min(o + ln, len(b))
        n = {"key": key, "value_at": p, "vlen": vl, "text": typ == 1, "children": []}
        if vl:
            p = (p + (vl * 2 if wide and typ == 1 else vl) + 3) & ~3
        while p + 6 <= end and len(n["children"]) < 500:
            child, nxt = node(p)
            n["children"].append(child)
            if nxt <= p:
                break
            p = (nxt + 3) & ~3
        return n, o + max(ln, 1)

    root, _ = node(0)
    out = {}
    v = root["value_at"]
    if root["vlen"] >= 52 and u32(b, v) == 0xFEEF04BD:
        fms, fls, pms, pls = struct.unpack_from("<IIII", b, v + 8)
        out["file_version"] = f"{fms >> 16}.{fms & 0xffff}.{fls >> 16}.{fls & 0xffff}"
        out["product_version"] = f"{pms >> 16}.{pms & 0xffff}.{pls >> 16}.{pls & 0xffff}"
    for ch in root["children"]:
        if ch["key"] == "StringFileInfo":
            for table in ch["children"]:
                strs = {}
                for s in table["children"]:
                    if s["vlen"]:
                        strs[s["key"]] = rdsz(b, s["value_at"], wide)[0].strip()
                out.setdefault("strings", {})[table["key"]] = strs
        elif ch["key"] == "VarFileInfo":
            for var in ch["children"]:
                if var["key"] == "Translation" and var["vlen"] >= 4:
                    out["translations"] = ["%04x-%04x" % struct.unpack_from("<HH", b, var["value_at"] + 4 * k)
                                           for k in range(var["vlen"] // 4)]
    return out


def img_dims(body):
    """(width, height, bits) of an icon/cursor image. A DIB's height includes the AND mask."""
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack_from(">II", body, 16)
        return w, h, 32
    if u32(body, 0) == 12:
        w, h, _, bpp = struct.unpack_from("<HHHH", body, 4)
        return w, h // 2, bpp
    w, h, _, bpp = struct.unpack_from("<iiHH", body, 4)
    return w, abs(h) // 2, bpp


def pack_ico(ents, ftype):
    hdr = struct.pack("<HHH", 0, ftype, len(ents))
    off, dirs, blob = 6 + 16 * len(ents), b"", b""
    for e in ents:
        f1, f2 = e["hotspot"] if ftype == 2 else (e.get("planes", 1), e["bits"])
        dirs += struct.pack("<BBBBHHII", e["w"] & 255, e["h"] & 255, e.get("colors", 0) & 255, 0, f1, f2,
                            len(e["body"]), off)
        off += len(e["body"])
        blob += e["body"]
    return hdr + dirs + blob


def group_entries(gb, parts, cursor):
    """RT_GROUP_ICON/RT_GROUP_CURSOR directory + its RT_ICON/RT_CURSOR parts -> image entries."""
    n = u16(gb, 4)
    ents = []
    for k in range(min(n, 64)):
        e = gb[6 + 14 * k:20 + 14 * k]
        if len(e) < 14:
            break
        rid = u16(e, 12)
        data = parts.get(rid)
        if not data or len(data) < 16:
            continue
        if cursor:
            hot, body = struct.unpack_from("<HH", data, 0), data[4:]
            w, h, bits = img_dims(body)
            ents.append({"w": w, "h": h, "bits": bits, "hotspot": hot, "body": body, "part": rid})
        else:
            w, h, bits = img_dims(data)
            ents.append({"w": w, "h": h, "bits": bits, "colors": e[2], "planes": u16(e, 4) or 1,
                         "body": data, "part": rid})
    return ents


def dib_to_bmp(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return b, ".png"
    hsz = u32(b, 0)
    if hsz == 12:
        bpp = u16(b, 10)
        pal = 3 * (1 << bpp if bpp <= 8 else 0)
    elif 40 <= hsz <= 124:
        bpp, comp, clr = u16(b, 14), u32(b, 16), u32(b, 32)
        pal = 4 * (clr or (1 << bpp if bpp <= 8 else 0))
        if hsz == 40 and comp in (3, 6):
            pal += 12 if comp == 3 else 16  # BI_BITFIELDS masks follow a plain header
    else:
        raise Bad(f"unknown bitmap header size {hsz}")
    return b"BM" + struct.pack("<IHHI", 14 + len(b), 0, 0, 14 + hsz + pal) + b, ".bmp"


SNIFF = ((b"\x89PNG", ".png"), (b"GIF8", ".gif"), (b"\xff\xd8\xff", ".jpg"), (b"BM", ".bmp"), (b"MZ", ".exe"),
         (b"<?xml", ".xml"), (b"\xef\xbb\xbf<?xml", ".xml"), (b"PK\x03\x04", ".zip"), (b"MSFT", ".tlb"),
         (b"\0\0\1\0", ".ico"), (b"%PDF", ".pdf"))


def sniff_ext(data, tname):
    if data[:4] == b"RIFF":
        return {b"WAVE": ".wav", b"AVI ": ".avi", b"ACON": ".ani"}.get(data[8:12], ".riff")
    for magic, ext in SNIFF:
        if data.startswith(magic):
            return ext
    if re.match(rb"\s*<(!doctype\s+)?html", data[:200], re.I):
        return ".htm"
    return {"html": ".htm", "manifest": ".xml", "anicursor": ".ani", "aniicon": ".ani", "font": ".fnt"}.get(tname, ".bin")


def cmd_pe(a):
    src = Path(a.file)
    d = src.read_bytes()
    info, res, wide = read_pe(d)
    out = default_out(src, a.out)
    out.mkdir(parents=True, exist_ok=True)
    Image = pil()
    errors = list(info.get("warnings", []))
    langs = Counter(r["lang"] for r in res)
    primary = langs.most_common(1)[0][0] if langs else 0

    def tname(t):
        return RT.get(t, str(t)) if isinstance(t, int) else str(t)

    def key(r):
        return str(r["name"]) if r["lang"] == primary else f"{r['name']}@{r['lang']}"

    index, menus, dialogs, accels, toolbars, versions, msgs = [], {}, {}, {}, {}, {}, {}
    strings: dict = {}
    other_strings: dict = {}
    parts = {"icon": {}, "cursor": {}}
    for r in res:  # only parts, the groups become files
        tn = tname(r["type"])
        if tn in parts and isinstance(r["name"], int):
            if r["name"] not in parts[tn] or r["lang"] == primary:
                parts[tn][r["name"]] = r["data"]
    files_of = {}
    for r in res:
        tn, b, k = tname(r["type"]), r["data"], key(r)
        ent = {"type": tn, "name": r["name"], "lang": r["lang"], "bytes": len(b)}
        files = []
        try:
            if tn in ("icon", "cursor"):
                ent["note"] = f"one image of a {tn} group (see group_{tn})"
            elif tn == "string" and isinstance(r["name"], int):
                (strings if r["lang"] == primary else other_strings.setdefault(LANGS.get(r["lang"], str(r["lang"])), {})) \
                    .update(dec_strings(b, r["name"], wide))
            elif tn == "messagetable":
                msgs.update(dec_msgtable(b))
            elif tn == "menu":
                menus[k] = dec_menu(b, wide)
            elif tn == "dialog":
                dialogs[k] = dec_dialog(b, wide)
            elif tn == "accelerator":
                accels[k] = dec_accel(b, wide)
            elif tn == "version":
                versions[k] = dec_version(b, wide)
            elif tn == "toolbar":
                ver, w, h, n = struct.unpack_from("<HHHH", b, 0)
                toolbars[k] = {"button_w": w, "button_h": h, "ids": [u16(b, 8 + 2 * i) for i in range(min(n, 500))]}
            elif tn in ("group_icon", "group_cursor"):
                cur = tn == "group_cursor"
                ents = group_entries(b, parts["cursor" if cur else "icon"], cur)
                stem = f"{'cursor' if cur else 'icon'}_{safe_part(k)}"
                sub = out / ("cursors" if cur else "icons")
                sub.mkdir(exist_ok=True)
                f = sub / (stem + (".cur" if cur else ".ico"))
                f.write_bytes(pack_ico(ents, 2 if cur else 1))
                files.append(rel(f, out))
                ent["images"] = [{"size": f"{e['w']}x{e['h']}", "bits": e["bits"],
                                  **({"hotspot": list(e["hotspot"])} if cur else {})} for e in ents]
                if Image:
                    for e in ents:
                        pf = sub / f"{stem}_{e['w']}x{e['h']}_{e['bits']}bit.png"
                        try:  # Pillow reads one-image .ico best, cursors too
                            Image.open(io.BytesIO(pack_ico([e], 1))).convert("RGBA").save(pf)
                            files.append(rel(pf, out))
                        except Exception as ex:  # noqa: BLE001 - one odd image must not stop the rest
                            errors.append(f"{stem} {e['w']}x{e['h']}: no PNG ({ex})")
            elif tn == "bitmap":
                data, ext = dib_to_bmp(b)
                (out / "bitmaps").mkdir(exist_ok=True)
                f = out / "bitmaps" / f"bitmap_{safe_part(k)}{ext}"
                f.write_bytes(data)
                files.append(rel(f, out))
                if ext == ".bmp" and Image:
                    try:
                        im = Image.open(f)
                        ent["size"] = f"{im.width}x{im.height}"
                        ent["bits"] = u16(b, 14) if u32(b, 0) >= 40 else u16(b, 10)
                        im.convert("RGBA" if im.mode in ("RGBA", "LA", "PA") else "RGB").save(f.with_suffix(".png"))
                        files.append(rel(f.with_suffix(".png"), out))
                    except Exception as ex:  # noqa: BLE001
                        errors.append(f"bitmap {k}: no PNG ({ex})")
            elif tn == "manifest":
                f = out / f"manifest_{safe_part(k)}.xml"
                f.write_bytes(b)
                files.append(rel(f, out))
            else:
                raise LookupError
        except LookupError:  # no decoder, keep raw
            pass
        except Exception as ex:  # noqa: BLE001 - a damaged resource is reported, the rest still decode
            errors.append(f"{tn} {k}: could not decode ({type(ex).__name__}: {ex}); kept raw")
            ent["error"] = str(ex)
        if not files and tn not in ("icon", "cursor") and (ent.get("error") or tn not in (
                "string", "messagetable", "menu", "dialog", "accelerator", "version", "toolbar")):
            f = out / "raw" / safe_part(tn) / (safe_part(k) + sniff_ext(b, tn))
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b)
            files.append(rel(f, out))
        ent["files"] = files
        files_of[(tn, k)] = files
        index.append(ent)

    # a string with a menu id is MFC's "Prompt\nTip"
    # accelerators and toolbar buttons get their menu command
    labels = {}

    def enrich(items, path):
        for it in items:
            if "items" in it:
                enrich(it["items"], path + [it.get("label", "")])
            elif "id" in it:
                labels.setdefault(it["id"], " > ".join(path + [it["label"]]))
                s = strings.get(it["id"])
                if s:
                    prompt, _, tip = s.partition("\n")
                    if prompt:
                        it["prompt"] = prompt
                    if tip:
                        it["tooltip"] = tip
    for m in menus.values():
        enrich(m, [])
    for lst in accels.values():
        for x in lst:
            if x["command"] in labels:
                x["menu"] = labels[x["command"]]
    for k, tb in toolbars.items():
        buttons = []
        for i in tb["ids"]:
            if i == 0:
                buttons.append({"separator": True})
                continue
            bt = {"id": i}
            if i in labels:
                bt["menu"] = labels[i]
            s = strings.get(i, "")
            if "\n" in s:
                bt["tooltip"] = s.partition("\n")[2]
            if s:
                bt["prompt"] = s.partition("\n")[0]
            buttons.append(bt)
        tb["buttons"] = buttons
        del tb["ids"]
        if ("bitmap", k) in files_of:
            tb["bitmap"] = [f for f in files_of[("bitmap", k)] if f.endswith(".png")] or files_of[("bitmap", k)]

    written = {}
    for name, obj in (("menus", menus), ("dialogs", dialogs), ("accelerators", accels), ("toolbars", toolbars),
                      ("version", versions), ("messagetable", {str(k): v for k, v in sorted(msgs.items())})):
        if obj:
            written[name] = rel(Path(wjson(out / f"{name}.json", obj)), out)
    if strings:
        written["strings"] = rel(Path(wjson(out / "strings.json", {str(k): v for k, v in sorted(strings.items())})), out)
    for lang, tbl in other_strings.items():
        wjson(out / f"strings.{safe_part(lang)}.json", {str(k): v for k, v in sorted(tbl.items())})
    ui = [{"id": k, "text": v} for k, v in sorted({**msgs, **strings}.items()) if looks_ui(v)]
    if ui:
        written["ui_strings"] = rel(Path(wjson(out / "strings_ui.json", ui)), out)
    counts = Counter(e["type"] for e in index)

    def count_items(items):
        return sum(count_items(i["items"]) if "items" in i else (0 if i.get("separator") else 1) for i in items)

    main_menu = max(menus.items(), key=lambda kv: count_items(kv[1]))[0] if menus else None
    holds = {}
    if menus:
        holds["menu"] = (f"{len(menus)} menus, {sum(count_items(m) for m in menus.values())} commands; the biggest "
                         f"(#{main_menu}) reads: " + ", ".join(i.get("label", "") for i in menus[main_menu] if "label" in i))
    if dialogs:
        holds["dialog"] = f"{len(dialogs)} dialogs: " + "; ".join(
            f"#{k} '{v.get('caption', '')}' ({len(v.get('controls', []))} controls)" for k, v in list(dialogs.items())[:40])
    if strings:
        holds["string"] = f"{len(strings)} strings, {len(ui)} look like UI text (strings_ui.json)"
    for tn, word in (("group_icon", "icons"), ("group_cursor", "cursors")):
        if counts.get(tn):
            sizes = sorted({im["size"] for e in index if e["type"] == tn for im in e.get("images", [])})
            holds[tn] = f"{counts[tn]} {word} (.{'ico' if tn == 'group_icon' else 'cur'} + PNG per image); sizes {', '.join(sizes)}"
    if counts.get("bitmap"):
        holds["bitmap"] = f"{counts['bitmap']} bitmaps in bitmaps/: " + ", ".join(
            f"{e['name']} {e.get('size', '?')}" for e in index if e["type"] == "bitmap")[:600]
    if accels:
        holds["accelerator"] = f"{sum(len(v) for v in accels.values())} shortcuts in {len(accels)} tables"
    if toolbars:
        holds["toolbar"] = "; ".join(f"#{k}: {len(v['buttons'])} buttons of {v['button_w']}x{v['button_h']}"
                                     for k, v in toolbars.items())
    ver = next(iter(versions.values()), {})
    vstr = next(iter(ver.get("strings", {}).values()), {})
    summary = {
        "file": str(src), "out": str(out), **{k: v for k, v in info.items() if k != "warnings"},
        "product": vstr.get("ProductName"), "file_description": vstr.get("FileDescription"),
        "version": vstr.get("FileVersion") or ver.get("file_version"), "company": vstr.get("CompanyName"),
        "languages": [LANGS.get(x, x) for x in langs], "resources": len(index), "counts": dict(counts),
        "holds": holds, "menu_bar": [i.get("label") for i in menus.get(main_menu, []) if "label" in i] if menus else [],
        "ui_text_sample": [u["text"] for u in ui[:25]], "files": written,
        "note": "dialog positions are dialog units (DLU): x DLU = 1/4 of the font's average character width, y DLU = "
                "1/8 of its height; for MS Sans Serif 8 at 96 dpi about 1.5 px across and 1.625 px down",
        "warnings": errors[:50]}
    wjson(out / "index.json", index)
    wjson(out / "summary.json", summary)
    return summary


# ---------------------------------------------------------------- HTML Help (.chm) with LZX

def encint(b, o):
    v = 0
    while True:
        c = b[o]
        o += 1
        v = (v << 7) | (c & 0x7f)
        if not c & 0x80:
            return v, o


class Bits:
    """LZX bit reader: 16-bit little-endian words, most significant bit first."""
    __slots__ = ("b", "p", "buf", "n")

    def __init__(self, b, pos):
        self.b, self.p, self.buf, self.n = b, pos, 0, 0

    def ensure(self, k):
        while self.n < k:
            p, b = self.p, self.b
            w = (b[p] | (b[p + 1] << 8)) if p + 1 < len(b) else (b[p] if p < len(b) else 0)
            self.p = p + 2
            self.buf = (self.buf << 16) | w
            self.n += 16

    def get(self, k):
        if k == 0:
            return 0
        self.ensure(k)
        self.n -= k
        v = self.buf >> self.n
        self.buf &= (1 << self.n) - 1
        return v

    def peek(self, k):
        self.ensure(k)
        return self.buf >> (self.n - k)

    def skip(self, k):
        self.n -= k
        self.buf &= (1 << self.n) - 1


def huff(lens):
    """Canonical Huffman lengths -> (table indexed by maxlen bits, maxlen). Entries are sym<<5 | length."""
    maxl = max(lens) if lens else 0
    if maxl == 0:
        return None
    table = [-1] * (1 << maxl)
    code = 0
    for L in range(1, maxl + 1):
        span = 1 << (maxl - L)
        for sym, ln in enumerate(lens):
            if ln == L:
                st = code << (maxl - L)
                if st + span > len(table):
                    raise Bad("damaged LZX data (over-full Huffman table)")
                table[st:st + span] = [(sym << 5) | L] * span
                code += 1
        code <<= 1
    return table, maxl


def hdecode(bs, t):
    if t is None:
        raise Bad("damaged LZX data (empty Huffman table used)")
    table, maxl = t
    e = table[bs.peek(maxl)]
    if e < 0:
        raise Bad("damaged LZX data (bad Huffman code)")
    bs.skip(e & 31)
    return e >> 5


def read_lengths(bs, lens, a, b):
    pre = huff([bs.get(4) for _ in range(20)])
    x = a
    while x < b:
        z = hdecode(bs, pre)
        if z == 17:
            run, val = bs.get(4) + 4, 0
        elif z == 18:
            run, val = bs.get(5) + 20, 0
        elif z == 19:
            run = bs.get(1) + 4
            val = (lens[x] - hdecode(bs, pre)) % 17
        else:
            run, val = 1, (lens[x] - z) % 17
        for _ in range(min(run, b - x)):
            lens[x] = val
            x += 1


def lzx_decompress(comp, resets, frames_per_reset, ulen, wbits):
    """Decode a CHM LZX stream, whole output as window. resets holds each reset's compressed offset."""
    extra, base = [0] * 52, [0] * 52
    j = 0
    for i in range(0, 52, 2):
        extra[i] = extra[i + 1] = j
        if i != 0 and j < 17:
            j += 1
    j = 0
    for i in range(52):
        base[i] = j
        j += 1 << extra[i]
    nslots = {15: 30, 16: 32, 17: 34, 18: 36, 19: 38, 20: 42, 21: 50}.get(wbits)
    if not nslots:
        raise Bad(f"unsupported LZX window (2^{wbits})")
    out = bytearray()
    FRAME = 0x8000
    nframes = (ulen + FRAME - 1) // FRAME
    e8_size = 0
    for ri, frame in enumerate(range(0, nframes, frames_per_reset)):
        if ri >= len(resets):
            raise Bad("LZX reset table is shorter than the data")
        bs = Bits(comp, resets[ri])
        R = [1, 1, 1]
        main_len, len_len = [0] * (256 + nslots * 8), [0] * 249
        if bs.get(1):
            e8_size = (bs.get(16) << 16) | bs.get(16)
        remain, btype, blen = 0, 0, 0
        mainT = lenT = alT = None
        for f in range(frame, min(frame + frames_per_reset, nframes)):
            target = min(ulen, (f + 1) * FRAME)
            while len(out) < target:
                if remain == 0:
                    if btype == 3:
                        if blen & 1:
                            bs.p += 1
                        bs.buf, bs.n = 0, 0
                    btype = bs.get(3)
                    blen = remain = (bs.get(16) << 8) | bs.get(8)
                    if btype == 2:
                        alT = huff([bs.get(3) for _ in range(8)])
                    if btype in (1, 2):
                        read_lengths(bs, main_len, 0, 256)
                        read_lengths(bs, main_len, 256, len(main_len))
                        mainT = huff(main_len)
                        read_lengths(bs, len_len, 0, 249)
                        lenT = huff(len_len)
                    elif btype == 3:
                        if bs.n == 0:
                            bs.ensure(16)
                        bs.buf, bs.n = 0, 0
                        R = list(struct.unpack_from("<III", comp, bs.p))
                        bs.p += 12
                    else:
                        raise Bad(f"damaged LZX data (block type {btype} in frame {f})")
                    if remain == 0:
                        raise Bad("damaged LZX data (empty block)")
                run = min(remain, target - len(out))
                if btype == 3:
                    out += comp[bs.p:bs.p + run]
                    bs.p += run
                    remain -= run
                    continue
                while run > 0:
                    m = hdecode(bs, mainT)
                    if m < 256:
                        out.append(m)
                        run -= 1
                        remain -= 1
                        continue
                    m -= 256
                    ml = m & 7
                    if ml == 7:
                        ml += hdecode(bs, lenT)
                    ml += 2
                    mo = m >> 3
                    if mo > 2:
                        if btype == 2 and extra[mo] >= 3:
                            eb = extra[mo]
                            mo = base[mo] - 2 + ((bs.get(eb - 3) << 3) if eb > 3 else 0) + hdecode(bs, alT)
                        elif mo != 3:
                            mo = base[mo] - 2 + bs.get(extra[mo])
                        else:
                            mo = 1
                        R = [mo, R[0], R[1]]
                    elif mo == 0:
                        mo = R[0]
                    elif mo == 1:
                        mo = R[1]
                        R = [mo, R[0], R[2]]
                    else:
                        mo = R[2]
                        R = [mo, R[1], R[0]]
                    st = len(out) - mo
                    if st < 0:
                        raise Bad("damaged LZX data (match before start)")
                    if mo >= ml:
                        out += out[st:st + ml]
                    else:
                        for k in range(ml):
                            out.append(out[st + k])
                    run -= ml
                    remain -= ml
            bs.n -= bs.n & 15  # every frame ends on a 16-bit boundary
            bs.buf &= (1 << bs.n) - 1
    del out[ulen:]
    if e8_size:  # undo x86 CALL translation on a copy, window stays encoded
        fixed = bytearray(out)
        for f in range(min(nframes, 32768)):
            s, e = f * FRAME, min(len(out), (f + 1) * FRAME)
            i = s
            while i < e - 10:
                if out[i] == 0xE8:
                    absv = struct.unpack_from("<i", out, i + 1)[0]
                    if -i <= absv < e8_size:
                        struct.pack_into("<i", fixed, i + 1, absv - i if absv >= 0 else absv + e8_size)
                    i += 5
                else:
                    i += 1
        out = fixed
    return bytes(out)


def chm_open(d):
    if d[:4] != b"ITSF":
        raise Bad("not a CHM file (no ITSF header)")
    ver = u32(d, 4)
    s0off, s0len, s1off, s1len = struct.unpack_from("<QQQQ", d, 0x38)
    content = struct.unpack_from("<Q", d, 0x58)[0] if ver >= 3 and u32(d, 8) >= 0x60 else s1off + s1len
    if d[s1off:s1off + 4] != b"ITSP":
        raise Bad("CHM directory header missing (damaged file)")
    hl, chunk, nchunks = u32(d, s1off + 8), u32(d, s1off + 0x10), u32(d, s1off + 0x2c)
    entries = {}
    for i in range(min(nchunks, 1 << 20)):
        c = d[s1off + hl + i * chunk:s1off + hl + (i + 1) * chunk]
        if c[:4] != b"PMGL":
            continue
        o, end = 0x14, chunk - u32(c, 4)
        try:
            while o < end:
                n, o = encint(c, o)
                name = c[o:o + n].decode("utf-8", "replace")
                o += n
                sec, o = encint(c, o)
                off, o = encint(c, o)
                ln, o = encint(c, o)
                entries[name] = (sec, off, ln)
        except IndexError:
            pass
    if not entries:
        raise Bad("CHM directory is empty or damaged")
    return content, entries


def parse_sitemap(text):
    """.hhc/.hhk sitemap -> nested [{name, local, children}]"""
    root = []
    stack, cur, last = [root], None, None
    for m in re.finditer(r"<\s*(/?)\s*(ul|object|param)\b([^>]*)>", text, re.I):
        close, tag, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if tag == "ul" and not close:
            if last is not None and stack[-1] and stack[-1][-1] is last:
                stack.append(last.setdefault("children", []))
            else:
                stack.append(stack[-1])
        elif tag == "ul" and close and len(stack) > 1:
            stack.pop()
        elif tag == "object" and not close:
            cur = {}
        elif tag == "param" and cur is not None:
            n = re.search(r'name\s*=\s*"([^"]*)"', attrs, re.I)
            v = re.search(r'value\s*=\s*"([^"]*)"', attrs, re.I)
            if n and v:
                k = n.group(1).lower()
                if k in ("name", "local", "url") and k not in cur:
                    cur[k] = html.unescape(v.group(1))
        elif tag == "object" and close and cur is not None:
            if cur.get("name") or cur.get("local"):
                stack[-1].append(cur)
                last = cur
            cur = None
    return root


def flat(items):
    for it in items:
        yield it
        yield from flat(it.get("children", []))


def html_text(b):
    try:
        t = b.decode("utf-8")
    except UnicodeDecodeError:
        t = b.decode("cp1252", "replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I)
    body = re.sub(r"<(script|style|head)\b.*?</\1>", " ", t, flags=re.S | re.I)
    return (strip_html(title.group(1)) if title else ""), strip_html(body)


def cmd_chm(a):
    src = Path(a.file)
    d = src.read_bytes()
    content, entries = chm_open(d)
    out = default_out(src, a.out)
    out.mkdir(parents=True, exist_ok=True)
    warnings = []

    def raw(name):
        s, o, ln = entries[name]
        if s != 0:
            raise Bad(f"{name} is not stored uncompressed")
        return d[content + o:content + o + ln]

    section1 = b""
    if any(s == 1 for s, _, _ in entries.values()):
        try:
            cd = raw("::DataSpace/Storage/MSCompressed/ControlData")
            lver, reset_int, win = struct.unpack_from("<III", cd, 8)
            if lver == 2:
                reset_int *= 0x8000
                win *= 0x8000
            rt = raw("::DataSpace/Storage/MSCompressed/Transform/{7FC28940-9D31-11D0-9B27-00A0C91E9C7C}/"
                     "InstanceData/ResetTable")
            _, nent, _, rhl = struct.unpack_from("<IIII", rt, 0)
            ulen, _, bsize = struct.unpack_from("<QQQ", rt, 16)
            offs = [struct.unpack_from("<Q", rt, rhl + 8 * k)[0] for k in range(nent)]
            frames_per_reset = max(1, reset_int // 0x8000)
            step = max(1, reset_int // max(bsize, 1))  # reset table rows per reset interval
            section1 = lzx_decompress(raw("::DataSpace/Storage/MSCompressed/Content"), offs[::step], frames_per_reset,
                                      ulen, win.bit_length() - 1)
        except (Bad, KeyError, struct.error, IndexError) as ex:
            warnings.append(f"compressed section unreadable ({ex}); only uncompressed files were saved")
    index, sysinfo = [], {}
    for name, (s, o, ln) in sorted(entries.items()):
        if name.endswith("/") or name.startswith("::"):
            continue
        data = d[content + o:content + o + ln] if s == 0 else section1[o:o + ln]
        if len(data) < ln:
            index.append({"file": name, "bytes": ln, "error": "missing"})
            continue
        if name.startswith(("/#", "/$")):
            if name == "/#SYSTEM":
                p = 4
                while p + 4 <= len(data):
                    code, sz = struct.unpack_from("<HH", data, p)
                    val = data[p + 4:p + 4 + sz].split(b"\0")[0].decode("cp1252", "replace")
                    nm = {0: "contents_file", 1: "index_file", 2: "default_topic", 3: "title", 6: "compiled_file",
                          9: "compiler", 16: "default_font"}.get(code)
                    if nm and val:
                        sysinfo[nm] = val
                    p += 4 + sz
            continue  # other internal tables just index the files saved below
        f = out / "files" / safe_rel(name)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        index.append({"file": name, "bytes": ln, "saved": rel(f, out)})
    files = out / "files"
    toc, keywords, topics = [], [], []
    for f in sorted(files.rglob("*")) if files.exists() else []:
        low = f.suffix.lower()
        if low == ".hhc":
            toc = parse_sitemap(f.read_bytes().decode("cp1252", "replace"))
        elif low == ".hhk":
            keywords = [{"keyword": it.get("name"), "topic": it.get("local")} for it in
                        flat(parse_sitemap(f.read_bytes().decode("cp1252", "replace")))]
        elif low in (".htm", ".html"):
            title, text = html_text(f.read_bytes())
            topics.append({"file": rel(f, files), "title": title, "words": len(text.split()), "text": text})
    wjson(out / "toc.json", toc)
    wjson(out / "keywords.json", keywords)
    wjson(out / "topics.json", topics)
    wjson(out / "index.json", index)
    kinds = Counter(Path(e["file"]).suffix.lower() or "(none)" for e in index)
    summary = {"file": str(src), "out": str(out), **sysinfo, "files": len(index), "by_extension": dict(kinds),
               "topics": len(topics), "toc_entries": sum(1 for _ in flat(toc)), "keywords": len(keywords),
               "topic_titles": [t["title"] or t["file"] for t in topics][:60],
               "saved": {"files": "files/", "toc": "toc.json", "keywords": "keywords.json", "topics": "topics.json"},
               "warnings": warnings}
    wjson(out / "summary.json", summary)
    return summary


# ---------------------------------------------------------------- WinHelp (.hlp)

def lz77(src):
    out, i, n = bytearray(), 0, len(src)
    while i < n:
        ctl = src[i]
        i += 1
        for bit in range(8):
            if i >= n:
                break
            if ctl & (1 << bit):
                if i + 1 >= n:
                    break
                w = src[i] | (src[i + 1] << 8)
                i += 2
                ln, st = (w >> 12) + 3, len(out) - ((w & 0xFFF) + 1)
                if st < 0:
                    raise Bad("damaged LZ77 data")
                for k in range(ln):
                    out.append(out[st + k])
            else:
                out.append(src[i])
                i += 1
    return bytes(out)


def hlp_file(d, off):
    _, used, _ = struct.unpack_from("<IIB", d, off)
    return d[off + 9:off + 9 + used]


def btree(data):
    """Leaf records of a WinHelp B+ tree, parsed with its own structure string (z=string, 4/L=long, 2=short)."""
    magic, _, pagesize = struct.unpack_from("<HHH", data, 0)
    if magic != 0x293B:
        raise Bad("not a WinHelp B-tree")
    structure = data[6:22].split(b"\0")[0].decode("latin-1")
    root, _, _, nlevels, total = struct.unpack_from("<HhHHI", data, 26)
    page = root
    for _ in range(max(nlevels - 1, 0)):
        page = u16(data, 38 + page * pagesize + 4)  # an index page's first child
    out, seen = [], set()
    while page != 0xFFFF and page not in seen and len(out) <= total:
        seen.add(page)
        o = 38 + page * pagesize
        if o + 8 > len(data):
            break
        _, n, _, nxt = struct.unpack_from("<HHHH", data, o)
        p = o + 8
        for _ in range(n):
            rec = []
            for ch in structure:
                if ch == "z":
                    s, p = asz(data, p)
                    rec.append(s)
                elif ch in "4L":
                    rec.append(u32(data, p))
                    p += 4
                elif ch in "2":
                    rec.append(u16(data, p))
                    p += 2
                elif ch in "1a":
                    rec.append(data[p])
                    p += 1
                else:
                    raise Bad(f"B-tree field type {ch!r} not handled")
            out.append(rec)
        page = nxt
    return out


def hall_phrases(pi, img):
    nent, _, imgsize, imgcsize = struct.unpack_from("<IIII", pi, 4)
    bits = u16(pi, 24) & 0xF
    state = {"mask": 0, "val": 0, "p": 28}

    def bit():
        state["mask"] = (state["mask"] << 1) & 0xFFFFFFFF
        if not state["mask"]:
            state["val"] = u32(pi, state["p"]) if state["p"] + 4 <= len(pi) else 0
            state["p"] += 4
            state["mask"] = 1
        return bool(state["val"] & state["mask"])

    offs = [0]
    for _ in range(min(nent, 100000)):
        n = 1
        while bit():
            n += 1 << bits
        for k in range(min(bits, 5)):
            if bit():
                n += 1 << k
        offs.append(offs[-1] + n)
    image = img if imgsize == imgcsize else lz77(img)
    return [image[offs[k]:offs[k + 1]] for k in range(len(offs) - 1)]


def old_phrases(p, minor):
    num = u16(p, 0)
    start = 4 if minor <= 16 else 8
    offs = [u16(p, start + 2 * k) for k in range(num + 1)]
    data = p[start + 2 * (num + 1):]
    if minor > 16:
        data = lz77(data)
    return [data[offs[k] - offs[0]:offs[k + 1] - offs[0]] for k in range(num)]


def expand(b, phrases, hall):
    out, i, n = bytearray(), 0, len(b)
    while i < n:
        c = b[i]
        i += 1
        if hall:
            if c & 1 == 0:
                k = c >> 1
            elif c & 3 == 1:
                k = 128 + (c >> 2) * 256 + (b[i] if i < n else 0)
                i += 1
            elif c & 7 == 3:
                out += b[i:i + (c >> 3) + 1]
                i += (c >> 3) + 1
                continue
            else:
                out += (b" " if c & 15 == 7 else b"\0") * ((c >> 4) + 1)
                continue
            out += phrases[k] if k < len(phrases) else b""
        elif 0 < c < 0x10 and i < n:
            k = 256 * (c - 1) + b[i]
            i += 1
            out += phrases[k >> 1] if (k >> 1) < len(phrases) else b""
            if k & 1:
                out.append(32)
        else:
            out.append(c)
    return bytes(out)


def cmd_hlp(a):
    src = Path(a.file)
    d = src.read_bytes()
    if len(d) < 16 or u32(d, 0) != 0x00035F3F:
        raise Bad("not a WinHelp file (magic 3F 5F 03 00 missing)")
    out = default_out(src, a.out)
    out.mkdir(parents=True, exist_ok=True)
    warnings = []
    dirlist = btree(hlp_file(d, u32(d, 4)))
    files = {}
    for name, off in dirlist:
        try:
            files[name] = hlp_file(d, off)
        except struct.error:
            warnings.append(f"internal file {name} lies past the end")
    for name, data in files.items():
        f = out / "internal" / safe_part(name.lstrip("|") or name)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    sysinfo, minor, flags = {}, 0, 0
    s = files.get("|SYSTEM", b"")
    if len(s) >= 12 and u16(s, 0) == 0x036C:
        minor, flags = u16(s, 2), u16(s, 10)
        sysinfo["winhelp"] = {15: "3.0", 21: "3.1", 27: "4.0", 33: "4.0 (MVB)"}.get(minor, f"minor {minor}")
        if minor <= 16:
            sysinfo["title"] = asz(s, 12)[0]
        else:
            p = 12
            while p + 4 <= len(s):
                typ, sz = struct.unpack_from("<HH", s, p)
                data = s[p + 4:p + 4 + sz]
                if typ in (1, 2, 8):
                    sysinfo[{1: "title", 2: "copyright", 8: "citation"}[typ]] = asz(data, 0)[0]
                elif typ == 4:
                    sysinfo.setdefault("macros", []).append(asz(data, 0)[0])
                elif typ == 6 and len(data) >= 11:
                    if asz(data, 2)[0].strip():
                        sysinfo.setdefault("windows", []).append(asz(data, 2)[0])
                elif typ == 10:
                    sysinfo["contents_file"] = asz(data, 0)[0]
                p += 4 + sz
    titles, keywords = [], []
    try:
        if "|TTLBTREE" in files:
            titles = [{"topic": r[0], "title": r[1]} for r in btree(files["|TTLBTREE"])]
        if "|KWBTREE" in files:
            keywords = [r[0] for r in btree(files["|KWBTREE"])]
    except (Bad, struct.error, IndexError) as ex:
        warnings.append(f"title/keyword tree unreadable: {ex}")
    topics = []
    try:
        hall = "|PhrIndex" in files and "|PhrImage" in files
        phrases = hall_phrases(files["|PhrIndex"], files["|PhrImage"]) if hall else (
            old_phrases(files["|Phrases"], minor) if "|Phrases" in files else [])
        compressed = minor > 16 and flags in (4, 8)
        bsize = 2048 if minor <= 16 or flags == 8 else 4096
        t = files.get("|TOPIC", b"")
        stream = bytearray()
        for k in range(0, len(t), bsize):
            payload = t[k + 12:k + bsize]
            stream += lz77(payload)[:16384] if compressed else payload
        p, cur = 0, None
        while p + 21 <= len(stream) and len(topics) < 20000:
            size, dlen2, _, _, dlen1, typ = struct.unpack_from("<IIIIIB", stream, p)
            if size < 21 or dlen1 < 21 or dlen1 > size or typ not in (1, 2, 0x20, 0x23):
                break
            ld2 = bytes(stream[p + dlen1:p + size])
            if dlen2 > len(ld2) and phrases:
                ld2 = expand(ld2, phrases, hall)[:dlen2]
            if typ == 2:
                cur = {"title": ld2.split(b"\0")[0].decode("cp1252", "replace"), "paragraphs": []}
                topics.append(cur)
            else:
                text = re.sub(r"\s+", " ", ld2.replace(b"\0", b"").decode("cp1252", "replace")).strip()
                if text:
                    if cur is None:
                        cur = {"title": "", "paragraphs": []}
                        topics.append(cur)
                    cur["paragraphs"].append(text)
            p += size
    except (Bad, struct.error, IndexError, KeyError) as ex:
        warnings.append(f"topic text only partly read: {ex}")
    contents = None
    for sib in src.parent.glob("*"):
        if sib.stem.lower() == src.stem.lower() and sib.suffix.lower() == ".cnt":
            lines = sib.read_bytes().decode("cp1252", "replace").splitlines()
            contents = [{"level": int(m.group(1)), "text": m.group(2).split("=")[0].strip(),
                         **({"topic": m.group(2).split("=", 1)[1].strip()} if "=" in m.group(2) else {})}
                        for m in (re.match(r"(\d+)\s+(.*)", x) for x in lines) if m]
            shutil.copyfile(sib, out / sib.name)
            wjson(out / "contents.json", contents)
    wjson(out / "titles.json", titles)
    wjson(out / "keywords.json", keywords)
    wjson(out / "topics.json", topics)
    untitled = "(untitled: a popup or What's This topic)"
    (out / "topics.txt").write_text("\n\n".join(f"## {t['title'] or untitled}\n" + "\n".join(t["paragraphs"])
                                                for t in topics), encoding="utf-8")
    index = [{"name": n, "bytes": len(b)} for n, b in files.items()]
    wjson(out / "index.json", index)
    summary = {"file": str(src), "out": str(out), **sysinfo, "internal_files": [f"{e['name']} ({e['bytes']} B)" for e in index],
               "topics": len(topics), "topic_titles": [t["title"] for t in topics if t["title"]][:60] or
               [t["title"] for t in titles if t["title"]][:60],
               "untitled_topics": sum(1 for t in topics if not t["title"]), "keywords": len(keywords), "contents_entries": len(contents or []),
               "pictures": [n for n in files if re.match(r"\|?bm\d+$", n)],
               "note": "|bmN pictures are WinHelp SHG/MRB images, saved raw in internal/; helpdeco converts them",
               "warnings": warnings}
    wjson(out / "summary.json", summary)
    return summary


# ---------------------------------------------------------------- macOS apps

class OldPlist:
    """Parse NeXTSTEP/OpenStep text plists and .strings files, comments included."""
    ESC = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}

    def __init__(self, t):
        self.t, self.i = t, 0

    def ws(self):
        t = self.t
        while self.i < len(t):
            if t[self.i].isspace():
                self.i += 1
            elif t.startswith("//", self.i):
                e = t.find("\n", self.i)
                self.i = len(t) if e < 0 else e + 1
            elif t.startswith("/*", self.i):
                e = t.find("*/", self.i + 2)
                self.i = len(t) if e < 0 else e + 2
            else:
                break

    def dict_body(self, end):
        d = {}
        while True:
            self.ws()
            if self.i >= len(self.t) or self.t[self.i] == end:
                self.i += 1
                return d
            k = self.value()
            self.ws()
            if self.t[self.i:self.i + 1] == "=":
                self.i += 1
                v = self.value()
                self.ws()
            else:
                v = k
            if self.t[self.i:self.i + 1] == ";":
                self.i += 1
            d[k] = v

    def value(self):
        self.ws()
        t = self.t
        c = t[self.i:self.i + 1]
        if c == "{":
            self.i += 1
            return self.dict_body("}")
        if c == "(":
            self.i += 1
            arr = []
            while True:
                self.ws()
                if self.i >= len(t) or t[self.i] == ")":
                    self.i += 1
                    return arr
                arr.append(self.value())
                self.ws()
                if t[self.i:self.i + 1] == ",":
                    self.i += 1
        if c == "<":
            e = t.find(">", self.i)
            hx = re.sub(r"\s", "", t[self.i + 1:e])
            self.i = e + 1
            return hx
        if c in "\"'":
            self.i += 1
            buf = []
            while self.i < len(t) and t[self.i] != c:
                ch = t[self.i]
                if ch == "\\" and self.i + 1 < len(t):
                    nx = t[self.i + 1]
                    if nx in "Uu" and re.match(r"[0-9A-Fa-f]{4}", t[self.i + 2:self.i + 6]):
                        buf.append(chr(int(t[self.i + 2:self.i + 6], 16)))
                        self.i += 6
                        continue
                    m = re.match(r"[0-7]{1,3}", t[self.i + 1:self.i + 4])
                    if m:
                        buf.append(chr(int(m.group(0), 8)))
                        self.i += 1 + len(m.group(0))
                        continue
                    buf.append(self.ESC.get(nx, nx))
                    self.i += 2
                    continue
                buf.append(ch)
                self.i += 1
            self.i += 1
            return "".join(buf)
        m = re.compile(r"[^\s{}()<>=;,\"']+").match(t, self.i)
        if not m:
            raise Bad(f"old-style plist: unexpected {c!r} at {self.i}")
        self.i = m.end()
        return m.group(0)

    @classmethod
    def parse(cls, t, strings=False):
        p = cls(t.lstrip("﻿"))
        p.ws()
        if strings and not t.lstrip("﻿").lstrip().startswith("{"):
            return p.dict_body("")
        return p.value()


def decode_text(b):
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16")
    if b[:3] == b"\xef\xbb\xbf":
        return b[3:].decode("utf-8", "replace")
    if len(b) > 1 and b.count(b"\0") > len(b) // 4:
        return b.decode("utf-16-le" if b[1:2] == b"\0" else "utf-16-be", "replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("mac_roman")


def load_plist(b, strings=False):
    if b[:6] == b"bplist" or b.lstrip()[:5] in (b"<?xml", b"<plis", b"<!DOC"):
        return plistlib.loads(b)
    t = decode_text(b)
    if t.lstrip("﻿").lstrip()[:5] in ("<?xml", "<plis", "<!DOC"):
        return plistlib.loads(t.lstrip("﻿").encode("utf-8"))
    return OldPlist.parse(t, strings)


def jsonable(v):
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, bytes):
        return {"bytes": len(v), "hex": v[:64].hex()}
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, plistlib.UID):
        return {"uid": v.data}
    return v


LABEL_KEYS = {"NSTitle", "NSContents", "NSToolTip", "NSPlaceholderString", "NSAlternateTitle", "NSLabel",
              "NSPaletteLabel", "NSKeyEquiv", "NSHeaderToolTip", "NSStringValue", "NSText", "NSMenuItemTitle",
              "UITitle", "UIText", "UIPlaceholder", "NSAccessibilityTitle", "NSWindowTitle", "NSAlternateContents"}


class Ref(int):
    """A reference to another object inside a nib."""


def nib_archive(b):
    """Parse Apple's NIBArchive (compiled .nib since 10.8) into strings, labels and the object graph.
    Graph objects are their class plus a (key, value) list, references as Ref."""
    def varint(o):
        v, sh = 0, 0
        while True:
            c = b[o]
            o += 1
            v |= (c & 0x7f) << sh
            sh += 7
            if c & 0x80:
                return v, o
    nobj, oobj, nkey, okey, nval, oval, ncls, ocls = struct.unpack_from("<8I", b, 18)
    keys, o = [], okey
    for _ in range(nkey):
        ln, o = varint(o)
        keys.append(b[o:o + ln].decode("utf-8", "replace"))
        o += ln
    classes, o = [], ocls
    for _ in range(ncls):
        ln, o = varint(o)
        nx, o = varint(o)
        o += 4 * nx
        classes.append(b[o:o + ln].rstrip(b"\0").decode("utf-8", "replace"))
        o += ln
    values, o = [], oval
    for _ in range(nval):
        k, o = varint(o)
        typ = b[o]
        o += 1
        if typ in (0, 1, 2, 3):
            size = (1, 2, 4, 8)[typ]
            v = int.from_bytes(b[o:o + size], "little")
            o += size
        elif typ in (4, 5, 9):
            v = None if typ == 9 else typ == 5
        elif typ in (6, 7):
            v = struct.unpack_from("<f" if typ == 6 else "<d", b, o)[0]
            o += 4 if typ == 6 else 8
        elif typ == 8:
            ln, o = varint(o)
            v = b[o:o + ln]
            o += ln
        elif typ == 10:
            v = Ref(u32(b, o))
            o += 4
        else:
            raise Bad(f"NIBArchive value type {typ}")
        values.append((keys[k] if k < len(keys) else k, v))
    nodes, o = [], oobj
    for _ in range(nobj):
        ci, o = varint(o)
        vi, o = varint(o)
        vc, o = varint(o)
        nodes.append((classes[ci] if ci < len(classes) else "?", values[vi:vi + vc]))
    objs = [(cls, dict(kv)) for cls, kv in nodes]
    strs = {}
    for i, (_cls, vals) in enumerate(objs):
        if isinstance(vals.get("NS.bytes"), bytes):
            strs[i] = vals["NS.bytes"].decode("utf-8", "replace")
    labels = []
    for cls, vals in objs:
        for k, v in vals.items():
            if isinstance(v, Ref) and strs.get(v) and not cls.endswith("Connector") and (
                    k in LABEL_KEYS or k.endswith(("Title", "Label", "ToolTip"))):
                labels.append({"class": cls, "key": k, "text": strs[v]})
    return [c for c, _ in objs], list(strs.values()), labels, nodes


def nib_keyed(top):
    """NSKeyedArchiver binary plist (.nib files from 10.5 to 10.7, and keyedobjects.nib in folders)."""
    objs = top.get("$objects", []) if isinstance(top, dict) else []

    def deref(v):
        return objs[v.data] if isinstance(v, plistlib.UID) and v.data < len(objs) else v

    def cname(o):
        c = deref(o.get("$class")) if isinstance(o, dict) else None
        return c.get("$classname", "?") if isinstance(c, dict) else "?"

    def text(v):
        v = deref(v)
        if isinstance(v, str):
            return v
        if isinstance(v, dict) and "NS.string" in v:
            return deref(v["NS.string"]) if isinstance(deref(v["NS.string"]), str) else None
        if isinstance(v, dict) and isinstance(v.get("NS.bytes"), bytes):
            return v["NS.bytes"].decode("utf-8", "replace")
        return None

    classes, strs, labels, nodes = [], [], [], []
    for o in objs:
        if isinstance(o, str) and o != "$null":
            strs.append(o)
            nodes.append(("NSString", [("NS.string", o)]))
        elif isinstance(o, dict) and "$class" in o:
            cls = cname(o)
            classes.append(cls)
            kv = []
            for k, v in o.items():
                if k == "$class":
                    continue
                if k == "NS.objects" and isinstance(v, list):
                    kv += [(k, Ref(x.data) if isinstance(x, plistlib.UID) else x) for x in v]
                else:
                    kv.append((k, Ref(v.data) if isinstance(v, plistlib.UID) else v))
                if k in LABEL_KEYS or k.endswith(("Title", "Label", "ToolTip")):
                    t = text(v)
                    if t and not cls.endswith("Connector"):
                        labels.append({"class": cls, "key": k, "text": t})
            nodes.append((cls, kv))
        else:
            nodes.append(("?", []))
    return classes, strs, labels, nodes


def nib_any(b):
    """(classes, strings, labels, object graph or None) from any compiled or source nib."""
    if b[:10] == b"NIBArchive":
        return nib_archive(b)
    if b[:6] == b"bplist":
        return nib_keyed(plistlib.loads(b))
    if b.lstrip()[:5] in (b"<?xml", b"<docu", b"<arch"):  # designable.nib is the source form, plain XML
        t = b.decode("utf-8", "replace")
        labels = [{"class": m.group(1), "key": m.group(2), "text": html.unescape(m.group(3))} for m in
                  re.finditer(r'<(\w+)[^>]*?\b(title|stringValue|placeholderString|toolTip|label)="([^"]+)"', t)]
        classes = re.findall(r'customClass="(\w+)"', t) + re.findall(r'<(\w+) ', t)
        return classes, [x["text"] for x in labels], labels, None
    # NeXT typedstream objects.nib, printable runs are all we get
    strs = [s.decode("mac_roman") for s in re.findall(rb"[\x20-\x7e\x80-\xff]{3,}", b)]
    return [s for s in strs if re.fullmatch(r"(NS|IB)[A-Z]\w+", s)], strs, [], None


KEY_NAMES = {"": "Up", "": "Down", "": "Left", "": "Right", "": "ForwardDelete",
             "": "Home", "": "End", "": "PageUp", "": "PageDown", "": "Help",
             "\x08": "Backspace", "\x7f": "Delete", "\r": "Return", "\x03": "Enter", "\x1b": "Esc", "\t": "Tab",
             "\x19": "BackTab", " ": "Space"}
MOD_BITS = ((1 << 23, "Fn"), (1 << 18, "Ctrl"), (1 << 19, "Opt"), (1 << 17, "Shift"), (1 << 20, "Cmd"))


def mac_shortcut(key: str, mask: int):
    """Menu key equivalent as text in Apple's modifier order ('Shift+Cmd+S', 'Opt+Cmd+Left', 'F5').
    Upper-case letters imply Shift, as in AppKit."""
    if not key:
        return None
    on = {name for bit, name in MOD_BITS if mask & bit}
    if len(key) == 1 and key.isalpha() and key.isupper():
        on.add("Shift")
    k = KEY_NAMES.get(key)
    if k is None:
        k = f"F{ord(key) - 0xF703}" if len(key) == 1 and 0xF704 <= ord(key) <= 0xF726 else key.upper()
    return "+".join([name for _, name in MOD_BITS if name in on] + [k])


def nib_menus(nodes):
    """Menus in a nib's object graph as trees, main menu first. Items carry title, shortcut, action and
    hidden/alternate/disabled/checked flags."""
    def first(i, key):
        return next((v for k, v in nodes[i][1] if k == key), None) if isinstance(i, int) and i < len(nodes) else None

    def text(v, depth=0):
        if not isinstance(v, Ref) or v >= len(nodes) or depth > 4:
            return v if isinstance(v, str) else None
        for k, x in nodes[v][1]:
            if k == "NS.bytes" and isinstance(x, bytes):
                return x.decode("utf-8", "replace")
            if k == "NS.string":
                return x if isinstance(x, str) else text(x, depth + 1)
        return None

    def items(v):
        if not isinstance(v, Ref) or v >= len(nodes):
            return []
        return [x for k, x in nodes[v][1] if k in ("UINibEncoderEmptyKey", "NS.objects") and isinstance(x, Ref)]

    actions, subs = {}, set()
    for cls, kv in nodes:
        d = dict(kv)
        if "Connector" in cls and "Outlet" not in cls and isinstance(d.get("NSSource"), Ref):
            label = text(d.get("NSLabel"))
            if label and (cls != "NSNibConnector" or label.endswith(":")):
                actions.setdefault(int(d["NSSource"]), label)
        if isinstance(d.get("NSSubmenu"), Ref):
            subs.add(int(d["NSSubmenu"]))

    def walk(m, seen):
        out = []
        for it in items(first(m, "NSMenuItems")):
            if "MenuItem" not in nodes[it][0]:
                continue
            d = dict(nodes[it][1])
            if d.get("NSIsSeparator"):
                out.append({"separator": True})
                continue
            e = {"title": text(d.get("NSTitle")) or ""}
            sc = mac_shortcut(text(d.get("NSKeyEquiv")) or "", d.get("NSKeyEquivModMask") or 0)
            if sc:
                e["shortcut"] = sc
            if it in actions:
                e["action"] = actions[it]
            for key, flag in (("NSIsHidden", "hidden"), ("NSIsAlternate", "alternate"), ("NSIsDisabled", "disabled")):
                if d.get(key):
                    e[flag] = True
            if d.get("NSState") == 1:
                e["checked"] = True
            if d.get("NSTag"):
                e["tag"] = d["NSTag"]
            sub = d.get("NSSubmenu")
            if isinstance(sub, Ref) and int(sub) not in seen and len(seen) < 64:
                e["submenu"] = walk(int(sub), seen | {int(sub)})
            out.append(e)
        return out

    menus = [i for i, (cls, kv) in enumerate(nodes) if any(k == "NSMenuItems" for k, _ in kv)]
    roots = [m for m in menus if m not in subs]
    main = [m for m in roots if text(first(m, "NSName")) == "_NSMainMenu"]
    rest = sorted((m for m in roots if m not in main), key=lambda m: -len(items(first(m, "NSMenuItems"))))
    return [{"menu": text(first(m, "NSTitle")) or "", **({"main_menu": True} if m in main else {}),
             "items": walk(m, {m})} for m in main + rest if items(first(m, "NSMenuItems"))]


def xib_menus(b):
    """Menus from a source .xib (or a designable.nib in that XML form)."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(b)
    except ET.ParseError:
        return []
    mods = {"control": 1 << 18, "option": 1 << 19, "shift": 1 << 17, "command": 1 << 20, "function": 1 << 23}

    def walk(menu):
        out = []
        for it in menu.findall("./items/menuItem"):
            if it.get("isSeparatorItem") == "YES":
                out.append({"separator": True})
                continue
            e = {"title": it.get("title", "")}
            mm = it.find("./modifierMask[@key='keyEquivalentModifierMask']")
            mask = 1 << 20 if mm is None else sum(bit for k, bit in mods.items() if mm.get(k) == "YES")
            sc = mac_shortcut(it.get("keyEquivalent", ""), mask)
            if sc:
                e["shortcut"] = sc
            act = it.find("./connections/action")
            if act is not None and act.get("selector"):
                e["action"] = act.get("selector")
            for attr, flag in (("hidden", "hidden"), ("alternate", "alternate")):
                if it.get(attr) == "YES":
                    e[flag] = True
            sub = it.find("./menu")
            if sub is not None:
                e["submenu"] = walk(sub)
            out.append(e)
        return out
    inner = {id(m) for it in root.iter("menuItem") for m in it.findall("./menu")}
    return [{"menu": m.get("title", ""), **({"main_menu": True} if m.get("systemMenu") == "main" else {}),
             "items": walk(m)} for m in root.iter("menu") if id(m) not in inner]


def count_menu_items(menus):
    return sum(1 + count_menu_items([{"items": e["submenu"]}]) if "submenu" in e else 1
               for m in menus for e in m["items"] if not e.get("separator"))


# ---- property lists with archives inside (presets, command sets, document templates)

NS_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def unarchive(top, depth=0):
    """NSKeyedArchiver plist as plain data. Unknown objects become {"$class": name, ...keys}, and archives
    or plists nested in bytes are decoded too."""
    objs = top.get("$objects", [])

    def cls_of(v):
        c = v.get("$class")
        c = objs[c.data] if isinstance(c, plistlib.UID) and c.data < len(objs) else c
        return c.get("$classname") if isinstance(c, dict) else None

    def r(v, stack):
        if isinstance(v, plistlib.UID):
            i = v.data
            if i >= len(objs):
                return None
            if i in stack or len(stack) > 60:
                return {"$ref": i}
            return r(objs[i], stack | {i})
        if isinstance(v, str):
            return None if v == "$null" else decoded(v, depth + 1)
        if isinstance(v, list):
            return [r(x, stack) for x in v]
        if not isinstance(v, dict):
            return decoded(v, depth + 1)
        if "NS.keys" in v and "NS.objects" in v:
            return {str(r(k, stack)): r(x, stack) for k, x in zip(v["NS.keys"], v["NS.objects"])}
        if "NS.objects" in v:
            return [r(x, stack) for x in v["NS.objects"]]
        if "NS.string" in v:
            return r(v["NS.string"], stack)
        for k in ("NS.bytes", "NS.data"):
            if k in v:
                return decoded(r(v[k], stack) if isinstance(v[k], plistlib.UID) else v[k], depth + 1)
        if "NS.time" in v:
            return (NS_EPOCH + timedelta(seconds=v["NS.time"])).isoformat()
        if "NS.relative" in v:
            return r(v["NS.relative"], stack)
        if "NS.uuidbytes" in v and isinstance(v["NS.uuidbytes"], bytes):
            return v["NS.uuidbytes"].hex()
        name = cls_of(v)
        out = {"$class": name} if name else {}
        for k, x in v.items():
            if k == "$class":
                continue
            if k in ("NSRGB", "NSWhite", "NSCMYK") and isinstance(x, bytes):
                out[k] = x.rstrip(b"\0").decode("ascii", "replace").strip()
            else:
                out[k] = r(x, stack)
        return out

    roots = top.get("$top", {})
    if isinstance(roots, dict) and set(roots) == {"root"}:
        return r(roots["root"], frozenset())
    return {str(k): r(x, frozenset()) for k, x in (roots.items() if isinstance(roots, dict) else [])}


def decoded(v, depth=0):
    """Plist value as JSON-ready data, decoding archives and plists hidden in bytes or base64."""
    if depth > 12:
        return jsonable(v)
    if isinstance(v, dict):
        if "$objects" in v and "$top" in v:
            try:
                return {"$archive": unarchive(v, depth + 1)}
            except (KeyError, TypeError, AttributeError, IndexError):
                pass
        return {str(k): decoded(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [decoded(x, depth + 1) for x in v]
    if isinstance(v, str) and len(v) > 16 and v.lstrip()[:8] in ("YnBsaXN0", "PD94bWwg", "PHBsaXN0"):
        with contextlib.suppress(ValueError):
            return decoded(base64.b64decode(v), depth + 1)
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        if b[:6] == b"bplist" or b.lstrip()[:5] in (b"<?xml", b"<plis"):
            with contextlib.suppress(Exception):
                return decoded(plistlib.loads(b), depth + 1)
        if b and len(b) <= 256 and re.fullmatch(rb"[\x20-\x7e\t\r\n]+", b):
            return {"text": b.decode("ascii")}
    return jsonable(v)


def top_keys(d):
    if isinstance(d, dict):
        return list(d)[:15]
    if isinstance(d, list):
        return [f"list of {len(d)}"] + (list(d[0])[:12] if d and isinstance(d[0], dict) else [])
    return [type(d).__name__]


# ---- images without Pillow: PNG writing, .icns entries, CoreUI asset catalogues (.car)

def png_write(path: Path, w: int, h: int, pixels: bytes, mode: str = "RGBA") -> Path:
    """Stdlib-only PNG from raw 8-bit pixels. mode is RGBA, RGB, LA or L."""
    ctype, bpp = {"L": (0, 1), "RGB": (2, 3), "LA": (4, 2), "RGBA": (6, 4)}[mode]
    stride = w * bpp
    raw = b"".join(b"\0" + pixels[y * stride:(y + 1) * stride] for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    return path


def unpremultiply(px: bytes, bpp: int) -> tuple[bytes, str]:
    """Premultiplied BGRA (bpp 4) or gray+alpha (bpp 2) pixels as straight RGBA / LA."""
    Image = _PIL if _PIL else None  # Pillow is faster here, but never required
    n = len(px) // bpp
    if Image:
        with contextlib.suppress(ValueError, OSError):
            if bpp == 4:
                return Image.frombuffer("RGBa", (1, n), px, "raw", "BGRa", 0, 1).convert("RGBA").tobytes(), "RGBA"
            return Image.frombuffer("La", (1, n), px, "raw", "La", 0, 1).convert("LA").tobytes(), "LA"
    out = bytearray(px[:n * bpp])
    if bpp == 4:  # BGRA -> RGBA by slices, math only for half-transparent pixels
        out[0::4], out[2::4] = px[2:n * 4:4], px[0:n * 4:4]
    alphas = px[bpp - 1:n * bpp:bpp]
    if alphas.translate(None, b"\x00\xff"):
        for j, a in enumerate(alphas):
            if 0 < a < 255:
                i, k = j * bpp, 255 / a
                for c in range(bpp - 1):
                    out[i + c] = min(255, round(out[i + c] * k))
    return bytes(out), "RGBA" if bpp == 4 else "LA"


ICNS_SIZES = {"ic04": (16, 1), "ic05": (32, 1), "icp4": (16, 1), "icp5": (32, 1), "icp6": (64, 1), "ic07": (128, 1),
              "ic08": (256, 1), "ic09": (512, 1), "ic10": (512, 2), "ic11": (16, 2), "ic12": (32, 2), "ic13": (128, 2),
              "ic14": (256, 2), "icsb": (18, 1), "icsB": (18, 2), "sb24": (24, 1), "SB24": (24, 2),
              "is32": (16, 1), "il32": (32, 1), "ih32": (48, 1), "it32": (128, 1)}
ICNS_MASKS = {"is32": "s8mk", "il32": "l8mk", "ih32": "h8mk", "it32": "t8mk"}


def icns_unpack(b: bytes, n: int) -> bytes:
    """Unpack .icns PackBits (24-bit and ARGB channels) to n bytes."""
    out, p = bytearray(), 0
    while len(out) < n and p < len(b):
        c = b[p]
        p += 1
        if c < 0x80:
            out += b[p:p + c + 1]
            p += c + 1
        else:
            out += b[p:p + 1] * (c - 125)
            p += 1
    return bytes(out[:n]).ljust(n, b"\0")


def icns_images(f: Path, dest_dir: Path) -> tuple[list, list]:
    """Every image in an .icns, read by hand -> (files, problems). PNG and JPEG 2000 kept as is (plus a sips
    PNG on macOS), ARGB and 24-bit RLE rebuilt as PNG with masks."""
    b = f.read_bytes()
    if b[:4] != b"icns":
        raise Bad("not an .icns file")
    ents, p = {}, 8
    while p + 8 <= len(b):
        typ, ln = b[p:p + 4].decode("latin-1"), struct.unpack_from(">I", b, p + 4)[0]
        if ln < 8:
            break
        ents[typ] = b[p + 8:p + ln]
        p += ln
    made, problems, names = [], [], set()
    stem = safe_part(f.stem)

    def target(px, scale, ext):
        return dest_dir / f"{stem}_{px}x{px}{'@%dx' % scale if scale > 1 else ''}{ext}"

    def rank(typ):  # PNG beats ARGB beats old RLE, one file per size
        d = ents[typ]
        return 0 if d[:4] == b"\x89PNG" else 1 if d[:4] == b"ARGB" else 2
    for typ in sorted(ents, key=rank):
        d = ents[typ]
        size, scale = ICNS_SIZES.get(typ, (None, 1))
        try:
            if d[:8] == b"\x89PNG\r\n\x1a\n":
                px = size or struct.unpack_from(">I", d, 16)[0]
                dest = target(px, scale, ".png")
                if dest.name not in names:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(d)
                    made.append(dest)
                    names.add(dest.name)
            elif d[:12] == b"\0\0\0\x0cjP  \r\n\x87\n" or d[:4] == b"\xffO\xffQ":
                dest = target(size or 0, scale, ".png")
                if dest.name in names:
                    continue
                jp2 = dest.with_suffix(".jp2")
                jp2.parent.mkdir(parents=True, exist_ok=True)
                jp2.write_bytes(d)
                if sys.platform == "darwin" and shutil.which("sips") and subprocess.run(
                        ["sips", "-s", "format", "png", str(jp2), "--out", str(dest)], capture_output=True,
                        timeout=60).returncode == 0 and dest.exists():
                    jp2.unlink()
                    made.append(dest)
                else:
                    made.append(jp2)
                    problems.append(f"{typ}: JPEG 2000 kept as .jp2 (convert with sips or Pillow+OpenJPEG)")
                names.add(dest.name)
            elif size and (d[:4] == b"ARGB" or typ in ICNS_MASKS):
                dest = target(size, scale, ".png")
                if dest.name in names:
                    continue
                n = size * size
                if d[:4] == b"ARGB":
                    planes = icns_unpack(d[4:], 4 * n)
                    a, r, g, bl = (planes[k * n:(k + 1) * n] for k in range(4))
                else:
                    body = d[4:] if typ == "it32" else d
                    rgb = body if len(body) == 3 * n else icns_unpack(body, 3 * n)
                    r, g, bl = (rgb[k * n:(k + 1) * n] for k in range(3))
                    mask = ents.get(ICNS_MASKS[typ], b"")
                    a = mask[:n] if len(mask) >= n else b"\xff" * n
                pix = bytearray(4 * n)
                pix[0::4], pix[1::4], pix[2::4], pix[3::4] = r, g, bl, a
                made.append(png_write(dest, size, size, bytes(pix)))
                names.add(dest.name)
        except (struct.error, ValueError, OSError, subprocess.SubprocessError) as ex:
            problems.append(f"{typ}: {ex}")
    return made, problems


CSI_COMPRESSION = {0: "none", 1: "rle", 2: "zip", 3: "lzvn", 4: "lzfse", 5: "jpeg-lzfse", 6: "blurred",
                   7: "astc", 8: "palette", 9: "hevc", 10: "deepmap-lzfse", 11: "deepmap2"}
CAR_ATTRS = {1: "element", 2: "part", 3: "size", 4: "direction", 6: "value", 7: "appearance", 8: "dimension1",
             9: "dimension2", 10: "state", 11: "layer", 12: "scale", 13: "localization", 14: "presentation_state",
             15: "idiom", 16: "subtype", 17: "identifier", 18: "previous_value", 19: "previous_state",
             20: "size_class_h", 21: "size_class_v", 22: "memory_class", 23: "graphics_class", 24: "display_gamut",
             25: "deployment_target", 26: "glyph_weight", 27: "glyph_size"}
_LIBCOMPRESSION = None


def apple_decompress(data: bytes, algo: str, size: int):
    """LZFSE or raw LZVN via macOS's system libcompression. None elsewhere."""
    global _LIBCOMPRESSION
    if _LIBCOMPRESSION is None:
        _LIBCOMPRESSION = False
        if sys.platform == "darwin":
            with contextlib.suppress(OSError):
                import ctypes
                lib = ctypes.CDLL("/usr/lib/libcompression.dylib")
                lib.compression_decode_buffer.restype = ctypes.c_size_t
                lib.compression_decode_buffer.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                                                          ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
                _LIBCOMPRESSION = (lib, ctypes)
    if not _LIBCOMPRESSION:
        return None
    lib, ctypes = _LIBCOMPRESSION
    if algo == "lzvn":  # raw LZVN wrapped as one 'bvxn' block of LZFSE
        data = b"bvxn" + struct.pack("<II", size, len(data)) + data + b"bvx$"
    buf = ctypes.create_string_buffer(size + 64)
    n = lib.compression_decode_buffer(buf, size + 64, data, len(data), None, 0x801)
    return buf.raw[:n] if n else None


def bom_store(b: bytes):
    """Blocks and named variables of a BOM store (the container of .car catalogues and .bom files)."""
    if b[:8] != b"BOMStore":
        raise Bad("not a BOM store")
    _v, _nb, io_, _il, vo, _vl = struct.unpack_from(">6I", b, 8)
    n = struct.unpack_from(">I", b, io_)[0]

    def blk(i):
        if not 0 < i < n:
            return b""
        a, ln = struct.unpack_from(">II", b, io_ + 4 + 8 * i)
        return b[a:a + ln] if a + ln <= len(b) else b""
    nv, p, names = struct.unpack_from(">I", b, vo)[0], vo + 4, {}
    for _ in range(nv):
        idx, ln = struct.unpack_from(">IB", b, p)
        names[b[p + 5:p + 5 + ln].decode("latin-1")] = idx
        p += 5 + ln
    return n, blk, names


def bom_tree(blk, idx):
    """(key, value) pairs of a BOM B-tree, from its leftmost leaf along the leaf chain."""
    t = blk(idx)
    if t[:4] != b"tree":
        return []
    node = struct.unpack_from(">I", t, 8)[0]
    for _ in range(64):
        d = blk(node)
        if len(d) < 20:
            return []
        if struct.unpack_from(">H", d, 0)[0]:
            break
        node = struct.unpack_from(">I", d, 12)[0]
    pairs, seen = [], set()
    while node and node not in seen:
        seen.add(node)
        d = blk(node)
        if len(d) < 12:
            break
        _leaf, cnt, fwd = struct.unpack_from(">HHI", d, 0)
        for k in range(cnt):
            if 12 + 8 * k + 8 > len(d):
                break
            v, kk = struct.unpack_from(">II", d, 12 + 8 * k)
            pairs.append((blk(kk), blk(v)))
        node = fwd
    return pairs


def csi_parse(d: bytes):
    """Parse a CoreUI 'CTSI' rendition header (name, size, scale, format, TLVs, payload)."""
    _ver, _flags, w, h, scale, pf = struct.unpack_from("<5I4s", d, 4)
    tlv_len, _u1, _u2, body_len = struct.unpack_from("<4I", d, 168)
    tlvs, p = {}, 184
    while p + 8 <= 184 + tlv_len:
        tag, ln = struct.unpack_from("<II", d, p)
        tlvs[tag] = d[p + 8:p + 8 + ln]
        p += 8 + ln
    return {"name": d[40:168].split(b"\0")[0].decode("utf-8", "replace"), "w": w, "h": h, "scale": scale / 100 or 1,
            "format": pf[::-1].decode("latin-1").strip("\0 ") or "-", "tlvs": tlvs,
            "body": d[184 + tlv_len:184 + tlv_len + body_len]}


def csi_pixels(c):
    """(pixels, mode, None) for an ARGB or GA8 rendition, or (None, None, why not)."""
    body, w, h = c["body"], c["w"], c["h"]
    bpp = {"ARGB": 4, "GA8": 2}.get(c["format"])
    if not bpp:
        return None, None, ("not an image (a colour, name list or image set)" if c["format"] == "-" else
                            f"pixel format {c['format']} (not decoded here)")
    if body[:4] != b"MLEC":
        return None, None, "no pixel data (" + (body[:4].decode("latin-1") or "empty") + ")"
    comp, count = struct.unpack_from("<II", body, 8)
    if comp not in (0, 2, 3, 4):
        return None, None, f"{CSI_COMPRESSION.get(comp, comp)} compression"
    chunks = []
    if body[16:20] == b"KCBC":  # row bands, each compressed on its own
        p = 16
        while body[p:p + 4] == b"KCBC" and p + 20 <= len(body):
            rows, ln = struct.unpack_from("<II", body, p + 12)
            chunks.append((rows, body[p + 20:p + 20 + ln]))
            p += 20 + ln
    else:
        chunks.append((h, body[16:16 + count]))
    raw = bytearray()
    t = c["tlvs"].get(1007, b"")  # bytes per row, when rows are padded
    stride = max(struct.unpack_from("<I", t)[0] if len(t) >= 4 else 0, w * bpp)
    for rows, data in chunks:
        need = rows * stride
        if comp == 0:
            part = data
        elif comp == 2:
            part = zlib.decompress(data, 47)  # gzip or zlib wrapper
        else:
            part = apple_decompress(data, "lzvn" if comp == 3 else "lzfse", need)
            if part is None:
                return None, None, f"{CSI_COMPRESSION[comp]} needs macOS (libcompression)"
        if rows and len(part) >= rows * w * bpp and len(part) % rows == 0 and len(part) // rows > w * bpp:
            st = len(part) // rows  # padded rows, keep just the pixels
            part = b"".join(part[y * st:y * st + w * bpp] for y in range(rows))
        raw += part
    if len(raw) < w * h * bpp:
        return None, None, "pixel data shorter than the image"
    px, mode = unpremultiply(bytes(raw[:w * h * bpp]), bpp)
    return px, mode, None


def car_extract(f: Path, dest: Path, budget, deadline=None):
    """Write every rendition of a CoreUI .car catalogue, atlases cut apart. Read by hand so old (pre-2015)
    catalogues that `assetutil` rejects work. budget(nbytes) -> bool says if there's room left to write."""
    b = f.read_bytes()
    _n, blk, names = bom_store(b)
    kf = blk(names.get("KEYFORMAT", 0))
    attrs = list(struct.unpack_from(f"<{struct.unpack_from('<I', kf, 8)[0]}I", kf, 12)) if kf[:4] == b"tmfk" else []
    rends = []
    for key, d in bom_tree(blk, names.get("RENDITIONS", 0)):
        if d[:4] == b"ISTC":
            vals = struct.unpack_from(f"<{len(key) // 2}H", key) if key else ()
            rends.append((dict(zip(attrs, vals)), d))
    if not rends:  # no readable tree, take every rendition block unkeyed
        n_, blk_, _ = bom_store(b)
        rends = [({}, d) for d in (blk_(i) for i in range(1, n_)) if d[:4] == b"ISTC"]
    rows, why, used, atlases = [], Counter(), set(), {}

    def unique(name):
        stem, ext = os.path.splitext(safe_part(name) or "rendition")
        cand, k = stem + ext, 2
        while cand.lower() in used:
            cand, k = f"{stem}_{k}{ext}", k + 1
        used.add(cand.lower())
        return dest / cand

    def write(path, data):
        if not budget(len(data)):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def pixels_of(c):
        if id(c) not in atlases:
            atlases[id(c)] = csi_pixels(c)
        return atlases[id(c)]

    parsed = []
    for key, d in rends:
        try:
            parsed.append((key, csi_parse(d)))
        except struct.error:
            why["damaged header"] += 1
    by_key = {tuple(sorted(k.items())): c for k, c in parsed if k}
    groups = {}
    for k, c in parsed:
        groups.setdefault(c["name"], []).append(k)
    vary = {nm: [a for a in sorted({a for k in ks for a in k}) if len({k.get(a, 0) for k in ks}) > 1]
            for nm, ks in groups.items() if len(ks) > 1}

    def stem_of(c, key):  # same name across states/appearances/scales, add what differs
        stem = os.path.splitext(c["name"])[0]
        for a in vary.get(c["name"], []):
            stem += f"@{key.get(a, 1)}x" if a == 12 else f"_{CAR_ATTRS.get(a, a)}{key.get(a, 0)}"
        return stem
    prog = Progress(f"{f.name}: renditions", len(parsed))
    for i, (key, c) in enumerate(parsed):
        prog.tick(note=f"({sum(v for k, v in why.items() if k.startswith('written'))} written) ")
        if deadline and time.time() > deadline:
            why["time limit reached"] += len(parsed) - i
            break
        ent = {"name": c["name"], "size": f"{c['w']}x{c['h']}", "scale": c["scale"], "format": c["format"],
               **({"key": {CAR_ATTRS.get(a, str(a)): v for a, v in key.items() if v}} if key else {})}
        body, out = c["body"], None
        try:
            if body[:4] == b"DWAR":  # raw 'DATA' (PNG, JPEG, PDF, SVG, HEIF, anything)
                flags, ln = struct.unpack_from("<II", body, 4)
                data = body[12:12 + ln]
                packed = data[:4] in (b"bvx2", b"bvx1", b"bvxn", b"bvx-")
                if packed:
                    data = apple_decompress(data, "lzfse", max(ln * 20, 1 << 20)) or b""
                ext = (".png" if data[:4] == b"\x89PNG" else ".jpg" if data[:3] == b"\xff\xd8\xff" else
                       ".pdf" if data[:4] == b"%PDF" else ".svg" if b"<svg" in data[:400] else
                       ".heic" if data[4:8] == b"ftyp" else os.path.splitext(c["name"])[1] or ".bin")
                if data:
                    out = write(unique(stem_of(c, key) + ext), data)
                    why["over the copy cap" if out is None else "written"] += 1
                else:
                    why["LZFSE data (needs macOS)" if packed else "empty data entry"] += 1
            elif 1010 in c["tlvs"] and c["tlvs"][1010][:4] == b"KLNI":  # a slice of a packed atlas
                t = c["tlvs"][1010]
                x, y, w, h = struct.unpack_from("<4I", t, 8)
                toks = dict(struct.unpack_from("<2H", t, q) for q in range(30, len(t) - 3, 4))
                toks.pop(0, None)
                atlas = next((a for k, a in by_key.items() if all(dict(k).get(at) == v for at, v in toks.items())), None)
                px, mode, err = pixels_of(atlas) if atlas else (None, None, "atlas not found")
                if px is None:
                    why[f"in an atlas: {err}"] += 1
                else:
                    bpp = 4 if mode == "RGBA" else 2
                    st = atlas["w"] * bpp
                    crop = b"".join(px[(y + r) * st + x * bpp:(y + r) * st + (x + w) * bpp] for r in range(h))
                    if budget(len(crop) // 3):
                        out = png_write(unique(stem_of(c, key) + ".png"), w, h, crop, mode)
                    why["over the copy cap" if out is None else "written (cut from its atlas)"] += 1
            else:
                px, mode, err = pixels_of(c)
                if px is None:
                    why[err] += 1
                elif budget(len(px) // 3):
                    out = png_write(unique(stem_of(c, key) + ".png"), c["w"], c["h"], px, mode)
                    why["written"] += 1
                else:
                    why["over the copy cap"] += 1
        except (struct.error, zlib.error, ValueError, OSError) as ex:
            why[f"unreadable ({type(ex).__name__})"] += 1
        if out:
            ent["file"] = str(out)
        rows.append(ent)
    return rows, why


def macho(path: Path):
    """Architectures, minimum OS and linked libraries from a Mach-O header (read, never loaded)."""
    CPU = {7: "i386", 0x01000007: "x86_64", 12: "arm", 0x0100000c: "arm64", 0x0200000c: "arm64_32",
           18: "ppc", 0x01000012: "ppc64"}
    with path.open("rb") as f:
        head = f.read(4096)
        if head[:8] == b"Joy!peff":
            return {"format": "PEF (Classic Mac OS / Carbon CFM, PowerPC)"}
        magic = head[:4]
        slices = []
        if magic in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf") and 0 < struct.unpack_from(">I", head, 4)[0] < 30:
            n = struct.unpack_from(">I", head, 4)[0]
            wide = magic[3] == 0xbf
            for k in range(n):
                if wide:
                    ct, _, off, size = struct.unpack_from(">IIQQ", head, 8 + 32 * k)
                else:
                    ct, _, off, size = struct.unpack_from(">IIII", head, 8 + 20 * k)
                slices.append((ct, off))
        else:
            slices.append((None, 0))
        out = {"format": "Mach-O universal" if len(slices) > 1 or slices[0][0] is not None else "Mach-O", "archs": []}
        for ct, off in slices:
            f.seek(off)
            h = f.read(32)
            if h[:4] in (b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
                end = "<"
            elif h[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf"):
                end = ">"
            else:
                out["archs"].append(CPU.get(ct, ct) if ct is not None else "unknown")
                if ct is None:
                    return {"format": "not a Mach-O file"}
                continue
            is64 = h[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf")
            cpu, sub, ftype, ncmds, sizeofcmds = struct.unpack_from(end + "IIIII", h, 4)
            arch = CPU.get(cpu, hex(cpu))
            out["archs"].append(arch + "e" if arch == "arm64" and sub & 0xff == 2 else arch)
            if len(out["archs"]) > 1 and "libraries" in out:
                continue  # load commands of the first slice are enough
            f.seek(off + (32 if is64 else 28))
            cmds = f.read(min(sizeofcmds, 1 << 20))
            p, libs = 0, []
            for _ in range(min(ncmds, 4000)):
                if p + 8 > len(cmds):
                    break
                cmd, size = struct.unpack_from(end + "II", cmds, p)
                if cmd in (0xc, 0x80000018, 0x8000001f, 0x80000023):
                    libs.append(asz(cmds, p + struct.unpack_from(end + "I", cmds, p + 8)[0], "utf-8")[0])
                elif cmd in (0x24, 0x25, 0x2f, 0x30):
                    v = struct.unpack_from(end + "I", cmds, p + 8)[0]
                    out["min_os"] = f"{v >> 16}.{(v >> 8) & 0xff}" + (f".{v & 0xff}" if v & 0xff else "")
                elif cmd == 0x32:
                    plat, v = struct.unpack_from(end + "II", cmds, p + 8)
                    out["platform"] = {1: "macOS", 2: "iOS", 3: "tvOS", 4: "watchOS", 6: "Mac Catalyst"}.get(plat, plat)
                    out["min_os"] = f"{v >> 16}.{(v >> 8) & 0xff}" + (f".{v & 0xff}" if v & 0xff else "")
                p += max(size, 8)
            out["libraries"] = libs
        return out


class Progress:
    """Progress line on stderr every few seconds, for long runs on big bundles."""

    def __init__(self, what: str, total: int | None = None, every: float = 5.0):
        self.what, self.total, self.every, self.n = what, total, every, 0
        self.t0 = self.last = time.time()

    def tick(self, k: int = 1, note: str = ""):
        self.n += k
        now = time.time()
        if now - self.last >= self.every:
            self.last = now
            log(f"{self.what}: {self.n}{'/%d' % self.total if self.total else ''} {note}({now - self.t0:.0f} s)")

    def seconds(self) -> float:
        return round(time.time() - self.t0, 1)


KEEP_EXT = {".strings", ".stringsdict", ".loctable", ".icns", ".car", ".plist", ".sdef", ".nib", ".xib", ".apcommands",
            ".html", ".htm", ".css", ".js", ".json", ".txt", ".rtf", ".xml", ".ttf", ".otf", ".psd"}
SKIP_EXT = {".dylib", ".so", ".a", ".o", ".h", ".hpp", ".modulemap", ".swiftmodule", ".swiftinterface", ".swiftdoc",
            ".metallib", ".mom", ".omo", ".cer", ".tbd", ".pyc", ".jar", ".node", ".mobileprovision"}


def wanted_member(name: str, size: int) -> bool:
    """What `mac` unzips. Framework/plug-in binaries, libraries and signatures stay packed."""
    low = [p.lower() for p in name.split("/") if p]
    if not low or "_codesignature" in low:
        return False
    ext = os.path.splitext(low[-1])[1]
    if ext in KEEP_EXT or ext in IMG_EXT or ext in SND_EXT:
        return True
    if any(p.endswith((".nib", ".storyboardc", ".help")) for p in low[:-1]):
        return ext not in SKIP_EXT
    if len(low) >= 3 and low[-2] == "macos" and low[-3] == "contents":
        return True
    return ext not in SKIP_EXT and size <= 1 << 20


@contextlib.contextmanager
def app_source(src: Path, max_mb: int):
    """-> (folder to look in, zip members left packed). Zips are partly unpacked to a temp folder, dmgs
    mounted read-only."""
    low = src.name.lower()
    if src.is_dir():
        yield src, []
    elif low.endswith(".dmg"):
        if sys.platform != "darwin" or not shutil.which("hdiutil"):
            raise Bad("a .dmg can only be mounted on macOS: extract it with `7z x FILE.dmg -oDIR` (the .app is inside, "
                      "often under a volume-name folder) and run `mac DIR`")
        mnt = tempfile.mkdtemp(prefix="inspect-dmg-")
        r = subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-noverify",
                            "-mountpoint", mnt, str(src)], input="Y\n", capture_output=True, text=True, timeout=600,
                           env={**os.environ, "PAGER": "true"}, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            os.rmdir(mnt)
            raise Bad(f"hdiutil could not mount the image: {(r.stderr or r.stdout).strip()[-300:]}")
        try:
            yield Path(mnt), []
        finally:
            subprocess.run(["hdiutil", "detach", mnt, "-force"], capture_output=True, timeout=120)
            with contextlib.suppress(OSError):
                os.rmdir(mnt)
    elif zipfile.is_zipfile(src):
        tmp = Path(tempfile.mkdtemp(prefix="inspect-zip-"))
        try:
            with zipfile.ZipFile(src) as z:
                members = [zi for zi in z.infolist() if not (
                    zi.is_dir() or zi.filename.startswith("__MACOSX/") or (zi.external_attr >> 16) & 0o170000 == 0o120000)]
                # __MACOSX/ fork shadows and symlinks (framework Current) hold nothing
                todo = [zi for zi in members if wanted_member(zi.filename, zi.file_size)]
                skipped = [(zi.filename, zi.file_size) for zi in members if not wanted_member(zi.filename, zi.file_size)]
                need = sum(zi.file_size for zi in todo)
                if need > max_mb * 4 * 1024 * 1024:
                    raise Bad(f"the parts of the zip worth reading unpack to {need >> 20} MB, more than {max_mb * 4} MB "
                              "(4 x --max-copy-mb): raise --max-copy-mb, or unpack it yourself and pass the folder")
                log(f"unzipping {len(todo)} of {len(members)} files ({need >> 20} MB); program code, libraries and "
                    f"signatures ({sum(s for _, s in skipped) >> 20} MB) stay zipped")
                prog = Progress("unzipping", len(todo))
                for zi in todo:
                    f = tmp / safe_rel(zi.filename)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(zi) as fi, f.open("wb") as fo:
                        shutil.copyfileobj(fi, fo, 1 << 20)
                    prog.tick(note=f"{zi.filename.rsplit('/', 1)[-1][:40]} ")
            yield tmp, skipped
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        raise Bad("expected an .app folder, a .zip/.ipa holding one, or a .dmg")


def find_apps(root: Path):
    if root.suffix.lower() == ".app" or (root / "Contents" / "Info.plist").exists():
        return [root]
    found = []
    for dp, dns, _ in os.walk(root):
        depth = len(Path(dp).relative_to(root).parts)
        keep = []
        for dn in dns:
            p = Path(dp) / dn
            if p.is_symlink():
                continue
            if dn.lower().endswith(".app"):
                found.append(p)
            elif depth < 4:
                keep.append(dn)
        dns[:] = keep
    return found


IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".pdf", ".heic", ".svg", ".pict", ".pct", ".psd"}
SND_EXT = {".aiff", ".aif", ".aifc", ".caf", ".wav", ".mp3", ".m4a", ".snd", ".au"}
BASE_LANGS = ("en", "English", "Base", "en_US", "en_GB", "_")


def sound_info(f: Path):
    b = f.open("rb").read(4096)
    try:
        if b[:4] == b"RIFF" and b[8:12] == b"WAVE":
            import wave
            with wave.open(str(f)) as w:
                return {"seconds": round(w.getnframes() / w.getframerate(), 3), "rate": w.getframerate(),
                        "channels": w.getnchannels()}
        if b[:4] == b"FORM" and b[8:12] in (b"AIFF", b"AIFC"):
            p = 12
            while p + 8 <= len(b):
                cid, size = b[p:p + 4], struct.unpack_from(">I", b, p + 4)[0]
                if cid == b"COMM":
                    ch, frames, _ = struct.unpack_from(">hIh", b, p + 8)
                    exp, mant = struct.unpack_from(">HQ", b, p + 16)
                    rate = mant * 2.0 ** ((exp & 0x7fff) - 16383 - 63) if mant else 0
                    return {"seconds": round(frames / rate, 3) if rate else None, "rate": round(rate), "channels": ch}
                p += 8 + size + (size & 1)
    except (EOFError, struct.error, ValueError, OSError):
        pass
    return {}


def to_png(f: Path, dest: Path, Image) -> list:
    """TIFF (all pages) or PSD (composite) to PNG via Pillow, else `sips` on macOS."""
    made = []
    if Image:
        with contextlib.suppress(Exception):
            with Image.open(f) as im:
                for k in range(getattr(im, "n_frames", 1) if f.suffix.lower() in (".tif", ".tiff") else 1):
                    im.seek(k)
                    d = dest.parent / f"{dest.stem}{'' if k == 0 else '_%d' % k}_{im.width}x{im.height}.png"
                    d.parent.mkdir(parents=True, exist_ok=True)
                    im.convert("RGBA").save(d)
                    made.append(d)
    if not made and sys.platform == "darwin" and shutil.which("sips"):
        d = dest.with_suffix(".png")
        d.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if subprocess.run(["sips", "-s", "format", "png", str(f), "--out", str(d)], capture_output=True,
                              timeout=120).returncode == 0 and d.exists():
                made.append(d)
    return made


def head8(f: Path) -> bytes:
    with f.open("rb") as fh:
        return fh.read(8)


REDIRECT = re.compile(r"""http-equiv=["']?refresh["']?[^>]*?url=([^"'>\s]+)|(?:window\.|document\.)?location(?:\.href)?"""
                      r"""\s*=\s*["']([^"']+)["']|location\.replace\(\s*["']([^"']+)["']""", re.I)


def inspect_bundle(app: Path, out: Path, max_copy: int, skipped=()):
    Image = pil()
    out.mkdir(parents=True, exist_ok=True)
    out_abs = out.resolve()
    timing = {}
    contents = app / "Contents" if (app / "Contents").is_dir() else app  # iOS bundles are flat
    res_dir = contents / "Resources" if (contents / "Resources").is_dir() else contents
    warnings, info = [], {}
    if (contents / "Info.plist").exists():
        try:
            info = load_plist((contents / "Info.plist").read_bytes())
        except Exception as ex:  # noqa: BLE001 - a damaged plist is reported, the rest still runs
            warnings.append(f"Info.plist unreadable: {ex}")
    if isinstance(info, dict):
        wjson(out / "Info.plist.json", jsonable(info))
    else:
        info = {}
    copied, last_skip = [0], [""]

    def room(size):
        if size > 20 * 1024 * 1024:
            last_skip[0] = f"bigger than 20 MB ({size >> 20} MB)"
            return False
        if copied[0] + size > max_copy * 1024 * 1024:
            last_skip[0] = f"over --max-copy-mb {max_copy} (raise it to copy more)"
            return False
        copied[0] += size
        return True

    def copy(f: Path, dest: Path):
        if not room(f.stat().st_size):
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dest)
        return dest

    def bundle_rel(f: Path) -> Path:
        """Path for images/, sounds/ etc, relative to Resources, else Contents."""
        for base in (res_dir, contents, app):
            with contextlib.suppress(ValueError):
                return f.relative_to(base)
        return Path(f.name)

    counts, index = Counter(), []
    strings, icons, images, nibs, sounds, helps, cars, others = {}, [], [], [], [], [], [], Counter()
    menus, data_files, car_rows = [], [], {}
    skip_dirs = {"_CodeSignature", "_MASReceipt"}
    prog = Progress("reading the bundle")
    t_walk = time.time()
    for dp, dns, fns in os.walk(contents):
        dpath = Path(dp)
        dns[:] = sorted(x for x in dns if x not in skip_dirs and not (dpath / x).is_symlink())
        if dpath.suffix.lower() == ".help" and dpath.parent == res_dir:
            helps.append(dpath)
        for fn in sorted(fns):
            f = dpath / fn
            if f.is_symlink():
                continue
            prog.tick(note=f"files (nibs {counts['nibs']}, images {counts['images']}, plists {counts['data_files']}) ")
            r = rel(f, app)
            ext = f.suffix.lower()
            parts = f.relative_to(app).parts
            in_nib = any(p.lower().endswith((".nib", ".storyboardc", ".xib")) for p in parts[:-1])
            lproj = next((p[:-6] for p in reversed(parts) if p.endswith(".lproj")), "_")
            in_help = any(p.lower().endswith(".help") for p in parts)
            try:
                if ext in (".strings", ".stringsdict") and not in_help:
                    data = load_plist(f.read_bytes(), strings=True)
                    if isinstance(data, dict):
                        dest = out / "strings" / safe_part(lproj) / (f.stem + (".stringsdict" if ext == ".stringsdict" else "") + ".json")
                        wjson(dest, jsonable(data))
                        strings.setdefault(lproj, {})[f.name] = {"entries": len(data), "file": rel(dest, out), "from": r}
                    counts["strings"] += 1
                elif ext == ".loctable":
                    data = load_plist(f.read_bytes())
                    for lang, table in (data.items() if isinstance(data, dict) else []):
                        if isinstance(table, dict) and lang != "LocProvenance":
                            dest = out / "strings" / safe_part(lang) / (f.stem + ".json")
                            wjson(dest, jsonable(table))
                            strings.setdefault(lang, {})[f.stem + ".loctable"] = {"entries": len(table),
                                                                                    "file": rel(dest, out), "from": r}
                    counts["strings"] += 1
                elif ext == ".icns":
                    counts["icns"] += 1
                    icon_dir = out / "icons" if f.parent == res_dir else out / "icons" / bundle_rel(f).parent
                    made, problems = icns_images(f, icon_dir)
                    if not made and Image:  # old 1- and 8-bit sets, let Pillow try
                        with contextlib.suppress(Exception):
                            for s in sorted(Image.open(f).info.get("sizes", [])):
                                im = Image.open(f)
                                im.size = s[:2]
                                im.load(scale=s[2])
                                dest = icon_dir / f"{safe_part(f.stem)}_{s[0]}x{s[1]}{'@%dx' % s[2] if s[2] > 1 else ''}.png"
                                dest.parent.mkdir(parents=True, exist_ok=True)
                                im.save(dest)
                                made.append(dest)
                    if not made and sys.platform == "darwin" and shutil.which("sips"):  # Apple's own reader
                        dest = icon_dir / f"{safe_part(f.stem)}.png"
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with contextlib.suppress(OSError, subprocess.SubprocessError):
                            if subprocess.run(["sips", "-s", "format", "png", str(f), "--out", str(dest)],
                                              capture_output=True, timeout=120).returncode == 0 and dest.exists():
                                made.append(dest)
                    if not made:
                        warnings.append(f"{r}: icon not converted ({'; '.join(problems) or 'no entry this reader knows'})")
                    icons.append({"file": r, "pngs": [rel(p, out) for p in made], **({"problems": problems} if problems else {})})
                elif ext in IMG_EXT and not in_help:
                    counts["images"] += 1
                    ent = {"file": r, "bytes": f.stat().st_size}
                    if Image and ext not in (".pdf", ".svg", ".psd"):
                        with contextlib.suppress(Exception):
                            with Image.open(f) as im:
                                ent["size"] = f"{im.width}x{im.height}"
                    if ext in (".tif", ".tiff", ".psd", ".pict", ".pct"):
                        pngs = to_png(f, out / "images" / bundle_rel(f).with_suffix(""), Image)
                        copied[0] += sum(p.stat().st_size for p in pngs)
                        if pngs:
                            ent["png"] = [rel(p, out) for p in pngs]
                    if "png" not in ent:
                        dest = copy(f, out / "images" / bundle_rel(f))
                        ent.update({"copy": rel(dest, out)} if dest else {"not_copied": last_skip[0]})
                    images.append(ent)
                elif ext in (".nib", ".xib") or in_nib:
                    b = f.read_bytes()
                    if not b or (fn.lower() in ("info.nib", "classes.nib") and not b.startswith((b"bplist", b"NIB"))):
                        continue
                    counts["nibs"] += 1
                    classes, strs, labels, nodes = nib_any(b)
                    ui = [s for s in dict.fromkeys(strs) if looks_ui(s)]
                    name = rel(f, contents).replace("/", "__")
                    tree = nib_menus(nodes) if nodes else []
                    if not tree and b.lstrip()[:5] in (b"<?xml", b"<docu") and not (f.parent / "keyedobjects.nib").exists():
                        tree = xib_menus(b)
                    ent = {"file": r, "labels": len(labels), "ui_strings": len(ui)}
                    if tree:
                        mdest = wjson(out / "menus" / f"{safe_part(name)}.json", {"from": r, "language": lproj, "menus": tree})
                        n_items = count_menu_items(tree)
                        ent.update({"menus": rel(Path(mdest), out), "menu_items": n_items})
                        menus.append({"nib": r, "json": rel(Path(mdest), out), "language": lproj, "items": n_items,
                                      "main_menu": any(m.get("main_menu") for m in tree),
                                      "titles": [e["title"] for e in tree[0]["items"] if e.get("title")][:20]})
                    dest = wjson(out / "nibs" / f"{safe_part(name)}.json",
                                 {"from": r, "language": lproj, "classes": dict(Counter(classes).most_common()),
                                  "labels": labels, "ui_strings": ui, "strings": list(dict.fromkeys(strs)),
                                  **({"menus": ent["menus"]} if tree else {})})
                    nibs.append({"file": r, "json": rel(Path(dest), out), **{k: v for k, v in ent.items() if k != "file"}})
                elif ext in SND_EXT:
                    counts["sounds"] += 1
                    ent = {"file": r, "bytes": f.stat().st_size, **sound_info(f)}
                    dest = copy(f, out / "sounds" / bundle_rel(f))
                    ent.update({"copy": rel(dest, out)} if dest else {"not_copied": last_skip[0]})
                    sounds.append(ent)
                elif ext == ".car":
                    counts["asset_catalogs"] += 1
                    t_car = time.time()
                    if f.stat().st_size > 5 << 20:
                        log(f"asset catalogue {r} ({f.stat().st_size >> 20} MB): reading its renditions")
                    ent = {"file": r, "bytes": f.stat().st_size}
                    if sys.platform == "darwin" and shutil.which("assetutil"):  # Apple's reader, only parses
                        pr = None
                        try:
                            pr = subprocess.run(["assetutil", "--info", str(f)], capture_output=True, text=True,
                                                errors="replace", timeout=300, encoding="utf-8")
                            items = json.loads(pr.stdout)
                            names = sorted({x.get("Name") for x in items if isinstance(x, dict) and x.get("Name")})
                            ent["names"] = len(names)
                            ent["json"] = rel(Path(wjson(out / "assets" / f"{safe_part(str(bundle_rel(f)))}.json",
                                                         {"names": names, "items": items})), out)
                        except (ValueError, subprocess.SubprocessError) as ex:
                            old = "invalid CSIHeader" in ((pr.stderr or "") + (pr.stdout or "") if pr else "")
                            ent["note"] = ("an old CoreUI catalogue that this macOS's assetutil cannot read (it only "
                                           "prints 'skipping invalid CSIHeader'); the renditions below come from "
                                           "inspect_app's own reader" if old else
                                           f"assetutil gave no JSON ({type(ex).__name__}); renditions below from inspect_app's own reader")
                    folder = out / "assets" / bundle_rel(f).with_suffix("")
                    try:
                        rows, why = car_extract(f, folder, room, deadline=time.time() + 600)
                        written = [x for x in rows if x.get("file")]
                        for x in written:
                            x["file"] = rel(Path(x["file"]), out)
                        ent["renditions"] = len(rows)
                        ent["images_written"] = len(written)
                        if written:
                            ent["folder"] = rel(folder, out)
                        ent["outcome"] = dict(why.most_common())
                        ent["renditions_json"] = rel(Path(wjson(out / "assets" / f"{safe_part(str(bundle_rel(f)))}.renditions.json",
                                                                rows)), out)
                        if "names" not in ent:
                            ent["names"] = len({x["name"] for x in rows})
                        car_rows[r] = len(written)
                    except (Bad, struct.error, ValueError) as ex:
                        ent["renditions_error"] = f"{type(ex).__name__}: {ex}"
                    ent["seconds"] = round(time.time() - t_car, 1)
                    cars.append(ent)
                elif ext == ".sdef" and not in_help:
                    counts["data_files"] += 1
                    dest = copy(f, out / "data" / bundle_rel(f))
                    t = f.read_bytes().decode("utf-8", "replace")
                    data_files.append({"file": r, "copy": rel(dest, out) if dest else None, "bytes": f.stat().st_size,
                                       "kind": "AppleScript dictionary",
                                       "top_keys": re.findall(r'<command\s[^>]*name="([^"]+)"', t)[:15]})
                elif not in_help and not in_nib and fn not in ("Info.plist", "version.plist", "PkgInfo") and (
                        ext in (".plist", ".apcommands") or head8(f) == b"bplist00") and lproj in BASE_LANGS:
                    data = decoded(load_plist(f.read_bytes()))
                    counts["data_files"] += 1
                    dest = wjson(out / "data" / f"{bundle_rel(f)}.json", data)
                    dump = json.dumps(data, default=str)
                    data_files.append({"file": r, "json": rel(Path(dest), out), "bytes": f.stat().st_size,
                                       "top_keys": top_keys(data), **({"archives_decoded": dump.count('"$archive"')}
                                                                     if '"$archive"' in dump else {})})
                elif not in_help:
                    others[ext or "(none)"] += 1
            except Exception as ex:  # noqa: BLE001 - one damaged file must not stop the rest
                warnings.append(f"{r}: {type(ex).__name__}: {ex}")
            index.append(r)
    timing["walk"] = round(time.time() - t_walk, 1)
    for name, _size in skipped:  # zipped program code is still listed and counted
        index.append(name)
        ext = os.path.splitext(name)[1].lower()
        others[ext or "(none)"] += 1
    # help books are the original's words, copy pages and titles
    help_out = []
    for h in helps:
        pages, targets = [], Counter()
        for f in sorted(h.rglob("*")):
            if f.is_file() and not f.is_symlink():
                if f.suffix.lower() in (".html", ".htm"):
                    raw = f.read_bytes()
                    title, text = html_text(raw)
                    go = next((x for m in REDIRECT.finditer(raw.decode("utf-8", "replace")) for x in m.groups() if x), None)
                    if go and "://" in go:
                        targets[go] += 1
                    pages.append({"file": rel(f, h), "title": title, "words": len(text.split()),
                                  **({"redirect": go} if go else {})})
                copy(f, out / "help" / h.name / rel(f, h))
        book = {"book": h.name, "pages": len(pages), "titles": [p["title"] for p in pages if p["title"]][:40]}
        if pages and targets and all(p.get("redirect") or p["words"] < 30 for p in pages):
            book["online_only"] = True
            book["redirects"] = [u for u, _ in targets.most_common(5)]
            book["note"] = (f"this Help Book is a stub that sends readers to Apple's online help: fetch the archived "
                            f"pages with `wayback.py urls` / `wayback.py text` on {book['redirects'][0]}")
            log(f"note: {h.name} is online help only: {book['redirects'][0]}")
        help_out.append(book)
        wjson(out / "help" / f"{h.name}.pages.json", pages)
    # main executable, header only
    exe = {}
    name = info.get("CFBundleExecutable")
    cand = [contents / "MacOS" / name] if name else []
    cand += sorted((contents / "MacOS").glob("*")) if (contents / "MacOS").is_dir() else []
    cand += [app / name] if name else []
    for c in cand:
        if c.is_file():
            try:
                exe = {"file": rel(c, app), **macho(c)}
            except (OSError, struct.error) as ex:
                exe = {"file": rel(c, app), "error": str(ex)}
            break
    wjson(out / "executable.json", exe)
    frameworks = sorted(p.name for p in (contents / "Frameworks").glob("*")) if (contents / "Frameworks").is_dir() else []
    base = next((lg for lg in BASE_LANGS if lg in strings), next(iter(strings), None))
    sample = {}
    if base:
        for meta in sorted(strings[base].values(), key=lambda m: -m["entries"])[:2]:
            tbl = json.loads((out / meta["file"]).read_text(encoding="utf-8"))
            sample.update({k: v for k, v in list(tbl.items())[:12] if isinstance(v, str) and k and v})
    menus.sort(key=lambda m: (not m["main_menu"], m["language"] not in BASE_LANGS, -m["items"]))
    own = [d for d in data_files if not d["file"].startswith(("Contents/Frameworks/", "Contents/PlugIns/"))]
    useful = ["Info.plist.json"] + [m["json"] for m in menus[:1]]
    if base:
        useful += [m["file"] for m in sorted(strings[base].values(), key=lambda m: -m["entries"])[:4]]
    useful += [d.get("json") or d.get("copy") for d in sorted(own, key=lambda d: -d["bytes"])[:3] if d.get("json") or d.get("copy")]
    useful += [n["json"] for n in sorted(nibs, key=lambda n: -(n["labels"] + n["ui_strings"]))[:4]]
    big_icon = max((p for i in icons for p in i["pngs"]), key=lambda p: int(re.search(r"_(\d+)x", p).group(1))
                   * (2 if "@2x" in p else 1) if re.search(r"_(\d+)x", p) else 0, default=None)
    if big_icon:
        useful.append(big_icon)
    useful += [c["folder"] + "/" for c in sorted(cars, key=lambda c: -c.get("images_written", 0))[:2] if c.get("folder")]
    useful += [f"help/{h['book']}/" for h in help_out if not h.get("online_only")]
    wjson(out / "index.json", index)
    wjson(out / "images.json", images)
    wjson(out / "sounds.json", sounds)
    wjson(out / "data.json", data_files)
    timing["total"] = round(time.time() - prog.t0, 1)
    summary = {
        "app": str(app), "out": str(out_abs),
        "name": info.get("CFBundleDisplayName") or info.get("CFBundleName") or app.stem,
        "bundle_id": info.get("CFBundleIdentifier"), "version": info.get("CFBundleShortVersionString"),
        "build": info.get("CFBundleVersion"), "min_os": info.get("LSMinimumSystemVersion") or info.get("MinimumOSVersion")
        or exe.get("min_os"), "copyright": info.get("NSHumanReadableCopyright"),
        "document_types": [t.get("CFBundleTypeName") for t in info.get("CFBundleDocumentTypes", []) if isinstance(t, dict)][:20],
        "executable": exe, "frameworks": frameworks[:40], "files": len(index), "counts": dict(counts),
        "languages": sorted(strings), "base_language": base, "sample_strings": sample,
        "strings_per_language": {lg: sum(m["entries"] for m in v.values()) for lg, v in sorted(strings.items())},
        "menus": menus[:20], "icons": icons, "images": len(images), "images_json": "images.json",
        "sounds": sounds[:30], "sounds_json": "sounds.json", "nibs": sorted(nibs, key=lambda n: -n["labels"])[:30],
        "data_files": sorted(own, key=lambda d: -d["bytes"])[:25] + sorted(
            (d for d in data_files if d not in own), key=lambda d: -d["bytes"])[:15], "data_json": "data.json",
        "help_books": help_out, "asset_catalogs": cars, "other_files_by_extension": dict(others.most_common(15)),
        **({"left_zipped": {"files": len(skipped), "mb": round(sum(s for _, s in skipped) / 1048576, 1),
                            "note": "program code, libraries and signatures: listed in index.json, not unpacked"}}
           if skipped else {}),
        "most_useful": [u for u in dict.fromkeys(useful) if u], "copied_mb": round(copied[0] / 1048576, 1),
        "seconds": timing, "paths": "every file path here is relative to `out`",
        "warnings": warnings[:50] + ([f"... {len(warnings) - 50} more"] if len(warnings) > 50 else [])}
    wjson(out / "summary.json", summary)
    return summary


def cmd_mac(a):
    src = Path(a.path)
    if not src.exists():
        raise Bad(f"{src} does not exist")
    out = default_out(src, a.out)
    t0 = time.time()
    with app_source(src, a.max_copy_mb) as (root, skipped):
        unzip_s = round(time.time() - t0, 1)
        apps = find_apps(root)
        if not apps:
            raise Bad("no .app bundle found inside (looked four folders deep)")

        def left(app):  # this app's packed zip members, relative to it
            base = rel(app, root).replace(os.sep, "/").rstrip("/") + "/"
            return [(n[len(base):], s) for n, s in skipped if n.startswith(base)] if skipped else []
        if len(apps) == 1:
            s = inspect_bundle(apps[0], out, a.max_copy_mb, left(apps[0]))
            s["seconds"]["unzip_or_mount"] = unzip_s
            return s
        return {"out": str(out), "apps": [inspect_bundle(p, out / slugify(p.stem), a.max_copy_mb, left(p))
                                          for p in apps[:10]]}


# ---------------------------------------------------------------- Apple II disk images

APPLESOFT = ("END FOR NEXT DATA INPUT DEL DIM READ GR TEXT PR# IN# CALL PLOT HLIN VLIN HGR2 HGR HCOLOR= HPLOT DRAW "
             "XDRAW HTAB HOME ROT= SCALE= SHLOAD TRACE NOTRACE NORMAL INVERSE FLASH COLOR= POP VTAB HIMEM: LOMEM: "
             "ONERR RESUME RECALL STORE SPEED= LET GOTO RUN IF RESTORE & GOSUB RETURN REM STOP ON WAIT LOAD SAVE "
             "DEF POKE PRINT CONT LIST CLEAR GET NEW TAB( TO FN SPC( THEN AT NOT STEP + - * / ^ AND OR > = < SGN "
             "INT ABS USR FRE SCRN( PDL POS SQR RND LOG EXP COS SIN TAN ATN PEEK LEN STR$ VAL ASC CHR$ LEFT$ "
             "RIGHT$ MID$").split()  # tokens $80-$EA
# Integer BASIC $00-$7F, "|"-split. ROM repeats a spelling per syntax role
INTBASIC = ("HIMEM:||_ |:|LOAD |SAVE |CON |RUN |RUN |DEL |,|NEW |CLR |AUTO |,|MAN |HIMEM:|LOMEM:|+|-|*|/|=|#|>=|>|"
            "<=|<>|<| AND | OR | MOD |^|+|(|,| THEN | THEN |,|,|\"|\"|(|!|!|(|PEEK |RND |SGN |ABS |PDL |RNDX |(|+|-|"
            "NOT |(|=|#|LEN(|ASC(|SCRN(|,|(|$|$|(|,|,|;|;|;|,|,|,|TEXT|GR |CALL |DIM |DIM |TAB |END|INPUT |"
            "INPUT |INPUT |FOR |=| TO | STEP |NEXT |,|RETURN|GOSUB |REM |LET |GOTO |IF |PRINT |PRINT |PRINT|POKE |,|"
            "COLOR=|PLOT |,|HLIN |,| AT |VLIN |,| AT |VTAB |=|=|)|)|LIST |,|LIST |POP|NODSP |DSP |NOTRACE|DSP |"
            "DSP |TRACE|PR#|IN#").split("|")
PO_SECTOR = (0, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 15)  # ProDOS-order slot -> DOS 3.3 sector (its own inverse)
DOS_TYPES = {0x00: "T", 0x01: "I", 0x02: "A", 0x04: "B", 0x08: "S", 0x10: "R", 0x20: "a", 0x40: "b"}
PRODOS_TYPES = {0x00: "UNK", 0x04: "TXT", 0x06: "BIN", 0x08: "FOT", 0x0F: "DIR", 0x19: "ADB", 0x1A: "AWP",
                0x1B: "ASP", 0xB3: "S16", 0xC0: "PNT", 0xC1: "PIC", 0xE0: "LBR", 0xEF: "PAS", 0xF0: "CMD",
                0xFA: "INT", 0xFB: "IVR", 0xFC: "BAS", 0xFD: "VAR", 0xFE: "REL", 0xFF: "SYS"}
HGR_RGB = {(0, 0): (255, 68, 253), (1, 0): (20, 245, 60), (0, 1): (20, 207, 253), (1, 1): (255, 106, 60)}


def a2_chars(b, newlines=False) -> str:
    """Apple II text as str, control chars as U+2400 symbols. newlines=True turns CR into newlines."""
    out = []
    for c in b:
        c &= 0x7F
        out.append("\n" if newlines and c == 13 else chr(0x2400 + c) if c < 32 else "\u2421" if c == 127 else chr(c))
    return "".join(out)


def applesoft_list(b: bytes) -> list[str]:
    """Detokenize an Applesoft program (no length prefix), one string per LIST line."""
    out, p = [], 0
    while p + 4 <= len(b) and len(out) < 30000:
        if u16(b, p) == 0:
            break
        num, q, s, quoted = u16(b, p + 2), p + 4, [], False
        while q < len(b) and b[q]:
            c = b[q]
            if c == 0x22:
                quoted = not quoted
            if c >= 0x80 and not quoted and c - 0x80 < len(APPLESOFT):
                s.append(("" if not s or s[-1].endswith(" ") else " ") + APPLESOFT[c - 0x80] + " ")
            else:
                s.append(a2_chars(bytes([c])))
            q += 1
        out.append(f"{num} {''.join(s).strip()}")
        p = q + 1
    return out


def intbasic_list(b: bytes) -> list[str]:
    """Detokenize an Integer BASIC program (no length prefix). Lines are length, number, tokens, $01.
    $B0-$B9 after a token starts a 16-bit constant, other high-bit bytes are name letters."""
    out, p = [], 0
    while p + 3 < len(b) and len(out) < 30000:
        end = p + b[p]
        if b[p] < 4 or end > len(b):
            break
        num, q, s, after_tok = u16(b, p + 1), p + 3, [], True
        while q < end and b[q] != 0x01:
            c = b[q]
            if c >= 0x80:
                if after_tok and 0xB0 <= c <= 0xB9 and q + 2 < end:
                    s.append(str(u16(b, q + 1)))
                    q += 3
                    after_tok = False
                    continue
                s.append(a2_chars(bytes([c])))
                after_tok = False
            elif c == 0x28:  # string literal up to the closing quote token
                q += 1
                lit = bytearray()
                while q < end and b[q] != 0x29:
                    lit.append(b[q])
                    q += 1
                s.append('"' + a2_chars(lit) + '"')
                after_tok = True
            elif c == 0x5D:  # REM, rest of line is text
                s.append(" REM " + a2_chars(b[q + 1:end].rstrip(b"\x01")))
                break
            else:
                s.append(INTBASIC[c])
                after_tok = True
            q += 1
        out.append(f"{num} {''.join(s).strip()}")
        p = end
    return out


def hgr_png(data: bytes, stem: str) -> list[str]:
    """8K hi-res screen ($2000/$4000) as two 280x192 PNGs, exact bits and a rough colour-monitor view."""
    mono, rgb = bytearray(280 * 192), bytearray(280 * 192 * 3)
    for y in range(192):
        base = (y & 7) * 0x400 + ((y >> 3) & 7) * 0x80 + (y >> 6) * 0x28
        row = [(data[base + xb] if base + xb < len(data) else 0) for xb in range(40)]
        bits = [(row[x // 7] >> (x % 7)) & 1 for x in range(280)]
        for x in range(280):
            if not bits[x]:
                continue
            mono[y * 280 + x] = 255
            white = (x and bits[x - 1]) or (x < 279 and bits[x + 1])
            rgb[(y * 280 + x) * 3:(y * 280 + x) * 3 + 3] = bytes((255, 255, 255) if white else
                                                                 HGR_RGB[(x & 1, row[x // 7] >> 7)])
    return [str(png_write(Path(stem + ".png"), 280, 192, bytes(mono), "L")),
            str(png_write(Path(stem + ".colour.png"), 280, 192, bytes(rgb), "RGB"))]


def a2_image(d: bytes, src: Path):
    """(DOS-order sector reader, ProDOS block reader, order name, raw image) for a 140K .dsk/.do/.po or a .2mg."""
    if d[:4] == b"2IMG":
        fmt, off, ln = u32(d, 0x0C), u32(d, 0x18), u32(d, 0x1C)
        if fmt == 2:
            raise Bad(".2mg holding a nibble image: not supported (convert it to .po with CiderPress II)")
        d, order = d[off:off + (ln or len(d) - off)], "dos" if fmt == 0 else "prodos"
    elif d[:4] in (b"WOZ1", b"WOZ2") or len(d) in (232960, 223440):
        raise Bad("a .woz or .nib image holds the raw disk signal, which this reader does not decode: convert it to "
                  ".dsk or .po with CiderPress II (or an emulator's disk export), then run a2 on that")
    elif src.suffix.lower() in (".po", ".hdv") or len(d) != 143360:
        order = "prodos"
    else:
        order = "dos"

    def sector(t, s, order=order):
        if order == "prodos" and len(d) == 143360:
            s = PO_SECTOR.index(s)
        o = (t * 16 + s) * 256
        return d[o:o + 256].ljust(256, b"\0")

    def block(n, order=order):
        if order == "dos":
            t, k = divmod(n, 8)
            return sector(t, PO_SECTOR[2 * k], "dos") + sector(t, PO_SECTOR[2 * k + 1], "dos")
        return d[n * 512:n * 512 + 512].ljust(512, b"\0")
    return sector, block, order, d


def dos33_read(sector, t, s):
    """DOS 3.3 file bytes via its track/sector list chain. Holes read as zeros."""
    pairs, seen = [], set()
    while t and (t, s) not in seen and len(seen) < 560 and t < 50 and s < 32:
        seen.add((t, s))
        ts = sector(t, s)
        pairs += [(ts[12 + 2 * i], ts[13 + 2 * i]) for i in range(122)]
        t, s = ts[1], ts[2]
    while pairs and pairs[-1] == (0, 0):
        pairs.pop()
    return b"".join(sector(tt, ss) if (tt or ss) else b"\0" * 256 for tt, ss in pairs[:560])


def dos33_catalog(sector):
    v = sector(17, 0)
    if not (0 < v[1] < 35 and v[2] < 16 and v[0x35] == 16 and v[0x27] == 122):
        return None
    files, deleted, seen = [], 0, set()
    t, s = v[1], v[2]
    while t and (t, s) not in seen and len(seen) < 64 and t < 35 and s < 16:
        seen.add((t, s))
        c = sector(t, s)
        for i in range(7):
            e = c[11 + 35 * i:46 + 35 * i]
            if e[0] == 0:
                continue
            if e[0] == 0xFF:
                deleted += 1
                continue
            files.append({"name": a2_chars(e[3:33]).rstrip(), "type": DOS_TYPES.get(e[2] & 0x7F, f"${e[2] & 0x7F:02X}"),
                          "locked": bool(e[2] & 0x80), "sectors": u16(e, 33), "ts": (e[0], e[1])})
        t, s = c[1], c[2]
    return {"format": "DOS 3.3", "volume": v[6], "files": files, "deleted": deleted}


def prodos_read(block, storage, key, eof, nblocks, depth=0):
    """A ProDOS file's data fork (seedling, sapling, tree or GS/OS extended). Sparse blocks read as zeros."""
    need = min((eof + 511) // 512, 65536)
    ptr = lambda ix, i: ix[i] | ix[256 + i] << 8  # noqa: E731
    get = lambda n: block(n) if 0 < n < nblocks else b"\0" * 512  # noqa: E731
    if storage == 1:
        data = get(key)
    elif storage == 2:
        ix = get(key)
        data = b"".join(get(ptr(ix, i)) for i in range(min(need, 256)))
    elif storage == 3:
        mx, parts = get(key), []
        for j in range((need + 255) // 256):
            ix = get(ptr(mx, j))
            parts += [get(ptr(ix, i)) for i in range(min(256, need - 256 * j))]
        data = b"".join(parts)
    elif storage == 5 and depth == 0:
        x = get(key)
        return prodos_read(block, x[0], u16(x, 1), x[5] | x[6] << 8 | x[7] << 16, nblocks, 1)
    else:
        return b""
    return data[:eof]


def prodos_catalog(block, nbytes):
    k = block(2)
    if not (u16(k, 0) == 0 and k[4] >> 4 == 0xF and k[4] & 15 and k[0x23] == 0x27 and k[0x24] == 0x0D):
        return None
    nblocks = u16(k, 0x29) or nbytes // 512
    files, seen = [], set()

    def walk(key, path, depth):
        b, first = key, True
        while b and b not in seen and len(seen) < 4096 and b < nblocks:
            seen.add(b)
            blk = block(b)
            for i in range(1 if first else 0, 13):
                e = blk[4 + 0x27 * i:4 + 0x27 * (i + 1)]
                st, nl = e[0] >> 4, e[0] & 15
                if st == 0 or nl == 0:
                    continue
                name = path + e[1:1 + nl].decode("ascii", "replace")
                if st == 0xD:
                    if depth < 8:
                        walk(u16(e, 0x11), name + "/", depth + 1)
                    continue
                dt_, tm = u16(e, 0x21), u16(e, 0x23)
                yr = dt_ >> 9
                when = (f"{(2000 if yr < 40 else 1900) + yr}-{dt_ >> 5 & 15:02d}-{dt_ & 31:02d} "
                        f"{tm >> 8 & 31:02d}:{tm & 63:02d}") if dt_ else None
                files.append({"name": name, "type": PRODOS_TYPES.get(e[0x10], f"${e[0x10]:02X}"),
                              "aux": u16(e, 0x1F), "blocks": u16(e, 0x13), "storage": st, "key": u16(e, 0x11),
                              "eof": e[0x15] | e[0x16] << 8 | e[0x17] << 16, "modified": when,
                              "locked": not e[0x1E] & 0x02})
            first = False
            b = u16(blk, 2)
    walk(2, "", 0)
    return {"format": "ProDOS", "volume": k[5:5 + (k[4] & 15)].decode("ascii", "replace"), "files": files,
            "blocks": nblocks}


def cmd_a2(a):
    src = Path(a.file)
    d = src.read_bytes()
    sector, block, order, img = a2_image(d, src)
    cat = dos33_catalog(sector) if len(img) == 143360 else None
    if cat is None and len(img) == 143360:  # extension can lie, try the other order
        other = a2_image(d, src.with_suffix(".po" if order == "dos" else ".dsk"))
        cat = dos33_catalog(other[0])
        if cat:
            sector, block, order = other[:3]
    if cat is None:
        cat = prodos_catalog(block, len(img))
        if cat is None and len(img) == 143360:
            other = a2_image(d, src.with_suffix(".po" if order == "dos" else ".dsk"))
            cat = prodos_catalog(other[1], len(img))
            if cat:
                sector, block, order = other[:3]
    if cat is None:
        raise Bad("no DOS 3.3 or ProDOS catalog: a boot-only game disk with its own loader, a 13-sector (DOS 3.2), "
                  "Pascal or CP/M disk, or not an Apple II image. Boot it in an emulator to see it; its sectors "
                  "are still searchable as data (`strings -n 6`, high bit stripped)")
    out = default_out(src, a.out)
    out.mkdir(parents=True, exist_ok=True)
    used, index, warnings = set(), [], []
    for f in cat["files"]:
        base = safe_part(f["name"]).replace(" ", "_").replace("/", "_")
        while base.lower() in used:
            base += "_"
        used.add(base.lower())
        stem = out / "files" / base
        ent = {k: v for k, v in f.items() if k not in ("ts", "key", "storage")}
        if cat["format"] == "DOS 3.3":
            raw = dos33_read(sector, *f["ts"])
            kind = f["type"].upper()
            addr = None
            if kind in ("A", "I"):
                prog = raw[2:2 + u16(raw, 0)] if len(raw) >= 2 else b""
            elif kind == "B" and len(raw) >= 4:
                addr, prog = u16(raw, 0), raw[4:4 + u16(raw, 2)]
            else:
                prog = raw
            kind = {"A": "applesoft", "I": "integer", "T": "text", "B": "binary"}.get(kind, "other")
        else:
            raw = prodos_read(block, f["storage"], f["key"], f["eof"], cat["blocks"])
            prog, addr = raw, f["aux"] if f["type"] in ("BIN", "SYS", "FOT") else None
            if f["type"] == "SYS":
                addr = 0x2000
            kind = {"BAS": "applesoft", "INT": "integer", "TXT": "text", "BIN": "binary", "SYS": "binary",
                    "FOT": "binary"}.get(f["type"], "other")
        stem.parent.mkdir(parents=True, exist_ok=True)
        saved = [rel(Path(str(stem) + ".raw"), out)]
        Path(str(stem) + ".raw").write_bytes(raw)
        ent.update(kind=kind, bytes=len(prog))
        try:
            if kind in ("applesoft", "integer"):
                lines = (applesoft_list if kind == "applesoft" else intbasic_list)(prog)
                p = Path(str(stem) + ".bas.txt")
                p.write_text("\n".join(lines) + "\n", encoding="utf-8")
                saved.append(rel(p, out))
                ent["lines"] = len(lines)
                if prog and not lines:
                    warnings.append(f"{f['name']}: no BASIC lines decoded (machine code saved as BASIC?)")
            elif kind == "text":  # sequential ends at first NUL, random-access is NUL-separated
                txt = a2_chars(prog, newlines=True)
                cut = txt.find("\u2400")
                parts = [txt[:cut]] if 0 <= cut and cut >= len(txt) - 256 else txt.split("\u2400")
                txt = "\n".join(x for x in parts if x.strip("\n"))
                p = Path(str(stem) + ".txt")
                p.write_text(txt.rstrip("\n") + "\n", encoding="utf-8")
                saved.append(rel(p, out))
            elif kind == "binary":
                ent["load"] = f"${addr:04X}" if addr is not None else None
                p = Path(str(stem) + ".bin")
                p.write_bytes(prog)
                saved.append(rel(p, out))
                if addr in (0x2000, 0x4000) and 8184 <= len(prog) <= 8192:
                    saved += [rel(Path(x), out) for x in hgr_png(prog, str(stem) + ".hgr")]
                    ent["picture"] = "hi-res screen"
        except (IndexError, struct.error) as ex:
            warnings.append(f"{f['name']}: damaged ({ex}); raw bytes saved")
        ent["saved"] = saved
        index.append(ent)
    wjson(out / "index.json", index)
    summary = {"file": str(src), "out": str(out), "format": cat["format"], "sector_order": order,
               "volume": cat["volume"], "files": len(index),
               "by_kind": dict(Counter(e["kind"] for e in index)),
               "catalog": [f"{e['type']:<3} {e.get('sectors', e.get('blocks', ''))!s:>4} {e['name']}"
                           + "  -> " + next((x for x in e["saved"] if x.endswith((".txt", ".hgr.png"))), e["saved"][-1])
                           for e in index][:80],
               **({"deleted_entries": cat["deleted"]} if cat.get("deleted") else {}),
               "warnings": warnings,
               "next": "Read the .bas.txt listings (the program as written: every message, room, rule and number) and "
                       "the .txt files; open the .png screens. `.bin` is machine code or data at its load address "
                       "(a 6502 disassembler reads it). Nothing was run."}
    wjson(out / "summary.json", summary)
    return summary


# ---------------------------------------------------------------- archives and disc images

def iso_entries(f):
    """ISO 9660 file list (Joliet names when present): [(path, size, extent)]."""
    pvd, jol = None, None
    for s in range(16, 64):
        f.seek(s * 2048)
        v = f.read(2048)
        if len(v) < 2048 or v[1:6] != b"CD001":
            break
        if v[0] == 1 and pvd is None:
            pvd = v
        elif v[0] == 2 and v[88:90] == b"%/" and v[90:91] in b"@CE":
            jol = v
        elif v[0] == 255:
            break
    desc = jol or pvd
    if desc is None:
        raise Bad("no ISO 9660 volume descriptor")
    enc = "utf-16-be" if jol else "latin-1"
    out, seen = [], set()

    def walk(extent, size, path, depth):
        if extent in seen or depth > 16:
            return
        seen.add(extent)
        f.seek(extent * 2048)
        data = f.read(min(size, 16 * 1024 * 1024))
        o = 0
        while o < len(data):
            ln = data[o]
            if ln == 0:
                o = (o // 2048 + 1) * 2048
                continue
            ext, sz = u32(data, o + 2), u32(data, o + 10)
            flags, nl = data[o + 25], data[o + 32]
            raw = data[o + 33:o + 33 + nl]
            o += ln
            if raw in (b"\0", b"\1"):
                continue
            name = raw.decode(enc, "replace").split(";")[0].rstrip(".") if raw else "_"
            p = f"{path}/{name}" if path else name
            if flags & 2:
                walk(ext, sz, p, depth + 1)
            else:
                out.append((p, sz, ext))
            if len(out) > 200000:
                return

    walk(u32(desc, 156 + 2), u32(desc, 156 + 10), "", 0)
    return out


OTHER_FORMATS = (
    (b"7z\xbc\xaf\x27\x1c", "7z", "`7z l FILE` / `7z x FILE -oDIR` (p7zip), or `bsdtar -tf FILE`"),
    (b"Rar!", "rar", "`unrar l FILE` or `7z l FILE` or `bsdtar -tf FILE`"),
    (b"MSCF", "Microsoft cabinet", "`cabextract -l FILE` or `7z l FILE` or `bsdtar -tf FILE`"),
    (b"ISc(", "InstallShield cabinet", "`unshield l FILE`"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "OLE compound file (MSI installer?)",
     "`msiextract -l FILE` (msitools), `lessmsi l FILE`, or `7z l FILE`"),
    (b"xar!", "xar (macOS .pkg)", "`pkgutil --expand FILE DIR` on a Mac, or `bsdtar -tf FILE`; the payload is a cpio archive"),
    (b"SIT!", "StuffIt", "`unar FILE` (The Unarchiver) or `lsar FILE`"),
    (b"StuffIt", "StuffIt 5", "`unar FILE` or `lsar FILE`"),
    (b"BinHex", "BinHex", "`unar FILE`"),
    (b"\x1f\x8b", "gzip", "`gunzip -k FILE`, then list the result"),
)


def program_kind(name):
    n = name.lower().replace("\\", "/")
    base = n.rsplit("/", 1)[-1]
    if ".app/" in n + "/":
        return "mac app"
    if base.endswith((".exe", ".com", ".scr", ".cpl")):
        return "installer" if re.match(r"(setup|install|instmsi|uninst|unins\d|_inst|isetup)", base) else "program"
    if base.endswith((".dll", ".ocx", ".drv", ".vbx")):
        return "library"
    if base.endswith((".chm", ".hlp", ".cnt", ".col")):
        return "help"
    if base.endswith((".msi", ".cab", ".pkg", ".mpkg", ".dmg", ".sit", ".hqx", ".ex_", ".dl_", ".hl_", ".ch_")) \
            or base in ("setup.inf", "setup.lst", "data.tag"):
        return "installer"
    if base.endswith((".ini", ".inf", ".reg", ".plist")):
        return "settings"
    return None


def cmd_list(a):
    src = Path(a.archive)
    out = default_out(src, a.out)
    entries, fmt, opener = [], None, None
    head = src.open("rb").read(64)
    if zipfile.is_zipfile(src):
        fmt = "zip" + (" (self-extracting program)" if head[:2] == b"MZ" else "")
        z = zipfile.ZipFile(src)
        entries = [(zi.filename, zi.file_size) for zi in z.infolist() if not zi.is_dir()]
        opener = lambda n: z.open(n)  # noqa: E731
    else:
        with src.open("rb") as f:
            f.seek(0x8001)
            is_iso = f.read(5) == b"CD001"
            if is_iso:
                fmt = "iso9660"
                iso = iso_entries(f)
                entries = [(p, s) for p, s, _ in iso]
                where = {p: (e, s) for p, s, e in iso}

                def opener(n):
                    with src.open("rb") as g:
                        g.seek(where[n][0] * 2048)
                        return io.BytesIO(g.read(where[n][1]))
        if not is_iso and head[:2] != b"MZ" and tarfile.is_tarfile(src):
            fmt = "tar"
            t = tarfile.open(src)
            members = {m.name: m for m in t.getmembers() if m.isfile()}
            entries = [(n, m.size) for n, m in members.items()]
            opener = lambda n: t.extractfile(members[n])  # noqa: E731
    if fmt is None:
        hint = None
        for magic, name, tool in OTHER_FORMATS:
            if head.startswith(magic):
                hint = {"format": name, "use": tool}
        if hint is None and head[:2] == b"MZ":
            blob = src.open("rb").read(4 * 1024 * 1024)
            for sig, name, tool in ((b"Inno Setup", "Inno Setup installer", "`innoextract -l FILE` / `innoextract FILE -d DIR`"),
                                    (b"Nullsoft", "NSIS installer", "`7z l FILE` / `7z x FILE -oDIR`"),
                                    (b"InstallShield", "InstallShield installer",
                                     "`7z x FILE -oDIR` (often works), then `unshield x data1.cab`"),
                                    (b"MSCF", "program with an embedded cabinet", "`7z l FILE` or `cabextract -l FILE`"),
                                    (b"WISE", "Wise installer", "`7z x FILE -oDIR` or e_wise")):
                if sig in blob:
                    hint = {"format": name, "use": tool}
                    break
            if hint is None:
                hint = {"format": "Windows program", "use": "`inspect_app.py pe FILE` reads its resources directly"}
        if hint is None and src.suffix.lower() == ".dmg":
            hint = {"format": "Apple disk image", "use": "`inspect_app.py mac FILE` on a Mac (mounts it read-only), "
                    "elsewhere `7z x FILE -oDIR`"}
        if hint is None:
            hint = {"format": "unknown", "use": "`7z l FILE` or `bsdtar -tf FILE` open most archive and installer formats"}
        hint["use"] = hint["use"].replace("FILE", str(src))
        if shutil.which("bsdtar") and "bsdtar" not in hint["use"]:
            hint["also"] = f"bsdtar is installed here: `bsdtar -tf {src}` lists many of these formats"
        return {"archive": str(src), "listed": False, **hint}
    groups: dict = {}
    for n, s in entries:
        k = program_kind(n)
        if k == "mac app":
            n = n[:n.lower().index(".app/") + 4] if ".app/" in n.lower() else n
            if n in groups.get(k, []):
                continue
        if k:
            groups.setdefault(k, []).append(n if k == "mac app" else f"{n} ({s} B)")
    got = []
    if a.get:
        for n, _ in entries:
            if fnmatch.fnmatch(n.lower(), a.get.lower()) or fnmatch.fnmatch(n.lower().rsplit("/", 1)[-1], a.get.lower()):
                dest = out / "files" / safe_rel(n)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with opener(n) as fi, dest.open("wb") as fo:
                    shutil.copyfileobj(fi, fo)
                got.append(str(dest))
    out.mkdir(parents=True, exist_ok=True)
    wjson(out / "listing.json", [{"name": n, "bytes": s, **({"kind": program_kind(n)} if program_kind(n) else {})}
                                 for n, s in entries])
    nxt = []
    if groups.get("program"):
        nxt.append("--get '<path of the program>' then `inspect_app.py pe` on the copy")
    if groups.get("help"):
        nxt.append("--get '*.chm' (or .hlp) then `inspect_app.py chm`/`hlp`")
    if groups.get("mac app"):
        nxt.append("`inspect_app.py mac` on a zip holding the app, or --get '*.app/*' and pass the copied .app folder")
    if groups.get("installer") and not groups.get("program"):
        nxt.append("the program is inside an installer: --get it, then `inspect_app.py list` on that file")
    return {"archive": str(src), "format": fmt, "entries": len(entries), "total_bytes": sum(s for _, s in entries),
            "candidates": {k: v[:40] for k, v in groups.items()}, "listing": str(out / "listing.json"),
            "extracted": got, "next": nxt}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pe", help="Windows program/DLL resources: icons, cursors, bitmaps, menus, dialogs, strings...")
    s.add_argument("file")
    s.add_argument("--out", help="output folder (default dossier/originals/<name>-inspect/)")
    s.set_defaults(fn=cmd_pe)
    s = sub.add_parser("chm", help="HTML Help: every file, contents tree, index, topic texts")
    s.add_argument("file")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_chm)
    s = sub.add_parser("hlp", help="WinHelp: internal files, titles, keywords, topic text (formatting dropped)")
    s.add_argument("file")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_hlp)
    s = sub.add_parser("mac", help="macOS .app (folder, .zip or .dmg): Info.plist, strings, icons, menus, nibs, images, "
                                   "sounds, data plists, asset catalogues")
    s.add_argument("path")
    s.add_argument("--out")
    s.add_argument("--max-copy-mb", type=int, default=300, help="cap on images, sounds, help pages and asset-catalogue "
                                                                "renditions written (a zip may unpack 4x this)")
    s.set_defaults(fn=cmd_mac)
    s = sub.add_parser("a2", help="Apple II .dsk/.po/.2mg: DOS 3.3/ProDOS catalog, files, BASIC listings, text, "
                                  "hi-res screens as PNG")
    s.add_argument("file")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_a2)
    s = sub.add_parser("list", help="what is inside a zip, tar or ISO image, programs first; --get copies members out")
    s.add_argument("archive")
    s.add_argument("--get", metavar="GLOB", help="copy matching members (path or file-name glob) to OUT/files/")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_list)
    ws_arg = pop_ws_arg() if argv is None else None
    a = ap.parse_args(argv)
    enter_ws(ws_arg)
    try:
        summary = a.fn(a)
    except Bad as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=1))
        sys.exit(2)
    except (struct.error, IndexError, ValueError, UnicodeDecodeError, zipfile.BadZipFile, tarfile.TarError) as e:
        print(json.dumps({"ok": False, "error": f"damaged or unexpected input ({type(e).__name__}: {e})"}, indent=1))
        sys.exit(2)
    except FileNotFoundError as e:
        print(json.dumps({"ok": False, "error": f"not found: {e.filename}"}, indent=1))
        sys.exit(2)
    print(json.dumps(summary, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    from _common import use_heavy
    use_heavy("PIL")
    main()
