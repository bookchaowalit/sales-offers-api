"""Verify platform-issued bearer tokens (RS256) for a cell's ``<cell>-api`` audience.

The same token is forwarded to the platform for every decision, so the
platform can refuse it too; local verification only rejects bad tokens early
and binds the request to one workspace for row-level security.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

MAX_LIFETIME_SECONDS = 3600  # owner tokens (no delegation) are always short-lived
HARD_MAX_LIFETIME_SECONDS = 30 * 86400
CHANNELS = {"api", "mcp", "workflow", "cli", "event"}


class Unauthenticated(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    actor: uuid.UUID
    workspace: uuid.UUID
    delegation: uuid.UUID | None
    channel: str
    token: str  # forwarded to the platform; never logged

    def __repr__(self) -> str:  # keep the token out of logs and tracebacks
        return f"Principal(actor={self.actor}, workspace={self.workspace}, channel={self.channel})"


class JwksKeys:
    """RS256 keys from the platform JWK Set, cached; an unknown ``kid`` refetches at most once a minute.

    A fetch failure keeps the last good keys, so a platform outage never empties the cache.
    """

    def __init__(
        self,
        url: str,
        *,
        ttl: float = 600,
        min_refresh: float = 60,
        timeout: float = 5.0,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.ttl, self.min_refresh = ttl, min_refresh
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self.clock = clock
        self._keys: dict[str, Any] = {}
        self._fetched = float("-inf")
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        self._fetched = self.clock()
        try:
            response = self.client.get(self.url)
            response.raise_for_status()
            keys = {}
            for jwk in response.json().get("keys", []):
                if (
                    jwk.get("kty") == "RSA"
                    and jwk.get("use", "sig") == "sig"
                    and jwk.get("alg", "RS256") == "RS256"
                ):
                    keys[str(jwk.get("kid", ""))] = jwt.PyJWK(
                        jwk, algorithm="RS256"
                    ).key
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, jwt.PyJWKError):
            return
        if keys:
            self._keys = keys

    def keys_for(self, token: str) -> list[Any]:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            return []
        with self._lock:
            age = self.clock() - self._fetched
            if age >= self.ttl or (
                kid is not None and kid not in self._keys and age >= self.min_refresh
            ):
                self._refresh()
            if kid is not None:
                return [self._keys[kid]] if kid in self._keys else []
            return list(
                self._keys.values()
            )  # tokens minted before kid headers: try every published key


@dataclass(frozen=True)
class TokenVerifier:
    public_key_pem: str  # pinned key; "" when only the JWK Set is used
    issuer: str
    audience: str
    max_lifetime: int = HARD_MAX_LIFETIME_SECONDS
    jwks: JwksKeys | None = field(default=None, compare=False)

    def _decode(self, token: str) -> dict[str, Any]:
        keys = self.jwks.keys_for(token) if self.jwks else []
        if self.public_key_pem:
            keys.append(self.public_key_pem)  # fallback while cells move to the JWK Set
        if not keys:
            raise jwt.InvalidKeyError("no verification key")
        for key in keys[:-1]:
            try:
                return self._decode_with(token, key)
            except jwt.InvalidSignatureError:
                continue
        return self._decode_with(token, keys[-1])

    def _decode_with(self, token: str, key: Any) -> dict[str, Any]:
        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iat", "sub", "jti"]},
        )

    def verify(self, authorization: str | None, channel: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise Unauthenticated("missing bearer token")
        token = authorization.removeprefix("Bearer ")
        try:
            claims = self._decode(token)
            lifetime = claims["exp"] - claims["iat"]
            limit = (
                self.max_lifetime
                if claims.get("del")
                else min(self.max_lifetime, MAX_LIFETIME_SECONDS)
            )
            if lifetime > limit:
                raise Unauthenticated("token lifetime too long")
            actor = uuid.UUID(claims["sub"])
            workspace = uuid.UUID(claims["ws"])
            delegation = uuid.UUID(claims["del"]) if claims.get("del") else None
        except Unauthenticated:
            raise
        except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
            raise Unauthenticated("invalid token") from exc
        channel = channel or "api"
        if channel not in CHANNELS:
            raise Unauthenticated("unknown channel")
        return Principal(
            actor=actor,
            workspace=workspace,
            delegation=delegation,
            channel=channel,
            token=token,
        )
