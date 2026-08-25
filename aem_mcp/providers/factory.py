from __future__ import annotations

from ..config import Settings, get_settings
from .base import AEMProvider
from .errors import AEMProviderConfigurationError
from .local import LocalAEMProvider


def get_aem_provider(settings: Settings | None = None) -> AEMProvider:
    selected = settings or get_settings()
    mode = selected.aem_runtime_mode.strip().lower()
    if mode == "local": return LocalAEMProvider()
    if mode == "cloud":
        if selected.aem_cloud_provider_mode.strip().lower() == "direct_http":
            from .direct_cloud import DirectAEMCloudProvider
            return DirectAEMCloudProvider(selected)
        from .adobe_cloud import AdobeCloudProvider
        return AdobeCloudProvider(selected)
    raise AEMProviderConfigurationError("AEM_RUNTIME_MODE must be local or cloud.")


def require_local_runtime(settings: Settings | None = None) -> None:
    if (settings or get_settings()).aem_runtime_mode.strip().lower() == "cloud":
        raise AEMProviderUnsupported("This operation has not yet been migrated to the Adobe Cloud provider.")


def get_repository_aem_client(settings: Settings | None = None):
    """Return a repository client only for local or explicitly selected direct HTTP mode."""
    selected = settings or get_settings()
    runtime = selected.aem_runtime_mode.strip().lower()
    if runtime == "local":
        from ..aem_client import AEMClient
        return AEMClient(settings=selected)
    if runtime == "cloud" and selected.aem_cloud_provider_mode.strip().lower() == "direct_http":
        from .direct_cloud import DirectAEMCloudProvider
        return DirectAEMCloudProvider(selected)._client()
    raise AEMProviderUnsupported("Repository HTTP operations are unsupported in cloud OpenAPI mode.")


from .errors import AEMProviderUnsupported
