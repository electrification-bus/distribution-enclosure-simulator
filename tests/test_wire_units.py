"""Units and datatypes survive to the wire $description.

Both are str-enums in the SDK whose *value* is the Homie wire string, so the
profile's strings must resolve by value. Both have been hand-maintained name
tables, and both broke the same way.

``Unit`` was first: the table mapped "V" to a non-existent "VOLT" member and had
no entry for "min", silently dropping those units from the published
$description. A conformance check against a live panel caught it, not this suite.

``PropertyDatatype`` was the same defect eleven lines further up the same file,
left behind when the unit fix landed: six of the SDK's nine datatypes listed, and
a silent ``.get(dt, STRING)`` for the rest, so ``datetime``, ``duration`` and
``color`` would publish as ``string``.

The lesson both times is that enumerating the members by hand is the bug, so the
sweeps below enumerate nothing: they take every unit and datatype reachable from
the shipped profiles and assert each round-trips to a member whose value equals
the profile string. The fixed six-unit case is kept as a named regression for the
original conformance failure, but it is no longer the coverage claim."""

from __future__ import annotations

import json

import pytest

from ebus_panel_sim import (
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    Emitter,
    SetterRegistry,
)
from ebus_panel_sim.exceptions import ProfileValidationError
from ebus_panel_sim.wire.graph_builder import _to_sdk_datatype, _to_sdk_unit
from ebus_panel_sim.wire.profile_loader import load_profiles

from .conftest import PahoRecorder


def _shipped(attr: str) -> set[str]:
    """Every distinct `attr` on every property of every shipped profile.

    Both variants, because `reference` and `span` select different properties and
    a unit or datatype reachable from either reaches the wire.
    """
    out: set[str] = set()
    for variant in ("span", "span-alpha-test-b2", "reference"):
        for profile in load_profiles(variant=variant).values():
            for cap in profile.capabilities.values():
                for prop in cap.properties.values():
                    value = getattr(prop, attr)
                    if value is not None:
                        out.add(value)
    return out


def test_every_unit_the_profiles_ship_resolves_by_value() -> None:
    """Derived, so a newly selected unit is covered without editing this file.

    The hand-written tuple this replaces listed six units; the profiles carry
    seven. `kWh` (bess soc/soe, info/nameplate-capacity) was already outside it,
    and `catalogs/breaker.json` vendors `kA`, which would drop the moment a
    profile selected it.
    """
    units = _shipped("unit")
    assert units, "no units found: the sweep is not reaching the profiles"
    unresolved = set()
    for unit in units:
        resolved = _to_sdk_unit(unit)
        if resolved is None or resolved.value != unit:
            unresolved.add(unit)
    assert not unresolved, f"units that do not survive to the wire: {sorted(unresolved)}"


def test_every_datatype_the_profiles_ship_resolves_by_value() -> None:
    """The datatype axis, which had no test at all until now."""
    datatypes = _shipped("datatype")
    assert datatypes, "no datatypes found: the sweep is not reaching the profiles"
    for dt in sorted(datatypes):
        assert _to_sdk_datatype(dt).value == dt


def test_every_datatype_the_vendored_catalogs_define_resolves() -> None:
    """Fail forward: catch a datatype the SDK cannot model while it is still only
    vendored, rather than when a profile finally selects it.

    This is the check that would have caught the `datetime` case before it
    reached anyone. `catalogs/grid.json` has carried two `datetime` properties
    all along, published as `string` had they ever been selected.
    """
    import json as _json
    from pathlib import Path

    catalogs = Path(__file__).resolve().parents[1] / "src/ebus_panel_sim/wire/catalogs"
    seen: set[str] = set()
    for path in sorted(catalogs.glob("*.json")):
        raw = _json.loads(path.read_text())
        for section in ("properties", "property_patterns"):
            for defn in (raw.get(section) or {}).values():
                if isinstance(defn, dict) and defn.get("datatype"):
                    seen.add(defn["datatype"])
    assert "datetime" in seen, "expected grid.json's datetime properties in the sweep"
    for dt in sorted(seen):
        assert _to_sdk_datatype(dt).value == dt


def test_to_sdk_unit_resolves_by_value() -> None:
    # "V" and "min" are the two the conformance gate caught; kept as a named
    # regression for that failure, not as the coverage claim.
    for wire in ("V", "min", "W", "A", "Wh", "%"):
        resolved = _to_sdk_unit(wire)
        assert resolved is not None
        assert resolved.value == wire
    assert _to_sdk_unit(None) is None
    assert _to_sdk_unit("not-a-real-unit") is None


def test_an_unmodelled_datatype_raises_rather_than_guessing() -> None:
    """A unit degrades to absent; a datatype must not degrade at all.

    `$datatype` is required and consumers validate payloads against it, so a
    wrong one produces a tree that is confidently wrong. The old table's silent
    `string` default is exactly what this forbids.
    """
    with pytest.raises(ProfileValidationError, match="not one the SDK models"):
        _to_sdk_datatype("not-a-real-datatype", where="panel meter/x")


def _manifest() -> DeviceManifest:
    return DeviceManifest(
        instances=(
            DeviceInstance(
                "panel",
                "abc-123",
                "Span Panel",
                metadata={
                    "vendor-name": "Span",
                    "serial-number": "abc-123",
                    "firmware-version": "sim/v0.1.0",
                    "hardware-version": "rev2",
                    "panel-size": "40",
                    "main-breaker-rating-a": "200",
                    "panel-model": "MAIN_40",
                    "postal-code": "94103",
                    "time-zone": "America/Los_Angeles",
                },
            ),
            DeviceInstance(
                "bess",
                "abc-123-bess",
                "Battery",
                metadata={"vendor-name": "Span", "nameplate-capacity-kwh": "13.5"},
            ),
        )
    )


def test_panel_description_carries_units_on_the_wire(rec: PahoRecorder) -> None:
    cfg = BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
    )
    Emitter(_manifest(), SetterRegistry(), bess_configs=(cfg,)).start()
    panel = json.loads(rec.retained["ebus/5/abc-123/$description"])
    nodes = panel["nodes"]
    assert nodes["meter"]["properties"]["voltage-a"]["unit"] == "V"
    assert nodes["meter"]["properties"]["voltage-b"]["unit"] == "V"
    assert nodes["shed-forecast"]["properties"]["total-time-remaining"]["unit"] == "min"
    assert (
        nodes["shed-forecast"]["properties"]["full-charge-time-to-priority-shed"]["unit"] == "min"
    )


def test_published_description_datatypes_are_ones_a_profile_declares(rec: PahoRecorder) -> None:
    """End-to-end: every `$datatype` on the wire is one some profile declares.

    The sweeps above test the conversion in isolation; this tests that what it
    produces is what a consumer receives, with no layer in between mangling it.

    Stated as a subset rather than a per-property equality on purpose. The same
    catalog property may legally carry different datatypes on different device
    classes: `info/model` is a `string` on bess/evse/mid/pv and an `enum` of panel
    models on panel, because a profile may narrow a catalog datatype. A naive
    equality check reads that as a bug (it did, on the first draft of this test).
    A subset still catches the failure that matters: `string` where the profile
    says `datetime` is a pair no profile declares.
    """
    cfg = BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
    )
    Emitter(_manifest(), SetterRegistry(), bess_configs=(cfg,)).start()

    declared: set[tuple[str, str, str]] = set()
    for variant in ("span", "span-alpha-test-b2", "reference"):
        for profile in load_profiles(variant=variant).values():
            for cap in profile.capabilities.values():
                for key, prop in cap.properties.items():
                    declared.add((cap.type, key, prop.datatype))

    on_wire: set[tuple[str, str, str]] = set()
    for topic, payload in rec.retained.items():
        if not topic.endswith("/$description"):
            continue
        for node in json.loads(payload).get("nodes", {}).values():
            cap_type = node.get("type")
            for key, prop in (node.get("properties") or {}).items():
                on_wire.add((cap_type, key, prop["datatype"]))

    assert len(on_wire) > 20, f"only {len(on_wire)} properties on the wire; sweep not reaching it"
    undeclared = on_wire - declared
    assert not undeclared, f"wire datatypes no profile declares: {sorted(undeclared)}"
