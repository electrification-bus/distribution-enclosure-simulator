from ebus_panel_sim.manifest import DeviceInstance, DeviceManifest
from ebus_panel_sim.wire.graph_builder import BuiltGraph, build_graph
from ebus_panel_sim.wire.mapping_loader import load_mapping_table
from ebus_panel_sim.wire.profile_loader import load_profiles


def _manifest_panel_with_one_circuit(*, bess: bool = False) -> DeviceManifest:
    instances = [
        DeviceInstance(entity_class="panel", instance_id="p1", display_name="Span"),
        DeviceInstance(entity_class="circuit", instance_id="c1", display_name="Kitchen"),
    ]
    if bess:
        instances.append(DeviceInstance(entity_class="bess", instance_id="b1", display_name="Bat"))
    return DeviceManifest(instances=tuple(instances))


def _build(*, bess: bool = False) -> BuiltGraph:
    profiles = load_profiles()
    mapping = load_mapping_table()
    return build_graph(_manifest_panel_with_one_circuit(bess=bess), mapping, profiles, mqtt_cfg={})


def test_build_graph_for_panel_and_one_circuit() -> None:
    g = _build()
    # Under child-of-parent, the circuit is its own child Device beneath the panel.
    assert "p1" in g.devices
    assert "c1" in g.devices
    child = g.devices["c1"]
    assert child.parent_id() == "p1"
    assert child.root_id() == "p1"
    # The parent enclosure advertises its child devices (ebus-sdk description()).
    assert "c1" in g.devices["p1"].description()["children"]
    # Circuit's properties are present on the child device under plain capability nodes.
    assert ("circuit", "c1", "meter/active-power") in g.properties
    assert ("circuit", "c1", "switch/relay") in g.properties


def test_build_graph_is_deterministic() -> None:
    g1 = _build()
    g2 = _build()
    assert sorted(g1.properties.keys()) == sorted(g2.properties.keys())
    d1 = g1.devices["p1"].description()
    d2 = g2.devices["p1"].description()
    # description() restamps ``version`` each call, so compare the stable parts.
    assert d1["children"] == d2["children"]
    assert sorted(d1["nodes"]) == sorted(d2["nodes"])


def test_build_graph_includes_panel_settable_property() -> None:
    g = _build(bess=True)
    assert ("panel", "p1", "shed/asserted-islanding-state") in g.properties


def test_shed_and_shed_forecast_are_published_only_with_a_bess() -> None:
    """``devices/distribution-enclosure.md``: both are published only when at
    least one BESS is commissioned."""
    for node in ("shed", "shed-forecast"):
        assert node in _build(bess=True).devices["p1"].description()["nodes"]
        assert node not in _build().devices["p1"].description()["nodes"]
    assert not any(path.startswith("shed") for _ec, _id, path in _build().properties)
