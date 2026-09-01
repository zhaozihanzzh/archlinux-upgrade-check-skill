"""Lifecycle + env injection for the mitmproxy mock proxy (Phase 1 of
mock-env-design.md).

Wraps `mitmdump` + scripts/mock_proxy_addon.py so the eval harness can start a
transparent HTTPS mock for archlinux.org / bbs.archlinux.org with one call, and
get back the env vars to inject into the pi subprocess (HTTPS_PROXY,
SSL_CERT_FILE, NO_PROXY for the LLM provider so its API is not MITM'd).

Used by skill_eval.py when --mock is passed. Also runnable standalone for
debugging:

    python3 scripts/mock_proxy.py start --mock-dir evals/mock/e1/http
    # ... run pi with the printed env vars ...
    python3 scripts/mock_proxy.py stop

Portability: expects `mitmdump` on PATH or at scripts/.venv/bin/mitmdump.
If absent, prints install instructions:
    python3 -m venv scripts/.venv && scripts/.venv/bin/pip install mitmproxy
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_MITMDUMP = os.path.join(SKILL_DIR, 'scripts', '.venv', 'bin', 'mitmdump')
ADDON = os.path.join(SKILL_DIR, 'scripts', 'mock_proxy_addon.py')
CONF_DIR = os.path.join(SKILL_DIR, '.mitm-conf')  # work-dir (bwrap-safe)
CA_CERT = os.path.join(CONF_DIR, 'mitmproxy-ca-cert.pem')
DEFAULT_PORT = 8888
MOCK_HOSTS = ("archlinux.org", "bbs.archlinux.org")


def find_mitmdump():
    """Return a usable mitmdump path, or None with install instructions."""
    if os.path.exists(VENV_MITMDUMP):
        return VENV_MITMDUMP
    return shutil.which('mitmdump')


def _llm_provider_hosts():
    """Read ~/.pi/agent/models.json and collect provider base-URL hosts so we
    can exclude them from MITM (don't intercept LLM API calls)."""
    hosts = set()
    models_json = os.path.expanduser('~/.pi/agent/models.json')
    try:
        with open(models_json) as f:
            d = json.load(f)
    except Exception:
        return []
    for cfg in (d.get('providers') or {}).values():
        base = cfg.get('baseUrl', '')
        if base:
            # strip scheme
            h = base.split('://', 1)[-1].split('/', 1)[0]
            if h:
                hosts.add(h)
    return sorted(hosts)


class MockProxy:
    def __init__(self, mock_dir, port=DEFAULT_PORT):
        self.mock_dir = os.path.abspath(mock_dir)
        self.port = port
        self.proc = None
        self.mitmdump = find_mitmdump()

    def start(self):
        if not self.mitmdump:
            raise RuntimeError(
                "mitmdump not found. Install with:\n"
                f"  python3 -m venv {os.path.join(SKILL_DIR,'scripts','.venv')} && "
                f"{os.path.join(SKILL_DIR,'scripts','.venv','bin','pip')} install mitmproxy"
            )
        os.makedirs(CONF_DIR, exist_ok=True)
        # confdir under the work dir so bwrap doesn't block CA generation.
        cmd = [
            self.mitmdump, '-s', ADDON,
            '--set', f'mock_dir={self.mock_dir}',
            '--set', f'confdir={CONF_DIR}',
            '--set', f'listen_port={self.port}',
            '--set', 'ssl_insecure=true',
            '--set', 'flow_detail=0',
            '-q',
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
        # Wait for the CA cert + port readiness (mitmproxy generates the CA
        # on first run, then listens).
        for _ in range(60):
            if os.path.exists(CA_CERT) and self._port_open():
                return
            if self.proc.poll() is not None:
                raise RuntimeError("mitmdump exited early; check confdir/permissions")
            time.sleep(0.5)
        raise RuntimeError("mitmdump did not become ready in 30s")

    def _port_open(self):
        import socket
        s = socket.socket()
        s.settimeout(0.5)
        try:
            s.connect(('127.0.0.1', self.port))
            s.close()
            return True
        except OSError:
            return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            # mitmproxy 12 may ignore SIGTERM; send SIGKILL to the whole
            # detached process group so no orphan holds ports/resources.
            try:
                os.killpg(os.getpgid(self.proc.pid), 9)
            except Exception:
                self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
        self.proc = None

    def env(self):
        """Env vars to inject into the pi subprocess so its curl hits the mock
        while the LLM API bypasses it."""
        no_proxy = ','.join(
            ['localhost', '127.0.0.1'] + _llm_provider_hosts()
        )
        return {
            'HTTPS_PROXY': f'http://localhost:{self.port}',
            'HTTP_PROXY': f'http://localhost:{self.port}',
            'https_proxy': f'http://localhost:{self.port}',
            'http_proxy': f'http://localhost:{self.port}',
            'SSL_CERT_FILE': CA_CERT,
            'REQUESTS_CA_BUNDLE': CA_CERT,
            # Set both cases: Node/undici honors lowercase no_proxy, curl and
            # requests honor uppercase NO_PROXY. Without this the LLM API call
            # itself gets MITM'd by the proxy and stalls.
            'NO_PROXY': no_proxy,
            'no_proxy': no_proxy,
        }


def main():
    """Tiny CLI for manual start/stop/status debugging."""
    if len(sys.argv) < 2 or sys.argv[1] not in ('start', 'stop', 'env'):
        print("usage: mock_proxy.py {start|stop|env} [--mock-dir DIR] [--port N]")
        sys.exit(2)
    action = sys.argv[1]
    mock_dir = 'evals/mock/e1/http'
    port = DEFAULT_PORT
    args = sys.argv[2:]
    for i, a in enumerate(args):
        if a == '--mock-dir' and i + 1 < len(args):
            mock_dir = args[i + 1]
        if a == '--port' and i + 1 < len(args):
            port = int(args[i + 1])
    mp = MockProxy(mock_dir, port=port)
    if action == 'start':
        mp.start()
        print(f"# mock proxy up on :{port}")
        print(f"# CA: {CA_CERT}")
        print(f"# NO_PROXY: {','.join(['localhost','127.0.0.1']+_llm_provider_hosts())}")
        # leave running; caller stops with `stop`
    elif action == 'stop':
        mp.stop()
        print("stopped")
    elif action == 'env':
        for k, v in mp.env().items():
            print(f'export {k}="{v}"')


if __name__ == '__main__':
    main()
