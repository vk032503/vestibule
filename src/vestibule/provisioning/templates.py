"""IndexTemplateStore — loads/validates `config/index_templates/*.yaml` (REQ-011).

Mirrors `YamlScenarioStore`'s eager-load-at-construction, fail-fast pattern exactly
(`vestibule.scenario.stores.yaml_store`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from vestibule.provisioning.model import (
    INDEX_TEMPLATE_INVALID,
    INDEX_TEMPLATE_NOT_FOUND,
    IndexTemplate,
    ProvisioningError,
)


class IndexTemplateStore:
    """Read-only, in-memory catalog of every `IndexTemplate` under `directory`.

    Never refreshes after construction — a template file edited/redeployed after this
    object is built is not observed by this instance; this is precisely why a
    reclaiming worker no longer re-resolves a template through its own
    `IndexTemplateStore` mid-claim, and instead reuses the exact `IndexTemplate`
    snapshot stamped into the claim at original-registration time (Assumption A14, see
    `provisioner.py`).
    """

    def __init__(self, directory: str | Path) -> None:
        """Loads and validates every `*.yaml` file under `directory` at construction.

        Args:
            directory: Directory containing one `*.yaml` file per template.

        Raises:
            ProvisioningError: `INDEX_TEMPLATE_INVALID` (PERMANENT) if any file fails
                to parse as YAML, is not a mapping, fails `IndexTemplate` validation, or
                its filename stem does not match its own declared `template_id` — fail
                fast, before any document is ever in scope (mirrors `YamlScenarioStore`).
        """
        self._directory = Path(directory)
        self._templates: dict[str, IndexTemplate] = {}
        for path in sorted(self._directory.glob("*.yaml")):
            template = _load_one(path)
            self._templates[template.template_id] = template

    def get(self, template_id: str) -> IndexTemplate | None:
        """Returns the loaded template, or `None` if `template_id` is unknown."""
        return self._templates.get(template_id)

    def get_or_raise(self, template_id: str) -> IndexTemplate:
        """Returns the loaded template.

        Args:
            template_id: The template to look up.

        Returns:
            The loaded `IndexTemplate`.

        Raises:
            ProvisioningError: `INDEX_TEMPLATE_NOT_FOUND` (PERMANENT) if `template_id`
                is unknown.
        """
        template = self._templates.get(template_id)
        if template is None:
            raise ProvisioningError(
                template_id,
                f"no index template registered for template_id={template_id!r}",
                error_code=INDEX_TEMPLATE_NOT_FOUND,
            )
        return template


def _load_one(path: Path) -> IndexTemplate:
    """Parses and validates one template YAML file.

    Args:
        path: The `*.yaml` file to load.

    Returns:
        The validated `IndexTemplate`.

    Raises:
        ProvisioningError: `INDEX_TEMPLATE_INVALID` (PERMANENT) on a YAML parse error,
            a non-mapping document, an `IndexTemplate` validation failure, or a
            filename-stem/`template_id` mismatch.
    """
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProvisioningError(
            "", f"{path}: invalid YAML: {exc}", error_code=INDEX_TEMPLATE_INVALID
        ) from exc
    if not isinstance(raw, dict):
        raise ProvisioningError(
            "",
            f"{path}: expected a YAML mapping at the document root, got "
            f"{type(raw).__name__}",
            error_code=INDEX_TEMPLATE_INVALID,
        )
    try:
        template = IndexTemplate(**raw)
    except ValidationError as exc:
        raise ProvisioningError(
            str(raw.get("template_id", "")),
            f"{path}: {exc}",
            error_code=INDEX_TEMPLATE_INVALID,
        ) from exc
    if template.template_id != path.stem:
        raise ProvisioningError(
            template.template_id,
            f"{path}: filename stem {path.stem!r} does not match declared "
            f"template_id {template.template_id!r}",
            error_code=INDEX_TEMPLATE_INVALID,
        )
    return template
