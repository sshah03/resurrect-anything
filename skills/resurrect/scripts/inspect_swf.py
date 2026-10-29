#!/usr/bin/env python3
"""Dirty-room inspector for archived Flash files (.swf). Reports FACTS and never exports expression.

What it prints (everything a rebuild needs to know, nothing it may copy):
  header     stage size, frame rate, frame count, SWF version, compression
  timeline   tags per frame, frame labels, exported names, sprites and their frame counts
  text       edit-text variables and initial text, button and frame labels
  palette    fill and line colours per shape with bounds (sizes), no outlines
  sounds     id, format, rate, duration, export name, no audio
  digest     per script, the names and numbers it uses (read this first, then `actions`)
  actions    ActionScript 1/2 disassembly (frames, clip events, buttons, functions) with
             pushed constants, so physics and rules can be read as numbers
  abc        ActionScript 3, only the constant pool (numbers and strings). For full AS3 logic
             use JPEXS FFDec (`ffdec -export script out file.swf`) in the same dirty room

Usage
  inspect_swf.py FILE.swf                      header, timeline, text, palette, sounds, digest, abc
  inspect_swf.py FILE.swf --section actions    full AS1/2 disassembly (after reading the digest)

Not decoded: static text (glyph indices need the font's code table, so read it from screenshots),
shape outlines, bitmaps, audio and video. Those are expression and left out on purpose. Embedded video
streams are named in the header (codec, size, frames, fps).

Restore mode: read the digest and scripts to port the game exactly, and use `--extract DIR` to save
its original sounds, bitmaps and video (.flv as stored, plus .mp4 when ffmpeg is installed). Record
constants in the ledger with locators (e.g. "copter.swf DoAction f2 @0x1a"). The file is parsed,
never executed. (Share-safe mode is facts only, read by a dirty-room subagent, with nothing extracted.
See references/share-safe.md.)
Stdlib only.
"""
from __future__ import annotations

import argparse
import lzma
import re
import struct
import sys
import zlib
from pathlib import Path

TAGS = {0: "End", 1: "ShowFrame", 2: "DefineShape", 4: "PlaceObject", 5: "RemoveObject",
        6: "DefineBits", 7: "DefineButton", 8: "JPEGTables", 9: "SetBackgroundColor", 10: "DefineFont",
        11: "DefineText", 12: "DoAction", 13: "DefineFontInfo", 14: "DefineSound",
        15: "StartSound", 18: "SoundStreamHead", 19: "SoundStreamBlock", 20: "DefineBitsLossless",
        21: "DefineBitsJPEG2", 22: "DefineShape2", 24: "Protect", 26: "PlaceObject2",
        28: "RemoveObject2", 32: "DefineShape3", 33: "DefineText2", 34: "DefineButton2",
        35: "DefineBitsJPEG3", 36: "DefineBitsLossless2", 37: "DefineEditText", 39: "DefineSprite",
        43: "FrameLabel", 45: "SoundStreamHead2", 46: "DefineMorphShape", 48: "DefineFont2",
        56: "ExportAssets", 57: "ImportAssets", 59: "DoInitAction", 60: "DefineVideoStream", 61: "VideoFrame",
        62: "DefineFontInfo2", 69: "FileAttributes", 70: "PlaceObject3", 72: "DoABC",
        75: "DefineFont3", 76: "SymbolClass", 77: "Metadata", 82: "DoABC2", 83: "DefineShape4",
        86: "DefineSceneAndFrameLabelData", 87: "DefineBinaryData", 88: "DefineFontName",
        90: "DefineBitsJPEG4"}

ACTIONS = {0x04: "nextFrame", 0x05: "prevFrame", 0x06: "play", 0x07: "stop", 0x08: "toggleQuality",
           0x09: "stopSounds", 0x0A: "add", 0x0B: "subtract", 0x0C: "multiply", 0x0D: "divide",
           0x0E: "equals", 0x0F: "less", 0x10: "and", 0x11: "or", 0x12: "not", 0x13: "stringEquals",
           0x14: "stringLength", 0x15: "stringExtract", 0x17: "pop", 0x18: "toInteger",
           0x1C: "getVariable", 0x1D: "setVariable", 0x20: "setTarget2", 0x21: "stringAdd",
           0x22: "getProperty", 0x23: "setProperty", 0x24: "cloneSprite", 0x25: "removeSprite",
           0x26: "trace", 0x27: "startDrag", 0x28: "endDrag", 0x29: "stringLess", 0x2A: "throw",
           0x2B: "castOp", 0x2C: "implementsOp", 0x30: "randomNumber", 0x31: "mbStringLength",
           0x32: "charToAscii", 0x33: "asciiToChar", 0x34: "getTime", 0x35: "mbStringExtract",
           0x36: "mbCharToAscii", 0x37: "mbAsciiToChar", 0x3A: "delete", 0x3B: "delete2",
           0x3C: "defineLocal", 0x3D: "callFunction", 0x3E: "return", 0x3F: "modulo",
           0x40: "newObject", 0x41: "defineLocal2", 0x42: "initArray", 0x43: "initObject",
           0x44: "typeOf", 0x45: "targetPath", 0x46: "enumerate", 0x47: "add2", 0x48: "less2",
           0x49: "equals2", 0x4A: "toNumber", 0x4B: "toString", 0x4C: "pushDuplicate",
           0x4D: "stackSwap", 0x4E: "getMember", 0x4F: "setMember", 0x50: "increment",
           0x51: "decrement", 0x52: "callMethod", 0x53: "newMethod", 0x54: "instanceOf",
           0x55: "enumerate2", 0x60: "bitAnd", 0x61: "bitOr", 0x62: "bitXor", 0x63: "bitLShift",
           0x64: "bitRShift", 0x65: "bitURShift", 0x66: "strictEquals", 0x67: "greater",
           0x68: "stringGreater", 0x69: "extends", 0x81: "gotoFrame", 0x83: "getURL",
           0x87: "storeRegister", 0x88: "constantPool", 0x8A: "waitForFrame", 0x8B: "setTarget",
           0x8C: "gotoLabel", 0x8D: "waitForFrame2", 0x8E: "defineFunction2", 0x8F: "try",
           0x94: "with", 0x96: "push", 0x99: "jump", 0x9A: "getURL2", 0x9B: "defineFunction",
           0x9D: "if", 0x9E: "call", 0x9F: "gotoFrame2"}

PROPS = ["_x", "_y", "_xscale", "_yscale", "_currentframe", "_totalframes", "_alpha", "_visible",
         "_width", "_height", "_rotation", "_target", "_framesloaded", "_name", "_droptarget",
         "_url", "_highquality", "_focusrect", "_soundbuftime", "_quality", "_xmouse", "_ymouse"]

CLIP_EVENTS = [  # ClipEventFlags bits, first byte first, MSB first
    "keyUp", "keyDown", "mouseUp", "mouseDown", "mouseMove", "unload", "enterFrame", "load",
    "dragOver", "rollOut", "rollOver", "releaseOutside", "release", "press", "initialize", "data",
    "", "", "", "", "", "construct", "keyPress", "dragOut"]

VIDEO_CODEC = {2: "FLV1 (Sorenson H.263)", 3: "Screen Video", 4: "VP6", 5: "VP6 with alpha", 6: "Screen Video 2"}

SOUND_FMT = {0: "pcm", 1: "adpcm", 2: "mp3", 3: "pcm-le", 4: "nellymoser16k", 5: "nellymoser8k",
             6: "nellymoser", 11: "speex"}


class Bits:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.byte, self.bit = data, pos, 0

    def ub(self, n: int) -> int:
        v = 0
        for _ in range(n):
            if self.byte >= len(self.d):
                raise EOFError
            v = (v << 1) | ((self.d[self.byte] >> (7 - self.bit)) & 1)
            self.bit += 1
            if self.bit == 8:
                self.bit, self.byte = 0, self.byte + 1
        return v

    def sb(self, n: int) -> int:
        v = self.ub(n)
        return v - (1 << n) if n and v & (1 << (n - 1)) else v

    def align(self) -> int:
        if self.bit:
            self.bit, self.byte = 0, self.byte + 1
        return self.byte


def rect(d: bytes, p: int):
    b = Bits(d, p)
    n = b.ub(5)
    xmin, xmax, ymin, ymax = (b.sb(n) / 20 for _ in range(4))
    return (xmin, xmax, ymin, ymax), b.align()


def skip_matrix(d: bytes, p: int) -> int:
    b = Bits(d, p)
    if b.ub(1):
        n = b.ub(5)
        b.ub(n * 2)
    if b.ub(1):
        n = b.ub(5)
        b.ub(n * 2)
    n = b.ub(5)
    b.ub(n * 2)
    return b.align()


def matrix_translate(d: bytes, p: int):
    """Translation and scale of a placement matrix."""
    b = Bits(d, p)
    sx = sy = 1.0
    if b.ub(1):
        n = b.ub(5)
        sx, sy = b.sb(n) / 65536, b.sb(n) / 65536
    if b.ub(1):
        n = b.ub(5)
        b.ub(n * 2)
    n = b.ub(5)
    tx, ty = b.sb(n) / 20, b.sb(n) / 20
    return (tx, ty, sx, sy), b.align()


def skip_cxform(d: bytes, p: int, alpha: bool) -> int:
    b = Bits(d, p)
    has_add, has_mult = b.ub(1), b.ub(1)
    n = b.ub(4)
    k = 4 if alpha else 3
    b.ub(n * k * (has_add + has_mult))
    return b.align()


def cstr(d: bytes, p: int):
    e = d.index(b"\x00", p)
    return d[p:e].decode("latin-1"), e + 1


def u16(d, p):
    return struct.unpack_from("<H", d, p)[0]


def u32(d, p):
    return struct.unpack_from("<I", d, p)[0]


def colour(d, p, alpha):
    c = "#%02x%02x%02x" % tuple(d[p:p + 3])
    return (c + ("" if not alpha or d[p + 3] == 255 else "@%d%%" % round(d[p + 3] / 2.55))), p + (4 if alpha else 3)


# ------------------------------------------------------------------ actions (AS1/AS2)

def fmt_num(x: float) -> str:
    if x != x or x in (float("inf"), float("-inf")):  # real constant pools hold NaN and infinities
        return repr(x)
    return str(int(x)) if x == int(x) and abs(x) < 1e15 else repr(round(x, 6))


def disasm(code: bytes, pool: list[str] | None = None, indent: str = "    ") -> list[str]:
    """Linear disassembly of an action list, pushes decoded."""
    pool = pool if pool is not None else []
    out, p = [], 0
    while p < len(code):
        off = p
        op = code[p]
        p += 1
        if op == 0:
            out.append(f"{indent}{off:04x} end")
            break
        ln = 0
        if op >= 0x80:
            ln = u16(code, p)
            p += 2
        payload, p = code[p:p + ln], p + ln
        name = ACTIONS.get(op, f"op_{op:02x}")
        arg = ""
        try:
            if op == 0x96:
                vals, q = [], 0
                while q < len(payload):
                    t = payload[q]
                    q += 1
                    if t == 0:
                        s, q = cstr(payload, q)
                        vals.append(repr(s))
                    elif t == 1:
                        vals.append(fmt_num(struct.unpack_from("<f", payload, q)[0]))
                        q += 4
                    elif t == 2:
                        vals.append("null")
                    elif t == 3:
                        vals.append("undefined")
                    elif t == 4:
                        vals.append(f"r{payload[q]}")
                        q += 1
                    elif t == 5:
                        vals.append("true" if payload[q] else "false")
                        q += 1
                    elif t == 6:
                        raw = payload[q:q + 8]
                        vals.append(fmt_num(struct.unpack("<d", raw[4:8] + raw[0:4])[0]))
                        q += 8
                    elif t == 7:
                        vals.append(str(struct.unpack_from("<i", payload, q)[0]))
                        q += 4
                    elif t in (8, 9):
                        i = payload[q] if t == 8 else u16(payload, q)
                        q += 1 if t == 8 else 2
                        vals.append(repr(pool[i]) if i < len(pool) else f"c{i}")
                    else:
                        vals.append(f"?type{t}")
                        break
                arg = ", ".join(vals)
            elif op == 0x88:
                n, q, pool[:] = u16(payload, 0), 2, []
                for _ in range(n):
                    s, q = cstr(payload, q)
                    pool.append(s)
                arg = f"{n} strings"
            elif op in (0x99, 0x9D):
                arg = f"-> {p + struct.unpack('<h', payload)[0]:04x}"
            elif op in (0x81,):
                arg = str(u16(payload, 0) + 1)
            elif op in (0x8B, 0x8C):
                arg = repr(cstr(payload, 0)[0])
            elif op == 0x83:
                a, q = cstr(payload, 0)
                b, _ = cstr(payload, q)
                arg = f"{a!r}, {b!r}"
            elif op == 0x87:
                arg = f"r{payload[0]}"
            elif op == 0x9B:
                fname, q = cstr(payload, 0)
                n = u16(payload, q)
                q += 2
                params = []
                for _ in range(n):
                    s, q = cstr(payload, q)
                    params.append(s)
                arg = f"function {fname or '<anon>'}({', '.join(params)}) body {u16(payload, q)} bytes"
            elif op == 0x8E:
                fname, q = cstr(payload, 0)
                n = u16(payload, q)
                q += 2 + 1 + 2
                params = []
                for _ in range(n):
                    q += 1
                    s, q = cstr(payload, q)
                    params.append(s)
                arg = f"function {fname or '<anon>'}({', '.join(params)}) body {u16(payload, q)} bytes"
            elif op == 0x9F:
                arg = "play" if payload and payload[0] & 1 else "stop"
            elif ln:
                arg = payload[:16].hex()
        except (IndexError, struct.error, ValueError):
            arg = "<undecodable>"
        out.append(f"{indent}{off:04x} {name} {arg}".rstrip())
    return annotate(out)


def annotate(lines: list[str]) -> list[str]:
    """Name getProperty/setProperty indexes (_x, _y, _rotation...)."""
    out = []
    for i, line in enumerate(lines):
        if line.split()[1:2] in (["getProperty"], ["setProperty"]):
            for prev in reversed(lines[max(0, i - 3):i]):
                if " push " in prev:
                    vals = prev.split(" push ", 1)[1].split(", ")
                    # getProperty pops (target, index), index pushed last.
                    # setProperty pops three, only safe from a single push.
                    if line.split()[1] == "getProperty":
                        idx = vals[-1]
                    else:
                        idx = vals[-2] if len(vals) >= 3 and prev is lines[i - 1] else ""
                    if idx.isdigit() and int(idx) < len(PROPS):
                        line += f"   ; {PROPS[int(idx)]}"
                    break
        out.append(line)
    return out


# ------------------------------------------------------------------ AS3 constant pool

def u30(d, p):
    v, s = 0, 0
    for _ in range(5):
        b = d[p]
        p += 1
        v |= (b & 0x7F) << s
        s += 7
        if not b & 0x80:
            break
    return v, p


def abc_pool(d: bytes) -> dict:
    p = 4  # minor, major
    ints, uints, dbls, strs = [], [], [], []
    n, p = u30(d, p)
    for _ in range(max(0, n - 1)):
        v, p = u30(d, p)
        ints.append(v - (1 << 32) if v & 0x80000000 else v)
    n, p = u30(d, p)
    for _ in range(max(0, n - 1)):
        v, p = u30(d, p)
        uints.append(v)
    n, p = u30(d, p)
    for _ in range(max(0, n - 1)):
        dbls.append(struct.unpack_from("<d", d, p)[0])
        p += 8
    n, p = u30(d, p)
    for _ in range(max(0, n - 1)):
        ln, p = u30(d, p)
        strs.append(d[p:p + ln].decode("utf-8", "replace"))
        p += ln
    return {"ints": ints, "uints": uints, "doubles": [fmt_num(x) for x in dbls], "strings": strs}


# ------------------------------------------------------------------ tags

class Report:
    extract_dir = None  # --extract saves originals here
    jpeg_tables = b""  # shared by every DefineBits JPEG
    names: dict = {}
    streams: dict = {}  # timeline -> streamed MP3 bytes (narration, synced music)

    def __init__(self):
        self.header, self.timeline, self.text, self.palette, self.sounds = [], [], [], [], []
        self.actions, self.abc, self.digest = [], [], []
        self.extracted = []
        self.version = 0
        self.fps = 0.0
        self.videos = {}  # id -> codec, size, declared frames, VideoFrame packets
        self.bitmaps = {"lossless": 0, "jpeg_alpha": 0}  # bitmaps that need Pillow


def place_object2(t: bytes, version: int, where: str, r: Report, frame: int):
    flags = t[0]
    depth = u16(t, 1)
    p = 3
    char = None
    if flags & 0x02:
        char = u16(t, p)
        p += 2
    at = None
    if flags & 0x04:
        at, p = matrix_translate(t, p)
    if flags & 0x08:
        p = skip_cxform(t, p, True)
    if flags & 0x10:
        p += 2
    name = None
    if flags & 0x20:
        name, p = cstr(t, p)
    if flags & 0x40:
        p += 2
    desc = f"[{where} f{frame}] place depth={depth}" + (f" char={char}" if char is not None else "") + \
           (f" name={name!r}" if name else "") + (f" at=({at[0]:g},{at[1]:g})" if at else "") + \
           (f" scale=({at[2]:.3g},{at[3]:.3g})" if at and (at[2], at[3]) != (1.0, 1.0) else "")
    r.timeline.append(desc)
    if not flags & 0x80:
        return
    p += 2  # reserved
    fl = 4 if version >= 6 else 2
    p += fl  # all-events flags
    while p + fl <= len(t):
        ev = t[p:p + fl]
        p += fl
        if not any(ev):
            break
        size = u32(t, p)
        p += 4
        bits = int.from_bytes(ev.ljust(4, b"\x00"), "big")
        names = [CLIP_EVENTS[i] for i in range(24) if CLIP_EVENTS[i] and bits & (1 << (31 - i))]
        body = t[p:p + size]
        if bits & (1 << (31 - 22)):  # keyPress carries a key code first
            body = body[1:]
        r.actions.append(f"[{where} f{frame}] onClipEvent({', '.join(names)}) on depth {depth}"
                         + (f" ({name})" if name else "") + (f" char={char}" if char is not None else ""))
        r.actions += disasm(body)
        p += size


def shape_colours(code: int, t: bytes, r: Report):
    sid = u16(t, 0)
    bounds, p = rect(t, 2)
    alpha = code in (32, 83)
    if code == 83:
        _, p = rect(t, p)
        p += 1
    cols = []
    try:
        n = t[p]
        p += 1
        if n == 0xFF and code != 2:
            n, p = u16(t, p), p + 2
        for _ in range(n):
            typ = t[p]
            p += 1
            if typ == 0x00:
                c, p = colour(t, p, alpha)
                cols.append(c)
            elif typ in (0x10, 0x12, 0x13):
                p = skip_matrix(t, p)
                g = t[p] & 0x0F
                p += 1
                stops = []
                for _ in range(g):
                    p += 1
                    c, p = colour(t, p, alpha)
                    stops.append(c)
                if typ == 0x13:
                    p += 2
                cols.append("gradient(" + " > ".join(stops) + ")")
            elif 0x40 <= typ <= 0x43:
                cols.append(f"bitmap#{u16(t, p)}")
                p = skip_matrix(t, p + 2)
        if code != 83:
            n = t[p]
            p += 1
            if n == 0xFF and code != 2:
                n, p = u16(t, p), p + 2
            for _ in range(n):
                w = u16(t, p)
                c, p = colour(t, p + 2, alpha)
                cols.append(f"line {w / 20:g}px {c}")
    except (IndexError, struct.error):
        cols.append("...")
    w, h = bounds[1] - bounds[0], bounds[3] - bounds[2]
    r.palette.append(f"shape {sid}: {w:g}x{h:g} px at ({bounds[0]:g},{bounds[2]:g}): {', '.join(cols) or 'none'}")


def slug(s: str) -> str:
    """File-name part of a class name (com.maker.app.sounds.ExitVoice -> ExitVoice)."""
    tail = re.split(r"[.:/]+", s)[-1] if s else ""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", tail or s).strip("-")[:60] or "x"


def _unpremultiply(img):
    """Flash premultiplies lossless alpha. PNG wants it straight, or edges go dark."""
    from PIL import Image
    return Image.frombytes("RGBa", img.size, img.convert("RGBA").tobytes()).convert("RGBA")


def _jpeg_bytes(data: bytes) -> bytes:
    if data.startswith(b"\xff\xd9\xff\xd8"):  # old encoders prefixed an empty JPEG block
        data = data[4:]
    return data


def save_bitmap(code: int, t: bytes, r: Report) -> None:
    """Save original bitmaps.

    DefineBits/JPEG2 -> .jpg. JPEG3/JPEG4 add a zlib alpha plane -> RGBA .png.
    Lossless/Lossless2 (zlib; colormapped, 15- or 24-bit; Lossless2 premultiplied) -> .png.
    SWF 8+ bitmap tags may hold a PNG or GIF, saved as is."""
    cid = u16(t, 0)
    base = Report.extract_dir / f"bitmap-{cid}-{slug(Report.names.get(cid, ''))}"
    if code in (6, 21, 35, 90):
        alpha = b""
        if code == 6:  # tables live in the shared JPEGTables tag
            data = t[2:]
            tables = _jpeg_bytes(Report.jpeg_tables)
            if tables[:2] == b"\xff\xd8" and tables[-2:] == b"\xff\xd9" and data[:2] == b"\xff\xd8":
                data = tables[:-2] + data[2:]  # drop the tables' EOI and the image's SOI
        elif code == 21:
            data = t[2:]
        else:
            off, start = u32(t, 2), 6 if code == 35 else 8  # JPEG4 adds a 2-byte deblocking parameter
            data, alpha = t[start:start + off], t[start + off:]
        data = _jpeg_bytes(data)
        if data[:4] == b"\x89PNG" or data[:4] == b"GIF8":
            out = base.with_suffix(".png" if data[:1] == b"\x89" else ".gif")
            out.write_bytes(data)  # PNG/GIF have their own transparency
            r.extracted.append(str(out))
            return
        if alpha:
            try:
                import io
                from PIL import Image
                img = Image.open(io.BytesIO(data)).convert("RGB")
                plane = zlib.decompress(alpha)
                if len(plane) < img.width * img.height:
                    raise ValueError("short alpha plane")
                img.putalpha(Image.frombytes("L", img.size, plane[:img.width * img.height]))
                raw = img.tobytes()
                semi = [i for i in range(3, len(raw), 4) if 0 < raw[i] < 255]
                if semi and sum(max(raw[i - 3:i]) <= raw[i] + 8 for i in semi) >= 0.9 * len(semi):
                    img = _unpremultiply(img)  # premultiplied by Flash (colour <= alpha at edges)
                out = base.with_suffix(".png")
                img.save(out)
                r.extracted.append(str(out))
                return
            except ImportError:
                r.extracted.append(f"(bitmap {cid}: install Pillow to keep its transparency; saved without alpha)")
            except Exception as e:  # bad alpha, but the colour is still worth keeping
                r.extracted.append(f"(bitmap {cid}: alpha not applied ({e.__class__.__name__}); saved without alpha)")
        out = base.with_suffix(".jpg")
        out.write_bytes(data)
        r.extracted.append(str(out))
        return
    fmt, w, h = t[2], u16(t, 3), u16(t, 5)
    try:
        from PIL import Image
    except ImportError:
        r.extracted.append(f"(bitmap {cid} skipped: install Pillow to save lossless bitmaps)")
        return
    try:
        if fmt == 5:  # 32 bpp, ARGB (Lossless2) or pad+RGB (Lossless)
            px = zlib.decompress(t[7:])[:w * h * 4]
            img = Image.frombytes("RGBA" if code == 36 else "RGB", (w, h), px, "raw", "ARGB" if code == 36 else "XRGB")
        elif fmt == 4:  # 15 bpp (Lossless only), big-endian 0RRRRRGGGGGBBBBB, 4-byte-padded rows
            px = zlib.decompress(t[7:])
            row = (w * 2 + 3) & ~3
            img = Image.new("RGB", (w, h))
            vals = [int.from_bytes(px[y * row + 2 * x:y * row + 2 * x + 2], "big") for y in range(h) for x in range(w)]
            img.putdata([((v >> 10 & 31) * 255 // 31, (v >> 5 & 31) * 255 // 31, (v & 31) * 255 // 31) for v in vals])
        elif fmt == 3:  # palette (RGBA in Lossless2), 8-bit indices, 4-byte-padded rows
            n = t[7] + 1
            px = zlib.decompress(t[8:])
            step = 4 if code == 36 else 3
            pal = [tuple(px[i * step:(i + 1) * step]) for i in range(n)]
            row = (w + 3) & ~3
            idx = px[n * step:]
            img = Image.new("RGBA" if code == 36 else "RGB", (w, h))
            blank = (0,) * step
            img.putdata([pal[i] if i < n else blank for y in range(h) for i in idx[y * row:y * row + w]])
        else:
            r.extracted.append(f"(bitmap {cid}: format {fmt} not supported)")
            return
        if code == 36:
            img = _unpremultiply(img)
        out = base.with_suffix(".png")
        img.save(out)
        r.extracted.append(str(out))
    except Exception as e:
        r.extracted.append(f"(bitmap {cid} failed: {e.__class__.__name__})")


def _flv(v: dict, fps: float) -> bytes:
    """A video stream's packets in FLV framing, otherwise unchanged.

    VideoFrame payloads are FLV packets minus the frame-type byte (and VP6's size-adjustment byte)."""
    codec, out = v["codec"], bytearray(b"FLV\x01\x01" + struct.pack(">I", 9) + b"\0\0\0\0")
    for i, (num, data) in enumerate(sorted(v["packets"], key=lambda x: x[0])):
        if codec == 2 and len(data) > 6:  # Sorenson H.263, type 0 is an intra frame
            b = Bits(data)
            b.ub(30)
            size = b.ub(3)
            b.ub({0: 16, 1: 32}.get(size, 0))
            key = b.ub(2) == 0
        elif codec in (4, 5):
            key = not data[(3 if codec == 5 else 0)] & 0x80
        else:
            if (codec in (3, 6) and len(data) > 3 and data[0] & 0x0F == codec
                    and ((data[1] & 0x0F) << 8 | data[2]) == v["w"] != ((data[0] & 0x0F) << 8 | data[1])):
                data = data[1:]  # ffmpeg's SWF muxer leaves a doubled frame-type byte
            key = i == 0
        body = bytes([(1 if key else 2) << 4 | codec]) + (b"\0" if codec in (4, 5) else b"") + data
        ms = round(num * 1000 / (fps or 12))
        out += (b"\x09" + struct.pack(">I", len(body))[1:] + struct.pack(">I", ms & 0xFFFFFF)[1:]
                + bytes([ms >> 24 & 0xFF]) + b"\0\0\0" + body + struct.pack(">I", 11 + len(body)))
    return bytes(out)


def save_videos(r: Report, swf: Path) -> None:
    """Embedded video as .flv (original packets) and .mp4 (ffmpeg, for footage.py)."""
    import shutil
    import subprocess
    for vid, v in sorted(r.videos.items()):
        if not v["packets"]:
            continue
        base = Report.extract_dir / f"video-{vid}-{slug(Report.names.get(vid, ''))}"
        flv = base.with_suffix(".flv")
        flv.write_bytes(_flv(v, r.fps))
        r.extracted.append(str(flv))
        if not shutil.which("ffmpeg"):
            r.extracted.append(f"(video {vid}: no ffmpeg for the .mp4; `ffmpeg -i {flv.name} {base.name}.mp4` later, "
                               "or open the .flv in VLC)")
            continue
        mp4 = base.with_suffix(".mp4")
        enc = ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-pix_fmt", "yuv420p"]
        for src in ([str(flv)], [str(swf)] if len(r.videos) == 1 else []):  # ffmpeg reads simple SWFs too
            if not src:
                continue
            for codec in (["-c:v", "libx264", "-crf", "18"], ["-c:v", "mpeg4", "-q:v", "2"]):
                done = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", *src, "-an", *enc, *codec, str(mp4)],
                                      capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
                if done.returncode == 0 and mp4.exists() and mp4.stat().st_size:
                    break
            if done.returncode == 0 and mp4.exists() and mp4.stat().st_size:
                r.extracted.append(str(mp4))
                break
        else:
            r.extracted.append(f"(video {vid}: ffmpeg could not convert it ({done.stderr.strip()[-120:]}); "
                               "the .flv holds the original packets)")


def walk(d: bytes, p: int, end: int, r: Report, where: str = "main", pool=None):
    frame = 1
    pool = pool if pool is not None else []
    counts = {}
    while p < end:
        h = u16(d, p)
        p += 2
        code, ln = h >> 6, h & 0x3F
        if ln == 0x3F:
            ln = u32(d, p)
            p += 4
        t, p = d[p:p + ln], p + ln
        name = TAGS.get(code, f"tag{code}")
        counts[name] = counts.get(name, 0) + 1
        try:
            if code == 0:
                break
            if code == 1:
                frame += 1
            elif code == 9:
                r.header.append(f"background {colour(t, 0, False)[0]}")
            elif code in (12, 59):
                if code == 59:
                    r.actions.append(f"[{where}] #initclip for sprite {u16(t, 0)}")
                    t = t[2:]
                else:
                    r.actions.append(f"[{where} f{frame}] frame actions")
                r.actions += disasm(t, pool)
            elif code == 26:
                place_object2(t, r.version, where, r, frame)
            elif code in (2, 22, 32, 83):
                shape_colours(code, t, r)
            elif code == 8:
                Report.jpeg_tables = t
            elif code in (6, 21, 35, 90, 20, 36):
                if code in (20, 36):
                    r.bitmaps["lossless"] += 1
                elif code in (35, 90):
                    r.bitmaps["jpeg_alpha"] += 1
                if Report.extract_dir:
                    save_bitmap(code, t, r)
            elif code == 60:  # id, frames, width, height, flags, codec
                r.videos[u16(t, 0)] = {"frames": u16(t, 2), "w": u16(t, 4), "h": u16(t, 6), "codec": t[9],
                                       "packets": []}
            elif code == 61:  # stream id, frame number, one compressed frame
                v = r.videos.setdefault(u16(t, 0), {"frames": 0, "w": 0, "h": 0, "codec": 0, "packets": []})
                v["packets"].append((u16(t, 2), t[4:] if Report.extract_dir else b""))
            elif code == 14:
                sid, fl, n = u16(t, 0), t[2], u32(t, 3)
                if Report.extract_dir and fl >> 4 == 2:  # MP3, skip the 2-byte seek count
                    out = Report.extract_dir / f"sound-{sid}-{slug(Report.names.get(sid, ''))}.mp3"
                    out.write_bytes(t[9:])
                    r.extracted.append(str(out))
                rate = [5512, 11025, 22050, 44100][(fl >> 2) & 3]
                r.sounds.append(f"sound {sid}: {SOUND_FMT.get(fl >> 4, fl >> 4)}, {rate} Hz, "
                                f"{'stereo' if fl & 1 else 'mono'}, {n / rate:.2f} s")
            elif code == 43:
                r.text.append(f"[{where} f{frame}] frame label {cstr(t, 0)[0]!r}")
            elif code == 56:
                n, q = u16(t, 0), 2
                for _ in range(n):
                    cid = u16(t, q)
                    s, q = cstr(t, q + 2)
                    Report.names[cid] = s
                    r.timeline.append(f"export char {cid} as {s!r}")
            elif code == 76:
                n, q = u16(t, 0), 2
                for _ in range(n):
                    cid = u16(t, q)
                    s, q = cstr(t, q + 2)
                    Report.names.setdefault(cid, s)  # AS3 files name their assets here, not in ExportAssets
                    r.timeline.append(f"class char {cid} = {s}")
            elif code == 37:
                cid = u16(t, 0)
                bounds, q = rect(t, 2)
                f1, f2 = t[q], t[q + 1]
                q += 2
                font = None
                if f1 & 0x01:
                    font = f"font {u16(t, q)} {u16(t, q + 2) / 20:g}px"
                    q += 4
                if f2 & 0x80:
                    _, q = cstr(t, q)
                col = None
                if f1 & 0x04:
                    col, q = colour(t, q, True)
                if f1 & 0x02:
                    q += 2
                if f2 & 0x20:
                    q += 9
                var, q = cstr(t, q)
                init = cstr(t, q)[0] if f1 & 0x80 else ""
                r.text.append(f"edit text {cid} ({bounds[1] - bounds[0]:g}x{bounds[3] - bounds[2]:g} px"
                              + (f", {font}" if font else "") + (f", {col}" if col else "")
                              + f"): var={var!r} text={init[:200]!r}")
            elif code == 34:
                bid = u16(t, 0)
                off = u16(t, 3)
                if off:
                    q = 3 + off
                    while q < len(t):
                        size = u16(t, q)
                        cond = u16(t, q + 2)
                        body = t[q + 4:(q + size) if size else len(t)]
                        r.actions.append(f"[button {bid}] on condition 0x{cond:04x}")
                        r.actions += disasm(body, pool)
                        if not size:
                            break
                        q += size
            elif code == 39:
                sid, nframes = u16(t, 0), u16(t, 2)
                r.timeline.append(f"sprite {sid}: {nframes} frames")
                walk(t, 4, len(t), r, f"sprite {sid}", pool)
            elif code in (72, 82):
                q = 0
                if code == 82:
                    q = 4
                    nm, q = cstr(t, q)
                    r.abc.append(f"DoABC {nm!r} ({len(t)} bytes)")
                try:
                    cp = abc_pool(t[q:])
                    r.abc.append("doubles: " + ", ".join(cp["doubles"][:300]))
                    r.abc.append("ints: " + ", ".join(map(str, cp["ints"][:300])))
                    r.abc.append("strings: " + ", ".join(repr(s) for s in cp["strings"] if 0 < len(s) < 80)[:20000])
                except (IndexError, struct.error, ValueError, EOFError, OverflowError) as e:
                    # Flex pools can break this parser, so scan strings
                    found = sorted({m.decode("utf-8", "replace") for m in re.findall(rb"[\x20-\x7e]{4,120}", t[q:])})
                    r.abc.append(f"(constant pool not decodable: {e.__class__.__name__}; raw string scan instead)")
                    r.abc.append("strings: " + ", ".join(repr(x) for x in found)[:30000])
            elif code == 87:  # embedded files: XML, sounds, images, even SWFs
                bid, blob = u16(t, 0), t[6:]
                kind = ("swf" if blob[:3] in (b"FWS", b"CWS", b"ZWS") else "png" if blob[:4] == b"\x89PNG" else
                        "jpg" if blob[:3] == b"\xff\xd8\xff" else "mp3" if blob[:3] == b"ID3" or blob[:2] == b"\xff\xfb" else
                        "xml" if blob.lstrip()[:1] == b"<" else "json" if blob.lstrip()[:1] in (b"{", b"[") else "bin")
                r.timeline.append(f"binary data {bid}: {kind}, {len(blob)} bytes {Report.names.get(bid, '')!r}")
                if Report.extract_dir:
                    out = Report.extract_dir / f"binary-{bid}-{slug(Report.names.get(bid, ''))}.{kind}"
                    out.write_bytes(blob)
                    r.extracted.append(str(out))
            elif code in (18, 45):
                fmt = (t[1] >> 4) & 15
                r.sounds.append(f"[{where}] streamed sound: {SOUND_FMT.get(fmt, fmt)}, synced to frames")
                Report.streams.setdefault(where, {"fmt": fmt, "data": bytearray()})
            elif code == 19:  # MP3 blocks start with sample count + seek (4 bytes)
                st = Report.streams.get(where)
                if st is not None and st["fmt"] == 2:
                    st["data"] += t[4:]
        except (IndexError, struct.error, ValueError, EOFError, OverflowError) as e:
            r.timeline.append(f"[{where} f{frame}] could not decode {name}: {e.__class__.__name__}")
    if where == "main":
        r.timeline.insert(0, f"{frame - 1} frames; tags: " + ", ".join(f"{k}x{v}" for k, v in sorted(counts.items())))


def inspect(data: bytes) -> Report:
    sig = data[:3]
    r = Report()
    r.version = data[3]
    if sig == b"FWS":
        body = data
    elif sig == b"CWS":
        body = data[:8] + zlib.decompress(data[8:])
    elif sig == b"ZWS":
        # 4-byte length, 5-byte props, then raw LZMA
        props = data[12:17]
        dict_size = struct.unpack("<I", props[1:5])[0]
        filt = [{"id": lzma.FILTER_LZMA1, "dict_size": dict_size,
                 "lc": props[0] % 9, "lp": (props[0] // 9) % 5, "pb": props[0] // 45}]
        body = data[:8] + lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=filt).decompress(data[17:])
    else:
        sys.exit("not a SWF file (expected FWS, CWS or ZWS)")
    bounds, p = rect(body, 8)
    fps = body[p + 1] + body[p] / 256
    frames = u16(body, p + 2)
    r.header += [f"SWF version {r.version}, {sig.decode()} ({'compressed' if sig != b'FWS' else 'uncompressed'})",
                 f"stage {bounds[1] - bounds[0]:g} x {bounds[3] - bounds[2]:g} px, {fps:g} fps, {frames} frames"]
    r.fps = fps
    walk(body, p + 4, len(body), r)
    for vid, v in sorted(r.videos.items()):
        n = len(v["packets"])
        codec = VIDEO_CODEC.get(v["codec"], f"codec {v['codec']}")
        r.header.append(f"video {vid}: {codec}, {v['w']}x{v['h']}, "
                        f"{n} frames" + (f" (declares {v['frames']})" if v["frames"] != n else "")
                        + (f", {fps:g} fps (the timeline's), {n / fps:.1f} s" if fps else "")
                        + ": --extract saves it as .flv (the original packets) and .mp4")
    return r


def build_digest(r: Report) -> None:
    """Names and numbers each script uses."""
    import re
    block, names, nums = None, [], []

    def flush():
        if block and (names or nums):
            r.digest.append(block)
            if names:
                r.digest.append("    names: " + ", ".join(dict.fromkeys(names)))
            if nums:
                r.digest.append("    numbers: " + ", ".join(dict.fromkeys(nums)))
    for line in r.actions:
        if not line.startswith("    "):
            flush()
            block, names, nums = line, [], []
            continue
        if " push " in line:
            for v in line.split(" push ", 1)[1].split(", "):
                if v.startswith("'") and v.strip("'") not in ("this", "_root", ""):
                    names.append(v.strip("'"))
                elif re.fullmatch(r"-?\d+(\.\d+)?(e-?\d+)?", v):
                    nums.append(v)
        if "; _" in line:
            names.append(line.rsplit("; ", 1)[1])
    flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--section", default="header,timeline,text,palette,sounds,digest,abc",
                    help="comma list of sections to print")
    ap.add_argument("--max-lines", type=int, default=4000)
    ap.add_argument("--extract", help="folder to save the original sounds (MP3), bitmaps (JPEG/PNG) and video (FLV, "
                                         "and MP4 with ffmpeg) into")
    ws_arg = None
    if argv is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _common import enter_ws, pop_ws_arg
        ws_arg = pop_ws_arg()  # absolute first, so cwd- and workspace-relative both work
    a = ap.parse_args(argv)
    if argv is None:
        enter_ws(ws_arg)  # relative paths are workspace paths
    if a.extract:
        inspect(Path(a.file).read_bytes())  # first pass only collects export names
        Report.streams = {}
        Report.extract_dir = Path(a.extract).resolve()  # so printed paths work anywhere
        Report.extract_dir.mkdir(parents=True, exist_ok=True)
    r = inspect(Path(a.file).read_bytes())
    if a.extract:
        save_videos(r, Path(a.file))
        try:
            import PIL  # noqa: F401
        except ImportError:
            n, j = r.bitmaps["lossless"], r.bitmaps["jpeg_alpha"]
            print((f"WARNING: Pillow is not installed for this Python: {n} lossless bitmap(s) SKIPPED" if n else
                   "note: Pillow is not installed, but this file holds 0 lossless bitmaps; nothing skipped")
                  + (f"; {j} JPEG(s) with alpha saved without transparency" if j else "")
                  + (". Rerun with a Python that has Pillow (pip install pillow)." if n or j else "."), file=sys.stderr)
        for where, st in Report.streams.items():  # narration and music streamed along a timeline
            if st["data"]:
                out = Report.extract_dir / f"stream-{slug(where)}.mp3"
                out.write_bytes(bytes(st["data"]))
                r.extracted.append(str(out))
        nested = [x for x in list(r.extracted) if x.endswith(".swf")]
        for sub_swf in nested:  # nested SWFs (preloaders, skins, libraries), one level deep
            base_dir = Report.extract_dir
            Report.extract_dir = None
            Report.names, Report.streams, Report.jpeg_tables = {}, {}, b""
            inspect(Path(sub_swf).read_bytes())  # names first
            Report.extract_dir = Path(sub_swf).with_suffix("")
            Report.extract_dir.mkdir(parents=True, exist_ok=True)
            try:
                sub = inspect(Path(sub_swf).read_bytes())
                save_videos(sub, Path(sub_swf))
                r.extracted += sub.extracted
            except (SystemExit, Exception) as e:  # one damaged nested file shouldn't stop the rest
                print(f"(nested {Path(sub_swf).name}: {e})", file=sys.stderr)
            Report.extract_dir = base_dir
        print(f"== extracted ({len(r.extracted)})")
        for x in r.extracted:
            print(x)
        print("Vector shapes and fonts are not extracted: run the SWF in Ruffle, or export them with JPEXS FFDec.")
        return
    build_digest(r)
    printed = 0
    for sec in a.section.split(","):
        lines = getattr(r, sec.strip(), None)
        if not lines:
            continue
        print(f"== {sec.strip()} ({len(lines)})")
        for line in lines:
            if printed >= a.max_lines:
                print(f"... truncated at --max-lines {a.max_lines}")
                return
            print(line)
            printed += 1


if __name__ == "__main__":
    from _common import use_heavy
    use_heavy("PIL")
    main()
