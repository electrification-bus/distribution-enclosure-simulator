"""Numbers are published in the panel's literal form: integer, N decimal places or
shortest, never -0.0 or an exponent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebus_panel_sim.exceptions import ProfileValidationError
from ebus_panel_sim.wire.literal import LiteralForm, LiteralKind, format_literal
from ebus_panel_sim.wire.profile_loader import ProfileTable, Variant, load_profiles

INT = LiteralForm("integer")
ONE = LiteralForm("fixed", 1)
TWO = LiteralForm("fixed", 2)
SHORTEST = LiteralForm("shortest")


@pytest.mark.parametrize(
    ("value", "form", "literal"),
    [
        (1481.2996763239476, ONE, "1481.3"),
        (60.04, TWO, "60.04"),
        (60.0, TWO, "60.00"),
        (2879.0, ONE, "2879.0"),
        (-1257.4, INT, "-1257"),
        (2.5, INT, "3"),
        (-2.5, INT, "-3"),
        (0.05, ONE, "0.1"),
        (-0.0, ONE, "0.0"),
        (-0.04, ONE, "0.0"),
        (-0.0, INT, "0"),
        (0.0, INT, "0"),
        (17468812.04, ONE, "17468812.0"),
        (1e-7, ONE, "0.0"),
        (1e16, INT, "10000000000000000"),
        (81.0, SHORTEST, "81"),
        (81, SHORTEST, "81"),
        (13.5, SHORTEST, "13.5"),
        (4640.25, SHORTEST, "4640.25"),
        (-0.0, SHORTEST, "0"),
        (1e-7, SHORTEST, "0.0000001"),
        (1e16, SHORTEST, "10000000000000000"),
    ],
)
def test_format_literal(value: float, form: LiteralForm, literal: str) -> None:
    assert format_literal(value, form) == literal


def _numeric(variant: Variant) -> dict[tuple[str, str], LiteralForm | None]:
    """Each integer or float property the variant declares, with its literal form."""
    table = load_profiles(variant=variant)
    return {
        (entity_class, f"{cap_name}/{key}"): prop.literal
        for entity_class, profile in table.items()
        for cap_name, cap in profile.capabilities.items()
        for key, prop in cap.properties.items()
        if prop.datatype in ("integer", "float")
    }


def test_every_number_the_span_variant_publishes_has_a_literal_form() -> None:
    # The span variant publishes no remote-ct: the emitter rejects one.
    missing = sorted(
        key for key, form in _numeric("span").items() if form is None and key[0] != "remote-ct"
    )
    assert not missing


def test_a_number_without_a_rounding_rule_in_the_captures_is_not_rounded() -> None:
    """The BESS capacity and PV nominal power show only integral samples, and the
    EVSE advertised current and the off-grid import limit show none."""
    numeric = _numeric("span")
    for key in (
        ("bess", "info/nameplate-capacity"),
        ("pv", "info/nominal-power"),
        ("evse", "meter/advertised-current"),
        ("panel", "pcs/off-grid-import-limit"),
    ):
        assert numeric[key] == SHORTEST, key


def test_an_integer_property_is_written_as_an_integer() -> None:
    table = load_profiles(variant="span")
    for profile in table.values():
        for cap in profile.capabilities.values():
            for prop in cap.properties.values():
                if prop.datatype == "integer":
                    assert prop.literal == INT


def test_the_reference_variant_declares_no_literal_form() -> None:
    assert all(form is None for form in _numeric("reference").values())


_BASE = {
    "$version": 1,
    "type": "energy.ebus.device.thing",
    "capabilities": {
        "meter": {
            "type": "energy.ebus.capability.meter",
            "properties": {
                "active-power": {"name": "Power", "datatype": "float", "unit": "W"},
                "label": {"name": "Label", "datatype": "string"},
                "count": {"name": "Count", "datatype": "integer"},
            },
        },
    },
}


def _load(tmp_path: Path, key: str, literal: object) -> ProfileTable:
    """The base above with a span overlay giving ``meter/<key>`` a literal form.
    Inline datatypes, so no catalog is consulted."""
    (tmp_path / "span").mkdir()
    (tmp_path / "thing.json").write_text(json.dumps(_BASE))
    overlay = {"capabilities": {"meter": {"properties": {key: {"literal": literal}}}}}
    (tmp_path / "span" / "thing.json").write_text(json.dumps(overlay))
    return load_profiles(tmp_path, variant="span", catalog_dir=tmp_path / "no-catalogs")


@pytest.mark.parametrize(
    ("literal", "form"),
    [
        ("integer", INT),
        ("0dp", LiteralForm("fixed", 0)),
        ("1dp", ONE),
        ("2dp", TWO),
        ("shortest", SHORTEST),
    ],
)
def test_a_profile_names_the_literal_form(tmp_path: Path, literal: str, form: LiteralForm) -> None:
    meter = _load(tmp_path, "active-power", literal)["thing"].capabilities["meter"]
    assert meter.properties["active-power"].literal == form
    assert meter.properties["label"].literal is None


@pytest.mark.parametrize("literal", ["int", "1 dp", "-1dp", "dp", 1, None])
def test_an_unknown_literal_form_is_rejected(tmp_path: Path, literal: object) -> None:
    with pytest.raises(ProfileValidationError, match="literal"):
        _load(tmp_path, "active-power", literal)


def test_a_literal_form_on_a_non_numeric_property_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProfileValidationError, match="literal"):
        _load(tmp_path, "label", "1dp")


@pytest.mark.parametrize(("kind", "places"), [("integer", 1), ("shortest", 2), ("fixed", -1)])
def test_a_form_takes_places_only_when_fixed(kind: LiteralKind, places: int) -> None:
    with pytest.raises(ValueError, match="places"):
        LiteralForm(kind, places)


@pytest.mark.parametrize("literal", ["1dp", "shortest"])
def test_an_integer_property_is_written_only_as_an_integer(tmp_path: Path, literal: str) -> None:
    with pytest.raises(ProfileValidationError, match="literal"):
        _load(tmp_path, "count", literal)
