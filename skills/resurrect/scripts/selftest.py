#!/usr/bin/env python3
"""Offline self-test. Runs the main commands of every script on small generated inputs, in a temp folder.
No network. Run after changing any script:  python3 selftest.py  (needs Pillow and ffmpeg for the media parts)
"""
from __future__ import annotations

import array
import json
import math
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
results = []


def run(name, args, cwd, expect_ok=True, contains=None):
    r = subprocess.run([PY, *args], cwd=cwd, capture_output=True, text=True, timeout=300,
                       encoding="utf-8", errors="replace")
    ok = (r.returncode == 0) == expect_ok and (contains is None or contains in (r.stdout + r.stderr))
    results.append((name, ok, "" if ok else (r.stderr.strip().splitlines() or r.stdout.strip().splitlines() or ["?"])[-1][:200]))
    return r


def make_media(d: Path):
    sr = 22050
    random.seed(1)
    x = []
    mel = [262, 330, 392, 523]
    for i in range(sr * 12):  # 3.2 s music loop, clicks every 1.7 s
        t = i / sr
        v = 6000 * math.sin(2 * math.pi * mel[int(t / 0.8) % 4] * t) * math.exp(-(t % 0.8) * 4)
        if (t % 1.7) < 0.05:
            v += 12000 * random.uniform(-1, 1)
        x.append(int(max(-32000, min(32000, v))))
    with wave.open(str(d / "a.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(array.array("h", x).tobytes())
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    (d / "frames").mkdir()
    for i in range(8):  # red square moving over blue sky
        im = Image.new("RGB", (160, 120), (90, 160, 230))
        ImageDraw.Draw(im).rectangle((10 + i * 12, 40, 26 + i * 12, 56), fill=(220, 40, 40))
        im.save(d / "frames" / f"f{i:02d}.png")
    gif = [Image.open(d / "frames" / f"f{i:02d}.png") for i in range(4)]
    gif[0].save(d / "t.gif", save_all=True, append_images=gif[1:], duration=[80, 80, 80, 300], loop=0)
    return True


def make_pe(path: Path):
    """Minimal 32-bit Windows program: menu, string table, dialog, shortcut, icon."""
    P = struct.pack

    def w(t):
        return t.encode("utf-16le") + b"\0\0"
    menu = P("<HH", 0, 0) + P("<H", 0x90) + w("&File") + P("<HH", 0, 100) + w("&New\tCtrl+N") + P("<HH", 0x80, 101) + w("E&xit")
    strs = b"".join(P("<H", len(t)) + t.encode("utf-16le") for t in
                    ["", "", "", "", "Creates a new picture\nNew", "Quits Paint"] + [""] * 10)  # ids 96..111
    dlg = P("<IIHhhhh", 0x80C800C0, 0, 1, 0, 0, 120, 60) + w("")[:2] + w("")[:2] + w("Attributes") + P("<H", 8) + w("MS Sans Serif")
    dlg += b"\0" * (-len(dlg) % 4) + P("<IIhhhhH", 0x50010001, 0, 35, 40, 50, 14, 1) + P("<HH", 0xFFFF, 0x80) + w("OK") + P("<H", 0)
    icon = P("<IiiHHIIiiII", 40, 16, 32, 1, 32, 0, 0, 0, 0, 0, 0) + bytes([0, 0, 255, 255]) * 256 + b"\0" * 64
    tree = {3: {1: icon}, 4: {2: menu}, 5: {108: dlg}, 6: {7: strs}, 9: {2: P("<HHHH", 0x89, ord("N"), 100, 0)},
            14: {128: P("<HHH", 0, 1, 1) + P("<BBBBHHIH", 16, 16, 0, 0, 1, 32, len(icon), 1)}}
    leaves = [(t, n, tree[t][n]) for t in sorted(tree) for n in sorted(tree[t])]
    hdr = lambda n: P("<IIHHHH", 0, 0, 0, 0, 0, n)  # noqa: E731
    tdir = 16 + 8 * len(tree)
    ndir = tdir + sum(16 + 8 * len(tree[t]) for t in tree)
    ent = ndir + 24 * len(leaves)
    data = ent + 16 * len(leaves)
    rs, off, k = bytearray(hdr(len(tree))), tdir, 0
    for t in sorted(tree):
        rs += P("<II", t, 0x80000000 | off)
        off += 16 + 8 * len(tree[t])
    for t in sorted(tree):
        rs += hdr(len(tree[t]))
        for n in sorted(tree[t]):
            rs += P("<II", n, 0x80000000 | (ndir + 24 * k))
            k += 1
    for j in range(len(leaves)):
        rs += hdr(1) + P("<II", 1033, ent + 16 * j)
    blob, p = b"", data
    for _, _, b in leaves:
        rs += P("<IIII", 0x1000 + p, len(b), 0, 0)
        b += b"\0" * (-len(b) % 4)
        blob += b
        p += len(b)
    rs += blob
    opt = bytearray(224)
    struct.pack_into("<HI", opt, 0, 0x10B, 0)
    struct.pack_into("<IIIIII", opt, 28, 0x400000, 0x1000, 0x200, 0, 0, 0)
    struct.pack_into("<H", opt, 68, 2)
    struct.pack_into("<I", opt, 92, 16)
    struct.pack_into("<II", opt, 96 + 16, 0x1000, len(rs))
    sec = P("<8sIIII12xI", b".rsrc", len(rs), 0x1000, len(rs) + (-len(rs) % 512), 0x200, 0x40000040)
    head = bytearray(b"MZ" + b"\0" * 58 + P("<I", 0x40)) + b"PE\0\0" + P("<HHIIIHH", 0x14C, 1, 0, 0, 0, 224, 0x102) + opt + sec
    path.write_bytes(bytes(head) + b"\0" * (0x200 - len(head)) + bytes(rs) + b"\0" * (-len(rs) % 512))


def make_bundle(app: Path):
    """Tiny macOS app: Info.plist, UTF-16 .strings, arm64 Mach-O header, 16x16 icns (with Pillow)."""
    import plistlib
    res = app / "Contents" / "Resources"
    (res / "en.lproj").mkdir(parents=True)
    (app / "Contents" / "MacOS").mkdir()
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleName": "Tiny", "CFBundleIdentifier": "org.example.tiny", "CFBundleShortVersionString": "1.2",
        "CFBundleExecutable": "Tiny", "LSMinimumSystemVersion": "10.4"}))
    (res / "en.lproj" / "Localizable.strings").write_text('/* greeting */\n"Hello" = "Hello, world";\n',
                                                          encoding="utf-16")
    (app / "Contents" / "MacOS" / "Tiny").write_bytes(
        struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0) + struct.pack("<6I", 0x32, 24, 1, 0xA0400, 0, 0))
    try:
        from PIL import Image
        Image.new("RGBA", (16, 16), (200, 30, 30, 255)).save(res / "AppIcon.icns")
    except ImportError:
        pass


def _png(w, h, rgb=(200, 30, 30)):
    """Tiny opaque PNG, stdlib only."""
    import zlib

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    rows = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def make_mac_extras(app: Path):
    """Mac bits the Aperture run hit: menu nib, icns, .car, framework media, presets, redirect-only Help Book."""
    import plistlib
    from plistlib import UID
    res = app / "Contents" / "Resources"
    objs, classes = ["$null"], {}

    def add(o):
        objs.append(o)
        return UID(len(objs) - 1)

    def cls(name):
        if name not in classes:
            classes[name] = add({"$classname": name, "$classes": [name, "NSObject"]})
        return classes[name]

    def menu(title, items, name=None):
        d = {"$class": cls("NSMenu"), "NSTitle": add(title), "NSMenuItems": add({"$class": cls("NSMutableArray"),
                                                                                  "NS.objects": items})}
        if name:
            d["NSName"] = add(name)
        return add(d)

    def item(title, key="", mask=0, sub=None, sep=False):
        d = {"$class": cls("NSMenuItem"), "NSTitle": add(title), "NSKeyEquiv": add(key)}
        if mask:
            d["NSKeyEquivModMask"] = mask
        if sub:
            d["NSSubmenu"] = sub
        if sep:
            d["NSIsSeparator"] = True
        return add(d)
    save_as = item("Save As\u2026", "S", 1 << 20)
    file_menu = menu("File", [save_as, item("", sep=True), item("Close", "w", 1 << 20)])
    main = menu("Main Menu", [item("File", sub=file_menu)], "_NSMainMenu")
    conn = add({"$class": cls("NSNibControlConnector"), "NSSource": save_as, "NSLabel": add("saveDocumentAs:")})
    top = add({"$class": cls("NSIBObjectData"), "NSRoot": main, "NSConnections": add(
        {"$class": cls("NSMutableArray"), "NS.objects": [conn]})})
    (res / "en.lproj" / "MainMenu.nib").write_bytes(plistlib.dumps(
        {"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"IB.objectdata": top}, "$objects": objs},
        fmt=plistlib.FMT_BINARY))
    # icon with a PNG entry (ic07, 128 px) and RLE ARGB (ic04, 16 px)
    argb = b"ARGB" + b"".join(bytes((0xFF, v, 0xFB, v)) for v in (255, 30, 60, 200))  # 4 planes of 256 bytes
    ents = [(b"ic07", _png(2, 2)), (b"ic04", argb)]
    body = b"".join(t + struct.pack(">I", len(d) + 8) + d for t, d in ents)
    (res / "Doc.icns").write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)
    # theme catalogue (.car) in a framework, one 2x2 BGRA rendition
    fw = app / "Contents" / "Frameworks" / "ProKit.framework" / "Versions" / "A" / "Resources"
    fw.mkdir(parents=True)
    px = bytes((0, 0, 255, 255, 0, 255, 0, 255, 255, 0, 0, 255, 0, 0, 0, 0))  # red green / blue clear
    body = b"MLEC" + struct.pack("<3I", 1, 0, len(px)) + px
    csi = (b"ISTC" + struct.pack("<5I", 1, 0, 2, 2, 100) + b"BGRA" + struct.pack("<IIHH", 0, 0, 0, 0)
           + b"Theme.png".ljust(128, b"\0") + struct.pack("<4I", 0, 0, 0, len(body)) + body)
    blocks = [b"", b"tmfk" + struct.pack("<4I", 0, 2, 17, 12),
              b"tree" + struct.pack(">4IB", 1, 3, 4096, 1, 0),
              struct.pack(">HHII", 1, 1, 0, 0) + struct.pack(">II", 5, 4),
              struct.pack("<2H", 7, 1), csi]
    data, addrs = bytearray(b"\0" * 32), []
    for b in blocks:
        addrs.append((len(data), len(b)) if b else (0, 0))
        data += b
    index_off = len(data)
    data += struct.pack(">I", len(blocks)) + b"".join(struct.pack(">II", *a) for a in addrs)
    vars_off = len(data)
    names = [(1, b"KEYFORMAT"), (2, b"RENDITIONS")]
    data += struct.pack(">I", len(names)) + b"".join(struct.pack(">IB", i, len(n)) + n for i, n in names)
    data[:32] = b"BOMStore" + struct.pack(">6I", 1, len(blocks), index_off, vars_off - index_off, vars_off,
                                          len(data) - vars_off)
    (fw / "ProThemeBits.car").write_bytes(bytes(data))
    (fw / "button.png").write_bytes(_png(3, 2))
    with wave.open(str(fw / "click.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\0\0" * 800)
    # presets holding a keyed archive as bytes
    arch = plistlib.dumps({"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": UID(1)},
                           "$objects": ["$null", {"$class": UID(2), "NS.keys": [UID(3)], "NS.objects": [UID(4)]},
                                        {"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]},
                                        "exposure", 0.5]}, fmt=plistlib.FMT_BINARY)
    (res / "FactoryPresets.plist").write_bytes(plistlib.dumps([{"name": "Warm", "data": arch}]))
    hb = res / "Tiny.help" / "Contents" / "Resources" / "English.lproj"
    hb.mkdir(parents=True)
    (hb / "index.html").write_text('<html><head><meta http-equiv="refresh" content="0;url=https://help.example.com/'
                                   'tiny/1.2/"><title>Tiny Help</title></head><body>Loading</body></html>',
                                   encoding="utf-8")


def make_pdf(path: Path, w=60, h=40):
    """One-page PDF: text plus a Flate RGB picture (PNG predictors). Stdlib only."""
    import zlib
    img = zlib.compress(b"".join(b"\0" + b"".join(bytes((x * 4 % 256, y * 6 % 256, 120)) for x in range(w))
                                 for y in range(h)))
    content = b"q 120 0 0 80 10 10 cm /Im1 Do Q BT /F1 12 Tf 10 150 Td (Hello manual) Tj ET"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /XObject << /Im1 5 0 R >> /Font << "
            b"/F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Contents 4 0 R >>", b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB /BitsPerComponent 8 "
            b"/Filter /FlateDecode /DecodeParms << /Predictor 15 /Colors 3 /Columns %d >> /Length %d >>\nstream\n"
            % (w, h, w, len(img)) + img + b"\nendstream"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)
    path.write_bytes(bytes(out))


def enhance_ok():
    """enhance.py needs numpy, here or in the `enhance.py setup` env."""
    if subprocess.run([PY, "-c", "import numpy"], capture_output=True).returncode == 0:
        return True
    sys.path.insert(0, str(HERE))
    from enhance import heavy_python
    if heavy_python():
        return True
    results.append(("enhance (skipped: no numpy; run enhance.py setup)", True, ""))
    return False


PO_SECTOR = (0, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 15)


def make_a2_dos33(path: Path, po_order: bool = False):
    """140K DOS 3.3 disk: Applesoft, Integer BASIC, text and 8K hi-res files.
    po_order writes it in ProDOS sector order, like some .po files."""
    img = bytearray(143360)

    def put(t, s, data):
        o = (t * 16 + s) * 256
        img[o:o + len(data)] = data
    vtoc = bytearray(256)
    vtoc[1], vtoc[2], vtoc[3], vtoc[6], vtoc[0x27], vtoc[0x34], vtoc[0x35], vtoc[0x37] = 17, 15, 3, 254, 122, 35, 16, 1
    put(17, 0, vtoc)
    # Applesoft: 10 PRINT "HI" / 20 GOTO 10 ($BA, $AB)
    l1 = struct.pack("<HH", 0x080B, 10) + b'\xba"HI"\0'
    l2 = struct.pack("<HH", 0x0813, 20) + b"\xab10\0"
    prog = l1 + l2 + b"\0\0"
    # same in Integer BASIC. $28/$29 quote, $B1 = 16-bit const
    ib = bytes([9, 10, 0, 0x61, 0x28, 0xC8, 0xC9, 0x29, 0x01, 8, 20, 0, 0x5F, 0xB1, 10, 0, 0x01])
    hgr = bytearray(8192)
    hgr[0:10] = b"\x7f" * 10  # row 0: 70 lit pixels
    files = [("HELLO", 0x02, struct.pack("<H", len(prog)) + prog), ("INTPROG", 0x01, struct.pack("<H", len(ib)) + ib),
             ("NOTES", 0x00, bytes(c | 0x80 for c in b"DOOM VALLEY\rTHE END\r") + b"\0"),
             ("PIC", 0x04, struct.pack("<HH", 0x2000, 8192) + bytes(hgr))]
    cat = bytearray(256)
    t, s = 18, 0
    for i, (name, ftype, data) in enumerate(files):
        secs = [data[k:k + 256] for k in range(0, len(data), 256)]
        ts_list = bytearray(256)
        ts_at = (t, s)
        for j, chunk in enumerate(secs):
            s += 1
            if s == 16:
                t, s = t + 1, 0
            ts_list[12 + 2 * j], ts_list[13 + 2 * j] = t, s
            put(t, s, chunk)
        put(*ts_at, ts_list)
        e = 11 + 35 * i
        cat[e], cat[e + 1], cat[e + 2] = ts_at[0], ts_at[1], ftype
        cat[e + 3:e + 33] = bytes(c | 0x80 for c in name.ljust(30).encode())
        cat[e + 33:e + 35] = struct.pack("<H", len(secs) + 1)
        t, s = t + 1, 0
    put(17, 15, cat)
    if po_order:
        img = bytearray(b"".join(img[(tt * 16 + PO_SECTOR[p]) * 256:(tt * 16 + PO_SECTOR[p]) * 256 + 256]
                                 for tt in range(35) for p in range(16)))
    path.write_bytes(bytes(img))


def make_a2_prodos(path: Path):
    """140K ProDOS volume with a BAS and a TXT file, each a one-block seedling."""
    img = bytearray(143360)
    key = bytearray(512)
    key[4] = 0xF0 | 4
    key[5:9] = b"TEST"
    key[0x23], key[0x24], key[0x25] = 0x27, 0x0D, 2
    key[0x29:0x2B] = struct.pack("<H", 280)
    prog = struct.pack("<HH", 0x080B, 10) + b'\xba"HI"\0' + b"\0\0"
    text = b"LINE ONE\rLINE TWO\r"
    for i, (name, ftype, blk, data) in enumerate([(b"STARTUP", 0xFC, 7, prog), (b"README", 0x04, 8, text)], 1):
        e = 4 + 0x27 * i
        key[e] = 0x10 | len(name)
        key[e + 1:e + 1 + len(name)] = name
        key[e + 0x10] = ftype
        key[e + 0x11:e + 0x13] = struct.pack("<H", blk)
        key[e + 0x13:e + 0x15] = struct.pack("<H", 1)
        key[e + 0x15:e + 0x18] = struct.pack("<I", len(data))[:3]
        key[e + 0x1E] = 0xE3
        key[e + 0x1F:e + 0x21] = struct.pack("<H", 0x0801 if ftype == 0xFC else 0)
        img[blk * 512:blk * 512 + len(data)] = data
    img[1024:1536] = key
    path.write_bytes(bytes(img))


def png_row(path: Path, y: int) -> bytes:
    """Row y of an 8-bit grey PNG with filter 0 (inspect_app's writer). Stdlib only."""
    b = path.read_bytes()
    w = struct.unpack(">I", b[16:20])[0]
    data, p = b"", 8
    while p < len(b):
        n, typ = struct.unpack(">I", b[p:p + 4])[0], b[p + 4:p + 8]
        if typ == b"IDAT":
            data += b[p + 8:p + 8 + n]
        p += 12 + n
    raw = zlib.decompress(data)
    return raw[y * (w + 1) + 1:(y + 1) * (w + 1)]


def ledger_a2_checks(S, d: Path, has_img: bool):
    # ledger: batch keys, edit, pack grouping/files/phase/guard
    r = run("ledger init (second workspace)", [S("ledger.py"), "init", "Map Game", "--type", "game", "--budget", "quick",
                                               "--root", str(d / "ws2")], d)
    results.append(("ledger init no longer points at rounds", "round start" not in r.stdout, r.stdout[-200:]))
    ws2 = d / "ws2" / "map-game"
    loc = "map p. 3, the long caption under the box diagram in the lower-left corner"
    k1 = d / "k1.jsonl"
    k1.write_text("\n".join(json.dumps(x) for x in [
        {"tag": "seen", "kind": "content", "claim": "Room A leads north to B", "source": "https://e.org/map",
         "locator": loc, "as_of": "1982", "key": "recD"},
        {"tag": "seen", "kind": "content", "claim": "Room B leads east to C", "source": "https://e.org/map",
         "locator": loc, "as_of": "1982"},
        {"tag": "seen", "kind": "content", "claim": "Room C holds a lamp", "source": "https://e.org/map",
         "locator": loc, "as_of": "1982"},
        {"tag": "inferred", "kind": "flow", "claim": "The lamp is needed", "feature": "flow", "reasoning": "dark",
         "key": "recA"},
        {"tag": "inferred", "kind": "behavior", "claim": "Dark rooms kill", "derived_from": ["@recA", "@recD"]},
        {"tag": "seen", "kind": "copy", "claim": "Ad gives the publisher's phone", "source": "https://e.org/ad",
         "quote": "call 555-123-4567 now", "as_of": "1982"}]) + "\n", encoding="utf-8")
    run("ledger add-batch: a rejected key's dependants say why, and which ID a good key is",
        [S("ledger.py"), "add-batch", str(k1), "--ws", str(ws2)], d, contains="use E0001 for @recD")
    rej = d / "k1.rejected.jsonl"
    results.append(("dependant line names the rejected line", rej.exists() and "defined on line 4" in rej.read_text(encoding="utf-8"),
                    rej.read_text(encoding="utf-8")[:200] if rej.exists() else "no rejected file"))
    rej.write_text(rej.read_text(encoding="utf-8").replace('"feature": "flow"', '"feature": "lamp"'), encoding="utf-8")
    run("ledger add-batch: the fixed rejected file merges", [S("ledger.py"), "add-batch", str(rej), "--ws", str(ws2)], d,
        contains='"added": 2')
    k2 = d / "k2.jsonl"
    k2.write_text(json.dumps({"tag": "inferred", "kind": "behavior", "claim": "The map is a loop",
                              "derived_from": ["@recD", "@recA"]}) + "\n", encoding="utf-8")
    run("ledger add-batch: a later file cites earlier files' keys", [S("ledger.py"), "add-batch", str(k2), "--ws", str(ws2)],
        d, contains='"added": 1')
    evs = [json.loads(x) for x in (ws2 / "evidence.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    loop = next((e for e in evs if e["claim"] == "The map is a loop"), {})
    results.append(("earlier keys resolve to their IDs", loop.get("derived_from") == ["E0001", "E0005"], str(loop)[:200]))
    for n, claim in ((3, "Other agent fact one"), (4, "Other agent fact two")):
        (d / f"k{n}.jsonl").write_text(json.dumps({"tag": "inferred", "kind": "behavior", "claim": claim,
                                                   "reasoning": "x", "key": "k1"}) + "\n", encoding="utf-8")
        run(f"ledger add-batch k{n} (key k1)", [S("ledger.py"), "add-batch", str(d / f"k{n}.jsonl"), "--ws", str(ws2)], d)
    (d / "k5.jsonl").write_text(json.dumps({"tag": "inferred", "kind": "behavior", "claim": "Cites k1",
                                            "derived_from": ["@k1"]}) + "\n", encoding="utf-8")
    run("ledger add-batch: a key two files bound differently needs the ID",
        [S("ledger.py"), "add-batch", str(d / "k5.jsonl"), "--ws", str(ws2)], d, contains="cite the ID")
    run("ledger add-batch: re-adding a merged line points at edit", [S("ledger.py"), "add-batch", str(k1), "--ws", str(ws2)],
        d, contains="ledger.py edit E0001")
    run("ledger add-batch --help lists the reserved feature words", [S("ledger.py"), "add-batch", "--help"], d,
        contains="sentiment legal asset tech business other verify")
    ad = next(e["id"] for e in evs if "phone" in e["claim"])
    run("ledger edit --quote", [S("ledger.py"), "edit", ad, "--quote", "call [number removed] now", "--why", "law 3",
                                "--forget", "--ws", str(ws2)], d, contains="quote updated")
    evs = [json.loads(x) for x in (ws2 / "evidence.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    e = next(x for x in evs if x["id"] == ad)
    results.append(("edit keeps the ID, records the change, forgets the old text",
                    e["quote"] == "call [number removed] now" and e["edits"][0]["fields"] == ["quote"]
                    and "555" not in json.dumps(e), str(e)[:200]))
    run("ledger edit rejects a seen item left without quote or locator",
        [S("ledger.py"), "edit", ad, "--quote", "", "--ws", str(ws2)], d, expect_ok=False, contains="not edited")
    pk = ws2 / "pack"
    (pk / "reference").mkdir(parents=True, exist_ok=True)
    (pk / "reference" / "title.txt").write_text("title screen transcription\n", encoding="utf-8")
    (pk / "README.md").write_text("# Pack\n\nScope:\n- Title: `reference/title.txt`\n- Map: `reference/map.png`\n"
                                  "- Ending: `ending.gif`\n- Tools: `verify.py run --serve app`, `app/index.html`\n",
                                  encoding="utf-8")
    if has_img:
        from PIL import Image
        Image.new("RGB", (64, 48), (0, 0, 0)).save(pk / "reference" / "blank.png")
    r = run("ledger pack names missing reference files", [S("ledger.py"), "pack", "--ws", str(ws2)], d,
            contains="reference/map.png, ending.gif")
    results.append(("pack leaves commands and app paths alone", "verify.py" not in r.stdout and "app/index" not in r.stdout,
                    r.stdout[-200:]))
    if has_img:
        results.append(("pack flags a blank reference image", "reference/blank.png" in r.stdout, r.stdout[-200:]))
    facts = (pk / "FACTS.md").read_text(encoding="utf-8")
    results.append(("FACTS.md groups facts that share source and locator",
                    "3 facts from" in facts and facts.count(loc) == 1, facts[:200]))
    r = run("ledger status after pack", [S("ledger.py"), "status", "--ws", str(ws2)], d, contains='"phase": "rebuild"')
    run("ledger pack x2 (unchanged)", [S("ledger.py"), "pack", "--ws", str(ws2)], d)
    run("runaway guard: third unchanged pack warns", [S("ledger.py"), "pack", "--ws", str(ws2)], d,
        contains="runaway guard")
    (pk / "reference" / "map.png").write_bytes(b"x")
    r = run("ledger pack after a pack change", [S("ledger.py"), "pack", "--ws", str(ws2)], d)
    results.append(("runaway guard: a changed pack is not a repeat", "runaway guard" not in r.stderr, r.stderr[-200:]))
    # inspect_app a2: DOS 3.3 (both orders) and ProDOS, parse only
    make_a2_dos33(d / "game.dsk")
    r = run("inspect_app a2 (DOS 3.3)", [S("inspect_app.py"), "a2", str(d / "game.dsk"), "--out", str(d / "a2")], d,
            contains='"format": "DOS 3.3"')
    fl = d / "a2" / "files"
    bas = (fl / "HELLO.bas.txt").read_text(encoding="utf-8") if (fl / "HELLO.bas.txt").exists() else ""
    results.append(("a2 detokenizes Applesoft", bas.split("\n")[:2] == ['10 PRINT "HI"', "20 GOTO 10"], bas[:100]))
    ib = (fl / "INTPROG.bas.txt").read_text(encoding="utf-8") if (fl / "INTPROG.bas.txt").exists() else ""
    results.append(("a2 detokenizes Integer BASIC", ib.split("\n")[:2] == ['10 PRINT "HI"', "20 GOTO 10"], ib[:100]))
    txt = (fl / "NOTES.txt").read_text(encoding="utf-8") if (fl / "NOTES.txt").exists() else ""
    results.append(("a2 text file as text", txt == "DOOM VALLEY\nTHE END\n", repr(txt[:80])))
    pic = fl / "PIC.hgr.png"
    row = png_row(pic, 0) if pic.exists() else b""
    results.append(("a2 hi-res screen to a 280x192 PNG", len(row) == 280 and set(row[:70]) == {255} and not any(row[70:])
                    and (fl / "PIC.hgr.colour.png").exists(), f"{len(row)} px"))
    make_a2_dos33(d / "game_po.po", po_order=True)
    run("inspect_app a2: DOS 3.3 in ProDOS order", [S("inspect_app.py"), "a2", str(d / "game_po.po"), "--out",
                                                    str(d / "a2po")], d, contains="HELLO.bas.txt")
    make_a2_prodos(d / "vol.po")
    run("inspect_app a2 (ProDOS)", [S("inspect_app.py"), "a2", str(d / "vol.po"), "--out", str(d / "a2p")], d,
        contains="STARTUP.bas.txt")
    pb = d / "a2p" / "files"
    results.append(("a2 ProDOS BAS and TXT", (pb / "STARTUP.bas.txt").exists() and
                    (pb / "STARTUP.bas.txt").read_text(encoding="utf-8").startswith('10 PRINT "HI"') and
                    (pb / "README.txt").read_text(encoding="utf-8") == "LINE ONE\nLINE TWO\n", ""))
    po = (d / "vol.po").read_bytes()
    (d / "vol.dsk").write_bytes(b"".join(po[(t * 16 + PO_SECTOR.index(s)) * 256:(t * 16 + PO_SECTOR.index(s)) * 256 + 256]
                                         for t in range(35) for s in range(16)))
    run("inspect_app a2: ProDOS in DOS order (.dsk)", [S("inspect_app.py"), "a2", str(d / "vol.dsk"), "--out",
                                                       str(d / "a2pd")], d, contains='"format": "ProDOS"')
    (d / "x.woz").write_bytes(b"WOZ2" + b"\0" * 1000)
    run("inspect_app a2 says .woz is unsupported", [S("inspect_app.py"), "a2", str(d / "x.woz")], d, expect_ok=False,
        contains="CiderPress")


def wayback_shots_checks(d: Path, results: list):
    import argparse
    import contextlib
    import io
    import json
    sys.path.insert(0, str(HERE))
    import wayback
    import _common

    # page fingerprints: parking, bare-domain title, WordPress
    fp_park = wayback.page_fingerprint('<frameset><frame src="http://x.com/?fp=abc&poru=1"></frameset>', "x.com")
    fp_dom = wayback.page_fingerprint("<title>Closerangegame.com</title>", "www.closerangegame.com")
    fp_wp = wayback.page_fingerprint('<title>COVID-19 News - Close Range Game</title><meta name="generator" '
                                     'content="WordPress 4.9" />', "closerangegame.com")
    orig = wayback.page_fingerprint("<title>Close Range Game</title><h1>Play</h1>", "closerangegame.com")
    results.append(("wayback fingerprint: parking page, bare-domain title, WordPress",
                    bool(fp_park["parked"]) and bool(fp_dom["parked"]) and fp_wp["cms"] == "wordpress"
                    and not orig["parked"], json.dumps([fp_park, fp_dom, fp_wp])[:200]))
    results.append(("wayback same_site: a later owner's WordPress sharing the name is not the original",
                    not wayback.same_site(fp_wp, orig) and wayback.same_site(orig, orig)
                    and not wayback.same_site(fp_park, orig), ""))

    # fake archive. Original 2009-2010, parked 2011-2014, WordPress squatter 2020
    def page_at(ts):
        y = int(ts[:4])
        if y <= 2010:
            return b"<title>Close Range Game</title><a href='close_range.html'>Play</a>"
        if y <= 2014:
            return b'<frameset><frame src="http://closerangegame.com/?fp=zz&poru=1"></frameset>'
        return b'<title>News - Close Range Game</title><link href="/wp-content/themes/x.css">'
    root_rows = [{"timestamp": f"{y}{m:02d}01000000", "original": "http://closerangegame.com/",
                  "mimetype": "text/html", "digest": f"D{y if y <= 2010 else y * 100 + m}", "length": "1600"}
                 for y in range(2009, 2022) for m in (1, 4, 7, 10) if not 2015 <= y <= 2019]
    listing = [{"timestamp": "20090501000000", "original": "http://www.closerangegame.com/img/bg.jpg",
                "mimetype": "image/jpeg", "length": "70000"},
               {"timestamp": "20090501000000", "original": "http://www.closerangegame.com/close_range.html",
                "mimetype": "text/html", "length": "900"},
               {"timestamp": "20210101000000", "original": "http://closerangegame.com/wp-admin/js/x.min.js",
                "mimetype": "application/javascript", "length": "9000"}]
    swf_rows = [{"timestamp": "20110510000000", "original": "http://www.closerangegame.com/close_range.swf",
                 "mimetype": "text/html", "statuscode": "200"},
                {"timestamp": "20110821000000", "original": "http://www.closerangegame.com/close_range.swf",
                 "mimetype": "text/html", "statuscode": "302"},
                {"timestamp": "20100101000000", "original": "http://www.closerangegame.com/theme.mp3",
                 "mimetype": "audio/mpeg", "statuscode": "200"}]

    def fake_cdx(url, **k):
        if any(str(f).startswith("original:") for f in k.get("filters", ())):
            return iter(swf_rows)
        if "timestamp:6" in (k.get("collapse") or ()):
            return iter(root_rows)
        return iter(listing)

    def fake_fetch(url, **k):
        import re as _re
        ts = _re.search(r"/web/(\d{14})", url).group(1)
        body = page_at(ts)
        return (body, url) if k.get("want_url") else body
    real = wayback.cdx_query, wayback.fetch
    wayback.cdx_query, wayback.fetch = fake_cdx, fake_fetch
    out, err = io.StringIO(), io.StringIO()
    try:
        eras = wayback.site_eras("closerangegame.com")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            wayback.cmd_inventory(argparse.Namespace(
                domain="closerangegame.com", frm=None, to=None, limit=10, pages=1, max_seconds=60, max_files=400,
                data=True, list_pages=False, assets_only=False, images=False, format="jsonl", all_eras=False,
                no_eras=False))
    finally:
        wayback.cdx_query, wayback.fetch = real
    later = (eras or {}).get("later") or {}
    results.append(("wayback site_eras finds the owner change (parking from 2011)",
                    bool(eras) and eras["original"]["to"].startswith("2010") and later.get("from", "").startswith("2011")
                    and bool(later.get("parked")), json.dumps(eras)[:200]))
    o, e = out.getvalue(), err.getvalue()
    results.append(("wayback inventory leaves out the later owner's files and says so",
                    "wp-admin" not in o and "bg.jpg" in o and "later hosted a different site (from 2011" in e
                    and "original itself" not in e, (e + o)[-200:]))
    results.append(("wayback inventory lists close_range.swf as known, never captured (not the mp3)",
                    '"known, never captured"' in o and "close_range.swf" in o
                    and "theme.mp3" not in [json.loads(l).get("url", "").rsplit("/", 1)[-1] for l in o.splitlines()
                                            if l.startswith('{"kind"') and "never" in l], o[-200:]))

    # known --format md and csv
    es = [{"first": f"199{i}-01-01", "last": f"199{i}-12-31", "ts": f"199{i}0101000000", "url": "http://a/x.html",
           "bytes": 1000 + i} for i in range(9)]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        _common.emit([{"url": "http://a/x.html", "eras": wayback.md_eras(es, "http://a/x.html"), "note": "y" * 300}],
                     "md", keep=("eras",))
    results.append(("known md eras printed whole (other long cells still cut)",
                    "1998-01-01..1998-12-31:1008" in buf.getvalue() and "y" * 121 not in buf.getvalue(),
                    buf.getvalue()[-200:]))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _common.emit([{"a": 1, "b": ["x", "y"]}, {"a": 2, "c": {"k": 1}}], "csv")
    results.append(("emit csv: every column, lists joined", buf.getvalue().splitlines()[0] == "a,b,c"
                    and "x; y" in buf.getvalue(), buf.getvalue()[:200]))

    # links: extract, merge by normalised URL
    html_ = ('<a href="/%7Ebob/index.html">Bob\'s page</a> <A HREF=http://www.foo.com/x#top><img alt="Foo"></A>'
             '<a href="mailto:a@b">mail</a>')
    l1 = wayback.extract_links(html_, "http://host.edu/list.html")
    found = [("http://host.edu/list.html", "19980101000000", l1),
             ("http://host.edu/list.html", "19990601000000", [("Bob", "http://host.edu/~bob/")]),
             ("http://yahoo.com/cat/", "19970301000000", [("Foo site", "http://foo.com/x")])]
    rows = {r["url"].rsplit("/", 2)[-2] if r["url"].endswith("/") else r["url"]: r for r in wayback.merge_links(found)}
    bob = next(r for r in wayback.merge_links(found) if "bob" in r["url"])
    foo = next(r for r in wayback.merge_links(found) if "foo" in r["url"])
    results.append(("wayback links: anchors and img alt, merged with first/last and sources",
                    len(l1) == 2 and bob["first"] == "1998-01-01" and bob["last"] == "1999-06-01" and bob["seen"] == 2
                    and foo["first"] == "1997-03-01" and len(foo.get("sources", {})) == 2 and len(rows) == 2,
                    json.dumps(wayback.merge_links(found))[:200]))

    # find on host/path: prefix query, not a domain scan
    seen = []
    wayback.cdx_query = lambda url, **k: (seen.append((url, k.get("match"))), iter([]))[1]
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            wayback.cmd_find(argparse.Namespace(domain="home.att.net/~natehall", keyword="swf", case=False, mime=None,
                                                any_status=False, prefix=None, max_seconds=30, frm=None, to=None,
                                                limit=10, pages=1, format="jsonl"))
    finally:
        wayback.cdx_query = real[0]
    results.append(("wayback find on a host/path does a prefix query on the path",
                    seen[:1] == [("home.att.net/~natehall", "prefix")] and ("home.att.net/%7Enatehall", "prefix") in seen,
                    str(seen)))

    # mirror gates program files behind --original, same as fetch
    def mfetch(url, **k):
        body = (b"<html><body><img src='a.gif'><embed src='game.swf'></body></html>" if url.endswith(".html")
                else b"GIF89a" + b"\0" * 20 if url.endswith(".gif") else b"FWS\x08" + b"\0" * 20)
        return (body, url) if k.get("want_url") else body
    wayback.cdx_query, wayback.fetch = (lambda *a_, **k_: iter([])), mfetch
    mout = d / "mirror-gate"
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as merr:
            wayback.cmd_mirror(argparse.Namespace(
                url="http://www.uni.edu/~kid/index.html", ts="19990101000000", out=str(mout), depth=0, max_pages=2,
                max_mb=5, max_seconds=60, whole_host=True, offsite=False, died=None, drift=3, max_captured=0,
                max_lookups=0, original=False, format="jsonl"))
    finally:
        wayback.cdx_query, wayback.fetch = real
    man = json.loads((mout / "MANIFEST.json").read_text(encoding="utf-8"))
    swf_missing = [m for m in man["missing"] if m["url"].endswith("game.swf")]
    results.append(("wayback mirror gates program files behind --original; warns on ~user --whole-host",
                    bool(swf_missing) and "--original" in swf_missing[0]["why"]
                    and not any(k.endswith(".swf") for k in man["files"]) and any(k.endswith(".gif") for k in man["files"])
                    and "shared server" in merr.getvalue(), json.dumps(man["missing"])[:200]))

    # shots: far-off sub-resources and the blank-image test
    import shots
    far = shots.far_resources([("https://web.archive.org/web/20000801000000im_/http://counter.x/c.gif", 200),
                               ("https://web.archive.org/web/19981201000000im_/http://a/b.gif", 200),
                               ("https://web.archive.org/web/20050101000000im_/http://a/err.gif", 404)],
                              "19981202133702")
    results.append(("shots far_resources lists images served > 6 months off the page",
                    len(far) == 1 and "counter.x" in far[0] and "+20 mo" in far[0], str(far)))
    try:
        from PIL import Image, ImageDraw
        Image.new("RGB", (200, 100), "white").save(d / "blank.png")
        im = Image.new("RGB", (200, 100), "white")
        ImageDraw.Draw(im).rectangle((10, 10, 120, 60), fill="navy")
        im.save(d / "page.png")
        results.append(("shots flat_image flags a blank capture, not a real one",
                        bool(shots.flat_image(d / "blank.png")) and shots.flat_image(d / "page.png") is None, ""))
    except ImportError:
        results.append(("shots flat_image (skipped: no Pillow)", True, ""))


def community_checks(d: Path, results: list):
    import argparse
    import contextlib
    import io
    sys.path.insert(0, str(HERE))
    # --- community.py and archive_search.py (offline, pure helpers and fake fetches, no network) ---
    # lives in main()'s TemporaryDirectory block, after the wayback check
    # (needs the imports just above)
    import community
    import archive_search
    url_c, thread_c = community.reddit_urls({"id": "c1", "link_id": "t3_abc", "permalink": None}, "comment")
    url_s, thread_s = community.reddit_urls({"id": "abc", "permalink": ""}, "submission")
    results.append(("community reddit comment without permalink keeps its own url",
                    url_c == "https://www.reddit.com/comments/abc/_/c1/" and thread_c == thread_s
                    and url_s == "https://www.reddit.com/comments/abc/", f"{url_c} {thread_c} {url_s}"))
    p1 = community.place_of({"source": "reddit", "url": url_c, "thread": thread_c})
    p2 = community.place_of({"source": "reddit", "url": "https://www.reddit.com/comments/zz/_/c2/",
                             "thread": "https://www.reddit.com/comments/zz/"})
    p3 = community.place_of({"source": "reddit", "url": "", "thread": "", "id": "q9"})
    results.append(("community place: one per thread, never the hash of an empty string",
                    p1 == community.place_of({"source": "reddit", "thread": thread_c}) and len({p1, p2, p3}) == 3
                    and "da39a3ee5e" not in (p1, p2, p3), f"{p1} {p2} {p3}"))
    old = community.search_plan("Doom Valley", "game", community.era_year(None, None, ["Doom Valley 1982"]))
    new = community.search_plan("Snake", "game", community.era_year("2015", None, ["Snake"]))
    results.append(("community retro plan for a 1982 game: Usenet, CASA, no app-era forums",
                    any("solutionarchive.com" in x for x in old) and any("comp.sys.apple2" in x for x in old)
                    and not any(f in " ".join(old) for f in ("toucharcade", "kongregate", "steamcommunity"))
                    and any("toucharcade" in x for x in new), " | ".join(old)[:200]))
    results.append(("community --videos takes a count or explicit ids",
                    community.video_arg("3") == (3, []) and community.video_arg(
                        "PYQhvW-tjNM,https://www.youtube.com/watch?v=yuTkgi7scKo") ==
                    (2, ["PYQhvW-tjNM", "yuTkgi7scKo"]), str(community.video_arg("PYQhvW-tjNM"))))
    k = community._norm_name
    cands = [{"id": "song", "title": "Yatta Bandz - Close Range (Official Video)", "channel": "Yatta Bandz",
              "views": 605915, "rank": 100},
             {"id": "onion", "title": "Video Game Consists Solely Of Shooting People In Face", "channel": "The Onion",
              "views": 1731506, "rank": 0},
             {"id": "film", "title": "At Close Range Official Trailer", "channel": "Rotten Tomatoes",
              "views": 167117, "rank": 101}]
    picked = [c["id"] for c in community.pick_videos(cands, [k("Close Range")], [], [k("The Onion")], [], 3)[0]]
    results.append(("community discuss picks the maker's video over most-watched namesakes",
                    picked == ["onion"], str(picked)))
    posts = ([{"title": "", "text": f"omg close range {i}", "score": i} for i in range(20)]
             + [{"title": "", "text": "In Close Range the menu screen had a red button and the levels looked "
                                      "like a gallery; you played it with the mouse", "score": 0}])
    ranked, cut = community.rank_posts(posts, ["Close Range"], 5)
    results.append(("community discuss: descriptive posts first, one-liners capped",
                    len(ranked) == 6 and cut == 15 and "menu screen" in ranked[0]["text"]
                    and ranked[1]["score"] == 19, f"{len(ranked)} {cut} {ranked[0]['text'][:30]}"))
    # archive_search: fuzzy results, leaf mapping, grep, walk
    results.append(("archive search quotes a bare multi-word name, leaves Lucene alone",
                    archive_search._auto_phrase("Doom Valley") == '"Doom Valley"'
                    and archive_search._auto_phrase('"A B" AND mediatype:texts') == '"A B" AND mediatype:texts'
                    and archive_search._auto_phrase("Snake") == "Snake", ""))
    fake = {}
    def fake_json(url, **kw):
        for key, val in fake.items():
            if key in url:
                return val
        raise FileNotFoundError(url)
    real_json = archive_search.fetch_json
    archive_search.fetch_json = fake_json
    try:
        fake.clear()
        fake["advancedsearch"] = {"response": {"numFound": 598, "docs": [
            {"identifier": f"crawl{i}", "title": f"Webwide Crawldata {i}", "mediatype": "web",
             "collection": [f"c{j}" for j in range(900)], "description": "x" * 400} for i in range(25)]}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            archive_search.cmd_search(argparse.Namespace(query="Doom Valley", any=True, mediatype=None,
                                                         collection=None, rows=25, page=1, sort=None,
                                                         format="jsonl"))
        lines = buf.getvalue().splitlines()
        results.append(("archive search: fuzzy matches print 5 titles, not 25 records with 900 collections",
                        len(lines) == 5 and len(buf.getvalue()) < 600, f"{len(lines)} lines, {len(buf.getvalue())} B"))
        results.append(("archive search: long collection lists are cut",
                        archive_search._short_collection([f"c{j}" for j in range(900)]) == "c0,c1,c2 +897 more", ""))
        # Softalk Dec 1982: leaf 0 is a colour card, not a page,
        # so text leaf 99 is renderer n98 (printed p.95)
        pn = {"pages": [{"leafNum": n, "pageNumber": str(n - 4) if n > 4 else ""} for n in range(1, 345)]}
        sd = "".join(f'<page leafNum="{n}"><addToAccessFormats>{"false" if n in (0, 345) else "true"}'
                     f'</addToAccessFormats></page>' for n in range(346))
        results.append(("archive leaf map: page_numbers.json and scandata agree, shifted past the colour card",
                        archive_search.leaf_map_from_page_numbers(pn)[99] == (98, "95")
                        and archive_search.leaf_map_from_scandata(sd)[99] == (98, ""), ""))
        fake.clear()
        fake["metadata/softalk"] = {"server": "ia0.example", "dir": "/1/items/softalk",
                                    "files": [{"name": "softalk_djvu.txt"}, {"name": "softalk_page_numbers.json"}]}
        fake["inside.php"] = {"matches": [
            {"text": "<IA_FTS_MATCH>Doom</IA_FTS_MATCH> of the valley", "par": [{"page": 300}]},
            {"text": "In <IA_FTS_MATCH>Doom Valley</IA_FTS_MATCH>, you attempt", "par": [{"page": 99}]}]}
        fake["softalk_page_numbers.json"] = pn
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            archive_search.cmd_grep(argparse.Namespace(identifier="softalk", query="Doom Valley", any=True,
                                                       chars=500, docs=25, limit=40, format="jsonl"))
        got = [json.loads(x) for x in buf.getvalue().splitlines()]
        results.append(("archive grep: exact phrase first, leaf mapped to the index `pages` takes",
                        got and got[0]["leaf"] == 98 and got[0]["printed_page"] == "95"
                        and got[0]["url"].endswith("/page/n98") and got[1]["leaf"] == 299, str(got)[:200]))
        # --walk: search the last words, append what follows
        hl = {'save the"': "you attempt {{{to save the}}} lives of United Nations",
              'United Nations"': "save the lives of {{{United Nations}}} ambassadors"}
        real_fts = archive_search._fts
        archive_search._fts = lambda q, n: [{"highlight": {"text": [v for key, v in hl.items() if key in q]}}]
        try:
            walked = archive_search._walk("softalk", "In **Doom Valley**, you attempt to save the", 3)
        finally:
            archive_search._fts = real_fts
        results.append(("archive fulltext --walk reads on past a snippet",
                        walked.endswith("to save the lives of United Nations ambassadors"), walked))
    finally:
        archive_search.fetch_json = real_json


def _wav(path, x, sr=22050):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(array.array("h", [int(max(-32000, min(32000, v))) for v in x]).tobytes())


def footage_fix_checks(d: Path, S, has_ff: bool, has_img: bool, run, results, PY=sys.executable):
    sr = 22050
    if has_ff:
        # common-audio: one 8 s piece shared at A 3 s = B 7 s
        def bursts(seed, secs):
            rnd = random.Random(seed)
            x, t = [], 0
            while len(x) < secs * sr:
                n, f, amp = int(sr * rnd.uniform(0.08, 0.45)), rnd.choice([150, 300, 700, 1500, 3200]), rnd.uniform(500, 12000)
                x += [amp * math.sin(2 * math.pi * f * i / sr) * math.exp(-i / n * 3) for i in range(n)]
            return x[:int(secs * sr)]
        shared = bursts(7, 8)
        _wav(d / "ca_a.wav", bursts(1, 3) + shared + bursts(2, 5))
        _wav(d / "ca_b.wav", bursts(3, 7) + shared + bursts(4, 2))
        _wav(d / "ca_c.wav", bursts(5, 16))
        r = run("footage common-audio finds a shared piece at its offset", [S("footage.py"), "common-audio",
                str(d / "ca_a.wav"), str(d / "ca_b.wav")], d, contains="shared audio")
        try:
            st = json.loads(r.stdout)["stretches"]
        except (ValueError, KeyError):
            st = []
        results.append(("common-audio: stretch at B - A = +4 s", any(abs(g["b_minus_a_s"] - 4.0) <= 0.2 for g in st),
                        str(st)[:200]))
        run("footage common-audio: unrelated audio is not shared", [S("footage.py"), "common-audio",
            str(d / "ca_a.wav"), str(d / "ca_c.wav")], d, contains="no shared audio")

        # sfx: clicks under loud music, plus the `stems` drums stem
        clicks = [1.0, 2.6, 4.1, 5.9, 7.4]
        bed = [7000 * math.sin(2 * math.pi * 220 * i / sr) + 5000 * math.sin(2 * math.pi * 277 * i / sr)
               for i in range(9 * sr)]
        drum = [0.0] * (9 * sr)
        rnd = random.Random(9)
        for c in clicks:
            for i in range(int(0.06 * sr)):
                drum[int(c * sr) + i] = 9000 * rnd.uniform(-1, 1) * math.exp(-i / (0.02 * sr))
        media, stems = d / "dossier" / "media", d / "dossier" / "stems" / "mix"
        media.mkdir(parents=True, exist_ok=True)
        stems.mkdir(parents=True, exist_ok=True)
        _wav(d / "mixa.wav", [p + q for p, q in zip(bed, drum)])
        _wav(stems / "drums.wav", drum)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x48:r=10:d=9",
                        "-i", str(d / "mixa.wav"), "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        str(media / "mixv.mp4")], check=False)
        (stems / "stems.json").write_text(json.dumps({"source": "dossier/media/mixv.mp4", "start": None, "offset_s": 0.0,
                                                      "stems": [{"stem": "drums", "file": "dossier/stems/mix/drums.wav"}]}),
                                          encoding="utf-8")
        ev = ",".join(f"{c - 0.3:.2f}:click" for c in clicks)  # frame-read labels run early
        run("footage sfx uses the drums stem when `stems` split the recording", [S("footage.py"), "sfx",
            str(media / "mixv.mp4"), "--out", str(d / "sfxs"), "--events", ev], d, contains="drums stem")
        info = json.loads((d / "sfxs" / "sfx.json").read_text(encoding="utf-8")) if (d / "sfxs" / "sfx.json").exists() else {}
        results.append(("sfx --events: every labelled click found near its label",
                        len(info.get("events", [])) == len(clicks), str(info.get("events"))[:200]))
        r = run("footage sfx --no-stems over a music bed", [S("footage.py"), "sfx", str(media / "mixv.mp4"), "--out",
                str(d / "sfxm"), "--no-stems", "--events", ev], d, contains="steady bed")
        results.append(("sfx on the mix: --events finds the clicks under the bed",
                        f"{len(clicks)}/{len(clicks)} moments" in r.stderr, r.stderr.strip()[-200:]))

    if has_img:
        from PIL import Image, ImageDraw
        rnd = random.Random(4)
        plate = Image.new("RGB", (120, 100))
        plate.putdata([(118 + rnd.randint(-6, 6),) * 3 for _ in range(120 * 100)])  # grey textured wall
        frame = plate.copy()
        ImageDraw.Draw(frame).rectangle((30, 25, 79, 74), fill=(200, 40, 40))  # the sprite
        frame.paste(plate.crop((50, 45, 57, 52)), (50, 45))  # 7x7 patch that matches the wall exactly
        ImageDraw.Draw(frame).rectangle((100, 5, 107, 12), fill=(20, 200, 20))  # stray speck somewhere else
        plate.save(d / "plate.png")
        frame.save(d / "sprite_on_plate.png")
        args = [S("footage.py"), "cut", str(d / "sprite_on_plate.png"), "--plate", str(d / "plate.png"),
                "--box", "0,0,120,100"]
        run("footage cut --plate fills holes the plate hid", args + ["--out", str(d / "cp.png")], d,
            contains="holes_filled")
        run("footage cut --plate --fill-holes 0 --largest", args + ["--out", str(d / "cp0.png"), "--fill-holes", "0",
            "--largest"], d, contains="dropped_pieces_px")
        try:
            a1, a0 = Image.open(d / "cp.png").getchannel("A"), Image.open(d / "cp0.png").getchannel("A")
            ok = a1.getpixel((53, 48)) == 255 and a0.getpixel((53, 48)) == 0 and a1.getpixel((103, 8)) == 255 \
                and a0.getpixel((103, 8)) == 0 and a1.getpixel((10, 10)) == 0
        except (OSError, ValueError) as e:
            ok, a1 = False, e
        results.append(("cut --plate: interior hole filled by default, kept with --fill-holes 0; --largest drops the speck",
                        ok, "" if ok else str(a1)[:200]))

    if has_img and has_ff:
        # hud: a sweeping block is motion, a square jumping every 2 s a value
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-filter_complex",
                        "color=c=gray:s=160x120:r=10:d=8[bg];color=c=white:s=24x24:r=10:d=8[fg];"
                        "[bg][fg]overlay=x='mod(t*37,136)':y='40+20*sin(t*3)'", "-pix_fmt", "yuv420p",
                        str(d / "hudm.mp4")], check=False)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-filter_complex",
                        "color=c=gray:s=160x120:r=10:d=8[bg];color=c=white:s=24x24:r=10:d=8[fg];"
                        "[bg][fg]overlay=x='20+30*floor(t/2)':y=40", "-pix_fmt", "yuv420p", str(d / "hudv.mp4")],
                       check=False)
        r = run("footage hud warns when the changes are motion", [S("footage.py"), "hud", str(d / "hudm.mp4"),
                "--region", "0,0,160,120", "--fps", "10", "--out", str(d / "hudm")], d, contains="look like motion")
        results.append(("hud motion: reported in the result", '"motion"' in r.stdout, r.stdout[-200:]))
        r = run("footage hud: discrete changes are not motion", [S("footage.py"), "hud", str(d / "hudv.mp4"),
                "--region", "0,0,160,120", "--fps", "10", "--out", str(d / "hudv")], d)
        results.append(("hud: no motion warning for a few discrete states", "look like motion" not in r.stderr,
                        r.stderr.strip()[-200:]))

        # scenes: relative --out in a workspace, no per-call note
        wsf = d / "wsfoot"
        (wsf / "dossier").mkdir(parents=True, exist_ok=True)
        (wsf / "state.json").write_text("{}", encoding="utf-8")
        (d / "elsewhere").mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-filter_complex",
                        "color=c=red:s=64x48:r=30:d=1.3[a];color=c=blue:s=64x48:r=30:d=1.4[b];[a][b]concat=n=2:v=1:a=0",
                        "-pix_fmt", "yuv420p", str(d / "sc2.mp4")], check=False)
        r = subprocess.run([PY, S("footage.py"), "scenes", str(d / "sc2.mp4"), "--out", "dossier/screens/x", "--ws",
                            str(wsf)], cwd=d / "elsewhere", capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
        results.append(("scenes: no relative-path note, the summary names the full folder",
                        r.returncode == 0 and "note: relative output path" not in r.stderr
                        and str((wsf / "dossier" / "screens" / "x").resolve()) in r.stderr
                        and (wsf / "dossier" / "screens" / "x" / "sheet.png").exists(), r.stderr.strip()[-200:]))


def _swf(tags: list[tuple[int, bytes]], frames: int = 2) -> bytes:
    """Minimal uncompressed SWF (64x48, 3 fps) around the given (code, body) tags."""
    bits = "01111" + "".join(format(v, "015b") for v in (0, 64 * 20, 0, 48 * 20))
    bits += "0" * (-len(bits) % 8)
    body = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)) + struct.pack("<HH", 3 << 8, frames)
    for code, t in tags + [(0, b"")]:
        body += (struct.pack("<H", code << 6 | len(t)) if len(t) < 63 else
                 struct.pack("<HI", code << 6 | 63, len(t))) + t
    return b"FWS\x06" + struct.pack("<I", 8 + len(body)) + body


def misc_checks2(S, d, has_img, has_ff, run, results):
    # community discuss/yt, archive --walk, footage scenes and common-audio,
    # inspect_swf video, ledger pack lost names, edit by key,
    # optional-tools report. All offline
    import argparse  # noqa: F401
    import contextlib
    import io
    import os
    here = Path(S("ledger.py")).parent
    sys.path.insert(0, str(here))
    import archive_search
    import community
    pasta = ("I sexually identify as an attack helicopter. Ever since I was a boy I dreamed of soaring over the "
             "oilfields dropping hot sticky loads on the people below, like the subservient chicken would")
    rows = [{"text": pasta, "score": 5}, {"text": pasta.replace("boy", "lad") + " lol", "score": 9},
            {"text": pasta, "score": 1}, {"text": "omg subservient chicken", "score": 1},
            {"text": "omg subservient chicken", "score": 2},
            {"text": "Subservient Chicken: when you typed a command like dance, the guy in the suit would do it",
             "score": 0}]
    uniq, n = community.drop_copies(rows)
    ranked, _ = community.rank_posts(uniq, ["Subservient Chicken"], 5)
    copies = [r for r in ranked if "helicopter" in r["text"]]
    results.append(("community discuss: a copypasta is one post (copies counted), short posts never merged",
                    n == 2 and len(copies) == 1 and copies[0].get("copies") == 3 and copies[0]["score"] == 9
                    and sum("omg" in r["text"] for r in ranked) == 2, f"{n} {[r.get('copies') for r in ranked]}"))
    results.append(("community discuss: how-it-worked words outrank a long post that only names it",
                    "when you typed" in ranked[0]["text"], ranked[0]["text"][:60]))
    drv = ("import sys, json; sys.path.insert(0, {h!r}); import community as c; "
           "c.cmd_yt = lambda a: print(json.dumps([a.target, a.timed])); sys.argv = ['community.py'] + {a!r}; c.main()")
    env = {k: v for k, v in os.environ.items() if k != "RESURRECT_WS"}
    wsy = d / "wsyt"
    (wsy / "dossier").mkdir(parents=True, exist_ok=True)
    (wsy / "state.json").write_text("{}", encoding="utf-8")
    (d / "noyt").mkdir(exist_ok=True)
    got = []
    for args in (["yt", "--timed", "jNQXAC9IVRw"], ["yt", "jNQXAC9IVRw", "--timed", "--ws", str(wsy)],
                 ["yt", "jNQXAC9IVRw", "--timed", "caps"]):
        r = subprocess.run([sys.executable, "-c", drv.format(h=str(here), a=args)], cwd=d / "noyt", env=env,
                           capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        got.append(r.stdout.strip())
    results.append(("community yt --timed: DIR optional (here, or dossier/media/captions in a workspace)",
                    got == ['["jNQXAC9IVRw", "."]', '["jNQXAC9IVRw", "dossier/media/captions"]',
                            '["jNQXAC9IVRw", "caps"]'], str(got)[:200]))

    # --walk: a domain in the last words matches nothing, the words after do
    hl = {'Chicken Web site"': "Subservient {{{Chicken Web site}}} (subservientchicken.com). To most",
          '"To most"': "(subservientchicken.com). {{{To most}}} advertising people"}
    real_fts = archive_search._fts
    archive_search._fts = lambda q, n: [{"highlight": {"text": [v for k, v in hl.items() if k in q]}}]
    try:
        notes = []
        walked = archive_search._walk("adland", "Welcome to Burger King's Subservient Chicken Web site", 3, notes)
    finally:
        archive_search._fts = real_fts
    results.append(("archive --walk steps past a punctuated word and says why it stopped",
                    walked.endswith("To most advertising people") and notes[0].startswith("+")
                    and "2/3 steps; stopped: no hit continues" in notes[0], f"{walked[-40:]} | {notes}"))

    # inspect_swf: embedded video, and the Pillow skip count
    vframe = bytes([0, 0, 0x84, 0, 0, 0x10, 0, 0, 0, 0])
    (d / "vid.swf").write_bytes(_swf([(60, struct.pack("<HHHHBB", 1, 2, 64, 48, 0, 2)), (61, b"\x01\x00\x00\x00" + vframe),
                                      (1, b""), (61, b"\x01\x00\x01\x00" + vframe), (1, b"")]))
    run("inspect_swf names an embedded video stream", [S("inspect_swf.py"), str(d / "vid.swf"), "--section", "header"],
        d, contains="video 1: FLV1 (Sorenson H.263), 64x48, 2 frames, 3 fps")
    px = zlib.compress(b"\x00\xff\x00\x00" * 4)
    (d / "bmp.swf").write_bytes(_swf([(20, struct.pack("<HBHH", 7, 5, 2, 2) + px), (1, b"")], frames=1))
    nopil = ("import sys; sys.modules['PIL'] = None; sys.path.insert(0, {h!r}); import inspect_swf; "
             "inspect_swf.main([{f!r}, '--extract', {o!r}])")
    noff = {**env, "PATH": ""}  # no ffmpeg: video stays .flv, with a command to convert later
    r1 = subprocess.run([sys.executable, "-c", nopil.format(h=str(here), f=str(d / "vid.swf"), o=str(d / "vx"))],
                        cwd=d, env=noff, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    r2 = subprocess.run([sys.executable, "-c", nopil.format(h=str(here), f=str(d / "bmp.swf"), o=str(d / "bx"))],
                        cwd=d, env=noff, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    results.append(("inspect_swf without Pillow: 0 lossless bitmaps says nothing skipped; 1 says 1 SKIPPED",
                    "0 lossless bitmaps; nothing skipped" in r1.stderr and "1 lossless bitmap(s) SKIPPED" in r2.stderr,
                    (r1.stderr + r2.stderr).strip()[-200:]))
    results.append(("inspect_swf --extract without ffmpeg writes the .flv and says how to convert it",
                    (d / "vx" / "video-1-x.flv").read_bytes()[:3] == b"FLV" and "ffmpeg -i video-1-x.flv" in r1.stdout,
                    r1.stdout.strip()[-200:] or r1.stderr.strip()[-200:]))
    if has_ff:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x48:rate=3", "-t", "1",
                        "-c:v", "flv1", str(d / "flv1.swf")], check=False)
        run("inspect_swf --extract saves an FLV1 video as .flv and .mp4", [S("inspect_swf.py"), str(d / "flv1.swf"),
            "--extract", str(d / "fx")], d, contains="video-0-x.mp4")
        pr = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=width,nb_read_frames",
                             "-of", "csv=p=0", str(d / "fx" / "video-0-x.flv")], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        results.append(("inspect_swf .flv holds the original packets (ffmpeg decodes all 3 frames)",
                        pr.stdout.strip() == "64,3", pr.stdout.strip() or pr.stderr.strip()[-200:]))
        run("footage common-audio names a missing input", [S("footage.py"), "common-audio", str(d / "a.wav"),
            str(d / "nope.wav")], d, expect_ok=False, contains="no such file: B")
    if has_ff and has_img:
        wsf = d / "wsscenes"
        (wsf / "dossier").mkdir(parents=True, exist_ok=True)
        (wsf / "state.json").write_text("{}", encoding="utf-8")
        for nm, col in (("clipa", "red"), ("clipb", "green")):
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-filter_complex",
                            f"color=c={col}:s=64x48:r=30:d=1.3[a];color=c=blue:s=64x48:r=30:d=1.4[b];"
                            "[a][b]concat=n=2:v=1:a=0", "-pix_fmt", "yuv420p", str(d / f"{nm}.mp4")], check=False)
            run(f"footage scenes {nm} (default --out)", [S("footage.py"), "scenes", str(d / f"{nm}.mp4"), "--ws",
                str(wsf)], d)
        sc = wsf / "dossier" / "screens" / "scenes"
        results.append(("scenes: each video gets its own folder under dossier/screens/scenes/",
                        all((sc / nm / "scenes.json").exists() for nm in ("clipa", "clipb"))
                        and not (sc / "scenes.json").exists(), str(sorted(p.name for p in sc.glob("*")))))

    # ledger: tools at init, edit by key, pack keeps lost files
    r = run("ledger init names missing optional tools (or says all found)", [S("ledger.py"), "init", "Lost Thing",
            "--type", "other", "--budget", "quick", "--root", str(d / "m2root")], d, contains='"optional_tools"')
    ws = d / "m2root" / "lost-thing"
    (d / "m2.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"tag": "seen", "kind": "behavior", "claim": "Typing dance makes it dance", "source": "https://e.org/a",
         "locator": "para 2", "as_of": "2004", "key": "k1"},
        {"tag": "seen", "kind": "behavior", "claim": "Typing sit makes it sit", "source": "https://e.org/b",
         "locator": "para 3", "as_of": "2004", "key": "k2"}]) + "\n", encoding="utf-8")
    run("ledger add-batch (keys k1, k2)", [S("ledger.py"), "add-batch", str(d / "m2.jsonl"), "--ws", str(ws)], d)
    r = run("ledger edit @key --dry-run shows the item's claim and the change", [S("ledger.py"), "edit", "@k2", "--claim",
            "Typing sit makes it sit down", "--dry-run", "--ws", str(ws)], d, contains="dry run: nothing written")
    ev = (ws / "evidence.jsonl").read_text(encoding="utf-8")
    results.append(("edit --dry-run: @k2 is E0002, shown by its old claim, nothing written",
                    r.stdout.startswith("E0002 'Typing sit makes it sit'") and "sit down" not in ev, r.stdout[:200]))
    run("ledger edit @key writes", [S("ledger.py"), "edit", "@k2", "--claim", "Typing sit makes it sit down", "--ws",
                                    str(ws)], d, contains="E0002: claim updated")
    run("ledger edit: an unknown key is refused", [S("ledger.py"), "edit", "@nope", "--claim", "x", "--ws", str(ws)], d,
        expect_ok=False, contains="no key @nope")
    pk = ws / "pack"
    (pk / "reference").mkdir(parents=True, exist_ok=True)
    (pk / "reference" / "title.txt").write_text("title\n", encoding="utf-8")
    (pk / "README.md").write_text("# Pack\n\nScope:\n- Title: `reference/title.txt`\n- Intro: `intro.mov`\n"
                                  "- Vest: `vest.mov` (lost: never archived)\n- Lost: `hat.mov`, `boots.mov`\n\n"
                                  "## Unknowns\n- `pencil.mov` and `reference/pencil.png`: never archived\n\n"
                                  "## Build\n- Ending: `ending.mov`\n", encoding="utf-8")
    r = run("ledger pack: lost files named on purpose are not missing", [S("ledger.py"), "pack", "--ws", str(ws)], d,
            contains="intro.mov, ending.mov: add each one")
    results.append(("pack skips names under Unknowns, on a Lost: line, or marked (lost), and says so",
                    not any(x in r.stdout for x in ("vest.mov", "hat.mov", "boots.mov", "pencil"))
                    and "mark it `name` (lost)" in r.stdout, r.stdout[-300:]))


def wayback_shots_checks2(d: Path, results: list):
    # --- wayback.py and shots.py, second batch (offline, fake CDX and fetch, no network) ---
    # called right after wayback_shots_checks in main()
    import argparse
    import contextlib
    import io
    import json
    import time
    sys.path.insert(0, str(HERE))
    import wayback
    import _common
    import shots

    # inventory: numbered series -> one row, single-file names flagged
    rows = [{"kind": "program", "host": "www.chick.example", "date": "2004-06-12", "ts": "20040612000000",
             "url": f"http://www.chick.example:80/clips/clip_{i}.swf", "bytes": "90600", "digest": "SAME"}
            for i in range(1, 21)]
    rows += [{"kind": "program", "host": "www.chick.example", "date": "2004-07-01", "ts": f"200407010000{i:02d}",
              "url": f"http://www.chick.example/vids/clip_{i}.swf", "bytes": str(1000 + i), "digest": f"D{i}"}
             for i in range(5, 40) if i not in (17, 30, 31)]
    rows += [{"kind": "program", "host": "www.chick.example", "date": "2004-07-29", "ts": "20040729000000",
              "url": "http://www.chick.example/vids/no.swf", "bytes": "91309", "digest": "SAME"},
             {"kind": "image", "host": "www.chick.example", "date": "2004-07-29", "ts": "20040729000000",
              "url": "http://www.chick.example/img/a1.gif", "bytes": "100"}]
    pats, rest = wayback.collapse_patterns(rows)
    by = {p["pattern"]: p for p in pats}
    clips, vids = by.get("/clips/clip_N.swf", {}), by.get("/vids/clip_N.swf", {})
    results.append(("wayback inventory: numbered series summarised, one-file group flagged with its twin",
                    len(pats) == 2 and clips.get("files") == 20 and "no.swf" in clips.get("one_file", "")
                    and vids.get("N") == "5..39" and vids.get("gaps") == "0-4, 17, 30-31" and "one_file" not in vids
                    and [r["url"] for r in rest] == ["http://www.chick.example/vids/no.swf",
                                                      "http://www.chick.example/img/a1.gif"], json.dumps(pats)[:200]))
    cdx_rows = [{"timestamp": r["ts"], "original": r["url"], "mimetype": "application/x-shockwave-flash",
                 "length": r["bytes"], "digest": r.get("digest", "")} for r in rows]
    real = wayback.cdx_query, wayback.fetch, wayback.closest_capture
    wayback.cdx_query = lambda url, **k: iter([] if any("original:" in str(f) for f in k.get("filters", ())) else cdx_rows)
    out, err = io.StringIO(), io.StringIO()
    saved = d / "inv-full.jsonl"
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            wayback.cmd_inventory(argparse.Namespace(
                domain="chick.example", frm=None, to="2006", limit=10, pages=1, max_seconds=30, max_files=400,
                data=False, list_pages=False, assets_only=False, images=False, format="jsonl", save=str(saved)))
    finally:
        wayback.cdx_query = real[0]
    o = out.getvalue()
    full = [json.loads(x) for x in saved.read_text(encoding="utf-8").splitlines()] if saved.exists() else []
    results.append(("wayback inventory prints the series row and saves every file with its ts",
                    '"pattern": "/clips/clip_N.swf"' in o and "clip_7.swf" not in o and len(full) == len(rows)
                    and all(r.get("ts") for r in full) and "--from-inventory" in err.getvalue(), o[-200:]))

    # fetch with no ts: EARLIEST good capture for programs, latest for pages
    wayback.cdx_query = lambda url, **k: iter([{"timestamp": "20040507054808", "original": url, "digest": "A"},
                                               {"timestamp": "20130101000000", "original": url, "digest": "B"}])
    wayback.closest_capture = lambda url, ts=None: {"timestamp": "20130101000000"}
    try:
        swf_ts, why = wayback.default_capture("http://x.example/bk_skinned.swf")
        page_ts, _ = wayback.default_capture("http://x.example/index.html")
    finally:
        wayback.cdx_query, wayback.fetch, wayback.closest_capture = real
    results.append(("wayback fetch without ts: earliest capture for program files, latest for pages",
                    swf_ts == "20040507054808" and "EARLIEST of 2" in why and page_ts == "20130101000000", why))

    # read_list: inventory, pattern (skipped), find/cdx, 'URL TS', replay URLs
    lst = d / "list.jsonl"
    lst.write_text("\n".join([json.dumps(rows[0]), json.dumps(pats[0]),
                              json.dumps({"timestamp": "20040101000000", "original": "http://www.chick.example:80/a.swf"})])
                   + "\n", encoding="utf-8")
    txt = d / "list.txt"
    txt.write_text("http://www.chick.example/b.swf 20040202000000\n"
                   "https://web.archive.org/web/20040303000000id_/http://www.chick.example/c.swf\n", encoding="utf-8")
    with contextlib.redirect_stderr(io.StringIO()):
        got = wayback.read_list(str(lst)) + wayback.read_list(str(txt), "c\\.swf")
    results.append(("wayback read_list: inventory/cdx JSONL, URL TS lines, replay URLs; pattern rows skipped",
                    got == [("http://www.chick.example/clips/clip_1.swf", "20040612000000"),
                            ("http://www.chick.example/a.swf", "20040101000000"),
                            ("http://www.chick.example/c.swf", "20040303000000")], str(got)))

    # mirror --from-inventory: listed ts per file, no CDX
    asked, cdx_calls = [], []

    def lfetch(url, **k):
        asked.append(url)
        time.sleep(0.05)
        body = b"FWS\x06" + url.encode()
        return (body, url) if k.get("want_url") else body
    wayback.fetch = lfetch
    wayback.cdx_query = lambda *a_, **k_: (cdx_calls.append(a_), iter([]))[1]
    biglist = d / "biglist.jsonl"
    biglist.write_text("".join(json.dumps(r) + "\n" for r in rows[20:30]), encoding="utf-8")
    mout = d / "mirror-list"
    ns = dict(url=None, ts=None, out=str(mout), depth=0, max_pages=2, max_mb=5, max_seconds=60, whole_host=False,
              offsite=False, died=None, drift=3, max_captured=0, max_lookups=5, original=True, format="jsonl",
              from_inventory=str(biglist), match=None, parallel=3)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as mo, contextlib.redirect_stderr(io.StringIO()):
            wayback.cmd_mirror(argparse.Namespace(**ns))
        man = json.loads((mout / "MANIFEST.json").read_text(encoding="utf-8"))
        exact = all(any(f"/web/{r['ts']}id_/" in u and u.endswith(r["url"].split("/")[-1]) for u in asked)
                    for r in rows[20:30])
        results.append(("wayback mirror --from-inventory fetches each row at its listed ts, no CDX lookups",
                        len(man["files"]) == 10 and exact and not cdx_calls and json.loads(mo.getvalue())["files"] == 10,
                        f"{len(man['files'])} files, cdx {len(cdx_calls)}, {asked[:2]}"))
        # out of time: nothing new starts, resume command printed
        asked.clear()
        with contextlib.redirect_stdout(io.StringIO()) as mo, contextlib.redirect_stderr(io.StringIO()):
            wayback.cmd_mirror(argparse.Namespace(**dict(ns, out=str(d / "mirror-late"), max_seconds=0)))
        res = json.loads(mo.getvalue())
        man = json.loads((d / "mirror-late" / "MANIFEST.json").read_text(encoding="utf-8"))
        results.append(("wayback mirror past --max-seconds: files listed 'not fetched: out of time', resume given",
                        not asked and res.get("not_fetched", {}).get("out_of_time") == 10 and "--from-inventory" in
                        res.get("resume", "") and all(m["why"] == "not fetched: out of time" for m in man["missing"]),
                        str(res)[:200]))
        # rerun reuses files already saved
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as me:
            wayback.cmd_mirror(argparse.Namespace(**ns))
        results.append(("wayback mirror rerun skips files an earlier run saved",
                        not asked and "10 file(s) already saved" in me.getvalue(), me.getvalue()[-200:]))
    finally:
        wayback.cdx_query, wayback.fetch, wayback.closest_capture = real

    # fetch() deadline blocks new requests, never cuts one mid-way
    try:
        _common.fetch("http://127.0.0.1:9/never", use_cache=False, deadline=time.time() - 1)
        ok = False
    except _common.OutOfTime:
        ok = True
    results.append(("fetch past its deadline starts nothing (OutOfTime, not retried)", ok, ""))

    # Drupal teasers repeat the opening, want each paragraph once
    opening = "The Subservient Chicken was launched by Burger King in April 2004 and became a viral sensation."
    paras = [opening, "Crispin Porter + Bogusky filmed hundreds of clips of a man in a chicken suit.",
             "Visitors typed commands and the chicken obeyed, unless the request was rude."]
    page = ("<html><head><title>Adland</title></head><body><div class='teaser'><p>" + opening + "</p></div>"
            "<div class='node'><div class='content'><div class='field field-name-body'><div class='field-items'>"
            + "".join(f"<p>{p}</p>" for p in paras) + "<p>" + "More history of the campaign. " * 10 + "</p>"
            "</div></div></div></div><div class='block related'><p>" + opening + "</p></div></body></html>")
    wayback.fetch = lambda url, **k: (page.encode(), url) if k.get("want_url") else page.encode()
    wayback.closest_capture = lambda url, ts=None: {"timestamp": "20090101000000"}
    try:
        with contextlib.redirect_stdout(io.StringIO()) as to, contextlib.redirect_stderr(io.StringIO()):
            wayback.cmd_text(argparse.Namespace(url="http://adland.example/x", ts="20090101000000", max=0,
                                                format="jsonl", whole=False))
    finally:
        wayback.cdx_query, wayback.fetch, wayback.closest_capture = real
    text = json.loads(to.getvalue())["text"] if to.getvalue().strip() else ""
    results.append(("wayback text: the article body, opening once, nothing lost",
                    text.count("April 2004") == 1 and all(p in text for p in paras[1:]) and "More history" in text,
                    text[:200]))

    # shots: aliases, SWF header, swf args, Ruffle pin, muted launch
    st = shots._steps('[{"do": "key", "key": "Enter"}, {"do": "shot", "name": "a"}, {"do": "type", "text": "hi"}]')
    results.append(("shots steps: key -> press, shot -> screenshot", [x["do"] for x in st] ==
                    ["press", "screenshot", "type"], str(st)))
    import struct
    nb = 15  # RECT 0..550 x 0..700 px in twips, 15 bits per field
    bits = ((nb << 60) | (0 << 45) | (11000 << 30) | (0 << 15) | 14000) << 7  # 65 bits, padded to 9 bytes
    rect = bits.to_bytes(9, "big")
    swf = b"FWS\x06" + struct.pack("<I", 30) + rect + bytes([0, 31]) + b"\0" * 8
    (d / "m.swf").write_bytes(swf)
    hd = shots.swf_header(d / "m.swf")
    results.append(("shots swf reads the stage size and frame rate from the header",
                    hd.get("width") == 550 and hd.get("height") == 700 and hd.get("fps") == 31, str(hd)))
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            shots.main(["swf", str(d / "no-such.swf"), "--steps", '[{"do": "shot"}]'])
        msg = ""
    except SystemExit as e:
        msg = str(e.code)
    results.append(("shots swf refuses a missing file before starting anything",
                    "no such file" in msg or "playwright not installed" in msg, msg[:200]))
    src = Path(shots.__file__).read_text(encoding="utf-8")
    results.append(("shots: Ruffle pinned with a SHA-256 per file; every browser launch muted",
                    shots.RUFFLE_VERSION and "ruffle.js" in shots.RUFFLE_SHA256
                    and all(len(h) == 64 for h in shots.RUFFLE_SHA256.values())
                    and "--mute-audio" in shots.EMULATOR_ARGS and "launch(args=[MUTE])" in src
                    and src.count("chromium.launch(") == src.count("chromium.launch(args=EMULATOR_ARGS)")
                    + src.count("chromium.launch(args=[MUTE])"), ""))
    try:
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (400, 300), "white")
        ImageDraw.Draw(im).rectangle((50, 20, 249, 219), fill="black")
        im.save(d / "flashpage.png")
        note = shots.flash_blank_note(d / "flashpage.png", [{"src": "https://web.archive.org/web/20040601000000oe_/"
                                      "http://x.example/movie.swf", "x": 50, "y": 20, "w": 200, "h": 200}],
                                      (400, 300, False))
        results.append(("shots wayback notes a Flash embed the replay left blank and points to `shots.py swf`",
                        bool(note) and note.get("blank") == 1 and "shots.py swf" in note["note"]
                        and "fetch http://x.example/movie.swf 20040601000000 --original" in note["note"], str(note)[:200]))
    except ImportError:
        results.append(("shots flash note (skipped: no Pillow)", True, ""))


def truncation_checks(d: Path, results: list):
    """Short responses get retried, then kept and flagged, never cached."""
    import http.server
    import os
    import socketserver
    import threading
    sys.path.insert(0, str(HERE))
    import _common

    class Short(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "5000")
            self.end_headers()
            self.wfile.write(b"<html>" + b"x" * 1033)
            self.close_connection = True

        def log_message(self, *a):
            pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), Short)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old_cache, old_sleep = os.environ.get("RESURRECT_CACHE"), _common.time.sleep
    os.environ["RESURRECT_CACHE"] = str(d / "trunc-cache")
    _common.time.sleep = lambda s: None
    try:
        body = _common.fetch(f"http://127.0.0.1:{srv.server_address[1]}/x")
        cached = [f for f in (d / "trunc-cache" / "http").rglob("*") if f.is_file()]
        results.append(("fetch keeps a cut-short body, flags it and does not cache it",
                        len(body) == 1039 and _common.last_truncated() and not cached,
                        f"{len(body)} B, truncated={_common.last_truncated()}, cached={len(cached)}"))
    finally:
        _common.time.sleep = old_sleep
        if old_cache is None:
            os.environ.pop("RESURRECT_CACHE", None)
        else:
            os.environ["RESURRECT_CACHE"] = old_cache
        srv.shutdown()


def heavy_env_checks(d: Path, results: list):
    """Scripts missing a package re-run in the resurrect env, whose tools join PATH."""
    import os
    old = os.environ.get("RESURRECT_CACHE")
    os.environ["RESURRECT_CACHE"] = str(d / "heavy-cache")
    try:
        env = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import _common; "
                              "print(_common.use_heavy('json', 'no_such_module_x'))", str(HERE)],
                             capture_output=True, text=True, env=os.environ.copy(), encoding="utf-8", errors="replace")
        results.append(("use_heavy names what is missing when there is no environment",
                        env.stdout.strip() == "['no_such_module_x']", env.stdout + env.stderr[-300:]))
        fake = d / "heavy-cache" / "heavy" / ("Scripts" if os.name == "nt" else "bin")
        fake.mkdir(parents=True, exist_ok=True)
        py = fake / ("python.exe" if os.name == "nt" else "python")
        if os.name != "nt":  # fake env Python that echoes its args
            py.write_text("#!/bin/sh\necho REEXEC \"$@\"\n", encoding="utf-8")
            py.chmod(0o755)
            probe = d / "probe_heavy.py"
            probe.write_text("import sys; sys.path.insert(0, sys.argv[1]); import _common, os\n"
                             "_common.use_heavy('no_such_module_x')\nprint('NOT REEXEC')\n", encoding="utf-8")
            r = subprocess.run([sys.executable, str(probe), str(HERE)], capture_output=True, text=True,
                               env=os.environ.copy(), encoding="utf-8", errors="replace")
            results.append(("a script missing a package re-runs in the environment",
                            r.stdout.startswith("REEXEC") and str(probe) in r.stdout, r.stdout + r.stderr[-300:]))
            r = subprocess.run([sys.executable, "-c", "import sys, os; sys.path.insert(0, sys.argv[1]); import _common; "
                                "print(os.environ['PATH'].split(os.pathsep)[-1])", str(HERE)],
                               capture_output=True, text=True, env=os.environ.copy(),
                               encoding="utf-8", errors="replace")
            results.append(("the environment's tools come last on PATH", r.stdout.strip() == str(fake),
                            r.stdout + r.stderr[-300:]))
    finally:
        if old is None:
            os.environ.pop("RESURRECT_CACHE", None)
        else:
            os.environ["RESURRECT_CACHE"] = old


def portability_checks(S, d: Path, results: list):
    """Cross-platform bits: JSONL line ends, no signal.alarm, Windows re-run and quoting,
    unwritable cache, whole-file writes, the version."""
    import os
    import socket
    import time
    sys.path.insert(0, str(HERE))
    import _common
    import enhance
    import ledger
    import wayback
    # U+2028 and U+0085 stay raw in JSON, must survive add/read
    run("ledger init (portability)", [S("ledger.py"), "init", "Odd Lines", "--type", "game", "--budget", "quick",
                                      "--root", str(d / "ws-port")], d)
    wsp = d / "ws-port" / "odd-lines"
    claim = "Title reads Odd\u2028Lines\u0085Deluxe"
    batch = d / "odd.jsonl"
    batch.write_text(json.dumps({"tag": "seen", "kind": "ui", "claim": claim, "source": "https://e.org/odd",
                                 "locator": "frame 2", "as_of": "2006", "feature": "title"}, ensure_ascii=False)
                     + "\n", encoding="utf-8")
    run("ledger add-batch (U+2028 and U+0085 in a claim)", [S("ledger.py"), "add-batch", str(batch), "--ws", str(wsp)],
        d, contains='"added": 1')
    got = [e.get("claim") for e in ledger.read_jsonl(wsp / "evidence.jsonl")]
    results.append(("a claim with U+2028 and U+0085 round-trips through the ledger", got == [claim], repr(got)[:200]))
    results.append(("jsonl_lines splits on \\n only and drops \\r",
                    _common.jsonl_lines('{"a": "x\u2028y"}\r\n{"b": 1}\n') == ['{"a": "x\u2028y"}', '{"b": 1}'], ""))
    # GitHub's strict YAML rejects ": " in unquoted values, Claude Code doesn't
    front = (HERE.parent / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    bad = [ln.split(":")[0] for ln in front.strip().split("\n")
           if ": " in ln.partition(": ")[2] and not ln.partition(": ")[2].startswith(('"', "'"))]
    results.append(("SKILL.md frontmatter values holding ': ' are quoted (strict YAML)", not bad, ", ".join(bad)))
    # no signal.alarm on Windows, the limit must still stop back-off
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # closed port: every attempt is refused at once
    old = os.environ.get("RESURRECT_CACHE")
    os.environ["RESURRECT_CACHE"] = str(d / "port-cache")
    t0, how = time.time(), "no stop"
    wayback._arm(1, alarm=False)
    try:
        _common.fetch(f"http://127.0.0.1:{port}/x", use_cache=False)
    except wayback._Deadline:
        how = "stopped"
    except Exception as e:  # noqa: BLE001
        how = repr(e)
    finally:
        wayback._disarm()
        if old is None:
            os.environ.pop("RESURRECT_CACHE", None)
        else:
            os.environ["RESURRECT_CACHE"] = old
    took = time.time() - t0
    results.append(("time limit without signal.alarm stops a fetch at the limit",
                    how == "stopped" and took < 4 and _common._HARD_STOP[0] is None, f"{how} after {took:.1f}s"))
    # Windows: child's exit status is ours, quoting for cmd.exe
    try:
        _common._run_child([sys.executable, "-c", "import sys; sys.exit(3)"])
        code = None
    except SystemExit as e:
        code = e.code
    results.append(("on Windows a re-run exits with the child's status", code == 3, str(code)))
    child = d / "child_probe.py"
    child.write_text("import sys; print('CHILD', *sys.argv[1:]); sys.exit(5)\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-c", "import os, sys; sys.path.insert(0, sys.argv[1]); import _common; "
                        "child = sys.argv[2]; os.name = 'nt'; sys.argv = ['x.py', 'a b']; "
                        "_common.reexec(sys.executable, child)", str(HERE), str(child)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    results.append(("reexec on Windows runs the script as a child", r.returncode == 5 and "CHILD a b" in r.stdout,
                    f"{r.returncode} {r.stdout[-200:]} {r.stderr[-200:]}"))
    win = _common.shell_join(["python", "C:\\My Files\\wayback.py", "mirror", "--max-seconds", "60"], nt=True)
    results.append(("resume command quoted for cmd.exe on Windows",
                    win == 'python "C:\\My Files\\wayback.py" mirror --max-seconds 60', win))
    results.append(("resume command quoted for sh elsewhere",
                    _common.shell_join(["python3", "/a b/w.py"], nt=False) == "python3 '/a b/w.py'", ""))
    # setup without uv finds a new Python: `py` on Windows, python3.1x elsewhere
    old_min, old_ver, old_which = enhance.MIN_PY, enhance._py_version, enhance.shutil.which
    vers = {("py", "-3.14"): None, ("py", "-3.13"): (99, 1), ("/bin/python3.12",): (99, 0)}
    enhance.MIN_PY = (99, 0)
    enhance._py_version = lambda cmd: vers.get(tuple(cmd))
    enhance.shutil.which = lambda n: {"py": "py", "python3.12": "/bin/python3.12"}.get(n)
    try:
        found = (enhance._find_python(nt=True), enhance._find_python(nt=False))
        vers[("/bin/python3.12",)] = (3, 9)
        found += (enhance._find_python(nt=False),)
    finally:
        enhance.MIN_PY, enhance._py_version, enhance.shutil.which = old_min, old_ver, old_which
    results.append(("setup without uv finds a new enough Python (py launcher, python3.1x) or none",
                    found == (["py", "-3.13"], ["/bin/python3.12"], None), str(found)))
    # unwritable cache (sandbox) -> .resurrect-cache/, said once
    blocker = d / "not-a-folder"
    blocker.write_text("x", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "RESURRECT_CACHE"}
    env["XDG_CACHE_HOME"] = str(blocker / "cache")
    here = d / "proj"
    here.mkdir(exist_ok=True)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import _common; _common.cache_dir(); "
            "print(_common.cache_dir())")
    r = subprocess.run([sys.executable, "-c", code, str(HERE)], cwd=here, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    want = (here / ".resurrect-cache").resolve()
    results.append(("an unwritable cache falls back to .resurrect-cache/ in the project, said once",
                    r.stdout.strip() == str(want) and r.stderr.count("cannot write") == 1
                    and (want / ".gitignore").exists(), r.stdout[-200:] + r.stderr[-300:]))
    env["RESURRECT_CACHE"] = str(d / "port-cache2")
    r = subprocess.run([sys.executable, "-c", code, str(HERE)], cwd=here, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    results.append(("RESURRECT_CACHE wins over the fallback", r.stdout.strip() == str(d / "port-cache2"),
                    r.stdout[-200:] + r.stderr[-200:]))
    # whole-file writes: a failed write keeps the old file, no temp left
    f = d / "aw" / "state.json"
    f.parent.mkdir(exist_ok=True)
    _common.atomic_write(f, '{"a": "\u00e9"}')
    t_ok = f.read_text(encoding="utf-8") == '{"a": "\u00e9"}'
    _common.atomic_write(f, b"{}")
    b_ok = f.read_bytes() == b"{}"
    try:
        _common.atomic_write(f, 5)  # not text or bytes, fails mid-write
    except TypeError:
        pass
    left = sorted(x.name for x in f.parent.iterdir())
    results.append(("atomic_write writes text and bytes, and a failed write keeps the old file",
                    t_ok and b_ok and f.read_bytes() == b"{}" and left == ["state.json"], str(left)))
    # User-Agent carries the plugin's version
    pj = HERE.parent.parent.parent / ".claude-plugin" / "plugin.json"
    if pj.exists():
        v = json.loads(pj.read_text(encoding="utf-8")).get("version")
        results.append(("_common.VERSION matches plugin.json", v == _common.VERSION, f"{_common.VERSION} vs {v}"))


def r53_checks(S, d: Path, has_img: bool, results: list):
    """Fixes from the Simpsonize Me run: tags across lines, fan videos, sheet cells, dossier and ./ paths."""
    sys.path.insert(0, str(HERE))
    import community
    import wayback
    lines = [x.strip() for x in wayback.strip_html_lines('<p>Hi</p>\n<img src="a"\n class="logo">\nOn <button\n '
                                                         'type="b">Go</button>') if x.strip()]
    results.append(("wayback text strips tags that span lines", lines == ["Hi", "On", "Go"], str(lines)))
    eras = [{"timestamp": f"20{y:02d}0101000000", "original": "http://p.com/"} for y in range(7, 27)]

    def fake(r, host):  # pre-launch 2007, app 2008-09, notice image 2010-13, parked after
        y = int(r["timestamp"][2:4])
        return {"title": "P", "parked": "buy this domain" if y >= 14 else None,
                "placeholder": "Coming Soon" if y == 7 else None, "programs": ["main.swf"] if 8 <= y <= 9 else []}
    real, wayback._era_check = wayback._era_check, fake
    try:
        ch = wayback.page_change(eras, "p.com")
    finally:
        wayback._era_check = real
    results.append(("timeline finds when the page stopped running the program, past a pre-launch notice",
                    ch and ch["date"] == "2010-01-01" and ch["kind"] == "program_gone"
                    and ch["last_as_original"] == "2009-01-01" and "before_launch" in ch, str(ch)[:300]))
    rows = [{"original": "http://p.com/poc/main.swf?session_id=1&component_base=http%3A%2F%2Fwww.p.com%2Frs%2Fswf%2F"
                         "&server_name_images=img.p.com&server_name=p.com"},
            {"original": "http://img.p.com/robots.txt"}, {"original": "http://p.com/poc/data.xml"}]
    leads = wayback.query_leads(rows)
    results.append(("inventory names the folders and hosts a movie's FlashVars point at that the archive lacks",
                    any("/rs/swf/" in x for x in leads) and any("img.p.com" in x for x in leads) and len(leads) == 2,
                    str(leads)))
    unc = wayback.uncaptured_from_rows([{"original": f"http://p.com//poc/m.swf?session_id={i}", "timestamp": "2008010100",
                                         "statuscode": "404", "mimetype": "text/html"} for i in range(30)])
    results.append(("never-captured files are one row whatever their query strings",
                    len(unc) == 1 and "30 query-string variants" in unc[0]["captures"], str(unc)[:200]))
    fp_park = wayback.page_fingerprint('<html data-adblockkey="x"><head></head><body><script>window.park = "1"'
                                       '</script></body></html>', "p.com")
    fp_empty = wayback.page_fingerprint("<html><head></head><body><!-- vbe --></body></html>", "p.com")
    results.append(("a parking script page and a blank page are not the original site",
                    bool(fp_park["parked"]) and not wayback.same_site(fp_empty, {"title": "Promo Site"}),
                    f"{fp_park} {fp_empty}"))
    k = community._norm_name
    cands = [{"id": "kb", "title": "Simpsonize Me Burger King ad", "channel": "Ad Vault", "rank": 0},
             {"id": "fan", "title": "simpsonize me walkthrough", "channel": "a fan", "rank": 1}]
    dropped = []
    strict = [c["id"] for c in community.pick_videos(cands, [k("Simpsonize Me")], [], [k("Burger King")], [], 5,
                                                      False, dropped)[0]]
    loose = [c["id"] for c in community.pick_videos(cands, [k("Simpsonize Me")], [], [k("Burger King")], [], 5,
                                                     True)[0]]
    results.append(("discuss keeps fans' videos of an invented name, and lists what the maker rule drops",
                    strict == ["kb"] and [c["id"] for c in dropped] == ["fan"] and loose == ["kb", "fan"],
                    f"strict={strict} dropped={[c['id'] for c in dropped]} loose={loose}"))
    run("ledger init (r53 workspace)", [S("ledger.py"), "init", "Promo Site", "--type", "website", "--root",
                                        str(d / "ws53")], d)
    ws = d / "ws53" / "promo-site"
    b = d / "r53.jsonl"
    b.write_text(json.dumps({"tag": "seen", "kind": "ui", "claim": "The intro shows a TV", "feature": "intro",
                             "source": "Ruffle run", "locator": "shot dossier/screens/t16.png", "as_of": "2007"}) + "\n",
                 encoding="utf-8")
    run("ledger add-batch (r53)", [S("ledger.py"), "add-batch", str(b), "--ws", str(ws)], d)
    (ws / "pack" / "reference").mkdir(parents=True, exist_ok=True)
    (ws / "pack" / "reference" / "intro.html").write_text("<p>intro</p>", encoding="utf-8")
    (ws / "pack" / "README.md").write_text("# Promo Site\n\n- intro: `reference/intro.html`\n", encoding="utf-8")
    r = subprocess.run([PY, S("ledger.py"), "pack", "--ws", str(ws)], cwd=d, capture_output=True, text=True, timeout=300,
                       encoding="utf-8", errors="replace")
    results.append(("ledger pack flags facts that point into dossier/", "points into dossier/" in r.stdout,
                    r.stdout[-300:]))
    import zlib
    raw = _swf([(12, b"\x96\x1e\x00\x00ABOUT by dev.person@studio-mail.net\x00")])
    (ws / "pack" / "assets").mkdir(parents=True, exist_ok=True)
    (ws / "pack" / "assets" / "m.swf").write_bytes(b"CWS" + raw[3:8] + zlib.compress(raw[8:]))
    r2 = subprocess.run([PY, S("ledger.py"), "pack", "--ws", str(ws)], cwd=d, capture_output=True, text=True, timeout=300,
                        encoding="utf-8", errors="replace")
    results.append(("ledger pack finds an e-mail address inside a compressed SWF",
                    "dev.person@studio-mail.net in assets/m.swf" in r2.stderr, r2.stderr[-300:]))
    sub = ws / "dossier" / "originals" / "f"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "m.swf").write_bytes(_swf([]))
    run("inspect_swf --extract with a ../ path", [S("inspect_swf.py"), "m.swf", "--extract", "../../swf/m"], sub)
    results.append(("a ../ path from inside the workspace means from there, not from the workspace root",
                    (ws / "dossier" / "swf" / "m").is_dir() and not (d / "ws53" / "swf").exists(), ""))
    if has_img:
        from PIL import Image
        fr = d / "mixed"
        fr.mkdir(exist_ok=True)
        Image.new("RGB", (271, 491), (200, 40, 40)).save(fr / "tall.png")
        for i in range(5):
            Image.new("RGB", (80, 12), (40, 40, 200)).save(fr / f"strip{i}.png")
        run("shots sheet (mixed sizes)", [S("shots.py"), "sheet", str(fr), "--out", str(d / "mixed.png")], d)
        h = Image.open(d / "mixed.png").height if (d / "mixed.png").exists() else 0
        results.append(("a sheet of mixed sizes keeps one modest cell height", 0 < h <= 2 * (300 + 16), f"height {h}"))


def main():
    import py_compile
    for f in sorted(HERE.glob("*.py")):  # fail on syntax errors now, not mid-run
        try:
            py_compile.compile(str(f), doraise=True)
            ok, why = True, ""
        except py_compile.PyCompileError as e:
            ok, why = False, str(e).strip().splitlines()[-1][:200]
        if not ok:
            results.append((f"compile {f.name}", False, why))
    results.append((f"compile all {len(list(HERE.glob('*.py')))} scripts", True, ""))
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        has_img = make_media(d)
        has_ff = bool(shutil.which("ffmpeg"))
        S = lambda n: str(HERE / n)  # noqa: E731
        # ledger: workspace, evidence, versions, check-spec
        run("ledger init", [S("ledger.py"), "init", "Test Thing", "--type", "game", "--budget", "quick",
                            "--viewport", "320x240"], d)
        ws = d / "resurrections" / "test-thing"
        batch = d / "b.jsonl"
        batch.write_text("\n".join(json.dumps(x) for x in [
            {"tag": "seen", "kind": "ui", "claim": "Title screen is blue", "source": "https://example.org/a",
             "locator": "frame 1", "as_of": "2006-05", "feature": "title", "key": "k1"},
            {"tag": "inferred", "kind": "behavior", "claim": "Blue means day", "derived_from": ["@k1"],
             "feature": "title"},
            {"tag": "unknown", "kind": "other", "claim": "No manual found", "searched": "archive.org, Google"},
            {"tag": "seen", "kind": "sentiment", "claim": "People miss the title tune", "source": "https://example.org/b",
             "quote": "I miss that tune so much", "feature": "title-tune"}]) + "\n", encoding="utf-8")
        run("ledger add-batch", [S("ledger.py"), "add-batch", str(batch), "--ws", str(ws)], d, contains='"added": 4')
        run("ledger versions add", [S("ledger.py"), "versions", "add", "launch", "--from", "2006", "--to", "2007",
                                    "--ws", str(ws)], d)
        run("ledger versions list", [S("ledger.py"), "versions", "--ws", str(ws)], d, contains="launch")
        run("ledger mourn", [S("ledger.py"), "mourn", "--ws", str(ws)], d, contains="title-tune")
        run("ledger pack (no README yet)", [S("ledger.py"), "pack", "--ws", str(ws)], d,
            contains="pack/README.md is missing")
        results.append(("pack FACTS.md written from the ledger",
                        "Title screen is blue" in (ws / "pack" / "FACTS.md").read_text(encoding="utf-8"), ""))
        (ws / "pack" / "reference").mkdir(parents=True, exist_ok=True)
        (ws / "pack" / "reference" / "page.html").write_text(
            "<a href=mailto:maker@school.edu>mail me</a> call 555-123-4567 <a href=mailto:fan@example.invalid>x</a>",
            encoding="utf-8")
        run("ledger pack flags contact details", [S("ledger.py"), "pack", "--ws", str(ws)], d,
            contains="maker@school.edu in reference/page.html")
        (ws / "pack" / "reference" / "page.html").unlink()
        (ws / "codex").mkdir(exist_ok=True)
        (ws / "codex" / "CODEX.md").write_text("# Codex\n\n## Identity\n- Blue title screen (E0001)\n- Tune [DESIGN]\n",
                                               encoding="utf-8")
        run("ledger check-spec", [S("ledger.py"), "check-spec", "codex/CODEX.md", "--ws", str(ws)], d, contains='"ok": true')
        # pantry, with a toy recipe
        rec = d / "recipe.json"
        rec.write_text(json.dumps({"basis": "per 100 g of finished product",
                                   "target": {"kcal": 380, "fat": 10, "carbs": 70, "protein": 4},
                                   "ingredients": [
                                       {"name": "flour", "grams": None, "per100g": {"kcal": 364, "fat": 1, "carbs": 76, "protein": 10}},
                                       {"name": "sugar", "grams": None, "per100g": {"kcal": 387, "fat": 0, "carbs": 100, "protein": 0}},
                                       {"name": "butter", "grams": None, "per100g": {"kcal": 717, "fat": 81, "carbs": 0, "protein": 1}}]}),
                       encoding="utf-8")
        run("pantry solve", [S("pantry.py"), "solve", str(rec)], d, contains="label_match")
        run("pantry match", [S("pantry.py"), "match", str(rec), "--out", str(d / "lm.json")], d, contains="panel_numbers")
        # inspect_app: toy Windows program and macOS bundle
        make_pe(d / "tiny.exe")
        run("inspect_app pe", [S("inspect_app.py"), "pe", str(d / "tiny.exe"), "--out", str(d / "pe")], d,
            contains="Creates a new picture")
        pe_menu = json.loads((d / "pe" / "menus.json").read_text(encoding="utf-8")) if (d / "pe" / "menus.json").exists() else {}
        item = (pe_menu.get("2") or [{}])[0].get("items", [{}])[0]
        results.append(("inspect_app pe menu item, shortcut and prompt",
                        item.get("label") == "New" and item.get("shortcut") == "Ctrl+N" and item.get("prompt") ==
                        "Creates a new picture", "" if item.get("label") == "New" else str(item)[:200]))
        make_bundle(d / "Tiny.app")
        make_mac_extras(d / "Tiny.app")
        r = run("inspect_app mac", [S("inspect_app.py"), "mac", str(d / "Tiny.app"), "--out", str(d / "mac")], d,
                contains='"Hello": "Hello, world"')
        if has_img:
            results.append(("inspect_app mac icns to png", any((d / "mac" / "icons").glob("*.png")), ""))
        try:
            mac = json.loads(r.stdout)
        except ValueError:
            mac = {}
        mfile = d / "mac" / "menus" / "Resources__en.lproj__MainMenu.nib.json"
        mm = json.loads(mfile.read_text(encoding="utf-8"))["menus"] if mfile.exists() else [{}]
        sub = ((mm[0].get("items") or [{}])[0].get("submenu") or [{}])
        results.append(("inspect_app mac menu tree with shortcut and action",
                        mm[0].get("main_menu") is True and sub[0].get("shortcut") == "Shift+Cmd+S"
                        and sub[0].get("action") == "saveDocumentAs:" and sub[1:2] == [{"separator": True}],
                        str(mm)[:200]))
        icons = {p.name for p in (d / "mac" / "icons").glob("Doc_*.png")}
        results.append(("inspect_app mac icns PNG and ARGB entries, no Pillow needed",
                        {"Doc_16x16.png", "Doc_128x128.png"} <= icons, str(sorted(icons))))
        car = next((c for c in mac.get("asset_catalogs", []) if c["file"].endswith("ProThemeBits.car")), {})
        results.append(("inspect_app mac theme .car in a framework: renditions written",
                        car.get("images_written") == 1 and (d / "mac" / car.get("folder", "-") / "Theme.png").exists(),
                        str(car)[:200]))
        imgs = json.loads((d / "mac" / "images.json").read_text(encoding="utf-8")) if (d / "mac" / "images.json").exists() else []
        fw_img = next((i for i in imgs if i["file"].endswith("button.png")), {})
        snd = next((x for x in mac.get("sounds", []) if x["file"].endswith("click.wav")), {})
        results.append(("inspect_app mac framework image and sound copied into the output folder",
                        bool(fw_img.get("copy")) and (d / "mac" / fw_img["copy"]).exists() and bool(snd.get("copy"))
                        and (d / "mac" / snd.get("copy", "-")).exists(), str(fw_img) + str(snd)))
        pre = d / "mac" / "data" / "FactoryPresets.plist.json"
        pre_ok = pre.exists() and json.loads(pre.read_text(encoding="utf-8"))[0]["data"] == {"$archive": {"exposure": 0.5}}
        results.append(("inspect_app mac preset plist with a keyed archive inside decoded", pre_ok,
                        pre.read_text(encoding="utf-8")[:200] if pre.exists() else "no data/FactoryPresets.plist.json"))
        hb = (mac.get("help_books") or [{}])[0]
        results.append(("inspect_app mac redirect-only help book flagged", hb.get("online_only") is True
                        and "help.example.com" in str(hb.get("redirects")), str(hb)[:200]))
        import zipfile
        blob = d / "Tiny.app" / "Contents" / "Frameworks" / "ProKit.framework" / "Versions" / "A" / "ProKit"
        blob.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * (2 << 20))  # framework code, stays zipped
        with zipfile.ZipFile(d / "Tiny.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted((d / "Tiny.app").rglob("*")):
                if f.is_file():
                    z.write(f, str(f.relative_to(d)))
        r = run("inspect_app mac on a zip", [S("inspect_app.py"), "mac", str(d / "Tiny.zip"), "--out", str(d / "macz")],
                d, contains='"left_zipped"')
        results.append(("inspect_app mac zip: outputs survive the temp folder",
                        (d / "macz" / "menus" / "Resources__en.lproj__MainMenu.nib.json").exists()
                        and "inspect-zip-" not in (d / "macz" / "images.json").read_text(encoding="utf-8")
                        if (d / "macz" / "images.json").exists() else False, ""))
        # extract pdf: text and embedded pictures
        make_pdf(d / "manual.pdf")
        run("extract pdf text", [S("extract.py"), "pdf", str(d / "manual.pdf")], d, contains="Hello manual")
        run("extract pdf --images", [S("extract.py"), "pdf", str(d / "manual.pdf"), "--images", str(d / "pdfimg")], d,
            contains='"images": 1')
        # inspect_js on a toy bundle
        (d / "b.js").write_text('var a="Snooze until later";var c=".x{color:#4285f4}";MSG_DONE="Mark done";',
                                encoding="utf-8")
        run("inspect_js", [S("inspect_js.py"), str(d / "b.js")], d, contains="Snooze until later")
        if has_ff:
            run("footage audio-check", [S("footage.py"), "audio-check", str(d / "a.wav")], d, contains="verdict")
            run("footage sfx", [S("footage.py"), "sfx", str(d / "a.wav"), "--out", str(d / "sfx")], d, contains="sound")
            run("footage loop", [S("footage.py"), "loop", str(d / "a.wav"), "--start", "0", "--end", "12",
                                 "--out", str(d / "loop.wav"), "--min-s", "2", "--max-s", "6"], d, contains="period_s")
        if has_img and has_ff:  # footdiff: same frames, delayed 1 s
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "2", "-i", str(d / "frames" / "f%02d.png"),
                            "-vf", "fps=10", str(d / "o.mp4")], check=False)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(d / "o.mp4"), "-vf", "tpad=start_duration=1",
                            str(d / "r.mp4")], check=False)
            run("footdiff compare", [S("footdiff.py"), "compare", str(d / "o.mp4"), str(d / "r.mp4"), "--fixed-speed",
                                     "--size", "80x60", "--out", str(d / "fd")], d, contains='"offset_s": 1.0')
            run("footdiff screens", [S("footdiff.py"), "screens", str(d / "o.mp4"), str(d / "r.mp4"), "--size", "80x60",
                                     "--out", str(d / "fd2")], d)
        if has_img:
            run("footage gif", [S("footage.py"), "gif", str(d / "t.gif"), "--out", str(d / "gif")], d, contains='"frames": 4')
            run("footage sprite", [S("footage.py"), "sprite", str(d / "frames"), "--out", str(d / "spr")], d,
                contains="poses")
            run("footage merge", [S("footage.py"), "merge", str(d / "frames"), "--out", str(d / "plate.png")], d,
                contains="unsure_share")
            run("footage cut", [S("footage.py"), "cut", str(d / "frames" / "f00.png"), "--box", "0,20,60,60",
                                "--out", str(d / "cut.png")], d, contains="kept_share")
            run("shots sheet", [S("shots.py"), "sheet", str(d / "frames"), "--out", str(d / "sheet.png")], d,
                contains='"frames": 8')
            (d / "frames" / "f00.meta.json").write_text("{}", encoding="utf-8")
            run("shots sheet skips non-images, makes --out folders", [S("shots.py"), "sheet",
                *sorted(str(p) for p in (d / "frames").iterdir()), "--out", str(d / "new" / "dir" / "s.png")], d,
                contains='"skipped_not_images"')
            run("shots sheet default --out next to the frames", [S("shots.py"), "sheet", str(d / "frames")], d)
            results.append(("shots sheet default --out is sheet.png in the folder", (d / "frames" / "sheet.png").exists(), ""))
            for x in ("f00.meta.json", "sheet.png"):
                (d / "frames" / x).unlink(missing_ok=True)
        if has_img and has_ff:  # scenes: listed times match ffmpeg seeks
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-filter_complex",
                            "color=c=red:s=64x48:r=30:d=1.3[a];color=c=yellow:s=64x48:r=30:d=1.6[b];"
                            "color=c=blue:s=64x48:r=30:d=1.4[c];[a][b][c]concat=n=3:v=1:a=0", "-pix_fmt", "yuv420p",
                            str(d / "sc.mp4")], check=False)
            r = run("footage scenes", [S("footage.py"), "--format", "json", "scenes", str(d / "sc.mp4"), "--out",
                                       str(d / "sc")], d, contains="t_seconds")
            from PIL import Image
            bad = []
            try:
                rows = json.loads(r.stdout)
            except ValueError:
                rows = []
            for row in rows:
                subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", str(row["t_seconds"]), "-i", str(d / "sc.mp4"),
                                "-frames:v", "1", str(d / "seek.png")], check=False)
                a_, b_ = (Image.open(x).convert("RGB").getpixel((32, 24)) for x in (row["frame"], d / "seek.png"))
                if sum(abs(p - q) for p, q in zip(a_, b_)) > 60:
                    bad.append((row["t_seconds"], a_, b_))
            results.append(("footage scenes times match ffmpeg seeks", len(rows) >= 3 and not bad, str(bad or rows)[:200]))
        # shots play: inline JSON steps aren't file names
        sys.path.insert(0, str(HERE))
        import shots
        from _common import _exists
        steps = shots._steps('[{"do": "click", "x": 4, "y": 5}, {"do": "wait", "ms": 10}]')
        results.append(("shots play takes inline --steps JSON", len(steps) == 2 and not _exists("[" + "x" * 5000 + "]"),
                        str(steps)))
        r = subprocess.run([PY, S("shots.py"), "play", "about:blank", "--settle", "0", "--timeout", "10", "--out",
                            str(d / "play"), "--steps", json.dumps([{"do": "wait", "ms": 1}] * 300)], cwd=ws,
                           capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
        results.append(("long inline --steps does not crash the workspace path check",
                        "File name too long" not in r.stderr and "OSError" not in r.stderr, r.stderr[-200:]))
        # wayback inventory lists the images it counts (fake CDX, no network)
        import argparse
        import contextlib
        import io
        import wayback
        cdx_rows = [{"timestamp": "20190401000000", "original": f"https://snake.example.com/img/s{i}.png",
                     "mimetype": "image/png", "length": "900"} for i in range(5)]
        real_cdx, wayback.cdx_query = wayback.cdx_query, lambda *a_, **k_: iter(cdx_rows)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                wayback.cmd_inventory(argparse.Namespace(
                    domain="snake.example.com", frm=None, to=None, limit=10, pages=1, max_seconds=30, max_files=400,
                    data=False, list_pages=False, assets_only=False, images=False, format="jsonl"))
        finally:
            wayback.cdx_query = real_cdx
        results.append(("wayback inventory lists the images it counts", buf.getvalue().count('"kind": "image"') == 5,
                        buf.getvalue()[-200:]))
        # ledger: sentiment unknowns need no feature, "@key" sources
        b2 = d / "b2.jsonl"
        b2.write_text("\n".join(json.dumps(x) for x in [
            {"tag": "seen", "kind": "content", "claim": "Manual lists 12 levels", "source": "dossier/manual.pdf",
             "locator": "p. 4", "as_of": "2006", "key": "man"},
            {"tag": "seen", "kind": "content", "claim": "Manual names the hero Pip", "source": "@man",
             "locator": "p. 7", "as_of": "2006"},
            {"tag": "unknown", "kind": "sentiment", "claim": "What players remember is unknown",
             "searched": "reddit, HN"}]) + "\n", encoding="utf-8")
        run("ledger add-batch (unknown sentiment, @key source)", [S("ledger.py"), "add-batch", str(b2), "--ws", str(ws)],
            d, contains='"added": 3')
        evs = [json.loads(x) for x in (ws / "evidence.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        pip = next((e for e in evs if "Pip" in e.get("claim", "")), {})
        results.append(("ledger resolves source @key to that line's source", pip.get("source") == "dossier/manual.pdf",
                        str(pip)[:200]))
        # enhance: known sprite at 3x, rescaled, JPEG'd, then half-pixel takes
        if has_img and enhance_ok():
            from PIL import Image
            random.seed(3)
            pal = [(20, 20, 20), (240, 240, 240), (220, 60, 50), (60, 170, 70), (250, 200, 40), (70, 90, 200)]
            sp = Image.new("RGB", (24, 20), pal[5])
            from PIL import ImageDraw
            dr = ImageDraw.Draw(sp)
            for _ in range(30):
                x, y = random.randrange(24), random.randrange(20)
                dr.rectangle((x, y, x + random.randrange(1, 5), y + random.randrange(1, 4)), fill=random.choice(pal))
            big = sp.resize((72, 60), Image.NEAREST).resize((81, 67), Image.BICUBIC).crop((2, 1, 80, 67))
            big.save(d / "pix.jpg", quality=60)
            run("enhance native", [S("enhance.py"), "native", str(d / "pix.jpg"), "--out", str(d / "pix.png")], d,
                contains='"scale_x": 3.3')
            for i, (dx, dy) in enumerate([(0, 0), (1, 0), (0, 1), (1, 1)]):
                Image.open(d / "frames" / "f03.png").crop((dx, dy, 158 + dx, 118 + dy)).resize((79, 59), Image.BOX) \
                    .save(d / f"take{i}.png")
            run("enhance stack", [S("enhance.py"), "stack", *(str(d / f"take{i}.png") for i in range(4)),
                                  "--out", str(d / "stack.png")], d, contains='"distinct_subpixel_positions": 4')
        # fit: rules hidden in readings
        (d / "words.csv").write_text("length,streak,points\n" + "".join(
            f"{L},{k},{10 * (L - 2) * k}\n" for L, k in [(3, 1), (5, 2), (8, 1), (4, 3), (6, 4), (7, 2), (3, 4), (8, 3)]),
                                     encoding="utf-8")
        run("fit rule", [S("fit.py"), "rule", str(d / "words.csv"), "--y", "points"], d,
            contains="points = -20*streak + 10*length*streak")
        y, vy, pts = 100.0, 0.0, []
        for fr in range(90):  # 30 fps, gravity 0.5 px/frame^2, each flap sets vy to -7
            if fr in (10, 32, 55):
                vy = -7
            pts.append({"t": fr / 30, "y": y})
            y, vy = y + vy, vy + 0.5
        (d / "track.json").write_text(json.dumps({"points": pts}), encoding="utf-8")
        run("fit motion", [S("fit.py"), "motion", str(d / "track.json"), "--fps", "30"], d,
            contains='"if_position_moves_before_speed_changes": -7.0')
        (d / "ranks.csv").write_text("score,rank\n120,C\n950,C\n1210,B\n4800,B\n5100,A\n", encoding="utf-8")
        run("fit cutoffs", [S("fit.py"), "cutoffs", str(d / "ranks.csv"), "--x", "score", "--label", "rank"], d,
            contains="score >= 5000 gives A")
        # packet: one feature's evidence at build time
        run("ledger packet", [S("ledger.py"), "packet", "title", "--ws", str(ws)], d, contains="E0001")
        # blueprint: two panels, the rebuild's divider 20 px off
        if has_img and enhance_ok():
            from PIL import Image, ImageDraw
            for name, split in (("ref.png", 140), ("now.png", 160)):
                im = Image.new("RGB", (320, 240), (240, 240, 240))
                dr = ImageDraw.Draw(im)
                dr.rectangle((0, 0, 319, 40), fill=(40, 70, 140))
                dr.rectangle((split, 41, 319, 239), fill=(255, 255, 255))
                dr.line((split, 41, split, 239), fill=(120, 120, 120))
                im.save(d / name)
            run("blueprint make", [S("blueprint.py"), "make", str(d / "ref.png"), "--out", str(d / "bp")], d,
                contains='"blocks"')
            run("blueprint diff", [S("blueprint.py"), "diff", str(d / "ref.png"), str(d / "now.png"), "--out",
                                   str(d / "bpd")], d, contains="nearest is at x=16")
        if not has_img:
            results.append(("media checks (skipped: no Pillow)", True, ""))
        if not has_ff:
            results.append(("audio/video checks (skipped: no ffmpeg)", True, ""))
        ledger_a2_checks(S, d, has_img)
        wayback_shots_checks(d, results)
        community_checks(d, results)
        footage_fix_checks(d, S, has_ff, has_img, run, results)
        misc_checks2(S, d, has_img, has_ff, run, results)
        wayback_shots_checks2(d, results)
        truncation_checks(d, results)
        r53_checks(S, d, has_img, results)
        heavy_env_checks(d, results)
        portability_checks(S, d, results)
        # verify lint on a minimal journey file
        (ws / "codex" / "parity.json").write_text(json.dumps({"features": [{"id": "F1", "name": "title", "priority": "soul"}]}),
                                                  encoding="utf-8")
        (ws / "codex" / "journeys.json").write_text(json.dumps([{"id": "J1", "feature": "F1", "steps": [
            {"do": "goto", "url": "/"}, {"do": "expect_visible", "selector": "body"}]}]), encoding="utf-8")
        run("verify lint", [S("verify.py"), "lint", "--ws", str(ws)], d)
        run("verify score", [S("verify.py"), "score", "--ws", str(ws)], d)
        (ws / "codex" / "screens.json").write_text(json.dumps([{"id": "S1", "name": "Credits", "then": None,
                                                                 "why": "never recorded"}]), encoding="utf-8")
        run("verify score (excused screen)", [S("verify.py"), "score", "--ws", str(ws)], d)
        sb = (ws / "codex" / "SCOREBOARD.md").read_text(encoding="utf-8") if (ws / "codex" / "SCOREBOARD.md").exists() else ""
        results.append(("scoreboard counts an excused screen", "1 excused" in sb, "" if "1 excused" in sb else sb[-200:]))
        bad = d / "bad.jsonl"
        bad.write_text('{"tag": "seen", "kind": "ui", "claim": "Credits roll", "source": "https://e.org", '
                       '"locator": "0:09", "feature": "credits", "as_of": "2006"}\nnot json {\n', encoding="utf-8")
        run("ledger add-batch keeps invalid lines", [S("ledger.py"), "add-batch", str(bad), "--ws", str(ws)], d,
            contains='"rejected": 1')
        rej = d / "bad.rejected.jsonl"
        results.append(("rejected file holds the invalid line", rej.exists() and "not json" in rej.read_text(encoding="utf-8"), ""))
        # look: plain `differs` keeps the screen open, the others settle it
        sys.path.insert(0, str(HERE))
        import verify
        (ws / "codex" / "LOOK.md").write_text(
            "## S1-title (code AAAA)\n- band 1: matches\n- band 2: other moment: mid-bob\n"
            "## S2-play (code BBBB)\n- band 1: differs: substitute font\n- band 2: differs, no trace: none settled\n",
            encoding="utf-8")
        bands = [{"band": 1}, {"band": 2}]
        st = verify._look_status(ws, {"visual": [
            {"name": "S1-title", "look": "x", "look_code": "AAAA", "look_bands": bands},
            {"name": "S2-play", "look": "y", "look_code": "BBBB", "look_bands": bands}]})
        results.append(("look: an open difference keeps its screen unsettled",
                        st["done"] == ["S1-title"] and st["differs"] == ["S2-play band 1"] and st["no_trace"] == 1,
                        str(st)))
    bad = [r for r in results if not r[1]]
    for name, ok, why in results:
        print(f"{'ok  ' if ok else 'FAIL'} {name}{'' if ok else '  -> ' + why}")
    skipped = [n for n, _, _ in results if "skipped" in n]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed"
          + (f" ({len(skipped)} group(s) skipped: missing Pillow, ffmpeg or numpy)" if skipped else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
