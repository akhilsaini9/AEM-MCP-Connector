from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable

import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from .config import Settings

logger = logging.getLogger("aem_mcp.auth")


class OAuthValidationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GoogleOIDCTokenVerifier:
    """Validate signed Google JWT bearer tokens using OIDC discovery and JWKS."""

    def __init__(
        self,
        settings: Settings,
        *,
        discovery_loader: Callable[[], Awaitable[dict[str, Any]]] | None = None,
        signing_key_loader: Callable[[str, str], Awaitable[Any]] | None = None,
        opaque_token_loader: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.settings = settings
        self._discovery_loader = discovery_loader or self._load_discovery
        self._signing_key_loader = signing_key_loader or self._load_signing_key
        self._opaque_token_loader = opaque_token_loader or self._load_opaque_token
        self._discovery: dict[str, Any] | None = None

    async def _load_discovery(self) -> dict[str, Any]:
        url = self.settings.mcp_oauth_issuer.rstrip("/") + "/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    async def _load_signing_key(self, token: str, jwks_uri: str) -> Any:
        # PyJWKClient caches Google's key set and refreshes it on key rotation.
        return PyJWKClient(jwks_uri, cache_keys=True).get_signing_key_from_jwt(token).key

    async def _load_opaque_token(self, token: str) -> dict[str, Any]:
        # Google access tokens are commonly opaque. Validate them at Google's
        # HTTPS token-info endpoint, then obtain the OIDC subject from userinfo.
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            info_response = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"access_token": token},
            )
            info_response.raise_for_status()
            user_response = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {token}"},
            )
            user_response.raise_for_status()
        return {**info_response.json(), **user_response.json()}

    async def _verify_opaque(self, token: str) -> AccessToken:
        try:
            claims = await self._opaque_token_loader(token)
        except OAuthValidationError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise OAuthValidationError("invalid_token") from exc
        audience = self.settings.mcp_oauth_audience.strip() or self.settings.mcp_oauth_client_id.strip()
        token_audience = claims.get("audience", claims.get("aud", claims.get("issued_to")))
        if token_audience != audience:
            raise OAuthValidationError("invalid_audience")
        if int(claims.get("expires_in", 0)) <= 0:
            raise OAuthValidationError("expired_token")
        subject = claims.get("sub", claims.get("user_id"))
        if not subject:
            raise OAuthValidationError("invalid_token")
        if claims.get("email") and claims.get("email_verified", claims.get("verified_email")) is not True:
            raise OAuthValidationError("email_not_verified")
        scopes = set(str(claims.get("scope", "")).split())
        if set(self.settings.oauth_required_scopes) - scopes:
            raise OAuthValidationError("insufficient_scope")
        return AccessToken(
            token="",
            client_id=str(token_audience),
            scopes=sorted(scopes),
            expires_at=int(time.time()) + int(claims["expires_in"]),
            resource=self.settings.mcp_public_base_url.rstrip("/") + self.settings.mcp_path,
            subject=str(subject),
            claims={"iss": self.settings.mcp_oauth_issuer.rstrip("/")},
        )

    async def verify(self, token: str) -> AccessToken:
        if token.count(".") != 2:
            return await self._verify_opaque(token)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OAuthValidationError("malformed_token") from exc
        algorithm = header.get("alg")
        if not algorithm or algorithm.lower() == "none" or algorithm not in {"RS256", "RS384", "RS512"}:
            raise OAuthValidationError("unsupported_signing_algorithm")

        try:
            discovery = self._discovery or await self._discovery_loader()
            self._discovery = discovery
            configured_issuer = self.settings.mcp_oauth_issuer.rstrip("/")
            if discovery.get("issuer", "").rstrip("/") != configured_issuer:
                raise OAuthValidationError("discovery_issuer_mismatch")
            key = await self._signing_key_loader(token, str(discovery["jwks_uri"]))
            audience = self.settings.mcp_oauth_audience.strip() or self.settings.mcp_oauth_client_id.strip()
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=audience,
                issuer=configured_issuer,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                leeway=30,
            )
        except OAuthValidationError:
            raise
        except jwt.ExpiredSignatureError as exc:
            raise OAuthValidationError("expired_token") from exc
        except jwt.InvalidIssuerError as exc:
            raise OAuthValidationError("invalid_issuer") from exc
        except jwt.InvalidAudienceError as exc:
            raise OAuthValidationError("invalid_audience") from exc
        except (jwt.PyJWTError, KeyError, httpx.HTTPError, ValueError) as exc:
            raise OAuthValidationError("invalid_token") from exc

        authorized_party = claims.get("azp")
        if authorized_party and authorized_party != self.settings.mcp_oauth_client_id:
            raise OAuthValidationError("unauthorized_client")
        if claims.get("email") and claims.get("email_verified") is not True:
            raise OAuthValidationError("email_not_verified")

        token_scopes = set()
        raw_scope = claims.get("scope", claims.get("scp", []))
        if isinstance(raw_scope, str):
            token_scopes.update(raw_scope.split())
        elif isinstance(raw_scope, list):
            token_scopes.update(str(item) for item in raw_scope)
        # Google ID tokens express these OIDC grants as verified claims, rather
        # than a scope claim. Do not infer any non-OIDC authorization scope.
        token_scopes.add("openid")
        if claims.get("email") and claims.get("email_verified") is True:
            token_scopes.add("email")
        if claims.get("name") or claims.get("picture"):
            token_scopes.add("profile")
        missing = set(self.settings.oauth_required_scopes) - token_scopes
        if missing:
            raise OAuthValidationError("insufficient_scope")

        return AccessToken(
            token="",  # Never retain or expose the caller's Google token.
            client_id=str(authorized_party or audience),
            scopes=sorted(token_scopes),
            expires_at=int(claims["exp"]),
            resource=self.settings.mcp_public_base_url.rstrip("/") + self.settings.mcp_path,
            subject=str(claims["sub"]),
            claims={"iss": str(claims["iss"])},
        )


def audit_auth(success: bool, *, issuer: str, subject: str | None = None, error: str | None = None) -> None:
    event: dict[str, Any] = {"event": "mcp_auth", "success": success, "issuer": issuer}
    if subject:
        event["subject_hash"] = hashlib.sha256(subject.encode()).hexdigest()[:16]
    if error:
        event["error_code"] = error
    logger.info(json.dumps(event, separators=(",", ":")))


def authenticated_context(access_token: AccessToken):
    return auth_context_var.set(AuthenticatedUser(access_token))


def reset_authenticated_context(token: Any) -> None:
    auth_context_var.reset(token)
