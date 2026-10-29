#!/usr/bin/env python3
"""Serve a folder for testing, with HTTP Range support. Browsers stream audio and video with Range
requests, and the stock http.server ignores them, so media fails to load at random.

    python3 serve.py PORT DIR      (PORT 0 picks a free port; the URL is printed)

Other scripts can call `start(DIR)` to serve a folder in a thread. It returns the server and its URL.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class _Server(ThreadingHTTPServer):
    request_queue_size = 128  # browsers open many at once, 5 dropped some (flaky journeys)
    daemon_threads = True


class RangeHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or os.path.isdir(path) or not os.path.isfile(path):
            return super().send_head()
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
        size = os.path.getsize(path)
        if not m or (not m.group(1) and not m.group(2)):
            return super().send_head()
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else size - 1
        else:  # suffix range, the last N bytes
            start, end = max(0, size - int(m.group(2))), size - 1
        if start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        end = min(end, size - 1)
        f = open(path, "rb")
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        try:
            while remaining > 0:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser stopped reading, normal for media
        finally:
            self._remaining = None

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def start(root, port: int = 0, host: str = "127.0.0.1"):
    """Serve ROOT from a background thread and return (server, url). Call server.shutdown() to stop it."""
    if not os.path.isdir(root):
        raise FileNotFoundError(f"no folder {root}")
    httpd = _Server((host, port), partial(RangeHandler, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://{host}:{httpd.server_address[1]}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", type=int, help="port to listen on (0: any free port)")
    ap.add_argument("root", nargs="?", default=".", help="folder to serve (default: the current folder)")
    ap.add_argument("--host", default="127.0.0.1", help="address to bind (default: this machine only)")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.root):
        ap.error(f"no folder {a.root}")
    httpd = _Server((a.host, a.port), partial(RangeHandler, directory=a.root))
    print(f"serving {os.path.abspath(a.root)} at http://{a.host}:{httpd.server_address[1]}", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
