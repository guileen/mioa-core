"""Egress probe transport: proxy scheme and credential encoding at the requests boundary."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from mioa_core.net import parse_proxy, probe_egress

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
        result = probe_egress(proxy, timeout=timeout)
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


if __name__ == "__main__":
    unittest.main()
