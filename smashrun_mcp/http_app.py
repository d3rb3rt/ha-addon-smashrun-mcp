#!/usr/bin/env python3
"""HTTP-Betrieb des Smashrun-MCP-Servers (Streamable HTTP), z. B. als Home-Assistant-App.

Absicherung (Claude erlaubt für eigene Connectoren keinen festen Zugangsschlüssel im Header,
deshalb liegt die Geheimhaltung im Pfad):
  * Der MCP-Endpunkt liegt unter  /<SECRET>/mcp . Jeder andere Pfad antwortet mit 404,
    auch bei falschem Geheimnis (es gibt keinen Hinweis, dass hier ein MCP-Server läuft).
  * Optional: SMASHRUN_ALLOWED_CIDRS (kommagetrennt) lässt nur diese Absender-IPs zu,
    z. B. Anthropics Ausgangsbereich 160.79.104.0/21. Die IP wird aus Cf-Connecting-Ip bzw.
    X-Forwarded-For gelesen, die Option ist deshalb nur hinter einem eigenen Tunnel/Proxy
    sinnvoll und der Port darf dann nicht direkt aus dem Internet erreichbar sein.
  * Zugriffslog ist aus, damit das Geheimnis nicht in Logs landet.

Umgebungsvariablen:
  SMASHRUN_SECRET_PATH   Pflicht, mind. 24 Zeichen aus A-Z a-z 0-9 _ -
  SMASHRUN_ACCESS_TOKEN  Smashrun-Token (siehe server.py)
  SMASHRUN_ALLOW_WRITE   0 = rein lesend (Standard in server.py: an)
  SMASHRUN_ALLOWED_CIDRS optional, kommagetrennt
  HOST (0.0.0.0), PORT (8099)
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import re
import sys

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

import server

SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{24,}$")


def parse_cidrs(raw: str) -> list:
    nets = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def client_ip(scope) -> str:
    headers = {k.lower(): v for k, v in scope.get("headers", [])}
    for name in (b"cf-connecting-ip", b"x-forwarded-for"):
        if name in headers:
            return headers[name].decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else ""


async def _reply(send, status: int, body: bytes = b"") -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class Guard:
    """ASGI-Wrapper: prüft Geheimpfad und IP, entfernt das Präfix und reicht an die MCP-App weiter."""

    def __init__(self, app, secret: str, cidrs: list):
        self.app = app
        self.secret = secret.encode()
        self.cidrs = cidrs

    def _ip_allowed(self, scope) -> bool:
        if not self.cidrs:
            return True
        try:
            ip = ipaddress.ip_address(client_ip(scope))
        except ValueError:
            return False
        return any(ip in net for net in self.cidrs)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return
        path = scope.get("path", "")
        if path == "/healthz":
            return await _reply(send, 200, b"ok")
        segment, _, rest = path[1:].partition("/")
        if not hmac.compare_digest(segment.encode(), self.secret) or not self._ip_allowed(scope):
            return await _reply(send, 404, b"Not Found")
        inner = dict(scope)
        inner["path"] = "/" + rest
        inner["raw_path"] = inner["path"].encode()
        await self.app(inner, receive, send)


def build_app(secret: str, cidrs: list):
    mcp_app = server.mcp.streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return Guard(mcp_app, secret, cidrs)


def main() -> None:
    secret = os.environ.get("SMASHRUN_SECRET_PATH", "")
    if not SECRET_RE.match(secret):
        sys.exit(
            "SMASHRUN_SECRET_PATH fehlt oder ist zu schwach (mind. 24 Zeichen aus A-Z a-z 0-9 _ -). "
            "Erzeugen mit: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    try:
        cidrs = parse_cidrs(os.environ.get("SMASHRUN_ALLOWED_CIDRS", ""))
    except ValueError as e:
        sys.exit(f"SMASHRUN_ALLOWED_CIDRS ungültig: {e}")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8099"))
    print(
        f"smashrun-mcp (HTTP) auf {host}:{port}, Schreibzugriff: {'an' if server.ALLOW_WRITE else 'aus'}, "
        f"IP-Filter: {', '.join(map(str, cidrs)) or 'aus'}",
        file=sys.stderr,
        flush=True,
    )
    uvicorn.run(build_app(secret, cidrs), host=host, port=port, access_log=False, log_level="info")


if __name__ == "__main__":
    main()
