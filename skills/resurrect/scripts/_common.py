"""Shared helpers for the resurrect skill scripts.

Stdlib only. Mostly a polite HTTP client, with:
  - a descriptive User-Agent (archives ask for this)
  - per-host rate limiting that works across separate processes
  - exponential backoff with jitter on 429 / 5xx / connection resets
  - an on-disk cache so the same archive URL is never fetched twice
"""
from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "2.5.5"
USER_AGENT = os.environ.get(
    "RESURRECT_UA",
    f"resurrect-skill/{VERSION} (digital preservation research; +https://github.com/sshah03/resurrect-anything)",
)

# Per-host spacing (s), under 80% of CDX's documented ~30 req/min.
# Override with RESURRECT_RATE_<HOST>=seconds.
HOST_INTERVALS = {
    "web.archive.org/cdx": 2.5,
    "web.archive.org": 1.0,
    "archive.org": 1.0,
    "hn.algolia.com": 0.4,
    "api.pullpush.io": 2.0,
    "www.reddit.com": 2.0,
    "old.reddit.com": 2.0,
    "archive.softwareheritage.org": 3.0,
    "raw.githubusercontent.com": 0.2,
    "api.github.com": 6.5,  # unauthenticated search allows 10 requests a minute
    "world.openfoodfacts.org": 6.0,  # OFF asks for <= 10 searches/min
    "api.nal.usda.gov": 2.0,
}
DEFAULT_INTERVAL = 1.0
MAX_WAIT = float(os.environ.get("RESURRECT_MAX_WAIT", "60"))
# idle timeout, not total, so slow downloads still finish
TIMEOUT = float(os.environ.get("RESURRECT_TIMEOUT", "60"))
_THROTTLE_LOCK = threading.Lock()  # parallel fetches still start one at a time
_TL = threading.local()

# Windows pipes default to ANSI and crash on other chars
# (a Japanese title, a curly quote)
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def jsonl_lines(text: str) -> list[str]:
    """Split JSONL text on "\n" only, dropping a Windows "\r".
    str.splitlines() also breaks at U+2028, U+2029, U+0085, which ensure_ascii=False JSON keeps in strings."""
    lines = [x[:-1] if x.endswith("\r") else x for x in text.split("\n")]
    if lines and lines[-1] == "":
        lines.pop()  # trailing newline, not an empty record
    return lines


def atomic_write(path, data) -> None:
    """Write text (UTF-8) or bytes via temp file, fsync and rename, so nobody sees half a file."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_CACHE_DIRS: dict = {}


def _writable(d: Path) -> bool:
    """Can we create and write in `d`? Claude Code's sandbox only allows the project."""
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / f".probe-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def cache_dir() -> Path:
    # per-user, so rate limits hold across workspaces and subagents.
    # Sandboxed? Fall back to .resurrect-cache/ in the project root.
    # $RESURRECT_CACHE wins. Decided once, before any chdir.
    env = os.environ.get("RESURRECT_CACHE")
    default = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "resurrect"
    key = (env, str(default))
    if key not in _CACHE_DIRS:
        d = Path(env or default).expanduser()
        if not env and not _writable(d):
            local = Path.cwd().resolve() / ".resurrect-cache"
            log(f"note: cannot write to {d} (a sandbox?), so the cache is {local}; to share one cache between "
                f"projects, allow writing to {d} (in Claude Code: sandbox.filesystem.allowWrite)")
            d = local
            d.mkdir(parents=True, exist_ok=True)
            if not (d / ".gitignore").exists():
                (d / ".gitignore").write_text("*\n", encoding="utf-8")  # keep downloads and the environment out of git
        d.mkdir(parents=True, exist_ok=True)
        _CACHE_DIRS[key] = d
    return _CACHE_DIRS[key]


def heavy_python() -> str | None:
    """Python of the environment `enhance.py setup` builds (Pillow, numpy, Playwright, yt-dlp), or None."""
    p = cache_dir() / "heavy" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(p) if p.exists() else None


def _run_child(argv: list) -> None:
    """Run argv as a child and exit with its status (exec for Windows)."""
    import subprocess
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(subprocess.run(argv).returncode)


def reexec(python: str, script: str | None = None) -> None:
    """Rerun this script under `python` with the same args and its exit status. os.execv, except on Windows,
    where execv returns to the shell at once (output after the prompt, status lost), so run a child."""
    argv = [python, os.path.abspath(script or sys.argv[0]), *sys.argv[1:]]
    if os.name == "nt":
        _run_child(argv)
    os.execv(python, argv)


def python_cmd() -> str:
    """This Python's short name (python3, python3.12, python) if PATH finds it, else its full path."""
    import shutil
    name = Path(sys.executable).stem
    found = shutil.which(name)
    try:
        same = bool(found) and Path(found).resolve() == Path(sys.executable).resolve()
    except OSError:
        same = False
    return name if same else sys.executable


def shell_join(args, nt: bool | None = None) -> str:
    """Quote args into one command line for this system's shell (cmd.exe rules on Windows)."""
    if os.name == "nt" if nt is None else nt:
        import subprocess
        return subprocess.list2cmdline([str(x) for x in args])
    import shlex
    return " ".join(shlex.quote(str(x)) for x in args)


def use_heavy(*modules: str) -> list[str]:
    """Rerun under the resurrect env's Python if this one lacks `modules` (Homebrew's, Debian's refuse pip).
    Call before changing folder or args. Returns the modules still missing."""
    import importlib.util
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    hp = heavy_python()
    if missing and hp and Path(sys.prefix).resolve() != Path(hp).parent.parent.resolve():
        reexec(hp)
    return missing


# add yt-dlp, playwright to PATH (after the user's own)
_hb = cache_dir() / "heavy" / ("Scripts" if os.name == "nt" else "bin")
if _hb.is_dir() and str(_hb) not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(_hb)


def _rate_key(url: str) -> str:
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower()
    if host == "web.archive.org" and p.path.startswith("/cdx"):
        return "web.archive.org/cdx"
    return host


def _interval(key: str) -> float:
    env = os.environ.get("RESURRECT_RATE_" + key.upper().replace(".", "_").replace("/", "_"))
    if env:
        return float(env)
    if key in HOST_INTERVALS:
        return HOST_INTERVALS[key]
    # match parent domain (e.g. ia800.us.archive.org -> archive.org)
    for k, v in HOST_INTERVALS.items():
        if "/" not in k and key.endswith("." + k):
            return v
    return DEFAULT_INTERVAL


def _throttle(key: str) -> None:
    stamp = cache_dir() / ".rate" / (hashlib.sha1(key.encode()).hexdigest()[:16])
    stamp.parent.mkdir(parents=True, exist_ok=True)
    wait = _interval(key)
    with _THROTTLE_LOCK:
        try:
            last = float(stamp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last = 0.0
        delta = time.time() - last
        if delta < wait:
            time.sleep(wait - delta)
        stamp.write_text(str(time.time()), encoding="utf-8")


def last_truncated() -> bool:
    """Did this thread's last fetch() return a truncated body? (Kept, never cached.)"""
    return getattr(_TL, "truncated", False)


def last_status(reset: bool = False) -> int:
    """This thread's last fetch() status, incl. archived errors kept by keep_errors=True.
    Use this, not the thread-shared fetch.last_status. reset=True sets it to 200."""
    if reset:
        _TL.status = 200
    return getattr(_TL, "status", 200)


class Truncated(Exception):
    """Body ended before its declared length (dropped connection or truncated capture). `partial` has the rest."""

    def __init__(self, partial: bytes):
        super().__init__(f"response cut short after {len(partial)} bytes")
        self.partial = partial


def _read_body(resp, max_bytes, url):
    """Read in chunks. TIMEOUT applies per read, so stalls get cut but slow steady downloads finish."""
    chunks, n = [], 0
    while True:
        want = 65536 if max_bytes is None else min(65536, max_bytes + 1 - n)
        if want <= 0:
            raise RuntimeError(f"response larger than {max_bytes} bytes: {url}")
        try:
            c = resp.read(want)
        except http.client.IncompleteRead as e:
            raise Truncated(b"".join(chunks) + (e.partial or b"")) from e
        if not c:
            cl = resp.headers.get("Content-Length") if resp.headers else None
            if cl and cl.isdigit() and n < int(cl):  # urllib quietly returns a short body even then
                raise Truncated(b"".join(chunks))
            return b"".join(chunks)
        chunks.append(c)
        n += len(c)


class OutOfTime(RuntimeError):
    """The caller's time limit hit before this request could start or retry. Never retried."""


_HARD_STOP: list = [None]  # (time.time() value, exception class) while hard_stop() is set


def hard_stop(at: float | None, exc=None) -> None:
    """Step time limit for Windows (no signal.alarm). From `at`, fetch() raises `exc` instead of starting
    a request or retry, and back-offs and silent servers wait no longer than `at`. at=None lifts it.
    Downloads already in progress aren't cut."""
    _HARD_STOP[0] = None if at is None else (at, exc)


def _check_hard_stop(wait: float = 0.0) -> None:
    """If the hard stop is within `wait` seconds, sleep until it and raise."""
    hs = _HARD_STOP[0]
    if hs and time.time() + wait >= hs[0]:
        time.sleep(max(0.0, hs[0] - time.time()))
        raise hs[1]


BLOCKED_EXT = {".exe", ".msi", ".dmg", ".pkg", ".app", ".apk", ".ipa", ".xap", ".swf", ".dcr",
               ".unity3d", ".jar", ".class", ".dll", ".so", ".bin", ".iso", ".img", ".cab",
               ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".deb", ".rpm", ".asar"}
BLOCKED_MAGIC = (b"MZ", b"FWS", b"CWS", b"ZWS", b"\x7fELF", b"\xcf\xfa\xed\xfe",
                 b"\xca\xfe\xba\xbe", b"Rar!", b"7z\xbc\xaf")


def refuse_binary(url: str, body: bytes | None = None, original: bool = False) -> None:
    """Block program/archive files unless original=True (law 2: they go to dossier/originals/, sandbox only)."""
    if original:
        return
    path = urllib.parse.urlparse(url).path.lower()
    ext = "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    if ext in BLOCKED_EXT or (body is not None and body[:8].startswith(BLOCKED_MAGIC)):
        raise PermissionError(
            f"not fetching program/archive file {url} by default. Surviving program files and archives "
            "(SWF, press-kit zips, disk images, APKs) are original material: fetch them with --original "
            "(saved under dossier/originals/, inspected or unzipped as data, never executed; SKILL.md law 2).")


def fetch(url: str, *, use_cache: bool = True, max_retries: int = 4,
          headers: dict | None = None, max_bytes: int | None = None,
          want_url: bool = False, keep_errors: bool = False, deadline: float | None = None):
    """GET a URL politely. Returns body bytes. Raises RuntimeError on final failure.

    404s raise FileNotFoundError ("not archived"). deadline is a time.time() value. No attempt or retry
    starts after it (OutOfTime), but a download in progress isn't cut. TIMEOUT s with no bytes means a retry.
    """
    key = hashlib.sha1(url.encode()).hexdigest()
    cpath = cache_dir() / "http" / key[:2] / key
    upath = cpath.with_suffix(".url")
    _TL.status, _TL.truncated = 200, False
    _check_hard_stop()
    if use_cache and cpath.exists():
        body = cpath.read_bytes()
        if want_url:
            return body, (upath.read_text(encoding="utf-8") if upath.exists() else url)
        return body

    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    rk = _rate_key(url)
    attempt = 0
    while True:
        if deadline is not None and time.time() >= deadline:
            raise OutOfTime(f"time limit reached before fetching {url}")
        _check_hard_stop()
        _throttle(rk)
        req = urllib.request.Request(url, headers=hdrs)
        hs = _HARD_STOP[0]
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT if not hs else
                                        max(1.0, min(TIMEOUT, hs[0] - time.time()))) as resp:
                body = _read_body(resp, max_bytes, url)
                if body[:2] == b"\x1f\x8b" and (resp.headers.get("Content-Encoding", "").lower() == "gzip" or
                                                 "html" in resp.headers.get("Content-Type", "")):
                    try:  # archives sometimes lie about gzip, or truncate it
                        body = gzip.decompress(body)
                    except (OSError, EOFError):
                        pass
                final_url = resp.geturl()
            break
        except urllib.error.HTTPError as e:
            if keep_errors and e.code not in (429, 500, 502, 503, 504):  # an archived error page is evidence
                body = e.read()
                fetch.last_status = _TL.status = e.code
                return (body, e.geturl()) if want_url else body
            if e.code == 404:
                raise FileNotFoundError(url) from e
            retriable = e.code in (429, 500, 502, 503, 504, 520, 522, 524)
            ra = e.headers.get("Retry-After") if e.headers else None
            if e.code == 429 and ra and ra.isdigit() and int(ra) > MAX_WAIT:
                raise RuntimeError(f"HTTP 429 for {url}: the server says retry in {int(ra) // 60} min "
                                   "(quota spent). Use another source or an API key.") from e
            # only three tries on 429, then switch source
            if not retriable or attempt >= (min(max_retries, 3) if e.code == 429 else max_retries):
                raise RuntimeError(f"HTTP {e.code} for {url}") from e
            base = 30.0 if e.code == 429 else 5.0
        except Truncated as e:
            # a capture truncated in the archive stays truncated, so after
            # two tries keep the partial body, uncached
            if attempt >= min(max_retries, 2):
                if not e.partial:
                    raise RuntimeError(f"network error for {url}: {e}") from e
                log(f"note: {url} came back cut short ({len(e.partial)} bytes, {attempt + 1} tries): kept as it is; "
                    "the archived copy itself may be truncated")
                _TL.truncated = True
                return (e.partial, url) if want_url else e.partial
            base = 5.0
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError,
                http.client.HTTPException) as e:
            if attempt >= max_retries:
                raise RuntimeError(f"network error for {url}: {e}") from e
            base = 5.0
        attempt += 1
        wait = min(MAX_WAIT, base * (2 ** (attempt - 1))) * (0.75 + random.random() / 2)
        if deadline is not None and time.time() + wait >= deadline:
            raise OutOfTime(f"gave up on {url} after {attempt} attempt(s): the time limit comes before the next retry")
        _check_hard_stop(wait)
        log(f"[retry {attempt}/{max_retries}] backing off {wait:.0f}s for {rk}")
        time.sleep(wait)

    if use_cache:
        cpath.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(cpath, body)
        atomic_write(upath, final_url)
    return (body, final_url) if want_url else body


def fetch_json(url: str, **kw):
    body = fetch(url, **kw)
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if text[:1] == "<":  # HTML here means an outage page, not "no results"
        title = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        what = (title.group(1).strip() if title else text[:80]).replace("\n", " ")
        raise RuntimeError(f"the service answered with an HTML page instead of data ({what!r}): it is probably down "
                           "or rate-limiting. This is NOT an empty result: retry later or switch source family.")
    return json.loads(text)


def emit(rows, fmt: str = "jsonl", keep=()) -> None:
    """Print rows (dicts) as jsonl (default), json, csv or md. md cuts cells at 120 chars except `keep` columns."""
    rows = list(rows)
    if fmt == "json":
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif fmt == "csv":  # lists joined with "; ", dicts as JSON
        import csv
        cols = list(dict.fromkeys(k for r in rows for k in r))
        w = csv.DictWriter(sys.stdout, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: "; ".join(map(str, v)) if isinstance(v, list) else
                        json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v for k, v in r.items()})
    elif fmt == "md":
        if not rows:
            print("_no results_")
            return
        if any(len(str(v)) > 120 for r in rows for k, v in r.items() if k not in keep):
            log("note: markdown output cuts long fields (transcripts, article text) to 120 characters; "
                "use the default jsonl output to read them in full")
        cols = list(dict.fromkeys(k for r in rows for k in r))  # all columns, not just the first row's
        print("| " + " | ".join(cols) + " |")
        print("|" + "---|" * len(cols))
        for r in rows:
            cells = [str(r.get(c, "")).replace("|", "\\|").replace("\n", " ")[:None if c in keep else 120] for c in cols]
            print("| " + " | ".join(cells) + " |")
    else:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))


def locate_ws(arg: str | None = None) -> Path | None:
    """Find the workspace: --ws, $RESURRECT_WS, a state.json at or above cwd,
    resurrections/.active (set by `ledger.py init/use`), or the only folder in resurrections/."""
    for cand in (arg, os.environ.get("RESURRECT_WS")):
        if cand:
            p = Path(cand).expanduser()
            return p if (p / "state.json").exists() else None
    here = Path.cwd()
    for d in (here, *list(here.parents)[:4]):  # also works from inside app/ or dossier/
        if (d / "state.json").exists():
            return d
    root = Path("resurrections")
    active = root / ".active"
    if active.exists():
        p = root / active.read_text(encoding="utf-8").strip()
        if (p / "state.json").exists():
            return p
    dirs = [d for d in root.glob("*") if (d / "state.json").exists()] if root.exists() else []
    return dirs[0] if len(dirs) == 1 else None


def hoist_globals(names=("--format", "--max-chars")) -> None:
    """Let global flags appear after the subcommand too (argparse wants them before it)."""
    args, front, i = sys.argv[1:], [], 0
    while i < len(args):
        a = args[i]
        key = a.split("=", 1)[0]
        if key in names:
            if "=" in a:
                front.append(a)
                del args[i]
            elif i + 1 < len(args):
                front += args[i:i + 2]
                del args[i:i + 2]
            else:
                i += 1
        else:
            i += 1
    sys.argv[1:] = front + args


LAUNCH_DIR = Path.cwd()  # where the command was typed (scripts chdir later)


def _exists(x: str) -> bool:
    """Path(x).exists() that survives inline JSON or over-long text."""
    try:
        return Path(x).exists()
    except (OSError, ValueError):
        return False


def out_note(path: str | Path) -> Path:
    """Absolute output path. Warns when a relative one lands in the workspace, not where it was typed."""
    p = Path(path)
    full = p.resolve()
    if not p.is_absolute() and Path.cwd().resolve() != LAUNCH_DIR.resolve():
        log(f"note: relative output path {path} is under the workspace ({full}), not under {LAUNCH_DIR}; "
            "pass an absolute path to write elsewhere")
    return full


def pop_ws_arg() -> str | None:
    """Take `--ws PATH` (or --ws=PATH) out of sys.argv wherever it appears, before argparse runs."""
    args, ws, i = sys.argv[1:], None, 0
    while i < len(args):
        if args[i] == "--ws" and i + 1 < len(args):
            ws = args[i + 1]
            del args[i:i + 2]
        elif args[i].startswith("--ws="):
            ws = args[i][5:]
            del args[i]
        else:
            i += 1
    sys.argv[1:] = args
    hoist_globals()
    if locate_ws(ws) is not None:
        # keep shell-resolved paths (globs, existing files) valid after chdir
        sub_seen = False
        for i, x in enumerate(sys.argv[1:], 1):
            if not x.startswith("-") and not sub_seen:
                sub_seen = True
                if "/" not in x and "." not in x:
                    continue  # subcommand name, even if a same-named file exists
            if not x.startswith("-") and _exists(x) and not Path(x).is_absolute():
                sys.argv[i] = str(Path(x).resolve())  # a file or folder, bare names included
            elif x.startswith(("./", "../")):
                sys.argv[i] = str(Path(x).resolve())  # relative to here on purpose, even if not created yet
            elif x.startswith("resurrections/") and not x.startswith("-"):
                # a not-yet-existing resurrections/<slug>/app/x path from the project root
                # would otherwise nest inside the workspace
                root_rel = Path(x)
                if (Path.cwd() / root_rel.parts[0] / root_rel.parts[1]).is_dir() if len(root_rel.parts) > 2 else False:
                    sys.argv[i] = str((Path.cwd() / root_rel).resolve())
    return ws


def enter_ws(arg: str | None = None) -> Path | None:
    """chdir to the workspace root so relative paths work from anywhere (agent shells forget `cd`)."""
    ws = locate_ws(arg)
    if ws is not None:
        ws = ws.resolve()
        if Path.cwd().resolve() != ws:
            # same fix-up as in pop_ws_arg
            sub_seen = False
            for i, x in enumerate(sys.argv[1:], 1):
                if not x.startswith("-") and not sub_seen:
                    sub_seen = True
                    if "/" not in x and "." not in x:
                        continue
                if not x.startswith("-") and (_exists(x) or x.startswith(("./", "../"))) and not Path(x).is_absolute():
                    sys.argv[i] = str(Path(x).resolve())
            os.chdir(ws)
        for x in sys.argv[1:]:  # output outside the workspace is usually a slip
            if x.startswith("/") and not _exists(x) and not Path(x).resolve().is_relative_to(ws) and \
                    Path(x).resolve().is_relative_to(ws.parent.parent):
                log(f"warning: {x} is outside the workspace ({ws}); is that where you meant?")
        log_activity(ws)
    return ws


def log_activity(ws: Path) -> None:
    """Runaway guard: log each script call, warn on the third identical one among recent calls.
    Repeating a step that already answered burns tokens. `ledger.py status` shows calls since the
    app or evidence last changed."""
    try:
        f = Path(ws) / ".activity.jsonl"
        cmd = [Path(sys.argv[0]).name, *sys.argv[1:]]
        if cmd[0] == "ledger.py" and (cmd[1:2] in (["status"], ["validate"]) or cmd[2:3] == ["list"]
                                      or cmd[1:] == ["versions"]):
            return  # read-only checks, fine to run often
        h = hashlib.sha1(json.dumps(cmd).encode())
        for x in sys.argv[1:]:  # rerunning on a changed file isn't a repeat
            try:
                arg_file = Path(x)
                if arg_file.is_file() and arg_file.stat().st_size < 5_000_000:
                    h.update(arg_file.read_bytes())
            except (OSError, ValueError):
                pass
        key = h.hexdigest()[:12]
        recent = []
        if f.exists():
            recent = [json.loads(x) for x in jsonl_lines(f.read_text(encoding="utf-8"))[-40:] if x.strip()]
        same = sum(1 for r in recent if r.get("key") == key)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "cmd": cmd[:12], "key": key}) + "\n")
        if same >= 2:
            log(f"runaway guard: this exact command has run {same + 1} times in the last {len(recent) + 1} script "
                "calls. If its answer will not change, stop repeating it: change the input, try another source "
                "or tool, or record the gap (`unknown` with `searched`) and move on.")
    except Exception:
        pass  # the guard must never break a run


def need_pil():
    """Exit with a clear message if an image step runs without Pillow."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("this step needs Pillow: run `enhance.py setup` once (it installs Pillow, numpy, Playwright and "
                 "yt-dlp in the resurrect environment); the text-only steps keep working without it")


def slugify(text: str) -> str:
    text = text.replace("'", "").replace("\u2019", "")  # Bob's -> bobs, not bob-s
    out = "".join(c.lower() if c.isalnum() else "-" for c in text)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:60].strip("-") or "item"


def markdown_html(text: str) -> str:
    """Tiny Markdown renderer for museum recipe cards and READMEs. Escapes text first."""
    import html as _h
    def inline(t):
        t = _h.escape(t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", t)
        t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<a href='\2' rel='noopener noreferrer'>\1</a>", t)
        return t
    out, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        l = lines[i]
        if not l.strip():
            i += 1
            continue
        h = re.match(r"^(#{1,6})\s+(.*)", l)
        if h:
            n = min(len(h.group(1)) + 1, 6)
            out.append(f"<h{n}>{inline(h.group(2))}</h{n}>")
            i += 1
        elif re.fullmatch(r"\s*([-*_])\s*(\1\s*){2,}", l):
            out.append("<hr>")
            i += 1
        elif l.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            head, body = rows[0], rows[1:]
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</tbody></table>")
        elif re.match(r"^\s*([-*+]|\d+[.)])\s+", l):
            ordered = bool(re.match(r"^\s*\d+[.)]", l))
            items = []
            while i < len(lines) and re.match(r"^\s*([-*+]|\d+[.)])\s+|^\s{2,}\S", lines[i]):
                m = re.match(r"^\s*([-*+]|\d+[.)])\s+(.*)", lines[i])
                if m:
                    items.append(m.group(2))
                elif items:
                    items[-1] += " " + lines[i].strip()
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6}\s|\s*\||\s*([-*+]|\d+[.)])\s)", lines[i]):
                para.append(lines[i].strip())
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
    return "\n".join(out)


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    import html as _html
    import re
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()
