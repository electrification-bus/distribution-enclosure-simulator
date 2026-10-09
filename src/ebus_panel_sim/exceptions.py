"""Public exception hierarchy for ebus-panel-sim."""

from __future__ import annotations


class EmitterError(Exception):
    """Base class for all emitter errors."""


class ManifestValidationError(EmitterError):
    """A DeviceManifest references unknown entity_class, missing required parent, or
    contains a duplicate (entity_class, instance_id) pair."""


class RuntimeSpecValidationError(EmitterError):
    """A RuntimeSpec is structurally invalid or references manifest entities that do not
    exist."""


class MissingSetterError(EmitterError):
    """A settable property declared by a vendored profile has no corresponding handler in
    the SetterRegistry passed to Emitter.__init__. ``missing`` carries the offending
    (entity_class, property_path) pairs."""

    def __init__(self, missing: list[tuple[str, str]]) -> None:
        self.missing = missing
        rendered = ", ".join(f"({c!r}, {p!r})" for c, p in missing)
        super().__init__(f"Settable properties without registered handlers: {rendered}")


class ProfileValidationError(EmitterError):
    """A vendored profile or mapping descriptor failed internal-consistency validation at
    load time. Defensive — should never fire in shipped code."""


class EmitterStateError(EmitterError):
    """Emitter operation called in the wrong state (e.g. tick() before start(), start()
    against a disconnected MQTT client)."""


class UnknownReferenceCaptureError(EmitterError, LookupError):
    """``load_reference_capture`` was given a name the package ships no reference
    capture under. ``name`` is that name and ``known`` the names it ships."""

    def __init__(self, name: str, known: tuple[str, ...]) -> None:
        self.name = name
        self.known = known
        super().__init__(f"No reference capture {name!r}; the package ships {', '.join(known)}")
