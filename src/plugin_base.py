"""Common base class for plugin lifecycle."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from typing import Any, ClassVar
import logging

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


@dataclass(frozen=True)
class PluginBaseConfig:
    """Base class for all plugin configurations."""

    enabled: bool = True
    module: str | None = None
    log: logging.Logger | None = None
    log_level: str = "INFO"
    version: str | None = None
    req_version: str | None = None
    api_version: str | None = None
    req_api_version: str | None = None

    def __post_init__(self) -> None:
        """Validate configured plugin and API version requirements."""
        self._check_version_requirement(
            value=self.version,
            requirement=self.req_version,
            value_name="plugin version",
            requirement_name="req_version",
        )
        self._check_version_requirement(
            value=self.api_version,
            requirement=self.req_api_version,
            value_name="plugin API version",
            requirement_name="req_api_version",
        )

    @staticmethod
    def _check_version_requirement(
        *,
        value: str | None,
        requirement: str | None,
        value_name: str,
        requirement_name: str,
    ) -> None:
        """Validate one version against an optional PEP 440 requirement."""
        # A missing requirement disables compatibility checking.
        if requirement is None:
            return
        # A requirement cannot be evaluated without an actual version.
        if value is None:
            raise ValueError(
                f"{value_name} is required when {requirement_name} is set"
            )
        # Parse the provided version and requirement with packaging.
        try:
            parsed_value = Version(value)
        except (InvalidVersion, TypeError) as exc:
            raise ValueError(f"invalid {value_name}: {value!r}") from exc
        try:
            specifier = SpecifierSet(requirement)
        except (InvalidSpecifier, TypeError) as exc:
            raise ValueError(
                f"invalid {requirement_name}: {requirement!r}"
            ) from exc
        if parsed_value not in specifier:
            raise ValueError(
                f"{value_name} {value!r} does not satisfy "
                f"{requirement_name} {requirement!r}"
            )


class PluginBaseCapability(ABC):
    """Optional base class for plugin-defined service interfaces.

    ``PluginManager`` indexes capabilities declared by a plugin's
    ``PluginBase.CAPABILITIES`` mapping. Capability objects do not have to
    inherit from this class; applications and plugins may use it voluntarily
    to give service interfaces an explicit ``CAPABILITY_ID`` contract.
    """

    CAPABILITY_ID: ClassVar[str]


class PluginBase(ABC):
    """Base class for plugins managed by PluginManager."""

    Config: ClassVar[type[Any] | None] = None
    CAPABILITIES: ClassVar[dict[str, str]] = {}
    name: ClassVar[str | None] = None

    def __init__(self, config: Any = None, context: Any = None) -> None:
        """Store the validated plugin config and runtime context."""
        self.config = config
        self.context = context

    def start(self) -> Any:
        """Start plugin activity; subclasses may override this hook."""
        pass

    def stop(self) -> None:
        """Stop active plugin work; subclasses may override this hook."""
        pass

    def close(self) -> None:
        """Release final plugin resources; subclasses may override it."""
        pass
