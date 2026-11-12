"""Egress probe transport: proxy scheme and credential encoding at the requests boundary."""

from __future__ import annotations

import http.server
import socket
import threading
import traceback
import unittest
from unittest.mock import patch

from mioa_core.net import NetworkError, parse_proxy, probe_egress
from mioa_os import LocalHttpRequests
from mioa_os_api import HttpRequestDenied

IPINFO = {
    "ip": "203.0.113.7",
    "country": "ZZ",
    "city": "Test",
    "timezone": "UTC",
    "loc": "1.5,2.5",
}


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def egress(proxy, timeout=8.0):
    with patch("requests.get", return_value=_Response(IPINFO)) as get:
        result = probe_egress(proxy, timeout=timeout, http=LocalHttpRequests())
    return result, get.call_args, get.call_count


class ProbeEgressProxyTransportTests(unittest.TestCase):
    def test_https_proxy_with_reserved_credentials_keeps_scheme(self):
        proxy = {
            "server": "https://proxy.example:8443",
            "username": "alice@corp.example",
            "password": "p@ss:w/rd?#%&=+ ",
        }
        result, call, count = egress(proxy, timeout=3.5)
        self.assertEqual(count, 1)
        self.assertEqual(result["source"], "ipinfo.io")
        self.assertEqual(call.args, ("https://ipinfo.io/json",))
        url = (
            "https://alice%40corp.example:"
            "p%40ss%3Aw%2Frd%3F%23%25%26%3D%2B%20@proxy.example:8443"
        )
        self.assertEqual(call.kwargs["proxies"], {"http": url, "https": url})
        self.assertEqual(call.kwargs["timeout"], 3.5)

    def test_missing_proxy_host_does_not_expose_credentials(self):
        with self.assertRaisesRegex(NetworkError, "proxy URL has no host") as caught:
            parse_proxy("http://private-user:private-password@")
        self.assertNotIn("private-user", str(caught.exception))
        self.assertNotIn("private-password", str(caught.exception))

    def test_invalid_proxy_authority_does_not_expose_credentials(self):
        for url in (
            "http://private-user:private-password@proxy.example:private-port",
            "http://private-user:private-password@proxy.example:65536",
            "http://private-user:private-password@[::1",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(NetworkError, "invalid proxy URL") as caught:
                    parse_proxy(url)
                self.assertIsNone(caught.exception.__context__)
                rendered = "".join(traceback.format_exception(caught.exception))
                for secret in ("private-user", "private-password", "private-port"):
                    self.assertNotIn(secret, rendered)

    def test_parse_ipv6_proxy_preserves_authority_and_credentials(self):
        for url, expected in (
            ("http://[::1]", {"server": "http://[::1]:8080"}),
            ("socks5h://[2001:db8::1]:9999", {"server": "socks5h://[2001:db8::1]:9999"}),
            ("https://user:p%40ss@[::1]:8443", {
                "server": "https://[::1]:8443", "username": "user", "password": "p@ss",
            }),
        ):
            with self.subTest(url=url):
                self.assertEqual(parse_proxy(url), expected)

    def test_parse_proxy_preserves_explicit_zero_port(self):
        for url, expected in (
            ("http://proxy.example:0", "http://proxy.example:0"),
            ("https://[::1]:0", "https://[::1]:0"),
            ("http://proxy.example", "http://proxy.example:8080"),
        ):
            with self.subTest(url=url):
                self.assertEqual(parse_proxy(url)["server"], expected)

    def test_ipv6_proxy_accepts_real_http_request(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, format, *args):
                pass

        class IPv6Server(http.server.HTTPServer):
            address_family = socket.AF_INET6

        try:
            server = IPv6Server(("::1", 0), Handler)
        except OSError as exc:
            self.skipTest(f"IPv6 loopback unavailable: {exc}")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            proxy = parse_proxy(f"http://[::1]:{server.server_port}")["server"]
            result = LocalHttpRequests().get_json(
                "http://example.test/", proxies={"http": proxy}, timeout=2)
            self.assertEqual(result, {"ok": True})
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_real_proxy_failure_reports_safe_error(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_CONNECT(self):
                self.send_error(407)

            def do_GET(self):
                self.send_error(407)

            def log_message(self, format, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            proxy = {
                "server": f"http://127.0.0.1:{server.server_port}",
                "username": "private-user",
                "password": "private-password",
            }
            with self.assertRaises(NetworkError) as caught:
                probe_egress(proxy, timeout=2, http=LocalHttpRequests())
            rendered = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn("private-user", rendered)
            self.assertNotIn("private-password", rendered)
            self.assertIn("ipinfo.io", rendered)
            self.assertIn("ip-api.com", rendered)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_parse_proxy_credentials_are_encoded_exactly_once(self):
        proxy = parse_proxy("https://us%65r%40corp:p%40ss%2Fword@proxy.example:8443")
        self.assertEqual(proxy, {
            "server": "https://proxy.example:8443",
            "username": "user@corp",
            "password": "p@ss/word",
        })
        _, call, _ = egress(proxy)
        url = "https://user%40corp:p%40ss%2Fword@proxy.example:8443"
        self.assertEqual(call.kwargs["proxies"], {"http": url, "https": url})

    def test_http_proxy_credentials_keep_http_scheme(self):
        proxy = {
            "server": "http://proxy.local:3128",
            "username": "user name",
            "password": "p@ss:word",
        }
        _, call, _ = egress(proxy)
        url = "http://user%20name:p%40ss%3Aword@proxy.local:3128"
        self.assertEqual(call.kwargs["proxies"], {"http": url, "https": url})

    def test_unauthenticated_and_direct_connections_are_unchanged(self):
        cases = [
            (
                {"server": "http://proxy.local:8080"},
                {"http": "http://proxy.local:8080", "https": "http://proxy.local:8080"},
            ),
            (
                {"server": "socks5://socks.local:1080"},
                {"http": "socks5://socks.local:1080", "https": "socks5://socks.local:1080"},
            ),
            (None, None),
        ]
        for proxy, expected in cases:
            with self.subTest(proxy=proxy):
                _, call, count = egress(proxy)
                self.assertEqual(count, 1)
                self.assertEqual(call.kwargs["proxies"], expected)
                self.assertEqual(call.kwargs["timeout"], 8.0)


    def test_proxy_failure_does_not_disclose_credentials(self):
        class FailingHttp:
            def get_json(self, url, *, proxies, timeout):
                raise OSError(f"proxy {proxies['https']} rejected user@corp p@ss/word")

        for proxy in (
            {"server": "http://proxy.local:8080", "username": "user@corp", "password": "p@ss/word"},
            {"server": "http://user%40corp:p%40ss%2Fword@proxy.local:8080"},
        ):
            with self.subTest(proxy=proxy):
                with self.assertRaises(NetworkError) as caught:
                    probe_egress(proxy, http=FailingHttp())
                rendered = "".join(traceback.format_exception(caught.exception))
                for secret in ("user@corp", "p@ss/word", "user%40corp", "p%40ss%2Fword"):
                    self.assertNotIn(secret, rendered)
                self.assertIn("ipinfo.io", rendered)
                self.assertIn("ip-api.com", rendered)

    def test_proxy_policy_denial_does_not_disclose_credentials_or_retry(self):
        class Deny:
            def __init__(self):
                self.urls = []

            def get_json(self, url, *, proxies, timeout):
                self.urls.append(url)
                raise HttpRequestDenied(f"denied {proxies['https']}")

        http = Deny()
        proxy = {"server": "http://proxy.local:8080", "username": "user@corp", "password": "p@ss/word"}
        with self.assertRaises(HttpRequestDenied) as caught:
            probe_egress(proxy, http=http)
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("user%40corp", rendered)
        self.assertNotIn("p%40ss%2Fword", rendered)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(http.urls, ["https://ipinfo.io/json"])

    def test_fallback_proxy_denial_drops_provider_context(self):
        class DenyFallback:
            def __init__(self):
                self.urls = []

            def get_json(self, url, *, proxies, timeout):
                self.urls.append(url)
                if len(self.urls) == 1:
                    raise OSError("first endpoint unavailable")
                raise HttpRequestDenied(f"denied {proxies['https']}")

        http = DenyFallback()
        proxy = {"server": "http://proxy.local:8080", "username": "user", "password": "secret"}
        with self.assertRaises(HttpRequestDenied) as caught:
            probe_egress(proxy, http=http)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(len(http.urls), 2)

    def test_direct_failure_retains_provider_diagnostics(self):
        class FailingHttp:
            def get_json(self, url, *, proxies, timeout):
                raise OSError("connection refused")

        with self.assertRaisesRegex(NetworkError, "connection refused"):
            probe_egress(None, http=FailingHttp())

    def test_host_policy_denial_is_not_retried_against_another_provider(self):
        class Deny:
            def __init__(self):
                self.urls = []

            def get_json(self, url, *, proxies, timeout):
                self.urls.append(url)
                raise HttpRequestDenied('network denied')

        http = Deny()
        with self.assertRaises(HttpRequestDenied):
            probe_egress(None, http=http)
        self.assertEqual(http.urls, ["https://ipinfo.io/json"])

if __name__ == "__main__":
    unittest.main()
