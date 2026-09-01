"""mitmproxy addon: serve md5(url)-named mock fixtures for Arch Linux hosts.

This makes the agent's own `curl https://archlinux.org/news/?page=1` hit the
SAME mock data the bundled script reads via ARCH_CHECK_MOCK_DIR, so with-skill
and baseline face an identical environment (mock-env-design.md, paradigm 1).

Usage:
    mitmdump -s mock_proxy_addon.py --set mock_dir=path/to/evals/mock/e1/http \
             --listen-port 8888

The agent (pi) is run with:
    HTTPS_PROXY=http://localhost:8888
    SSL_CERT_FILE=~/.mitmproxy/mitmproxy-ca-cert.pem   # trust the MITM CA
    NO_PROXY=<LLM-provider hosts>                        # don't MITM the model API

For every request whose host is archlinux.org or bbs.archlinux.org, the addon
computes md5(pretty_url) and looks for <hash>.html (then <hash>.json) under
mock_dir. If found, it returns the fixture; otherwise 404 (the mock world has
no other pages -- mirrors what the script sees when a fixture is absent).
All other hosts are forwarded untouched.
"""
import hashlib
import os

from mitmproxy import ctx, http

MOCK_HOSTS = {"archlinux.org", "bbs.archlinux.org"}


class ArchMockAddon:
    def load(self, loader):
        loader.add_option(
            "mock_dir", str, "",
            "Directory with md5(url)-named mock fixtures (.html/.json)",
        )

    def request(self, flow: http.HTTPFlow) -> None:
        host = flow.request.pretty_host
        if host not in MOCK_HOSTS:
            return  # forward to real upstream (model API, etc.)
        mock_dir = ctx.options.mock_dir
        if not mock_dir:
            flow.response = http.Response.make(
                500, b"mock_proxy_addon: mock_dir not set", {"Content-Type": "text/plain"}
            )
            return
        url = flow.request.pretty_url
        for ext, ct in ((".html", "text/html; charset=utf-8"),
                        (".json", "application/json; charset=utf-8")):
            fp = os.path.join(mock_dir, hashlib.md5(url.encode()).hexdigest() + ext)
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    content = f.read()
                flow.response = http.Response.make(
                    200, content, {"Content-Type": ct}
                )
                ctx.log.info(f"mock HIT  {url} -> {os.path.basename(fp)}")
                return
        # No fixture: return a neutral 404 (NOT a "mock" marker -- a curious
        # agent that curls an unmapped page should see an ordinary Not Found,
        # not a banner announcing the test harness). This mirrors the real web:\        
        # many pages genuinely 404.
        flow.response = http.Response.make(
            404, b"404 Not Found\n", {"Content-Type": "text/plain; charset=utf-8"},
        )
        ctx.log.info(f"mock MISS {url} -> 404")


addons = [ArchMockAddon()]
