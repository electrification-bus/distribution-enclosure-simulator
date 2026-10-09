"""The shipped example's two in-panel PV inverters on commissioned-system circuits."""

from __future__ import annotations

import json
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


def test_the_span_alpha_test_b2_example_publishes_that_panel_shape(rec: PahoRecorder) -> None:
    definition = load_definition(_EXAMPLES / "span_alpha_test_b2_minimal.yaml")
    assert definition.variant == "span-alpha-test-b2"
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    for tick in load_ticks(_EXAMPLES / "span_alpha_test_b2_minimal.ticks.yaml"):
        emitter.publish_tick(tick)

    circuits = [i.instance_id for i in definition.manifest.of_class("circuit")]
    assert circuits == ["circuit-1", "circuit-2", "circuit-3", "circuit-4"]
    assert not definition.manifest.of_class("pv")

    retained = rec.retained
    panel = "ebus/5/example-b2-001"
    assert retained[f"{panel}/info/hardware-version"] == "3.0"
    assert retained[f"{panel}/info/name"] == "Example Home"
    assert retained["ebus/5/lugs-upstream/connection/service-rating"] == "200"
    assert retained["ebus/5/circuit-1/meter/shared-with-device-ids"] == "circuit-2"
    assert retained["ebus/5/circuit-4/connection/feeds-role"] == "SOLAR"
    assert "ebus/5/remote-ct-1/meter/active-power" in retained
    assert retained["ebus/5/circuit-4/switch/relay-requester"] == "NONE"
    assert retained["ebus/5/circuit-3/connection/feeds-device-id"] == "evse"
    solar = json.loads(retained["ebus/5/circuit-4/$description"])
    assert "settable" not in solar["nodes"]["load-shed"]["properties"]["priority"]
    assert {t.rsplit("/", 1)[1] for t in retained if "/power-flows/" in t} == {
        "pv",
        "battery",
        "grid",
        "site",
    }
