from __future__ import annotations

from typing import Protocol

from .config import Settings


class AEMCloudTokenProvider(Protocol):
    async def get_access_token(self) -> str: ...


class LocalDevelopmentTokenProvider:
    """POC token source. The secret is neither transformed nor persisted."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def get_access_token(self) -> str:
        token = self._settings.aem_cloud_local_token.strip()
        if not token:
            raise RuntimeError("Cloud direct authentication is not configured.")
        return token
