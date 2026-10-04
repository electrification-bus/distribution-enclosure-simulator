"""The shipped example's two in-panel PV inverters on commissioned-system circuits."""

from __future__ import annotations

from pathlib import Path

from ebus_panel_sim import Emitter, SetterRegistry, load_definition, load_ticks

from .conftest import PahoRecorder

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_each_pv_inverter_is_its_own_device_named_by_its_feed_circuit(
    rec: PahoRecorder,
) -> None:
    definition = load_definition(_EXAMPLES / "forty_tab_minimal.yaml")
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    for tick in load_ticks(_EXAMPLES / "forty_tab_minimal.ticks.yaml"):
        emitter.publish_tick(tick)

    pvs = definition.manifest.of_class("pv")
    assert len(pvs) == 2
    for pv in pvs:
        feed = pv.metadata["feed"]
        assert pv.instance_id.startswith("example-40t-001-")
        assert pv.instance_id.endswith(f"-{feed}")
        assert rec.retained[f"ebus/5/{feed}/connection/feeds-device-id"] == pv.instance_id
        assert rec.retained[f"ebus/5/{feed}/load-shed/priority"] == "NEVER"
        assert rec.retained[f"ebus/5/{feed}/switch/relay-controllable"] == "false"
