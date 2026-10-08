"""The MAIN 32 r202639 capture's definition, published, writes numbers as the panel does.

``tests/fixtures/main32_r202639-tree-v1.json`` is a masked MAIN 32 on
``spanos3/r202639/03``; ``main32_r202639.yaml`` and ``.ticks.yaml`` are the
definition and ticks recorded with it. The panel writes ``power-flows/grid``,
``pv`` and ``battery`` as integers and ``site`` with one decimal, every reading
with one decimal, the BESS nameplate capacity and the PV nominal power (both
integral here) without a point, and never ``-0.0`` or an exponent. Every
non-root device's ``$description.name`` is its device id."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import pytest

from ebus_panel_sim import Emitter, SetterRegistry, load_definition, load_ticks
from ebus_panel_sim.capture import Tree, tree_from_retained, tree_from_snapshot

from .conftest import PahoRecorder

_FIXTURES = Path(__file__).parent / "fixtures"
_PANEL = "masked-panel"
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def _published(rec: PahoRecorder) -> Tree:
    definition = load_definition(_FIXTURES / "main32_r202639.yaml")
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    for tick in load_ticks(_FIXTURES / "main32_r202639.ticks.yaml")[:5]:
        emitter.publish_tick(tick)
    return tree_from_retained(rec.retained)


def _numbers(tree: Tree) -> dict[tuple[str, str], dict[str, str]]:
    """Every published integer or float, by (device type, property path), per device."""
    out: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for device_id, device in tree.items():
        for path, value in device.properties.items():
            declaration = device.declaration(path)
            if declaration is not None and declaration.get("datatype") in ("integer", "float"):
                out[(device.type, path)][device_id] = value
    return out


def _form(literal: str) -> str:
    """``integer``, or the number of decimal places as ``<n>dp``."""
    return f"{len(literal.split('.', 1)[1])}dp" if "." in literal else "integer"


@pytest.fixture
def published(rec: PahoRecorder) -> Tree:
    return _published(rec)


def test_every_number_takes_the_form_the_capture_shows(published: Tree) -> None:
    capture = tree_from_snapshot(
        json.loads((_FIXTURES / "main32_r202639-tree-v1.json").read_text(encoding="utf-8"))
    )
    in_capture = {
        key: {_form(v) for v in values.values()} for key, values in _numbers(capture).items()
    }
    compared = 0
    for key, values in _numbers(published).items():
        if key not in in_capture:  # unvalued in the capture, so it shows no form
            continue
        for device_id, value in values.items():
            assert _form(value) in in_capture[key], (key, device_id, value)
            compared += 1
    assert compared > 100


def test_power_flows_are_integers_except_site(published: Tree) -> None:
    flows = published[_PANEL].properties
    for path in ("power-flows/grid", "power-flows/pv", "power-flows/battery"):
        assert _form(flows[path]) == "integer", (path, flows[path])
    assert _form(flows["power-flows/site"]) == "1dp", flows["power-flows/site"]


def test_integral_bess_capacity_and_pv_nominal_power_have_no_point(published: Tree) -> None:
    numbers = _numbers(published)
    capacity = numbers[("bess", "info/nameplate-capacity")]
    nominal = numbers[("pv", "info/nominal-power")]
    assert capacity and all(_form(v) == "integer" for v in capacity.values()), capacity
    assert nominal and all(_form(v) == "integer" for v in nominal.values()), nominal


# Not readings at one decimal: pcs/priority is an integer, and the capture leaves
# pcs/off-grid-import-limit unvalued, so it shows no form for it.
_NOT_READINGS = frozenset({"pcs/priority", "pcs/off-grid-import-limit"})


def test_every_reading_the_capture_values_has_one_decimal(published: Tree) -> None:
    readings = {
        key: values
        for key, values in _numbers(published).items()
        if key[1].startswith(("meter/", "soc/", "pcs/")) and key[1] not in _NOT_READINGS
    }
    assert readings
    for key, values in readings.items():
        for device_id, value in values.items():
            assert _form(value) == "1dp", (key, device_id, value)


def test_no_number_is_negative_zero_or_an_exponent(published: Tree) -> None:
    for values in _numbers(published).values():
        for value in values.values():
            assert _NUMBER.match(value), value
            assert not (value.startswith("-") and float(value) == 0), value


def test_every_circuit_names_info_spaces_as_the_panel_does(published: Tree) -> None:
    circuits = [d for d in published.values() if d.type == "circuit"]
    assert len(circuits) == 16
    for circuit in circuits:
        declaration = circuit.declaration("info/spaces")
        assert declaration is not None
        assert declaration["name"] == "Physical panel position(s) the circuit occupies"


def test_every_device_but_the_panel_is_named_by_its_id(published: Tree) -> None:
    assert published[_PANEL].description["name"] == "SPAN Panel eBus Adapter"
    for device_id, device in published.items():
        if device_id != _PANEL:
            assert device.description["name"] == device_id
    assert published["circuit-2"].properties["info/name"] == "Clothes-Dryer"
