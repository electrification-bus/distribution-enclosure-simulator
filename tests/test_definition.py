"""Panel definition files: the manifest plus emitter options, as versioned YAML."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from ebus_panel_sim import (
    Emitter,
    LoadSheddingConfig,
    ManifestValidationError,
    PanelDefinition,
    SetterRegistry,
    TickInputs,
    dump_definition,
    load_definition,
    load_ticks,
)
from ebus_panel_sim.definition import SCHEMA, TICKS_SCHEMA, definition_from_dict

from .conftest import PahoRecorder

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _example() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "run_forty_tab_minimal", _EXAMPLES / "run_forty_tab_minimal.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _example_definition() -> PanelDefinition:
    example = _example()
    profile = example._load_profile(_EXAMPLES / "forty_tab_minimal.yaml")
    return PanelDefinition(
        manifest=example._build_manifest(profile),
        bess_configs=(example._build_bess_config(profile),),
        load_shedding=LoadSheddingConfig(soc_threshold_pct=25.0),
    )


def test_a_definition_round_trips_through_a_file(tmp_path: Path) -> None:
    definition = _example_definition()
    path = tmp_path / "panel.yaml"
    dump_definition(definition, path)
    assert load_definition(path) == definition
    assert yaml.safe_load(path.read_text())["schema"] == SCHEMA


def test_yaml_scalars_become_the_strings_the_manifest_expects() -> None:
    definition = definition_from_dict(
        {
            "schema": SCHEMA,
            "devices": [
                {"class": "lugs", "id": "l1", "metadata": {"direction": "upstream", "x": 200}},
                {"class": "circuit", "id": "c1", "metadata": {"never-backup": True}},
            ],
        }
    )
    lugs, circuit = definition.manifest.instances
    assert lugs.metadata["x"] == "200"
    assert lugs.display_name == "l1"
    assert circuit.metadata["never-backup"] == "true"
    assert definition.variant == "span"
    assert definition.bess_configs == ()
    assert definition.load_shedding is None


def _minimal(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"schema": SCHEMA, "devices": [{"class": "panel", "id": "p1"}]}
    raw.update(overrides)
    return raw


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (_minimal(schema="panel-sim-definition/0"), "schema must be"),
        (_minimal(extra=1), "unknown keys \\['extra'\\]"),
        (_minimal(variant="span-hw9"), "variant must be one of"),
        (_minimal(devices=[]), "devices must be a non-empty list"),
        (_minimal(devices=[{"class": "panel", "id": "p1", "kind": "x"}]), "unknown keys"),
        (_minimal(devices=[{"class": "panel"}]), "'id' must be a non-empty string"),
        (_minimal(bess=[{"instance_id": "b"}]), "bess\\[0\\]"),
        (
            _minimal(
                bess=[
                    {
                        "instance_id": "b",
                        "nameplate_capacity_kwh": 13.5,
                        "max_charge_w": 1.0,
                        "max_discharge_w": 1.0,
                        "charge_mode": "eco",
                    }
                ]
            ),
            "charge_mode must be one of",
        ),
        (_minimal(load_shedding={"threshold": 1}), "unknown keys \\['threshold'\\]"),
    ],
)
def test_a_malformed_definition_is_rejected(raw: dict[str, Any], match: str) -> None:
    with pytest.raises(ManifestValidationError, match=match):
        definition_from_dict(raw)


def test_from_definition_publishes_what_direct_construction_does(
    rec: PahoRecorder, tmp_path: Path
) -> None:
    definition = _example_definition()
    path = tmp_path / "panel.yaml"
    dump_definition(definition, path)
    tick = TickInputs(current_time=0.0, grid_online=True, circuits={})

    direct = Emitter(
        definition.manifest,
        SetterRegistry(),
        bess_configs=definition.bess_configs,
        load_shedding_config=definition.load_shedding,
    )
    direct.start()
    direct.publish_tick(tick)
    expected = _stable(rec.retained)
    direct.stop()
    rec.reset()

    loaded = Emitter.from_definition(load_definition(path), SetterRegistry())
    loaded.start()
    loaded.publish_tick(tick)
    assert _stable(rec.retained) == expected


def _stable(retained: dict[str, str]) -> dict[str, object]:
    """The retained tree without each ``$description``'s timestamp ``version``."""
    out: dict[str, object] = {}
    for topic, payload in retained.items():
        if topic.endswith("/$description"):
            description = json.loads(payload)
            description.pop("version", None)
            out[topic] = description
        else:
            out[topic] = payload
    return out


def test_metadata_is_read_exactly_as_written(tmp_path: Path) -> None:
    """No YAML number, octal, sexagesimal or null resolution in metadata."""
    path = tmp_path / "panel.yaml"
    path.write_text(
        f"schema: {SCHEMA}\n"
        "devices:\n"
        "  - class: panel\n"
        "    id: p1\n"
        "    metadata:\n"
        "      postal-code: 02134\n"
        "      zero: 00000\n"
        "      hardware-version: 2.10\n"
        "      clock: 1:30\n"
        "      tilde: ~\n"
        "      flag: true\n"
    )
    md = load_definition(path).manifest.instances[0].metadata
    assert md == {
        "postal-code": "02134",
        "zero": "00000",
        "hardware-version": "2.10",
        "clock": "1:30",
        "tilde": "~",
        "flag": "true",
    }


def test_a_non_scalar_metadata_value_is_rejected() -> None:
    raw = _minimal(devices=[{"class": "panel", "id": "p1", "metadata": {"tags": ["a", "b"]}}])
    with pytest.raises(ManifestValidationError, match="must be a scalar"):
        definition_from_dict(raw)


def test_config_fields_are_converted_to_their_types(tmp_path: Path) -> None:
    path = tmp_path / "panel.yaml"
    path.write_text(
        f"schema: {SCHEMA}\n"
        "devices: [{class: panel, id: p1}]\n"
        'bess: [{instance_id: b, nameplate_capacity_kwh: "13.5", max_charge_w: 3500, '
        "max_discharge_w: 3500, charge_hours: [1, 2]}]\n"
        "load_shedding: {soc_threshold_pct: 25}\n"
    )
    definition = load_definition(path)
    (bess,) = definition.bess_configs
    assert bess.nameplate_capacity_kwh == 13.5
    assert bess.charge_hours == (1, 2)
    assert definition.load_shedding == LoadSheddingConfig(soc_threshold_pct=25.0)


def test_a_config_field_of_the_wrong_type_is_rejected() -> None:
    raw = _minimal(
        bess=[
            {
                "instance_id": "b",
                "nameplate_capacity_kwh": "lots",
                "max_charge_w": 1,
                "max_discharge_w": 1,
            }
        ]
    )
    with pytest.raises(ManifestValidationError, match="nameplate_capacity_kwh must be float"):
        definition_from_dict(raw)


def test_a_wrong_schema_is_reported_before_unknown_keys() -> None:
    with pytest.raises(ManifestValidationError, match="schema must be"):
        definition_from_dict({"schema": "other/1", "devices": [], "extra": 1})


def _ticks_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "ticks.yaml"
    path.write_text(f"schema: {TICKS_SCHEMA}\nticks:\n{body}")
    return path


def test_tick_fields_are_converted_strictly(tmp_path: Path) -> None:
    path = _ticks_file(
        tmp_path,
        "  - {current_time: 60, grid_online: false, circuits: {c1: 120}, "
        "bess_communication: {b: LOST}}\n",
    )
    (tick,) = load_ticks(path)
    assert tick.grid_online is False
    assert tick.current_time == 60.0
    assert tick.circuits == {"c1": 120.0}
    assert tick.bess_communication == {"b": "LOST"}


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('  - {current_time: 0, grid_online: "maybe"}\n', "grid_online must be bool"),
        ("  - {current_time: 0, grid_online: true, circuits: [1, 2]}\n", "circuits must be"),
        (
            "  - {current_time: 0, grid_online: true, bess_communication: {b: GONE}}\n",
            "bess_communication",
        ),
        ("  - {grid_online: true}\n", "missing 'current_time'"),
    ],
)
def test_a_malformed_tick_is_rejected(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(ManifestValidationError, match=match):
        load_ticks(_ticks_file(tmp_path, body))
