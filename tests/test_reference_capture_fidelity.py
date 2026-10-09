"""Each reference capture, republished from its own definition, against the capture.

``tests/fixtures/r202639-<handle>-tree-v1.json`` is a masked tree of a SPAN panel
on ``spanos3/r202639/03``, and ``r202639-<handle>.yaml`` and ``.ticks.yaml`` are the
definition and the 60 one-second ticks recorded with it. The two MAIN 32 captures
are held to the same bar: ``main32_r202639`` with its definition and ticks, and
the r202633 capture ``main32-tree-v1.json``, which has neither, through the
definition ``panel-sim-capture`` writes from it and one tick sampled from it.

The emitter publishes the definition through every tick, and the retained tree
is compared with the capture device by device: ``$description`` keys, type and
children, nodes, declared properties and their datatype, settable, unit, format
and name, valued versus unvalued, the literal form of each value, the value
itself where the definition commissions it (``_COMPARED_BY_VALUE``), and the
sign of each power.
Masked identifiers, time-varying magnitudes and ``connection/count`` may differ.
Devices are aligned by role, not id: a branch circuit by its spaces and name, a
circuit device without ``info/spaces`` by its ordinal, lugs by direction, and any
other device by its type's ordinal.

Every capture must reproduce in full, but for the exceptions listed below, each
with the reason the emitter cannot close it. A listed exception that no longer
occurs fails too, so the list never outlives its reason."""

from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from ebus_panel_sim import (
    DeviceManifest,
    Emitter,
    PanelDefinition,
    SetterRegistry,
    TickInputs,
    load_definition,
    load_ticks,
)
from ebus_panel_sim.capture import (
    Device,
    Tree,
    definition_from_tree,
    ticks_from_samples,
    tree_from_retained,
    tree_from_snapshot,
)
from ebus_panel_sim.manifest_physics import unvalued_paths

from .conftest import PahoRecorder

_FIXTURES = Path(__file__).parent / "fixtures"
_HANDLES = sorted(
    path.name.removesuffix("-tree-v1.json") for path in _FIXTURES.glob("r202639-*-tree-v1.json")
)
_MAIN32_R202639 = "main32_r202639"
_MAIN32_R202633 = "main32-r202633"
_FEEDTHROUGH = (
    "The panel feeds a sub-panel through its downstream lugs (2700.6 W in the "
    "capture), which a definition cannot express, so the emitter's site lacks that "
    "load and its grid and upstream lugs run the other way."
)
_FED_BY_ENCLOSURE = (
    "The upstream lugs are fed by another enclosure, which a definition cannot "
    "express; panel-sim-capture reports it as a note."
)
# Differences a capture may still show, by prefix (the device role, the path and the
# kind of difference, without values), each with the reason the emitter cannot
# close it.
_EXCEPTIONS: dict[str, dict[str, str]] = {
    "r202639-b": {
        "distribution-enclosure #1: power-flows/site sign": (
            "The panel published site -8.8 W beside grid -0.2 W and no other flow, so "
            "its own flows do not balance; the emitter's do, and its site follows the "
            "circuits' small positive load."
        ),
    },
    _MAIN32_R202639: {
        "distribution-enclosure #1: power-flows/grid sign": _FEEDTHROUGH,
        "lugs UPSTREAM: meter/active-power sign": _FEEDTHROUGH,
    },
    _MAIN32_R202633: {
        f"lugs UPSTREAM: connection/fed-by-device-{key}: valued only by the capture": (
            _FED_BY_ENCLOSURE
        )
        for key in ("id", "status", "type")
    },
}
_NOT_COMPARED = frozenset({"connection/count"})
# Values a definition carries as commissioned, which neither vary with time nor are
# masked: compared exactly, not only by shape.
_COMPARED_BY_VALUE = frozenset(
    {
        "breaker/poles",
        "breaker/rating",
        "config/max-charge-current",
        "config/user-max-charge-current",
        "connection/backed-up",
        "connection/feeds-role",
        "connection/overcurrent-protection",
        "connection/service-rating",
        "info/dedicated",
        "info/locations",
        "info/model",
        "info/nominal-voltage",
        "info/tags",
        "pcs/off-grid-import-limit",
        "pcs/off-grid-import-limit-enablement",
        "pcs/priority",
    }
)
_ATTRIBUTES = ("datatype", "settable", "unit", "format", "name")
_SIGNED = re.compile(r"^(meter/active-power|power-flows/.+)$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def _mapping(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _nodes(device: Device) -> dict[str, dict[str, object]]:
    return {
        node: _mapping(body) for node, body in _mapping(device.description.get("nodes")).items()
    }


def _declarations(body: dict[str, object]) -> dict[str, dict[str, object]]:
    return {key: _mapping(decl) for key, decl in _mapping(body.get("properties")).items()}


def _roles(tree: Tree) -> dict[str, str]:
    """Role to device id, so a masked id never decides which devices are compared."""
    roles: dict[str, str] = {}
    ordinals: Counter[str] = Counter()
    for device_id in sorted(tree):
        device = tree[device_id]
        if device.type == "circuit" and not device.declares("info/spaces"):
            ordinals["meter-only circuit"] += 1
            role = f"meter-only circuit #{ordinals['meter-only circuit']}"
        elif device.type == "circuit":
            role = f"circuit {device.value('info/spaces')} {device.value('info/name')}"
        elif device.type == "lugs":
            role = f"lugs {device.value('info/direction')}"
        else:
            ordinals[device.type] += 1
            role = f"{device.type} #{ordinals[device.type]}"
        if role in roles:
            ordinals[role] += 1
            role = f"{role} ~{ordinals[role]}"
        roles[role] = device_id
    return roles


def _shape(value: str, declaration: dict[str, object]) -> str:
    """What a value looks like on the wire, by its declared datatype."""
    datatype = declaration.get("datatype")
    if value == "":
        return "empty"
    if datatype in ("integer", "float"):
        if value.startswith("-0") and value.strip("-0.") == "":
            return "negative zero"
        if not _NUMBER.match(value):
            return f"not a number ({datatype})"
        if "e" in value.lower():
            return "exponent"
        if "." not in value:
            return "integer"
        return f"{len(value.split('.', 1)[1])} decimal places"
    if datatype == "boolean":
        return "boolean" if value in ("true", "false") else "not a boolean"
    if datatype == "enum":
        return "in format" if value in str(declaration.get("format", "")).split(",") else "out"
    if datatype == "json":
        try:
            json.loads(value)
        except ValueError:
            return "not json"
        return "json"
    return "string"


def _sign(value: str) -> int:
    """The sign of a power, or 0 when it is under a watt or not a number."""
    try:
        number = float(value)
    except ValueError:
        return 0
    return 0 if abs(number) < 1.0 else (1 if number > 0 else -1)


def _children_by_type(tree: Tree, device: Device) -> Counter[str]:
    children = device.description.get("children", [])
    return Counter(tree[c].type for c in children if c in tree)


def _device_differences(captured: Device, published: Device) -> list[str]:
    out: list[str] = []
    top_c = set(captured.description) - {"nodes"}
    top_p = set(published.description) - {"nodes"}
    if top_c != top_p:
        out.append(f"$description keys: {sorted(top_c ^ top_p)}")
    for key in ("homie", "type"):
        if captured.description.get(key) != published.description.get(key):
            out.append(f"$description {key}")
    nodes_c, nodes_p = _nodes(captured), _nodes(published)
    if set(nodes_c) != set(nodes_p):
        out.append(f"nodes: {sorted(set(nodes_c) ^ set(nodes_p))}")
    for node in sorted(set(nodes_c) & set(nodes_p)):
        if nodes_c[node].get("type") != nodes_p[node].get("type"):
            out.append(f"node {node} type")
        decl_c, decl_p = _declarations(nodes_c[node]), _declarations(nodes_p[node])
        for key in sorted(set(decl_c) ^ set(decl_p)):
            if f"{node}/{key}" not in _NOT_COMPARED:
                where = "capture" if key in decl_c else "emitter"
                out.append(f"{node}/{key}: declared only by the {where}")
        for key in sorted(set(decl_c) & set(decl_p)):
            path = f"{node}/{key}"
            if path in _NOT_COMPARED:
                continue
            out.extend(
                f"{path} {attribute}: {decl_c[key].get(attribute)!r} vs "
                f"{decl_p[key].get(attribute)!r}"
                for attribute in _ATTRIBUTES
                if decl_c[key].get(attribute) != decl_p[key].get(attribute)
            )
            value_c, value_p = captured.properties.get(path), published.properties.get(path)
            if (value_c is None) != (value_p is None):
                where = "capture" if value_c is not None else "emitter"
                out.append(f"{path}: valued only by the {where}")
            elif value_c is not None and value_p is not None:
                shape_c, shape_p = _shape(value_c, decl_c[key]), _shape(value_p, decl_p[key])
                if shape_c != shape_p:
                    out.append(f"{path} shape: {shape_c} ({value_c}) vs {shape_p} ({value_p})")
                elif path in _COMPARED_BY_VALUE and value_c != value_p:
                    out.append(f"{path} value: {value_c} vs {value_p}")
                signs = (_sign(value_c), _sign(value_p))
                if _SIGNED.match(path) and all(signs) and signs[0] != signs[1]:
                    out.append(f"{path} sign: {value_c} vs {value_p}")
    return out


def _differences(captured: Tree, published: Tree) -> list[str]:
    roles_c, roles_p = _roles(captured), _roles(published)
    out = [f"device {role}: only in the capture" for role in sorted(set(roles_c) - set(roles_p))]
    out += [f"device {role}: only in the emitter" for role in sorted(set(roles_p) - set(roles_c))]
    for role in sorted(set(roles_c) & set(roles_p)):
        device_c, device_p = captured[roles_c[role]], published[roles_p[role]]
        if _children_by_type(captured, device_c) != _children_by_type(published, device_p):
            out.append(f"{role}: children by type")
        out += [f"{role}: {d}" for d in _device_differences(device_c, device_p)]
    return out


def _capture(handle: str) -> Tree:
    name = "main32-tree-v1.json" if handle == _MAIN32_R202633 else f"{handle}-tree-v1.json"
    return tree_from_snapshot(json.loads((_FIXTURES / name).read_text(encoding="utf-8")))


def _inputs(handle: str, captured: Tree) -> tuple[PanelDefinition, list[TickInputs]]:
    """The definition and ticks a capture is republished from."""
    if handle == _MAIN32_R202633:
        definition, _ = definition_from_tree(captured, mask=False)
        return definition, ticks_from_samples(captured, [(0.0, captured)], mask=False)
    return load_definition(_FIXTURES / f"{handle}.yaml"), load_ticks(
        _FIXTURES / f"{handle}.ticks.yaml"
    )


def _published(rec: PahoRecorder, definition: PanelDefinition, ticks: list[TickInputs]) -> Tree:
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    for tick in ticks:
        emitter.publish_tick(tick)
    return tree_from_retained(rec.retained)


def test_every_reference_capture_is_found() -> None:
    assert [f"r202639-{h}" for h in "abcde"] == _HANDLES
    for handle in _HANDLES:
        assert (_FIXTURES / f"{handle}.yaml").is_file()
        assert (_FIXTURES / f"{handle}.ticks.yaml").is_file()


@pytest.mark.parametrize("handle", [*_HANDLES, _MAIN32_R202639, _MAIN32_R202633])
def test_the_emitter_reproduces_the_capture_but_for_its_listed_exceptions(
    rec: PahoRecorder, handle: str
) -> None:
    captured = _capture(handle)
    differences = _differences(captured, _published(rec, *_inputs(handle, captured)))
    exceptions = _EXCEPTIONS.get(handle, {})
    unexplained = [d for d in differences if not any(d.startswith(e) for e in exceptions)]
    assert not unexplained, f"{len(unexplained)} differences:\n" + "\n".join(unexplained)
    stale = [e for e in exceptions if not any(d.startswith(e) for d in differences)]
    assert not stale, "exceptions that no longer occur:\n" + "\n".join(stale)
    assert len(differences) == len(exceptions), differences


@pytest.mark.parametrize("handle", [*_HANDLES, _MAIN32_R202639])
def test_every_unvalued_path_is_one_the_emitter_would_otherwise_publish(
    rec: PahoRecorder, handle: str
) -> None:
    """A definition's unvalued lists are minimal: republished without them, the
    emitter values every listed path, so none is implied by the profile, the
    variant's rules or an absent key."""
    definition, ticks = _inputs(handle, _capture(handle))
    listed = {
        (inst.instance_id, path)
        for inst in definition.manifest.instances
        for path in unvalued_paths(inst.metadata)
    }
    bare = dataclasses.replace(
        definition,
        manifest=DeviceManifest(
            instances=tuple(
                dataclasses.replace(
                    inst, metadata={k: v for k, v in inst.metadata.items() if k != "unvalued"}
                )
                for inst in definition.manifest.instances
            )
        ),
    )
    published = _published(rec, bare, ticks[:1])
    implied = sorted(
        (instance_id, path)
        for instance_id, path in listed
        if published[instance_id].properties.get(path) is None
    )
    assert not implied


def test_a_commissioned_value_is_compared_exactly_and_a_reading_by_shape() -> None:
    """Same shape, different values: a commissioned limit differs, a reading does not."""
    declared = {"datatype": "float", "unit": "A"}
    description = {
        "type": "energy.ebus.device.distribution-enclosure",
        "nodes": {
            "pcs": {"properties": {"off-grid-import-limit": declared}},
            "meter": {"properties": {"voltage-a": declared}},
        },
    }

    def device(limit: str, voltage: str) -> Device:
        return Device(
            description=description,
            properties={"pcs/off-grid-import-limit": limit, "meter/voltage-a": voltage},
        )

    assert _device_differences(device("47.9", "121.7"), device("48.0", "122.0")) == [
        "pcs/off-grid-import-limit value: 47.9 vs 48.0"
    ]
