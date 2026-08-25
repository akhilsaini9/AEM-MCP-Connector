from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncGenerator

import httpx

from .cloud_tokens import AEMCloudTokenProvider
from .config import Settings


class BearerTokenAuth(httpx.Auth):
    requires_request_body = True

    def __init__(self, provider: AEMCloudTokenProvider) -> None:
        self._provider = provider

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self._provider.get_access_token()}"
        yield request


class CloudDirectAsyncClient(httpx.AsyncClient):
    async def request(self, method: str, url: httpx.URL | str, **kwargs):  # type: ignore[no-untyped-def]
        try:
            return await super().request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            from .providers.errors import AEMProviderRemoteError
            raise AEMProviderRemoteError("AEM Cloud request timed out.", code="aem_cloud_timeout") from exc


async def reject_unsafe_cloud_response(response: httpx.Response) -> None:
    # Lazy import avoids making the transport/client layer depend on provider
    # package initialization.
    from .providers.errors import (
        AEMProviderAuthenticationRequired, AEMProviderPermissionDenied,
        AEMProviderRateLimited, AEMProviderRemoteError,
    )
    status = response.status_code
    if 300 <= status < 400:
        raise AEMProviderRemoteError("AEM Cloud redirect was rejected.", code="aem_cloud_redirect_rejected")
    if status == 401:
        raise AEMProviderAuthenticationRequired("AEM Cloud direct authentication failed.", code="cloud_direct_authentication_failed")
    if status == 403:
        raise AEMProviderPermissionDenied("AEM Cloud direct permission denied.", code="cloud_direct_permission_denied")
    if status == 429:
        raise AEMProviderRateLimited("AEM Cloud rate limit reached.", code="aem_rate_limited")
    if status >= 500:
        raise AEMProviderRemoteError("AEM Cloud remote service failed.", code="aem_remote_error")


@dataclass(frozen=True)
class AEMHttpTransport:
    base_url: str
    auth: httpx.Auth | tuple[str, str]
    timeout: float
    verify: bool
    follow_redirects: bool
    strict_csrf: bool
    cloud_direct: bool = False
    default_headers: dict[str, str] = field(default_factory=lambda: {"Accept": "application/json"})

    @classmethod
    def local(cls, settings: Settings) -> "AEMHttpTransport":
        return cls(settings.aem_base_url.rstrip("/"), (settings.aem_username, settings.aem_password), settings.aem_timeout_seconds, settings.aem_verify_ssl, True, False)

    @classmethod
    def for_cloud_direct(cls, settings: Settings, token_provider: AEMCloudTokenProvider) -> "AEMHttpTransport":
        return cls(settings.aem_cloud_author_url.rstrip("/"), BearerTokenAuth(token_provider), settings.aem_timeout_seconds, True, False, True, True)

    def client(self) -> httpx.AsyncClient:
        hooks = {"response": [reject_unsafe_cloud_response]} if self.cloud_direct else None
        client_type = CloudDirectAsyncClient if self.cloud_direct else httpx.AsyncClient
        return client_type(base_url=self.base_url, auth=self.auth, timeout=self.timeout, verify=self.verify,
                           follow_redirects=self.follow_redirects, headers=self.default_headers, event_hooks=hooks)
