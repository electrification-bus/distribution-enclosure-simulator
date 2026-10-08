"""Producer-supplied device identity manifest.

Frozen, immutable declaration of which entity instances exist on a panel. The producer
hands one to ``Emitter`` at construction; the emitter validates it against the vendored
mapping descriptors and profiles. Manifest mutations require emitter restart (no live
mutation API).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DeviceInstance:
    entity_class: str
    instance_id: str
    display_name: str
    metadata: dict[str, str] = field(default_factory=dict)
    # The device's ``$description.name`` where it differs from ``display_name``,
    # which a circuit also publishes as ``info/name``. The MAIN 32 capture on
    # release 202639 has each child device's id there.
    description_name: str | None = None

    @property
    def published_name(self) -> str:
        """The ``$description.name`` the device publishes."""
        return self.display_name if self.description_name is None else self.description_name


@dataclass(frozen=True, slots=True)
class DeviceManifest:
    instances: tuple[DeviceInstance, ...]

    def get(self, entity_class: str, instance_id: str) -> DeviceInstance:
        for inst in self.instances:
            if inst.entity_class == entity_class and inst.instance_id == instance_id:
                return inst
        raise KeyError(
            f"No DeviceInstance with entity_class={entity_class!r}, instance_id={instance_id!r}"
        )

    def of_class(self, entity_class: str) -> tuple[DeviceInstance, ...]:
        return tuple(i for i in self.instances if i.entity_class == entity_class)
