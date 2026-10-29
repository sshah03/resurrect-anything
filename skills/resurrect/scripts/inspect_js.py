#!/usr/bin/env python3
"""Pull what the user saw out of an archived web-app client (a minified JS/CSS bundle): every UI string,
message tables, embedded CSS and the colour palette. The bundle is only read, never run.

    inspect_js.py BUNDLE... [--section strings,messages,css,colours] [--grep REGEX] [--min-words 1]

  strings    human-readable string literals (labels, menus, toasts, empty states, help text)
  messages   i18n message tables (Closure MSG_*, gettext-style {id: "text"}) with their keys
  css        CSS rules embedded in the bundle or in a .css file (selector { declarations })
  colours    every colour value with its count (the palette), most used first

Give it several bundles, say the same client captured on different dates, and --diff prints the strings
that are only in the later file. Find bundles with `wayback.py inventory <domain>` (they show up as program
files) and fetch them with `wayback.py fetch <url> <ts> --original`.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import enter_ws, locate_ws, pop_ws_arg  # noqa: E402

STR_RX = re.compile(r'"((?:[^"\\\n]|\\.){2,400})"|\'((?:[^\'\\\n]|\\.){2,400})\'|`([^`$\\]{2,400})`')
MSG_RX = re.compile(r'\b(MSG_[A-Z0-9_]+)\s*=\s*(?:goog\.getMsg\()?\s*["\']((?:[^"\'\\]|\\.){1,400})["\']')
KV_RX = re.compile(r'["\']?([A-Za-z_][\w.-]{2,80})["\']?\s*:\s*["\']((?:[^"\'\\\n]|\\.){3,400})["\']')
CSS_RX = re.compile(r'([.#@:]?[A-Za-z_][\w\-.#:>+~\[\]="\' ,*()]{0,160})\{([^{}]{3,800})\}')
COLOUR_RX = re.compile(r'#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\([0-9., %]+\)|hsla?\([0-9., %deg]+\)')
CODEY = re.compile(r'^[\w$.\-/:@#?=&%+]*$|^\s*$|function|return |=>|\bvar\b|\bthis\.|[{};]\s*$|^[a-z]+(-[a-z]+)+$|'
                   r'^[A-Z_0-9]+$|^https?://|\.(js|css|png|svg|gif|jpg|woff2?)\b|^[a-z]+[A-Z]\w*$|<\w')


def unescape(s: str) -> str:
    try:
        return json.loads('"' + s.replace('"', '\\"').replace("\\'", "'") + '"')
    except ValueError:
        return s


def human(s: str, min_words: int) -> bool:
    """Text a person would read, not identifiers, selectors, paths or code."""
    if CODEY.search(s) and " " not in s.strip():
        return False
    letters = sum(c.isalpha() for c in s)
    if letters < 2 or letters / max(1, len(s)) < 0.55:
        return False
    if len(s.split()) < min_words:
        return False
    if re.search(r"[{}<>;=]|\\n\s*\w+\(|\bnull\b|\bundefined\b|\btypeof\b", s):
        return False
    if re.search(r"%[sd]\b|\b(?:expected|invalid|unrecognized|failed to|storing|cached|fetched|experiment|"
                 r"defaulting|whenAll|preparing to)\b", s, re.I) or re.search(r"//|\.png|\.svg|\.js\b", s):
        return False  # dev log lines and asset paths
    return bool(re.search(r"[A-Za-z]{2}", s))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--section", default="strings,messages,colours")
    ap.add_argument("--grep", help="regex: only matching strings/messages/rules")
    ap.add_argument("--min-words", type=int, default=1, help="strings with fewer words are dropped (2 removes most "
                                                             "labels; 1 keeps 'Snooze', 'Done')")
    ap.add_argument("--max", type=int, default=3000)
    ap.add_argument("--diff", action="store_true", help="strings in the last file that the first lacks, and vice versa")
    ap.add_argument("--json", action="store_true")
    ws_arg = None
    if argv is None:
        ws_arg = pop_ws_arg()
        if locate_ws(ws_arg) is not None:
            # other scripts' first bare arg is a subcommand, here a bundle.
            # resolve it before we move into the workspace
            for i, x in enumerate(sys.argv[1:], 1):
                if not x.startswith("-"):
                    if not Path(x).is_absolute() and Path(x).exists():
                        sys.argv[i] = str(Path(x).resolve())
                    break
    a = ap.parse_args(argv)
    if argv is None:
        enter_ws(ws_arg)  # so dossier/... paths work from any folder
    sections = set(a.section.split(","))
    grep = re.compile(a.grep, re.I) if a.grep else None
    per_file = []
    for f in a.files:
        text = Path(f).read_text(errors="replace", encoding="utf-8")
        strings = []
        seen = set()
        for m in STR_RX.finditer(text):
            s = unescape(next(g for g in m.groups() if g is not None))
            s = re.sub(r"\s+", " ", s).strip()
            if s and s not in seen and human(s, a.min_words) and (not grep or grep.search(s)):
                seen.add(s)
                strings.append(s)
        msgs = [(k, unescape(v)) for k, v in MSG_RX.findall(text) if not grep or grep.search(v)]
        if not msgs:  # plain-object tables {"snooze_until": "Snooze until..."}
            msgs = [(k, unescape(v)) for k, v in KV_RX.findall(text)
                    if re.search(r"(?i)msg|label|title|text|string|i18n|_desc|tooltip|hint", k) and human(unescape(v), 1)
                    and (not grep or grep.search(v))][:a.max]
        css = []
        if "css" in sections:
            for sel, body in CSS_RX.findall(text):
                if ":" in body and re.search(r"[a-z-]+\s*:", body) and not re.search(r"function|return|=>", body):
                    rule = f"{sel.strip()} {{ {body.strip()} }}"
                    if not grep or grep.search(rule):
                        css.append(rule)
        colours = collections.Counter(c.lower() for c in COLOUR_RX.findall(text))
        per_file.append({"file": f, "bytes": len(text), "strings": strings, "messages": msgs, "css": css,
                         "colours": colours})
    if a.diff and len(per_file) >= 2:
        first, last = set(per_file[0]["strings"]), set(per_file[-1]["strings"])
        out = {"only_in_last": sorted(last - first)[: a.max], "only_in_first": sorted(first - last)[: a.max]}
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return
    for r in per_file:
        if a.json:
            print(json.dumps({"file": r["file"], "strings": r["strings"][: a.max], "messages": r["messages"][: a.max],
                              "css": r["css"][: a.max], "colours": r["colours"].most_common(60)}, ensure_ascii=False))
            continue
        print(f"== {r['file']} ({r['bytes']:,} bytes)")
        if "strings" in sections:
            print(f"-- strings ({len(r['strings'])})")
            for s in r["strings"][: a.max]:
                print("  " + s)
        if "messages" in sections and r["messages"]:
            print(f"-- messages ({len(r['messages'])})")
            for k, v in r["messages"][: a.max]:
                print(f"  {k}: {v}")
        if "css" in sections:
            print(f"-- css rules ({len(r['css'])})")
            for c in r["css"][: a.max]:
                print("  " + c[:300])
        if "colours" in sections:
            print("-- colours: " + ", ".join(f"{c} x{n}" for c, n in r["colours"].most_common(40)))
    print("Strings are the original's own copy: cite the bundle (URL + capture date) as `seen` evidence and use them "
          "verbatim. Code is never run.", file=sys.stderr)


if __name__ == "__main__":
    main()
