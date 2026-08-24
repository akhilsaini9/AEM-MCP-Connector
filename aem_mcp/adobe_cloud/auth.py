from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import Settings
from .errors import AdobeCloudTokenExchangeError, AdobeCloudUserInfoError
from .sessions import AdobeCloudSession, AdobeCloudSessionStore, PendingOAuthState, connected_timestamp


@dataclass(frozen=True)
class AdobeTokenResponse:
    access_token: str
    expires_in: int
    refresh_token: str | None = None
    token_type: str = "Bearer"


class AdobeCloudOAuth:
    def __init__(self, settings: Settings, store: AdobeCloudSessionStore, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.store = store
        self._client = client

    async def authorization_url(self, session_key: str, *, now: float | None = None) -> str:
        state = secrets.token_urlsafe(32)
        await self.store.save_state(state, PendingOAuthState(session_key, (now or time.time()) + 600))
        params = {
            "client_id": self.settings.adobe_cloud_client_id,
            "redirect_uri": self.settings.adobe_cloud_redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.settings.adobe_cloud_scope_names),
            "state": state,
        }
        return self.settings.adobe_cloud_authorization_endpoint + "?" + urlencode(params)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._client:
            return await self._client.request(method, url, **kwargs)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            return await client.request(method, url, **kwargs)

    @staticmethod
    def _parse_token(response: httpx.Response) -> AdobeTokenResponse:
        if response.is_error:
            raise AdobeCloudTokenExchangeError("Adobe token exchange was rejected.")
        try:
            data = response.json()
            token = data.get("access_token")
            expires = int(data.get("expires_in", 0))
            if not isinstance(token, str) or not token or expires <= 0:
                raise ValueError
            refresh = data.get("refresh_token")
            return AdobeTokenResponse(token, expires, refresh if isinstance(refresh, str) and refresh else None, str(data.get("token_type") or "Bearer"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise AdobeCloudTokenExchangeError("Adobe token response was invalid.") from exc

    async def exchange_code(self, code: str) -> AdobeTokenResponse:
        if not code:
            raise AdobeCloudTokenExchangeError("Authorization code is missing.")
        try:
            response = await self._request("POST", self.settings.adobe_cloud_token_endpoint, data={
                "grant_type": "authorization_code", "client_id": self.settings.adobe_cloud_client_id,
                "client_secret": self.settings.adobe_cloud_client_secret, "redirect_uri": self.settings.adobe_cloud_redirect_uri,
                "code": code,
            })
            return self._parse_token(response)
        except AdobeCloudTokenExchangeError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AdobeCloudTokenExchangeError("Adobe token exchange failed.") from exc

    async def refresh(self, refresh_token: str) -> AdobeTokenResponse:
        try:
            response = await self._request("POST", self.settings.adobe_cloud_token_endpoint, data={
                "grant_type": "refresh_token", "client_id": self.settings.adobe_cloud_client_id,
                "client_secret": self.settings.adobe_cloud_client_secret, "refresh_token": refresh_token,
            })
            return self._parse_token(response)
        except AdobeCloudTokenExchangeError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AdobeCloudTokenExchangeError("Adobe token refresh failed.") from exc

    async def userinfo(self, access_token: str, token_type: str = "Bearer") -> dict[str, str | None]:
        try:
            response = await self._request("GET", self.settings.adobe_cloud_userinfo_endpoint, headers={"Authorization": f"{token_type} {access_token}"})
            if response.is_error:
                raise AdobeCloudUserInfoError("Adobe user identity verification failed.")
            data = response.json()
            subject = data.get("sub") or data.get("userId") or data.get("id")
            if not isinstance(subject, str) or not subject:
                raise ValueError
            email = data.get("email")
            return {"subject": subject, "email": email if isinstance(email, str) else None}
        except AdobeCloudUserInfoError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise AdobeCloudUserInfoError("Adobe user identity response was invalid.") from exc

    async def establish_session(self, session_key: str, token: AdobeTokenResponse, identity: dict[str, str | None], author_url: str) -> AdobeCloudSession:
        session = AdobeCloudSession(session_key=session_key, access_token=token.access_token,
            refresh_token=token.refresh_token, expires_at=time.time() + token.expires_in,
            token_type=token.token_type, adobe_user_id=identity["subject"] or "", adobe_email=identity.get("email"),
            connected_at=connected_timestamp(), author_url=author_url or None)
        await self.store.save(session)
        return session
