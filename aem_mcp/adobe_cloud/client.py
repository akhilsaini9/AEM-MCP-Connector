from __future__ import annotations

from urllib.parse import urljoin, urlsplit
from typing import Any
import httpx

from ..config import Settings
from .errors import AdobeCloudAuthenticationError, AdobeCloudPermissionError


class AdobeCloudClient:
    """Direct, read-only AEM Cloud HTTP client for an explicitly configured probe."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def probe(self, access_token: str) -> dict[str, Any]:
        author = self.settings.aem_cloud_author_url.strip().rstrip("/")
        path = self.settings.aem_cloud_test_path.strip()
        if not author:
            return {"connected": True, "author_url_configured": False, "api_probe_configured": bool(path), "status": "author_url_not_configured"}
        if not path:
            return {"connected": True, "author_url_configured": True, "api_probe_configured": False, "status": "oauth_connected_probe_not_configured"}
        if not path.startswith("/"):
            path = "/" + path
        url = urljoin(author + "/", path.lstrip("/"))
        try:
            if self._client:
                response = await self._client.get(url, headers={"Authorization": f"Bearer {access_token}"})
            else:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                    response = await client.get(url, headers={"Authorization": f"Bearer {access_token}"})
        except httpx.HTTPError as exc:
            raise AdobeCloudAuthenticationError("AEM Cloud API probe could not be completed.", code="ADOBE_CLOUD_PROBE_FAILED") from exc
        base = {"connected": True, "http_status": response.status_code, "author_host": urlsplit(author).hostname, "authorized": response.status_code < 400}
        if response.status_code == 401:
            raise AdobeCloudAuthenticationError("AEM Cloud rejected the Adobe user token.", code="ADOBE_CLOUD_PROBE_UNAUTHORIZED")
        if response.status_code == 403:
            raise AdobeCloudPermissionError("Adobe user is authenticated but lacks permission for the configured AEM endpoint.")
        if response.status_code == 404:
            return {**base, "authorized": None, "status": "endpoint_not_found"}
        if response.status_code >= 400:
            return {**base, "status": "api_probe_failed"}
        return base
