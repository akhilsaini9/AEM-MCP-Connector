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
        from .adobe_cloud import AdobeCloudProvider
        return AdobeCloudProvider(selected)
    raise AEMProviderConfigurationError("AEM_RUNTIME_MODE must be local or cloud.")


def require_local_runtime(settings: Settings | None = None) -> None:
    if (settings or get_settings()).aem_runtime_mode.strip().lower() == "cloud":
        raise AEMProviderUnsupported("This operation has not yet been migrated to the Adobe Cloud provider.")


from .errors import AEMProviderUnsupported
