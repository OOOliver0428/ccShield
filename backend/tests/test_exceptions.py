"""TDD: tests for typed exceptions defined in app.bilibili.exceptions.

These tests describe the contract that app/bilibili/exceptions.py must fulfill:
each typed Bili exception carries the code and the message, and the four
expected codes (-101 / -403 / -509) match the documented mappings.

TDD step 1: write tests FIRST. They MUST fail before implementation exists.
"""
from __future__ import annotations

import pytest

from app.bilibili.exceptions import (
    AuthExpiredError,
    BiliApiError,
    PermissionDeniedError,
    RateLimitedError,
)


def test_auth_expired_error_carries_minus_101_code() -> None:
    err = AuthExpiredError("cookie expired")
    assert err.code == -101
    assert err.message == "cookie expired"
    assert isinstance(err, BiliApiError)


def test_permission_denied_error_carries_minus_403_code() -> None:
    err = PermissionDeniedError("not a moderator")
    assert err.code == -403
    assert err.message == "not a moderator"
    assert isinstance(err, BiliApiError)


def test_rate_limited_error_carries_minus_509_code() -> None:
    err = RateLimitedError("slow down")
    assert err.code == -509
    assert err.message == "slow down"
    assert isinstance(err, BiliApiError)


def test_bili_api_error_carries_code_and_message() -> None:
    err = BiliApiError(code=-500, message="boom")
    assert err.code == -500
    assert err.message == "boom"
    assert str(err) == "boom"


def test_bili_api_error_subclasses_share_base() -> None:
    """All typed errors inherit from BiliApiError so callers can except once."""
    for cls in (AuthExpiredError, PermissionDeniedError, RateLimitedError):
        assert issubclass(cls, BiliApiError)


def test_bili_api_error_inherits_exception() -> None:
    err = BiliApiError(code=1, message="x")
    with pytest.raises(BiliApiError):
        raise err
