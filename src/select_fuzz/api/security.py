"""Network policy for a deliberately loopback-only control plane."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from select_fuzz.api.problems import ApiProblem, response


def require_loopback_bind(host: str) -> str:
    normalized = host.removesuffix(".").casefold()
    if normalized == "localhost":
        return host
    try:
        if ipaddress.ip_address(normalized).is_loopback:
            return host
    except ValueError:
        pass
    raise ValueError("control plane host must be loopback")


class LoopbackSecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.request_id = request.headers.get("x-request-id") or __import__("uuid").uuid4().hex
        authority = request.headers.get("host", "")
        host = urlsplit("//" + authority).hostname or ""
        try:
            require_loopback_bind(host)
        except ValueError:
            return response(request, ApiProblem(400, "invalid-host", "Invalid Host", "Host must be loopback."))
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            parsed_origin = urlsplit(origin) if origin is not None else None
            same_origin = (
                parsed_origin is not None
                and parsed_origin.scheme in {"http", "https"}
                and parsed_origin.netloc.casefold() == authority.casefold()
            )
            if origin is not None and not same_origin:
                return response(
                    request,
                    ApiProblem(403, "foreign-origin", "Forbidden", "Foreign origins cannot mutate runs."),
                )
            if request.url.path in {"/api/v1/runs", "/api/v1/replays"}:
                content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
                if content_type != "application/json":
                    return response(
                        request,
                        ApiProblem(
                            415, "unsupported-media-type", "Unsupported media type",
                            "This endpoint requires application/json.",
                        ),
                    )
        result = await call_next(request)
        result.headers["X-Request-ID"] = request.state.request_id
        result.headers["X-Frame-Options"] = "DENY"
        result.headers["Referrer-Policy"] = "no-referrer"
        result.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        return result
