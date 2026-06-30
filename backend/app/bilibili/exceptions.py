"""Typed B站 (Bilibili) API exceptions.

B站 endpoints respond with a JSON envelope:
    {"code": <int>, "message": <str>, "data": <any>}

`code == 0` is success; everything else is a business error. Each well-known
code is mapped to a typed exception so callers can `except` precisely:

    -101 → AuthExpiredError      cookie/SESSDATA expired
    -403 → PermissionDeniedError caller is not a moderator of the room
    -509 → RateLimitedError      too many requests
    other → BiliApiError         catch-all; carries code + message

All four inherit from `BiliApiError` so callers can also catch once at the
boundary.
"""
from __future__ import annotations


class BiliApiError(Exception):
    """Base class for all B站 HTTP business errors.

    Carries the `code` returned by B站 and the human-readable `message`.
    `str(err)` returns just the message (Python Exception convention);
    inspect `.code` for the numeric value and `repr(err)` for a code-prefixed
    debug representation.
    """

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class AuthExpiredError(BiliApiError):
    """code == -101: login cookie (SESSDATA) is missing or has expired."""

    def __init__(self, message: str = "auth expired") -> None:
        super().__init__(code=-101, message=message)


class PermissionDeniedError(BiliApiError):
    """code == -403: caller is not authorized (e.g. not a room moderator)."""

    def __init__(self, message: str = "permission denied") -> None:
        super().__init__(code=-403, message=message)


class RateLimitedError(BiliApiError):
    """code == -509: too many requests; B站 is throttling the caller."""

    def __init__(self, message: str = "rate limited") -> None:
        super().__init__(code=-509, message=message)


__all__ = [
    "AuthExpiredError",
    "BiliApiError",
    "PermissionDeniedError",
    "RateLimitedError",
]
