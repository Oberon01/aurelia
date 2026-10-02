"""The post-deploy check reaches Aurelia the way his devices do: through the proxy."""
import asyncio
import threading
import http.server

import pytest

from app.settings import VerifyConfig
from app.verify import Verifier, VerifyError


def server(handler_codes):
    """A stand-in for the orchestrator (/ready) and for Traefik (/health): codes per path."""
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            code = handler_codes.get(self.path, 404)
            self.send_response(code)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_unreachable_through_the_proxy_fails_the_release():
    orch, orch_url = server({"/ready": 200})
    proxy, proxy_url = server({"/health": 504})                  # Traefik can't reach the new orchestrator
    cfg = VerifyConfig(proxy_url=proxy_url, proxy_timeout_seconds=0.5, smoke_test=False)
    with pytest.raises(VerifyError, match="through the proxy \\(HTTP 504\\)"):
        asyncio.run(Verifier(orch_url).run(cfg, set()))
    orch.shutdown(); proxy.shutdown()


def test_reachable_through_the_proxy_passes():
    orch, orch_url = server({"/ready": 200})
    proxy, proxy_url = server({"/health": 200})
    cfg = VerifyConfig(proxy_url=proxy_url, smoke_test=False)
    assert "reachable through the proxy" in asyncio.run(Verifier(orch_url).run(cfg, set()))
    assert "reachable through the proxy" not in asyncio.run(Verifier(orch_url).run(VerifyConfig(proxy_url="", smoke_test=False), set()))
    orch.shutdown(); proxy.shutdown()
