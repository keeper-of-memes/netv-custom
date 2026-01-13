"""Shared utilities."""

from __future__ import annotations

from typing import Any

import urllib.error
import urllib.parse
import urllib.request


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that only allows http/https schemes."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in ("http", "https"):
            raise urllib.error.URLError(f"Unsafe redirect scheme: {parsed.scheme}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_urlopen(url: str, timeout: int = 30) -> Any:
    """Open URL with safe redirect handling."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise urllib.error.URLError(f"Unsafe URL scheme: {parsed.scheme}")
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    return opener.open(url, timeout=timeout)
