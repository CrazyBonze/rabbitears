#!/usr/bin/env python3
"""Local caching proxy for the Alpine CDN.

The VM's e1000+NAT path to the public CDN is slow (~50 KB/s), but the host
reaches the CDN at several MB/s. This proxy sits on the host's libvirt-NAT
address (192.168.122.1) and serves Alpine packages to the VM over the local
network: the host does the (fast) external download once and caches it, so
every VM install / `apk add` is served locally.

Point the VM's apk repository at:
    http://192.168.122.1:8081/alpine/v3.24/main
    http://192.168.122.1:8081/alpine/v3.24/community

Run (as root, so it can bind the NAT address and write the cache):
    python3 vm/apk-proxy.py
"""
import http.server
import os
import sys
import urllib.request

CDN = "http://dl-cdn.alpinelinux.org"
CACHE = os.environ.get("RB_APK_CACHE", "/var/lib/rabbitears-vm/mirror")
HOST = os.environ.get("RB_APK_PROXY_HOST", "192.168.122.1")
PORT = int(os.environ.get("RB_APK_PROXY_PORT", "8081"))


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def _serve(self, path: str) -> None:
        # path looks like /alpine/v3.24/main/x86_64/APKINDEX.tar.gz
        # Guard against path traversal / scheme injection: only serve /alpine/...
        if not path.startswith("/alpine/") or ".." in path:
            self.send_error(400, "bad path")
            return
        rel = path.lstrip("/")
        cache_path = os.path.join(CACHE, rel)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        if not os.path.exists(cache_path) or os.path.getsize(cache_path) == 0:
            # Literal http:// scheme (path is validated to be /alpine/... above).
            url = "http://dl-cdn.alpinelinux.org" + path
            self.log_message("fetch %s", url)
            tmp = cache_path + ".part"
            try:
                with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                os.replace(tmp, cache_path)
            except Exception as e:  # noqa: BLE001
                self.log_message("fetch failed %s: %s", url, e)
                self.send_error(502, f"upstream fetch failed: {e}")
                return
        with open(cache_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(self.path)

    def do_HEAD(self) -> None:  # noqa: N802
        self._serve(self.path)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write("[apk-proxy] " + (format % args) + "\n")


def main() -> None:
    os.makedirs(CACHE, exist_ok=True)
    http.server.ThreadingHTTPServer((HOST, PORT), ProxyHandler).serve_forever()


if __name__ == "__main__":
    main()
