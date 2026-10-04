"""Capture against a real panel's tree: a masked tree-v1 snapshot of a MAIN_32.

`tests/fixtures/main32-tree-v1.json` is a live SPAN panel's tree (released
firmware) with every device ID, serial number, postal code and Wi-Fi SSID
replaced and circuit names made generic. The other capture tests start from the
simulator's own output; this one starts from hardware."""

from __future__ import annotations

import json
from pathlib import Path

from ebus_panel_sim import Emitter, SetterRegistry, TickInputs
from ebus_panel_sim.capture import (
    Tree,
    definition_from_tree,
    ticks_from_samples,
    tree_from_retained,
    tree_from_snapshot,
)

from .conftest import PahoRecorder

_FIXTURE = Path(__file__).parent / "fixtures" / "main32-tree-v1.json"
# Published by the panel, not yet modeled by the simulator.
_NOT_MODELED = frozenset({"connection/count"})


def _tree() -> Tree:
    return tree_from_snapshot(json.loads(_FIXTURE.read_text(encoding="utf-8")))


def _rebuilt(rec: PahoRecorder, mask: bool) -> Tree:
    definition, _ = definition_from_tree(_tree(), mask=mask)
    em = Emitter.from_definition(definition, SetterRegistry())
    em.start()
    circuits = {i.instance_id: 0.0 for i in definition.manifest.of_class("circuit")}
    em.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits=circuits))
    return tree_from_retained(rec.retained)


def _declared(tree: Tree, device_id: str) -> set[str]:
    nodes = tree[device_id].description.get("nodes", {})
    return {f"{n}/{p}" for n, body in nodes.items() for p in body.get("properties", {})}


def test_the_rebuilt_panel_declares_what_the_real_one_does(rec: PahoRecorder) -> None:
    real, rebuilt = _tree(), _rebuilt(rec, mask=False)
    assert set(rebuilt) == set(real)
    for device_id in real:
        assert rebuilt[device_id].type == real[device_id].type
        assert _declared(rebuilt, device_id) == _declared(real, device_id) - _NOT_MODELED, (
            device_id
        )


def test_the_masked_definition_loads_and_publishes(rec: PahoRecorder) -> None:
    rebuilt = _rebuilt(rec, mask=True)
    assert len(rebuilt) == len(_tree())


def test_capture_reports_what_the_real_panel_does_not_publish() -> None:
    definition, notes = definition_from_tree(_tree(), mask=False)
    keys = {n.key for n in notes}
    assert {"bess dispatch", "inverter-type", "fed-by"} <= keys
    assert definition.load_shedding is not None
    assert definition.load_shedding.soc_threshold_pct == 49.0


def test_a_sample_of_the_real_tree_becomes_a_replayable_tick(rec: PahoRecorder) -> None:
    tree = _tree()
    definition, _ = definition_from_tree(tree, mask=False)
    (tick,) = ticks_from_samples(tree, [(0.0, tree)], mask=False)
    assert set(tick.circuits) <= {i.instance_id for i in definition.manifest.of_class("circuit")}
    assert tick.bess_communication
    em = Emitter.from_definition(definition, SetterRegistry())
    em.start()
    em.publish_tick(tick)
