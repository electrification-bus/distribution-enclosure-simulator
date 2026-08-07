"""Walk the manifest + mapping descriptors + profiles, build the ebus-sdk Device graph.

The graph is pure structure: a root ``Device`` (which owns the shared MQTT
connection, built from ``mqtt_cfg``) plus one child ``Device`` per
``child-of-parent`` entity, wired via ebus-sdk's ``parent=`` so the SDK
maintains ``children``/``root``/``parent`` and emits each device's
``$description`` itself. Construction opens no socket: the root's client
connects only when ``start_mqtt_client()`` is called. Behaviour wiring (settable
``/set`` callbacks) and per-tick value publishing live elsewhere; this module
owns topology + schema only.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

import ebus_sdk

from panel_sim.exceptions import ManifestValidationError, ProfileValidationError
from panel_sim.manifest import DeviceInstance, DeviceManifest
from panel_sim.wire._sdk_seam import make_property
from panel_sim.wire.mapping_loader import MappingDescriptor, MappingTable
from panel_sim.wire.profile_loader import Profile, ProfileTable

PropertyKey = tuple[str, str, str]


@dataclass(slots=True)
class BuiltGraph:
    """Structural result of walking the manifest.

    ``devices`` maps instance id -> the SDK ``Device`` (root and children).
    ``properties`` maps ``(entity_class, instance_id, "cap/key")`` -> the SDK
    ``Property``, the seam the publisher and setter-wiring use. ``root_id`` is
    the single root device's instance id. The SDK owns ``$description`` /
    ``children`` / ``$state``, so this graph carries no hand-built payloads.
    """

    devices: dict[str, ebus_sdk.Device] = field(default_factory=dict)
    properties: dict[PropertyKey, ebus_sdk.Property] = field(default_factory=dict)
    root_id: str = ""


def build_graph(
    manifest: DeviceManifest,
    mapping: MappingTable,
    profiles: ProfileTable,
    *,
    mqtt_cfg: dict[str, Any] | None = None,
    mqttc: Any | None = None,
) -> BuiltGraph:
    """Build the SDK device tree. No socket opens here.

    Exactly one of ``mqtt_cfg`` or ``mqttc`` names the root's connection, and
    children share whichever it is:

    - ``mqtt_cfg`` — a broker config dict (``host``/``port``/TLS/auth keys per
      ebus-mqtt-client) from which the SDK builds and owns a client.
    - ``mqttc`` — a client the caller already owns, per ebus-sdk's
      bring-your-own-transport contract. The SDK uses it as-is and never starts
      or stops it.
    """
    if (mqtt_cfg is None) == (mqttc is None):
        raise ManifestValidationError(
            "build_graph requires exactly one of mqtt_cfg= or mqttc=; "
            f"got mqtt_cfg={'set' if mqtt_cfg is not None else 'None'}, "
            f"mqttc={'set' if mqttc is not None else 'None'}"
        )

    graph = BuiltGraph()

    root_descriptors = [m for m in mapping.values() if m.placement.kind == "root-device"]
    if len(root_descriptors) != 1:
        raise ManifestValidationError(
            f"Expected exactly one root-device descriptor, got {len(root_descriptors)}"
        )
    root_class = root_descriptors[0].entity_class

    root_instances = manifest.of_class(root_class)
    if len(root_instances) != 1:
        raise ManifestValidationError(
            f"Expected exactly one {root_class!r} instance in manifest, got {len(root_instances)}"
        )
    root_instance = root_instances[0]

    # The SDK takes one or the other, never both: mqtt_cfg has it build a client
    # it owns; mqttc hands it one the caller owns.
    root_device = (
        ebus_sdk.Device(
            root_instance.instance_id,
            name=root_instance.display_name,
            type=profiles[root_class].type,
            mqttc=mqttc,
        )
        if mqttc is not None
        else ebus_sdk.Device(
            root_instance.instance_id,
            name=root_instance.display_name,
            type=profiles[root_class].type,
            mqtt_cfg=mqtt_cfg,
        )
    )
    graph.devices[root_instance.instance_id] = root_device
    graph.root_id = root_instance.instance_id

    _attach_profile(
        root_device,
        profiles[root_class],
        root_instance,
        graph,
        entity_class=root_class,
        parent_for_path=None,
        node_id_template=None,
    )

    # Topologically order non-root descriptors so a descriptor whose
    # ``child-of-parent`` placement names a parent_entity_class is processed
    # AFTER that parent's descriptor. Edges come solely from
    # ``child-of-parent.parent_entity_class`` references (``node-on-parent``
    # descriptors implicitly target the root and contribute no edge).
    ordered = _topo_sort_descriptors(mapping, root_class)

    for descriptor in ordered:
        ec = descriptor.entity_class
        for inst in manifest.of_class(ec):
            if descriptor.placement.kind == "node-on-parent":
                _attach_profile(
                    root_device,
                    profiles[ec],
                    inst,
                    graph,
                    entity_class=ec,
                    parent_for_path=root_instance,
                    node_id_template=descriptor.placement.node_id_template,
                )
            elif descriptor.placement.kind == "child-of-parent":
                parent_ec = descriptor.placement.parent_entity_class
                if parent_ec is None:
                    raise ProfileValidationError(
                        f"mapping {ec!r} placement.kind='child-of-parent' requires "
                        "parent_entity_class to be set"
                    )

                # Resolve the parent SDK device. If parent_ec is the root, it's the
                # single root device; otherwise the specific parent instance built
                # earlier by topo order.
                parent_instance: DeviceInstance
                if parent_ec == root_class:
                    parent_instance = root_instance
                else:
                    candidates = manifest.of_class(parent_ec)
                    if len(candidates) != 1:
                        raise ManifestValidationError(
                            f"Cannot place {ec!r} child instance {inst.instance_id!r}: "
                            f"expected exactly one {parent_ec!r} parent instance in "
                            f"manifest, got {len(candidates)}"
                        )
                    parent_instance = candidates[0]

                parent_device = graph.devices.get(parent_instance.instance_id)
                if parent_device is None:
                    raise ManifestValidationError(
                        f"Cannot place {ec!r} child instance {inst.instance_id!r}: "
                        f"parent device {parent_instance.instance_id!r} not yet built "
                        f"(topology bug — should have been ordered before this descriptor)"
                    )

                # ebus-sdk parents at construction via ``parent=<Device>``; it
                # appends the child to the parent's children and derives
                # root/parent itself. No parent_id/root_id/add_child anymore.
                child = ebus_sdk.Device(
                    inst.instance_id,
                    name=inst.display_name,
                    type=profiles[ec].type,
                    parent=parent_device,
                )
                graph.devices[inst.instance_id] = child
                _attach_profile(
                    child,
                    profiles[ec],
                    inst,
                    graph,
                    entity_class=ec,
                    parent_for_path=None,
                    node_id_template=descriptor.placement.node_id_template,
                )

    return graph


def _topo_sort_descriptors(mapping: MappingTable, root_class: str) -> list[MappingDescriptor]:
    """Return non-root mapping descriptors in topological order.

    Edges are derived from ``child-of-parent.parent_entity_class``: a child
    descriptor depends on its parent descriptor and must therefore be processed
    after it. Within a topological level, descriptors are tie-broken by
    ``placement.kind`` (``node-on-parent`` before ``child-of-parent``) and then
    by ``entity_class`` for determinism.

    Raises ``ProfileValidationError`` on cycles."""
    nodes: dict[str, MappingDescriptor] = {
        ec: m for ec, m in mapping.items() if m.placement.kind != "root-device"
    }

    # Build adjacency: edge parent_ec -> child_ec when child has parent_ec set
    # and parent_ec is itself a non-root descriptor (root parent contributes
    # no edge — the root device is built unconditionally first).
    in_degree: dict[str, int] = {ec: 0 for ec in nodes}
    successors: dict[str, list[str]] = {ec: [] for ec in nodes}
    for ec, descriptor in nodes.items():
        parent_ec = descriptor.placement.parent_entity_class
        if (
            descriptor.placement.kind == "child-of-parent"
            and parent_ec is not None
            and parent_ec != root_class
            and parent_ec in nodes
        ):
            successors[parent_ec].append(ec)
            in_degree[ec] += 1

    def _kind_rank(ec: str) -> int:
        return 0 if nodes[ec].placement.kind == "node-on-parent" else 1

    ready: deque[str] = deque(
        sorted(
            (ec for ec, deg in in_degree.items() if deg == 0),
            key=lambda ec: (_kind_rank(ec), ec),
        )
    )
    ordered: list[MappingDescriptor] = []
    while ready:
        ec = ready.popleft()
        ordered.append(nodes[ec])
        newly_ready: list[str] = []
        for succ in successors[ec]:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                newly_ready.append(succ)
        for succ in sorted(newly_ready, key=lambda e: (_kind_rank(e), e)):
            ready.append(succ)

    if len(ordered) != len(nodes):
        unresolved = sorted(ec for ec, deg in in_degree.items() if deg > 0)
        raise ProfileValidationError(
            "cycle detected in mapping descriptor parent_entity_class graph involving: "
            + ", ".join(unresolved)
        )
    return ordered


def _attach_profile(
    device: ebus_sdk.Device,
    profile: Profile,
    instance: DeviceInstance,
    graph: BuiltGraph,
    *,
    entity_class: str,
    parent_for_path: DeviceInstance | None,
    node_id_template: str | None,
) -> None:
    """Attach the profile's capabilities + properties to the given device.

    For its own device (parent_for_path is None) capability nodes use plain
    capability names. For node-on-parent entities, capability nodes are
    namespaced with the instance ID so multiple instances coexist on the parent
    without collision. All node/property additions run inside one
    ``state_transition()`` so the SDK coalesces the description republish into a
    single init->ready cycle instead of flapping per property.
    """
    single_capability = len(profile.capabilities) == 1
    with device.state_transition():
        for cap_name, cap in profile.capabilities.items():
            if parent_for_path is None:
                node_id = cap_name
            else:
                node_prefix = _render_node_id(node_id_template or "{instance_id}", instance)
                node_id = node_prefix if single_capability else f"{node_prefix}-{cap_name}"
            node = device.add_node_from_dict(
                {
                    "id": node_id,
                    "name": cap_name,
                    "type": cap.type,
                }
            )
            for prop_key, prop in cap.properties.items():
                sdk_prop = make_property(
                    node=node,
                    key=prop_key,
                    name=prop.name,
                    datatype=_to_sdk_datatype(prop.datatype),
                    unit=_to_sdk_unit(prop.unit),
                    format_str=prop.format,
                    settable=prop.settable,
                )
                graph.properties[
                    (entity_class, instance.instance_id, f"{cap_name}/{prop_key}")
                ] = sdk_prop


def _render_node_id(template: str, instance: DeviceInstance) -> str:
    return template.format(
        instance_id=instance.instance_id,
        instance_id_short=instance.instance_id[:8],
        display_name=instance.display_name,
    )


def _to_sdk_datatype(dt: str) -> ebus_sdk.PropertyDatatype:
    mapping = {
        "string": ebus_sdk.PropertyDatatype.STRING,
        "integer": ebus_sdk.PropertyDatatype.INTEGER,
        "float": ebus_sdk.PropertyDatatype.FLOAT,
        "boolean": ebus_sdk.PropertyDatatype.BOOLEAN,
        "enum": ebus_sdk.PropertyDatatype.ENUM,
        "json": getattr(ebus_sdk.PropertyDatatype, "JSON", ebus_sdk.PropertyDatatype.STRING),
    }
    return mapping.get(dt.lower(), ebus_sdk.PropertyDatatype.STRING)


def _to_sdk_unit(unit: str | None) -> ebus_sdk.Unit | None:
    """Map a Homie unit string to an ``ebus_sdk.Unit``.

    ``Unit`` is a str-enum whose *value* is the Homie wire string (``Unit.VOLTS``
    is ``"V"``, ``Unit.MINUTES`` is ``"min"``), so resolve by value: correct by
    construction for every unit the SDK models, and it cannot silently drop a unit
    the way the old hand-maintained name table did (which mapped "V" to a
    non-existent "VOLT" member and had no entry for "min"). An unmodeled unit
    resolves to None, omitting it from the wire."""
    if unit is None:
        return None
    try:
        return ebus_sdk.Unit(unit)
    except ValueError:
        return None
