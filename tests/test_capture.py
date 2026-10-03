"""Capture: a published tree -> a panel definition that reproduces it."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import yaml

from ebus_panel_sim import (
    BESSConfig,
    Emitter,
    PanelDefinition,
    SetterRegistry,
    TickInputs,
    load_definition,
)
from ebus_panel_sim.capture import (
    definition_from_tree,
    main,
    tree_from_retained,
    tree_from_snapshot,
)
from ebus_panel_sim.definition import definition_to_dict

from .conftest import PahoRecorder
from .test_definition import _EXAMPLES, _example, _stable

_TICK = TickInputs(
    current_time=0.0,
    grid_online=True,
    circuits={"placeholder": 0.0},
)


def _source() -> PanelDefinition:
    """The shipped example, with what a tree cannot carry set to capture's defaults:
    branch circuits in the panel, and default BESS dispatch."""
    example = _example()
    profile = example._load_profile(_EXAMPLES / "forty_tab_minimal.yaml")
    for circuit in profile["circuits"]:
        circuit["placement"] = "upstream-of-lugs"
    manifest = example._build_manifest(profile)
    bess = example._build_bess_config(profile)
    return PanelDefinition(
        manifest=manifest,
        bess_configs=(
            BESSConfig(
                instance_id=bess.instance_id,
                nameplate_capacity_kwh=bess.nameplate_capacity_kwh,
                max_charge_w=5000.0,
                max_discharge_w=5000.0,
            ),
        ),
    )


def _publish(rec: PahoRecorder, definition: PanelDefinition) -> dict[str, str]:
    rec.reset()
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    emitter.publish_tick(_TICK)
    retained = dict(rec.retained)
    emitter.stop()
    return retained


def test_a_captured_definition_reproduces_the_tree(rec: PahoRecorder) -> None:
    original = _publish(rec, _source())
    captured, _notes = definition_from_tree(tree_from_retained(original), mask=False)
    assert _stable(_publish(rec, captured)) == _stable(original)


def test_a_reference_tree_captures_with_assigned_tabs(rec: PahoRecorder) -> None:
    """The reference variant publishes no info/spaces, so tabs are assigned."""
    source = dataclasses.replace(_source(), variant="reference")
    original = _publish(rec, source)
    captured, notes = definition_from_tree(tree_from_retained(original), variant="reference")
    assert "tab-numbers" in {n.key for n in notes}
    retained = _publish(rec, captured)

    def descriptions(tree: dict[str, str]) -> int:
        return sum(t.endswith("/$description") for t in tree)

    assert descriptions(retained) == descriptions(original)


def test_masking_replaces_every_serial_and_keeps_references(rec: PahoRecorder) -> None:
    source = _source()
    original = _publish(rec, source)
    captured, _ = definition_from_tree(tree_from_retained(original))
    text = yaml.safe_dump(definition_to_dict(captured))
    for inst in source.manifest.instances:
        serial = inst.metadata.get("serial-number")
        if serial:
            assert serial not in text
    ids = {i.instance_id for i in captured.manifest.instances}
    feeds = [i.metadata["feed"] for i in captured.manifest.instances if "feed" in i.metadata]
    assert feeds and set(feeds) <= ids
    panel = captured.manifest.of_class("panel")[0]
    assert panel.instance_id == "masked-panel"
    assert panel.metadata["postal-code"] == "00000"
    # The masked definition still builds and publishes.
    _publish(rec, captured)


def test_unrecoverable_values_are_reported(rec: PahoRecorder) -> None:
    _, notes = definition_from_tree(tree_from_retained(_publish(rec, _source())))
    keys = {n.key for n in notes}
    assert "bess dispatch" in keys
    assert "inverter-type" in keys


def _snapshot(retained: dict[str, str]) -> dict[str, Any]:
    """The retained tree as a tree-v1 snapshot, numbers under numeric_properties."""
    devices: dict[str, Any] = {}
    for device_id, device in tree_from_retained(retained).items():
        numeric = {
            k: v
            for k, v in device.properties.items()
            if (device.declaration(k) or {}).get("datatype") in ("integer", "float")
        }
        devices[device_id] = {
            "description": device.description,
            "properties": {k: v for k, v in device.properties.items() if k not in numeric},
            "numeric_properties": numeric,
        }
    return {"metadata": {"schema": "tree-v1"}, "devices": devices}


def test_a_tree_v1_snapshot_gives_the_same_definition(rec: PahoRecorder) -> None:
    original = _publish(rec, _source())
    from_retained, _ = definition_from_tree(tree_from_retained(original), mask=False)
    from_snapshot, _ = definition_from_tree(tree_from_snapshot(_snapshot(original)), mask=False)
    assert from_snapshot == from_retained


def test_the_command_line_writes_a_loadable_definition(rec: PahoRecorder, tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(_snapshot(_publish(rec, _source()))))
    out = tmp_path / "panel.yaml"
    assert main(["--from-snapshot", str(snapshot), "-o", str(out)]) == 0
    assert load_definition(out).manifest.of_class("panel")[0].instance_id == "masked-panel"
