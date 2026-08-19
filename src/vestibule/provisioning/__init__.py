"""Dynamic Index Provisioning — Phase 3 pipeline component (REQ-011)."""

from vestibule.provisioning.adapters.azure_ai_search import AzureAISearchProvisioner
from vestibule.provisioning.adapters.base import IndexProvisionerAdapter
from vestibule.provisioning.adapters.in_memory import InMemoryProvisioner
from vestibule.provisioning.model import (
    INDEX_AUTO_CREATE_DISABLED,
    INDEX_PROVISION_CONFLICT,
    INDEX_PROVISION_FAILED,
    INDEX_PROVISION_TIMEOUT,
    INDEX_PROVISIONER_DEPENDENCY_MISSING,
    INDEX_PROVISIONER_UNAVAILABLE,
    INDEX_REGISTRY_UNAVAILABLE,
    INDEX_RETIRED,
    INDEX_SCHEMA_DRIFT,
    INDEX_TEMPLATE_INVALID,
    INDEX_TEMPLATE_NOT_FOUND,
    FieldSpec,
    HnswSettings,
    IndexDescription,
    IndexRegistryEntry,
    IndexTemplate,
    ProvisioningError,
)
from vestibule.provisioning.provisioner import IndexProvisioner, ProvisioningConfig
from vestibule.provisioning.registry import IndexRegistry, InMemoryIndexRegistry
from vestibule.provisioning.stores.cached_registry import CachedIndexRegistry
from vestibule.provisioning.stores.table_storage_registry import (
    TableStorageIndexRegistry,
)
from vestibule.provisioning.templates import IndexTemplateStore

__all__ = [
    "INDEX_AUTO_CREATE_DISABLED",
    "INDEX_PROVISION_CONFLICT",
    "INDEX_PROVISION_FAILED",
    "INDEX_PROVISION_TIMEOUT",
    "INDEX_PROVISIONER_DEPENDENCY_MISSING",
    "INDEX_PROVISIONER_UNAVAILABLE",
    "INDEX_REGISTRY_UNAVAILABLE",
    "INDEX_RETIRED",
    "INDEX_SCHEMA_DRIFT",
    "INDEX_TEMPLATE_INVALID",
    "INDEX_TEMPLATE_NOT_FOUND",
    "AzureAISearchProvisioner",
    "CachedIndexRegistry",
    "FieldSpec",
    "HnswSettings",
    "IndexDescription",
    "IndexProvisioner",
    "IndexProvisionerAdapter",
    "IndexRegistry",
    "IndexRegistryEntry",
    "IndexTemplate",
    "IndexTemplateStore",
    "InMemoryIndexRegistry",
    "InMemoryProvisioner",
    "ProvisioningConfig",
    "ProvisioningError",
    "TableStorageIndexRegistry",
]
