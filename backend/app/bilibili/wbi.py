"""WBI signing algorithm — ported from ccShield.

Reference: https://github.com/SocialSisterYi/bilibili-API-collect/blob/master/docs/misc/sign/wbi.md

Pure Python. No global httpx state — the caller passes an `httpx.AsyncClient`
so we don't own a network resource here. The module-level `wbi_signer` is a
process-wide cache (1 hour TTL) shared by any caller that uses it.
"""
from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from typing import Final

import httpx
from loguru import logger

# WBI 签名用的字符映射表 (reference: bilibili-API-collect)
# 64 indices into a 128-char (img_key + sub_key) string; reordered.
MIXIN_KEY_ENC_TAB: Final[tuple[int, ...]] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


def get_mixin_key(orig: str) -> str:
    """Reorder `orig` (a 128-char string of `img_key + sub_key`) by
    `MIXIN_KEY_ENC_TAB` and return the first 32 chars.

    This is the B站 canonical mixin-key derivation.
    """
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def enc_wbi(
    params: dict[str, str],
    img_key: str,
    sub_key: str,
    *,
    now: int | None = None,
) -> dict[str, str]:
    """Add WBI signature (`wts` + `w_rid`) to a params dict.

    Pure: does not mutate `params`. Returns a NEW dict containing the
    original params (with `!'()*`-chars stripped from values per spec) plus
    `wts` and `w_rid`.

    Args:
        params: request parameters to sign.
        img_key: B站 nav `wbi_img.img_url` filename stem.
        sub_key: B站 nav `wbi_img.sub_url` filename stem.
        now: optional pinned timestamp (used by tests). Defaults to
            `int(time.time())`.
    """
    signed: dict[str, str] = {}
    for k, v in params.items():
        if v is None:
            continue
        signed[k] = re.sub(r"[!'()*]", "", str(v))

    signed["wts"] = str(int(now if now is not None else time.time()))
    ordered = dict(sorted(signed.items()))
    query = urllib.parse.urlencode(ordered)
    mixin_key = get_mixin_key(img_key + sub_key)
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    ordered["w_rid"] = w_rid
    return ordered


class WbiSigner:
    """Caches B站 WBI keys (img_key, sub_key) with a 1-hour TTL.

    Keys are fetched from `https://api.bilibili.com/x/web-interface/nav`
    via the caller-supplied `httpx.AsyncClient`. The caller owns the
    client lifetime; this class only borrows it for HTTPS GETs.
    """

    DEFAULT_REFRESH_INTERVAL: Final[int] = 3600

    def __init__(self, refresh_interval: int = DEFAULT_REFRESH_INTERVAL) -> None:
        self._img_key: str | None = None
        self._sub_key: str | None = None
        self._last_update: float = 0.0
        self._refresh_interval: int = refresh_interval

    @property
    def img_key(self) -> str | None:
        return self._img_key

    @property
    def sub_key(self) -> str | None:
        return self._sub_key

    @property
    def last_update(self) -> float:
        return self._last_update

    @last_update.setter
    def last_update(self, value: float) -> None:
        """Allow tests / runtime to force expiry by setting this to 0."""
        self._last_update = value

    @property
    def refresh_interval(self) -> int:
        return self._refresh_interval

    def _is_cache_fresh(self, now: float) -> bool:
        return (
            self._img_key is not None
            and self._sub_key is not None
            and (now - self._last_update) < self._refresh_interval
        )

    async def get_keys(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """Return cached `(img_key, sub_key)`, fetching from B站 if stale.

        Raises:
            RuntimeError: if the nav endpoint is unreachable AND no cached
                keys exist. (When cached keys exist, the previous value is
                kept; ccShield's "fall-back to stale" behaviour is preserved
                because a transient nav failure should not block sign.)
        """
        now = time.time()
        if self._is_cache_fresh(now):
            assert self._img_key is not None
            assert self._sub_key is not None
            return self._img_key, self._sub_key

        try:
            response = await client.get(
                "https://api.bilibili.com/x/web-interface/nav",
                timeout=10.0,
            )
            data = response.json()
        except Exception as exc:
            logger.error("WBI nav request failed: {}", exc)
            return self._fallback_to_cached()

        if not isinstance(data, dict) or data.get("code") != 0:
            logger.error("WBI nav returned non-zero: {}", data)
            return self._fallback_to_cached()

        payload = data.get("data") or {}
        wbi_img = payload.get("wbi_img") or {}
        img_url = wbi_img.get("img_url")
        sub_url = wbi_img.get("sub_url")
        if not isinstance(img_url, str) or not isinstance(sub_url, str):
            raise RuntimeError(
                "WBI nav response missing wbi_img.img_url or wbi_img.sub_url"
            )

        self._img_key = img_url.split("/")[-1].split(".")[0]
        self._sub_key = sub_url.split("/")[-1].split(".")[0]
        self._last_update = now
        logger.info(
            "WBI keys refreshed: img_key={}…, sub_key={}…",
            self._img_key[:10],
            self._sub_key[:10],
        )
        assert self._img_key is not None
        assert self._sub_key is not None
        return self._img_key, self._sub_key

    def _fallback_to_cached(self) -> tuple[str, str]:
        """Return previously-cached keys or raise."""
        if self._img_key is not None and self._sub_key is not None:
            logger.warning("WBI nav failed; reusing stale keys")
            return self._img_key, self._sub_key
        raise RuntimeError("WBI keys unavailable and no cache present")

    async def sign(
        self, client: httpx.AsyncClient, params: dict[str, str]
    ) -> dict[str, str]:
        """Sign `params` with WBI; equivalent to `enc_wbi(*current_keys)`."""
        img_key, sub_key = await self.get_keys(client)
        return enc_wbi(params, img_key, sub_key)


# Process-wide signer instance. Tests may construct their own.
wbi_signer: Final[WbiSigner] = WbiSigner()


__all__ = [
    "MIXIN_KEY_ENC_TAB",
    "WbiSigner",
    "enc_wbi",
    "get_mixin_key",
    "wbi_signer",
]
