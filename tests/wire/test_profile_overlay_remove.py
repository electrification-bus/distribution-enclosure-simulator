"""An overlay ``null`` removes a base capability or property (JSON Merge Patch).

Profiles here carry inline datatypes, so no catalog is consulted."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ebus_panel_sim.exceptions import ProfileValidationError
from ebus_panel_sim.wire.profile_loader import ProfileTable, load_profiles

_BASE = {
    "$version": 1,
    "type": "energy.ebus.device.thing",
    "capabilities": {
        "meter": {
            "type": "energy.ebus.capability.meter",
            "properties": {
                "active-power": {"name": "Power", "datatype": "float", "unit": "W"},
                "frequency": {"name": "Frequency", "datatype": "float", "unit": "Hz"},
            },
        },
        "switch": {
            "type": "energy.ebus.capability.switch",
            "properties": {
                "requester": {"name": "Requester", "datatype": "enum", "format": "A,B,C"},
            },
        },
    },
}


def _load(tmp_path: Path, overlay: dict[str, Any]) -> ProfileTable:
    (tmp_path / "span").mkdir()
    (tmp_path / "thing.json").write_text(json.dumps(_BASE))
    (tmp_path / "span" / "thing.json").write_text(json.dumps(overlay))
    return load_profiles(tmp_path, variant="span", catalog_dir=tmp_path / "no-catalogs")


def test_a_null_capability_is_removed(tmp_path: Path) -> None:
    profile = _load(tmp_path, {"capabilities": {"meter": None}})["thing"]
    assert list(profile.capabilities) == ["switch"]


def test_a_null_property_is_removed(tmp_path: Path) -> None:
    profile = _load(tmp_path, {"capabilities": {"meter": {"properties": {"frequency": None}}}})[
        "thing"
    ]
    assert list(profile.capabilities["meter"].properties) == ["active-power"]


def test_the_reference_variant_keeps_what_the_overlay_removes(tmp_path: Path) -> None:
    _load(tmp_path, {"capabilities": {"meter": None}})
    reference = load_profiles(tmp_path, variant="reference", catalog_dir=tmp_path / "none")
    assert "meter" in reference["thing"].capabilities


def test_an_enum_is_narrowed_by_overriding_its_format(tmp_path: Path) -> None:
    overlay = {"capabilities": {"switch": {"properties": {"requester": {"format": "A,C"}}}}}
    prop = _load(tmp_path, overlay)["thing"].capabilities["switch"].properties["requester"]
    assert prop.format == "A,C"


@pytest.mark.parametrize(
    ("overlay", "match"),
    [
        ({"capabilities": {"door": None}}, "removes capability 'door'"),
        (
            {"capabilities": {"meter": {"properties": {"voltage": None}}}},
            "removes meter/voltage",
        ),
        (
            {"capabilities": {"switch": {"properties": {"requester": None}}}},
            "leaves capability 'switch' with no properties",
        ),
    ],
)
def test_a_removal_that_cannot_apply_is_rejected(
    tmp_path: Path, overlay: dict[str, Any], match: str
) -> None:
    with pytest.raises(ProfileValidationError, match=match):
        _load(tmp_path, overlay)
