"""Fetch a student shared link without letting it reach our own network.

A listing's URL is untrusted. Without a guard, a link like https://evil.example (which redirects to an internal
address, or whose DNS points at 127.0.0.1) would make our server fetch things only it can see. Rules:

- https only, no proxies
- the address is checked when the socket is opened (not before), so DNS tricks and rebinding can't slip past it
- every address a name resolves to must be public (no loopback, private, link local or cloud metadata ranges)
- redirects are followed at most 3 times, and each hop goes through the same checks
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
import urllib.request


class BlockedAddress(OSError):
    """The link points at an address that isn't on the public internet."""


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self) -> None:
        port = self.port or 443
        infos = socket.getaddrinfo(self.host, port, type=socket.SOCK_STREAM)
        if not infos:
            raise BlockedAddress(f"{self.host} did not resolve")
        for *_, addr in infos:
            if not ipaddress.ip_address(addr[0].split("%")[0]).is_global:
                raise BlockedAddress(f"{self.host} resolves to a non public address")
        sock = socket.create_connection((infos[0][4][0], port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)   # type: ignore[attr-defined]


class _HTTPSOnly(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req)


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non https address blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPSOnly(), _Redirects())


def fetch(url: str, timeout: float = 5, max_bytes: int = 20000, user_agent: str = "student-agents/1.0") -> tuple[int, str]:
    """Returns (status, first bytes of the body, lower cased). Raises on network errors and blocked addresses."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with opener().open(req, timeout=timeout) as r:
        return r.status, r.read(max_bytes).decode("utf-8", "replace").lower()
