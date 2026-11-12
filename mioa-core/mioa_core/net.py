"""Network identity: proxy transport, exit-geo probe, in-browser egress check."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from mioa_os_api import HttpRequestDenied, HttpRequests

PROXY_SCHEMES = {"http": 8080, "https": 8443, "socks5": 1080, "socks5h": 1080}


class NetworkError(RuntimeError):
    pass


def parse_proxy(url: str | None) -> dict[str, str] | None:
    """Playwright proxy dict for a proxy URL, or None for a direct connection."""
    if not url or url.strip().upper() in {"", "DIRECT", "NONE"}:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        parts = None
    if parts is None:
        raise NetworkError("invalid proxy URL")
    if parts.scheme not in PROXY_SCHEMES:
        raise NetworkError(
            f"unsupported proxy scheme {parts.scheme!r}; use one of {', '.join(sorted(PROXY_SCHEMES))}"
        )
    if not parts.hostname:
        raise NetworkError("proxy URL has no host")
    try:
        port = parts.port
    except ValueError:
        port = -1
    if port == -1:
        raise NetworkError("invalid proxy URL")
    if port is None:
        port = PROXY_SCHEMES[parts.scheme]
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    proxy: dict[str, str] = {"server": f"{parts.scheme}://{host}:{port}"}
    if parts.username:
        if parts.scheme.startswith("socks"):
            raise NetworkError(
                "Firefox rejects SOCKS credentials; use an HTTP proxy or authorise this machine's "
                "address on the SOCKS server"
            )
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    return proxy


def _authenticated_proxy_url(server: str, username: str, password: str) -> str:
    """Proxy URL with the configured scheme and percent-encoded credentials."""
    parts = urlsplit(server)
    scheme = parts.scheme or "http"
    authority = parts.netloc.rpartition("@")[2]
    return (
        f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}@{authority}"
    )


def probe_egress(proxy: dict[str, str] | None, timeout: float = 8.0, *, http: HttpRequests) -> dict[str, Any]:
    """Exit IP and geography through explicitly Host-authorized HTTP."""

    server = (proxy or {}).get("server")
    if server:
        proxies = {"http": server, "https": server}
        if proxy and proxy.get("username"):
            authenticated = _authenticated_proxy_url(
                server, proxy["username"], proxy.get("password", "")
            )
            proxies = {"http": authenticated, "https": authenticated}
    else:
        proxies = None

    attempts: list[str] = []
    denied = False
    try:
        data = http.get_json("https://ipinfo.io/json", proxies=proxies, timeout=timeout)
        loc = (data.get("loc") or "").split(",")
        return {
            "ip": data.get("ip"),
            "country": data.get("country"),
            "city": data.get("city") or data.get("region"),
            "timezone": data.get("timezone"),
            "latitude": float(loc[0]) if len(loc) == 2 else None,
            "longitude": float(loc[1]) if len(loc) == 2 else None,
            "source": "ipinfo.io",
        }
    except HttpRequestDenied:
        if not server:
            raise
        denied = True
    except Exception as exc:  # noqa: BLE001 - any failure falls through
        attempts.append(f"ipinfo.io: {'request failed' if server else str(exc)}")
    if denied:
        raise HttpRequestDenied("HTTP request denied by Host")

    try:
        data = http.get_json(
            "http://ip-api.com/json/?fields=status,message,query,country,countryCode,city,timezone,lat,lon",
            proxies=proxies,
            timeout=timeout,
        )
        if data.get("status") != "success":
            raise NetworkError(str(data.get("message") or data))
        return {
            "ip": data.get("query"),
            "country": data.get("countryCode"),
            "city": data.get("city"),
            "timezone": data.get("timezone"),
            "latitude": data.get("lat"),
            "longitude": data.get("lon"),
            "source": "ip-api.com",
        }
    except HttpRequestDenied:
        if not server:
            raise
        denied = True
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"ip-api.com: {'request failed' if server else str(exc)}")
    if denied:
        raise HttpRequestDenied("HTTP request denied by Host")

    raise NetworkError(
        "cannot determine the exit identity"
        + (" through configured proxy" if server else " on this host")
        + "; "
        + " | ".join(attempts)
    )


EGRESS_ECHO = "https://api.ipify.org?format=json"
EGRESS_GEO = "https://ipinfo.io/json"


async def browser_geography(page: Any, timeout: float = 25.0) -> dict[str, Any]:
    """Where the browser itself exits from, read inside the page it will use."""
    try:
        await page.goto(EGRESS_GEO, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        data = json.loads(await page.inner_text("body"))
        if data.get("ip"):
            loc = (data.get("loc") or "").split(",")
            return {
                "ip": data.get("ip"),
                "country": data.get("country"),
                "city": data.get("city") or data.get("region"),
                "timezone": data.get("timezone"),
                "latitude": float(loc[0]) if len(loc) == 2 else None,
                "longitude": float(loc[1]) if len(loc) == 2 else None,
                "source": "ipinfo.io (in page)",
            }
    except Exception:  # noqa: BLE001 - fall through to the IP-only echo
        pass
    await page.goto(EGRESS_ECHO, wait_until="domcontentloaded", timeout=int(timeout * 1000))
    body = await page.inner_text("body")
    try:
        return {"ip": json.loads(body)["ip"], "source": "api.ipify.org (in page)"}
    except Exception as exc:  # noqa: BLE001
        raise NetworkError(f"unreadable egress echo: {body[:120]!r}") from exc
