"""`commissioned-system`: the circuit a SPAN panel adds for a commissioned PV or
battery system.

Such a circuit locks both commissioning surfaces at once: the relay
(`relay-controllable = false`, no `$settable` on `switch/relay`, not shed, not
PCS-managed) and `load-shed/priority` (fixed at `NEVER`, no `$settable`)."""

from __future__ import annotations

import pytest

from ebus_panel_sim import (
    DeviceInstance,
    DeviceManifest,
    Emitter,
    ManifestValidationError,
    SetterRegistry,
    TickInputs,
)
from ebus_panel_sim.manifest_physics import ManifestPhysicsView

from .conftest import PahoRecorder
from .test_never_backup import _circuit, _graph, _panel, _property_declaration


def _system(cid: str, system: str = "pv", *, priority: str = "NEVER") -> DeviceInstance:
    inst = _circuit(cid, tabs="3", priority=priority)
    return DeviceInstance(
        "circuit", cid, cid, metadata={**inst.metadata, "commissioned-system": system}
    )


@pytest.mark.parametrize("system", ["pv", "backup"])
def test_both_surfaces_declare_no_settable(system: str) -> None:
    graph = _graph(_circuit("kitchen"), _system("sys", system))
    assert "settable" not in _property_declaration(graph, "sys", "switch", "relay")
    assert "settable" not in _property_declaration(graph, "sys", "load-shed", "priority")
    assert _property_declaration(graph, "kitchen", "switch", "relay")["settable"] is True
    assert _property_declaration(graph, "kitchen", "load-shed", "priority")["settable"] is True


def test_the_circuit_publishes_its_locked_state(rec: PahoRecorder) -> None:
    manifest = DeviceManifest(instances=(_panel(), _system("sys")))
    em = Emitter(manifest, SetterRegistry())
    em.start()
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"sys": -800.0})
    )
    assert rec.retained["ebus/5/sys/switch/relay-controllable"] == "false"
    assert rec.retained["ebus/5/sys/load-shed/priority"] == "NEVER"
    assert rec.retained["ebus/5/sys/switch/relay-requester"] == "CONFIGURATION"
    circuit = snap.circuits["sys"]
    assert circuit.pcs_managed is False
    assert circuit.is_sheddable is False
    assert circuit.is_never_backup is False


def test_relay_and_priority_sets_are_refused() -> None:
    setters = SetterRegistry()
    em = Emitter(DeviceManifest(instances=(_panel(), _system("sys"))), setters)
    em.start()
    for prop, value in (("switch/relay", "OPEN"), ("load-shed/priority", "OFF_GRID")):
        handler = setters.get("circuit", prop)
        assert handler is not None
        handler("circuit", "sys", prop, value)

    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"sys": -800.0})
    )
    assert snap.circuits["sys"].relay_state == "CLOSED"
    assert snap.circuits["sys"].priority == "NEVER"


def test_the_circuit_is_not_shed_when_the_panel_islands() -> None:
    em = Emitter(DeviceManifest(instances=(_panel(), _system("sys"))), SetterRegistry())
    em.start()
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=False, circuits={"sys": -800.0})
    )
    assert snap.circuits["sys"].relay_state == "CLOSED"


@pytest.mark.parametrize("priority", ["OFF_GRID", "SOC_THRESHOLD", "MUST_HAVE"])
def test_any_priority_but_never_is_rejected(priority: str) -> None:
    with pytest.raises(ManifestValidationError, match="commissioned-system"):
        ManifestPhysicsView(
            DeviceManifest(instances=(_panel(), _system("sys", priority=priority)))
        )


def test_an_unknown_system_is_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="commissioned-system"):
        ManifestPhysicsView(DeviceManifest(instances=(_panel(), _system("sys", "wind"))))


def test_the_physics_view_reads_the_key() -> None:
    view = ManifestPhysicsView(DeviceManifest(instances=(_panel(), _system("sys", "backup"))))
    circuit = view.circuit("sys")
    assert circuit.commissioned_system == "backup"
    assert circuit.always_on is True
    assert circuit.priority_locked is True
    assert circuit.never_backup is False
