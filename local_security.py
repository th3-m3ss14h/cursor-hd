#!/usr/bin/env python3
"""Small, dependency-free request checks for cursor-hd's loopback-only API."""
from __future__ import annotations

from urllib.parse import urlsplit

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
_ALLOWED_PORTS = {None, 5057}
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _host_parts(value: str):
    """Parse a Host header or URL authority into a normalized host/port pair."""
    try:
        parsed = urlsplit("//" + value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return None, None
    if (hostname not in _ALLOWED_HOSTS or port not in _ALLOWED_PORTS
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment):
        return None, None
    return hostname, port


def local_api_request_error(host: str, origin: str | None, method: str,
                            mimetype: str | None) -> str | None:
    """Return a rejection reason for unsafe requests, else ``None``.

    The server binds to 127.0.0.1:5057. API requests must use a loopback Host;
    an Origin, when present, must be the same local origin. Mutations without an
    Origin (for local command-line clients) must still send JSON, preventing
    text/plain HTML forms from exploiting ``get_json(force=True)`` endpoints.
    """
    host_name, host_port = _host_parts(host or "")
    if host_name is None:
        return "cursor-hd API only accepts loopback Host headers"

    if origin:
        try:
            parsed_origin = urlsplit(origin)
            origin_name = (parsed_origin.hostname or "").lower().rstrip(".")
            origin_port = parsed_origin.port
        except (TypeError, ValueError):
            return "invalid Origin header"
        if (parsed_origin.scheme.lower() != "http"
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.path not in ("", "/")
                or parsed_origin.query
                or parsed_origin.fragment):
            return "cursor-hd API only accepts a local HTTP Origin"
        if origin_name not in _ALLOWED_HOSTS or origin_port not in _ALLOWED_PORTS:
            return "cross-origin API requests are not allowed"
        if origin_name != host_name or origin_port != host_port:
            return "Origin must match the local API Host"

    if method.upper() in _MUTATING_METHODS:
        normalized_mimetype = (mimetype or "").lower().strip()
        if normalized_mimetype != "application/json" and not normalized_mimetype.endswith("+json"):
            return "API mutations require Content-Type: application/json"

    return None
