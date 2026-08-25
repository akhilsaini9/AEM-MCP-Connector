from __future__ import annotations

from .local import LocalAEMProvider
from ..aem_client import AEMClient
from ..cloud_tokens import AEMCloudTokenProvider, LocalDevelopmentTokenProvider
from ..config import Settings
from ..http_transport import AEMHttpTransport


class DirectAEMCloudProvider(LocalAEMProvider):
    """Thin repository-semantics adapter for direct authenticated AEMaaCS HTTP."""

    def __init__(self, settings: Settings, token_provider: AEMCloudTokenProvider | None = None) -> None:
        self.settings = settings
        self.token_provider = token_provider or LocalDevelopmentTokenProvider(settings)

    def _client(self) -> AEMClient:
        transport = AEMHttpTransport.for_cloud_direct(self.settings, self.token_provider)
        return AEMClient(transport, self.settings)
