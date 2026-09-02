"""A fake Splunk Web, so the browser engine can be tested against real Chromium.

The fake splunkd next door emulates the management API. This emulates the bit a
user actually looks at: a login form, and a dashboard page that behaves the way
Splunk Web behaves in the one respect the engine cares about. It renders nothing
immediately, fires XHRs at splunkd's raw proxy to run real searches, and only
paints a panel once those come back.

That last point is why this exists rather than a static HTML fixture. The whole
value of the browser engine is measuring the gap between "the page loaded" and
"the user can see data", and a page that paints instantly cannot demonstrate
that the engine measures the right one. The search XHRs are proxied to the real
fake splunkd, so a page load here creates real jobs with real sids that the
engine then joins back to real job statistics.

Standard library only, like its neighbour. Run it standalone:

    python tools/fake_web.py --splunkd http://127.0.0.1:8089 --port 8000
"""

from __future__ import annotations

import argparse
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

__all__ = ["FakeSplunkWeb", "main"]

LOGIN_PATH = "/en-GB/account/login"
RAW_PREFIX = "/en-GB/splunkd/__raw"
SESSION_COOKIE = "splunkweb_session"


LOGIN_HTML = """<!doctype html>
<html><head><title>Login | Splunk</title></head>
<body>
  <form method="post" action="{action}">
    <input type="text" name="username" placeholder="Username">
    <input type="password" name="password" placeholder="Password">
    <button type="submit">Sign in</button>
  </form>
</body></html>
"""

# The dashboard. Panels start empty and are only filled once a real search has
# been dispatched, polled to completion and read back, which is what makes the
# time-to-first-panel measurement mean something.
DASHBOARD_HTML = """<!doctype html>
<html><head><title>{dashboard} | Splunk</title>
<style>
  body {{ font-family: sans-serif; margin: 1rem; }}
  .dashboard-panel {{ border: 1px solid #ccc; padding: 8px; margin: 8px 0; min-height: 40px; }}
</style>
</head>
<body>
  <h1>{dashboard}</h1>
  <div id="panels"></div>
  <script>
    const PANELS = {panel_count};
    const SEARCH = {search!r};

    async function runPanel(index) {{
      const params = new URLSearchParams({{
        search: SEARCH + " panel=" + index,
        earliest_time: "0", latest_time: "now",
        exec_mode: "normal", output_mode: "json",
      }});
      const created = await fetch("{raw}/services/search/v2/jobs?output_mode=json", {{
        method: "POST",
        headers: {{ "Content-Type": "application/x-www-form-urlencoded" }},
        body: params.toString(),
      }});
      const {{ sid }} = await created.json();

      // Poll exactly as Splunk Web's data layer does, so the engine sees the
      // same request pattern it would against a real deployment.
      for (let attempt = 0; attempt < 400; attempt++) {{
        const res = await fetch(
          "{raw}/services/search/v2/jobs/" + encodeURIComponent(sid) + "?output_mode=json");
        const body = await res.json();
        const content = (body.entry && body.entry[0] && body.entry[0].content) || {{}};
        if (content.isDone === true || content.isDone === "1" || content.dispatchState === "DONE") {{
          return {{ sid, content }};
        }}
        await new Promise(r => setTimeout(r, 25));
      }}
      throw new Error("the job never finished");
    }}

    (async () => {{
      const host = document.getElementById("panels");
      for (let i = 0; i < PANELS; i++) {{
        try {{
          const {{ sid, content }} = await runPanel(i);
          const el = document.createElement("div");
          el.className = "dashboard-panel";
          el.setAttribute("data-test", "visualization");
          el.textContent = "panel " + i + " sid=" + sid +
            " events=" + (content.eventCount || 0);
          host.appendChild(el);
        }} catch (err) {{
          const el = document.createElement("div");
          el.className = "dashboard-panel";
          el.textContent = "panel " + i + " failed: " + err.message;
          host.appendChild(el);
        }}
      }}
      document.body.setAttribute("data-panels-complete", "true");
    }})();
    {extra_script}
  </script>
</body></html>
"""


@dataclass
class FakeWebConfig:
    host: str = "127.0.0.1"
    port: int = 0
    splunkd: str = "http://127.0.0.1:8089"
    username: str = "loadtest"
    password: str = "changeme"
    panels: int = 3
    search: str = "search index=main | stats count"
    # Emitted verbatim into the page. Used by tests to make the page throw, so
    # the engine's JavaScript-error accounting can be exercised.
    extra_script: str = ""
    log_requests: bool = False
    # Splunkd credentials the proxy presents. The browser has a web session,
    # not a management token, exactly as in a real deployment.
    splunkd_token: str = "fake-web-token"


@dataclass
class _WebStats:
    logins: int = 0
    dashboards_served: int = 0
    proxied: int = 0
    paths: Dict[str, int] = field(default_factory=dict)


class FakeSplunkWeb:
    """A running fake Splunk Web. Starts on construction, like its neighbour."""

    def __init__(self, config: Optional[FakeWebConfig] = None, **overrides) -> None:
        self.config = config or FakeWebConfig()
        for key, value in overrides.items():
            if not hasattr(self.config, key):
                raise TypeError(f"unknown option: {key!r}")
            setattr(self.config, key, value)

        self.stats = _WebStats()
        self.port: int = 0
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self.start()

    def start(self) -> "FakeSplunkWeb":
        if self._httpd is not None:
            return self
        httpd = _WebServer((self.config.host, self.config.port), _WebHandler)
        httpd.fake = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="fake-splunk-web",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = None

    def __enter__(self) -> "FakeSplunkWeb":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def base_url(self) -> str:
        return f"http://{self.config.host}:{self.port}"

    # ------------------------------------------------------------------

    def proxy(self, method: str, path: str, query: str, body: bytes) -> Tuple[int, bytes, str]:
        """Forward a raw-proxy call to the fake splunkd.

        Splunk Web does exactly this: the browser has a web session and the
        server holds the splunkd credential, so the page never sees a token.
        """
        url = self.config.splunkd.rstrip("/") + path
        if query:
            url += "?" + query
        request = urllib.request.Request(url, data=body or None, method=method)
        request.add_header("Authorization", f"Bearer {self.config.splunkd_token}")
        if body:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read(), response.headers.get(
                    "Content-Type", "application/json"
                )
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), "application/json"
        except Exception as exc:  # noqa: BLE001 - a proxy failure is a 502, not a crash
            return 502, json.dumps({"messages": [{"type": "ERROR", "text": str(exc)}]}).encode(), (
                "application/json"
            )


class _WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256
    fake: FakeSplunkWeb


class _WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Splunkd/10.4.0"
    sys_version = ""
    # See the note in fake_splunk.py: without this, every response pays a
    # delayed-ACK stall because the headers and body are separate writes.
    disable_nagle_algorithm = True

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        fake = getattr(self.server, "fake", None)
        if fake is not None and fake.config.log_requests:
            print("fake-web - " + (fmt % args))

    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        fake: FakeSplunkWeb = self.server.fake  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        fake.stats.paths[f"{method} {path}"] = fake.stats.paths.get(f"{method} {path}", 0) + 1

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        if path.startswith(RAW_PREFIX):
            fake.stats.proxied += 1
            status, payload, content_type = fake.proxy(
                method, path[len(RAW_PREFIX):], parsed.query, body
            )
            self._send_bytes(status, payload, content_type)
            return

        if path == LOGIN_PATH:
            if method == "GET":
                self._send_html(200, LOGIN_HTML.format(action=LOGIN_PATH))
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
            username = (form.get("username") or [""])[0]
            password = (form.get("password") or [""])[0]
            if username == fake.config.username and password == fake.config.password:
                fake.stats.logins += 1
                self.send_response(303)
                self.send_header("Location", "/en-GB/app/search/home")
                self.send_header("Set-Cookie", f"{SESSION_COOKIE}=ok; Path=/; HttpOnly")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # Wrong credentials land back on the login page, which is how the
            # engine detects a rejected login.
            self._send_html(401, LOGIN_HTML.format(action=LOGIN_PATH))
            return

        if path.startswith("/en-GB/app/"):
            if SESSION_COOKIE not in (self.headers.get("Cookie") or ""):
                self.send_response(303)
                self.send_header("Location", LOGIN_PATH)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            parts = [p for p in path.split("/") if p]
            dashboard = parts[-1] if parts else "home"
            fake.stats.dashboards_served += 1
            self._send_html(
                200,
                DASHBOARD_HTML.format(
                    dashboard=dashboard,
                    panel_count=fake.config.panels,
                    search=fake.config.search,
                    raw=RAW_PREFIX,
                    extra_script=fake.config.extra_script,
                ),
            )
            return

        self._send_html(404, "<!doctype html><html><body>not found</body></html>", 404)

    # ------------------------------------------------------------------

    def _send_html(self, status: int, html: str, _unused: int = 0) -> None:
        self._send_bytes(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            self.close_connection = True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="A fake Splunk Web for browser-engine tests")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--splunkd", default="http://127.0.0.1:8089")
    parser.add_argument("--username", default="loadtest")
    parser.add_argument("--password", default="changeme")
    parser.add_argument("--panels", type=int, default=3)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    web = FakeSplunkWeb(
        host=args.host,
        port=args.port,
        splunkd=args.splunkd,
        username=args.username,
        password=args.password,
        panels=args.panels,
        log_requests=args.verbose,
    )
    print(f"fake Splunk Web listening on {web.base_url}", flush=True)
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        print("shutting down", flush=True)
    finally:
        web.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
