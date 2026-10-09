"""Capture: a published tree -> a panel definition that reproduces it."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ebus_panel_sim import (
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    Emitter,
    LoadSheddingConfig,
    ManifestValidationError,
    PanelDefinition,
    SetterRegistry,
    TickInputs,
    dump_ticks,
    load_definition,
    load_ticks,
)
from ebus_panel_sim.capture import (
    CaptureError,
    capture_live,
    definition_from_tree,
    main,
    ticks_from_samples,
    tree_from_retained,
    tree_from_snapshot,
)

from .conftest import PahoRecorder
from .test_definition import _EXAMPLES, _stable

_TICK = TickInputs(
    current_time=0.0,
    grid_online=True,
    circuits={"placeholder": 0.0},
)


# The example's battery charge in tenths of a kWh, as the panel writes it (the
# example's 6.75 would come back as 6.8), so the charge a capture reads back is
# the one the source started from.
_BESS_CHARGE = {"initial-soe-kwh": "6.8"}


def _source() -> PanelDefinition:
    """The shipped example, with what a tree cannot carry set to capture's defaults:
    branch circuits in the panel, and default BESS dispatch. Load shedding uses the
    threshold the emitter's default shed policy publishes, which capture reads back.
    The battery starts at ``_BESS_CHARGE``."""
    example = load_definition(_EXAMPLES / "forty_tab_minimal.yaml")
    instances = tuple(
        DeviceInstance(
            i.entity_class,
            i.instance_id,
            i.display_name,
            {**i.metadata, "placement": "upstream-of-lugs"}
            if i.entity_class == "circuit"
            else {**i.metadata, **_BESS_CHARGE}
            if i.entity_class == "bess"
            else i.metadata,
        )
        for i in example.manifest.instances
    )
    (bess,) = example.bess_configs
    return PanelDefinition(
        manifest=DeviceManifest(instances=instances),
        bess_configs=(
            BESSConfig(
                instance_id=bess.instance_id,
                nameplate_capacity_kwh=bess.nameplate_capacity_kwh,
                max_charge_w=5000.0,
                max_discharge_w=5000.0,
            ),
        ),
        load_shedding=LoadSheddingConfig(soc_threshold_pct=20.0),
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
    values = [
        v
        for i in captured.manifest.instances
        for v in (i.instance_id, i.display_name, *i.metadata.values())
    ]
    kept = {i.instance_id for i in captured.manifest.instances}
    for inst in source.manifest.instances:
        serial = inst.metadata.get("serial-number")
        if serial:
            assert not any(serial in v for v in values), serial
        if inst.instance_id not in kept:
            assert inst.instance_id not in values, inst.instance_id
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


def _script(definition: PanelDefinition) -> list[TickInputs]:
    """Three ticks a minute apart: the battery link lost on the second, the grid
    down on the third."""
    circuits = [i.instance_id for i in definition.manifest.of_class("circuit")]
    feeds = {i.instance_id: i.metadata["feed"] for i in definition.manifest.of_class("evse")}
    battery = definition.bess_configs[0].instance_id
    out = []
    for n, online in enumerate((True, True, False)):
        powers = {cid: 100.0 * (k + 1) * (n + 1) for k, cid in enumerate(circuits)}
        powers[circuits[-1]] = -1500.0 * (n + 1)  # a backfeeding circuit
        if not online:
            # Shed circuits draw nothing: the emitter sizes battery dispatch on the
            # tick's circuit powers, before relay gating.
            for inst in definition.manifest.of_class("circuit"):
                if inst.metadata["default-priority"] == "OFF_GRID":
                    powers[inst.instance_id] = 0.0
        out.append(
            TickInputs(
                current_time=60.0 * n,
                grid_online=online,
                circuits=powers,
                # Off-grid the EVSE circuits are shed, so the chargers draw nothing.
                evse={e: powers.get(f, 0.0) if online else 0.0 for e, f in feeds.items()},
                bess_communication={battery: "LOST" if n == 1 else "OK"},
            )
        )
    return out


def _run(
    rec: PahoRecorder, definition: PanelDefinition, ticks: list[TickInputs]
) -> list[dict[str, str]]:
    """The retained tree after each tick."""
    rec.reset()
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    trees = []
    for tick in ticks:
        emitter.publish_tick(tick)
        trees.append(dict(rec.retained))
    emitter.stop()
    return trees


@pytest.mark.parametrize("mask", [False, True])
def test_recorded_ticks_replay_the_same_trees(rec: PahoRecorder, mask: bool) -> None:
    source = _source()
    script = _script(source)
    original = _run(rec, source, script)
    tree = tree_from_retained(original[0])
    samples = [
        (t.current_time, tree_from_retained(r)) for t, r in zip(script, original, strict=True)
    ]
    captured, _ = definition_from_tree(tree, mask=mask)
    replay = _run(rec, captured, ticks_from_samples(tree, samples, mask=mask))

    recorded = ticks_from_samples(tree, samples, mask=mask)
    assert [t.grid_online for t in recorded] == [True, True, False]
    assert [list(t.bess_communication.values()) for t in recorded] == [["OK"], ["LOST"], ["OK"]]
    if not mask:
        assert [_stable(r) for r in replay] == [_stable(r) for r in original]
    else:
        assert [len(r) for r in replay] == [len(r) for r in original]


def test_a_tick_recording_round_trips_through_a_file(tmp_path: Path) -> None:
    ticks = _script(_source())
    path = tmp_path / "ticks.yaml"
    dump_ticks(ticks, path)
    assert load_ticks(path) == ticks


def test_record_needs_a_live_capture(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--from-snapshot", "x.json", "--record", "3", "-o", str(tmp_path / "p.yaml")])


# --- review fixes ----------------------------------------------------------


def _with_instances(definition: PanelDefinition, *changes: Any) -> PanelDefinition:
    """``definition`` with instances replaced (same id) or appended."""
    by_id = {i.instance_id: i for i in definition.manifest.instances}
    for inst in changes:
        by_id[inst.instance_id] = inst
    return dataclasses.replace(
        definition, manifest=DeviceManifest(instances=tuple(by_id.values()))
    )


def _battery_on_a_breaker() -> PanelDefinition:
    source = _source()
    bess = source.manifest.of_class("bess")[0]
    feed = source.manifest.of_class("circuit")[0].instance_id
    md = {k: v for k, v in bess.metadata.items() if k != "relative-position"}
    return _with_instances(
        source,
        DeviceInstance(
            "bess",
            bess.instance_id,
            bess.display_name,
            {**md, "relative-position": "IN_PANEL", "feed": feed},
        ),
    )


def test_a_null_in_a_snapshot_is_an_unpublished_value(rec: PahoRecorder) -> None:
    snapshot = _snapshot(_publish(rec, _source()))
    circuit = next(
        d
        for d in snapshot["devices"].values()
        if d["description"]["type"] == "energy.ebus.device.circuit"
    )
    circuit["numeric_properties"]["breaker/rating"] = None
    definition, notes = definition_from_tree(tree_from_snapshot(snapshot), mask=False)
    values = [v for i in definition.manifest.instances for v in i.metadata.values()]
    assert "None" not in values
    assert "breaker-rating-a" in {n.key for n in notes}
    _publish(rec, definition)


def test_a_battery_on_a_breaker_stays_on_its_breaker(rec: PahoRecorder) -> None:
    source = _battery_on_a_breaker()
    original = _publish(rec, source)
    captured, _ = definition_from_tree(tree_from_retained(original), mask=False)
    bess = captured.manifest.of_class("bess")[0]
    assert bess.metadata["relative-position"] == "IN_PANEL"
    assert _stable(_publish(rec, captured)) == _stable(original)


def test_a_battery_circuit_is_left_out_of_recorded_ticks(rec: PahoRecorder) -> None:
    source = _battery_on_a_breaker()
    feed = source.manifest.of_class("bess")[0].metadata["feed"]
    retained = _publish(rec, source)
    tree = tree_from_retained(retained)
    (tick,) = ticks_from_samples(tree, [(0.0, tree)], mask=False)
    assert feed not in tick.circuits
    assert tick.circuits


def _foreign_battery(retained: dict[str, str]) -> dict[str, str]:
    """Another publisher's battery on the same broker, outside the panel's tree."""
    bess_topics = {
        t: v for t, v in retained.items() if t.startswith("ebus/5/bess/") and "/$" not in t
    }
    out = dict(retained)
    out["ebus/5/aaa-other-bess/$description"] = retained["ebus/5/bess/$description"]
    for t, v in bess_topics.items():
        out[t.replace("ebus/5/bess/", "ebus/5/aaa-other-bess/")] = v
    return out


def test_other_publishers_do_not_reach_the_ids_or_the_ticks(rec: PahoRecorder) -> None:
    retained = _foreign_battery(_publish(rec, _source()))
    tree = tree_from_retained(retained)
    captured, _ = definition_from_tree(tree)
    assert [i.instance_id for i in captured.manifest.of_class("bess")] == ["bess-1"]
    (tick,) = ticks_from_samples(tree, [(0.0, tree)])
    assert list(tick.bess_communication) == ["bess-1"]


def test_two_enclosures_need_a_root(rec: PahoRecorder) -> None:
    retained = _publish(rec, _source())
    other = {
        t.replace("ebus/5/example-40t-001/", "ebus/5/second-panel/"): v
        for t, v in retained.items()
        if t.startswith("ebus/5/example-40t-001/")
    }
    tree = tree_from_retained({**retained, **other})
    with pytest.raises(CaptureError, match="choose one with root"):
        definition_from_tree(tree)
    captured, _ = definition_from_tree(tree, root="example-40t-001", mask=False)
    assert captured.manifest.of_class("panel")[0].instance_id == "example-40t-001"


def test_a_mid_without_exactly_one_battery_is_reported_not_written(rec: PahoRecorder) -> None:
    source = _source()
    bess = source.manifest.of_class("bess")[0]
    second = DeviceInstance("bess", "bess-two", "Second battery", dict(bess.metadata))
    two = dataclasses.replace(
        _with_instances(source, second),
        bess_configs=(
            *source.bess_configs,
            dataclasses.replace(source.bess_configs[0], instance_id="bess-two"),
        ),
    )
    retained = _publish(
        rec,
        dataclasses.replace(
            two,
            manifest=DeviceManifest(
                instances=tuple(i for i in two.manifest.instances if i.entity_class != "mid")
            ),
        ),
    )
    # Re-add the published MID of the single-battery panel to the two-battery tree.
    single = _publish(rec, source)
    retained.update({t: v for t, v in single.items() if t.startswith("ebus/5/bess-mid/")})
    tree = tree_from_retained(retained)
    tree["example-40t-001"].description.setdefault("children", []).append("bess-mid")
    captured, notes = definition_from_tree(tree, mask=False)
    assert not captured.manifest.of_class("mid")
    assert "mid" in {n.key for n in notes}
    _publish(rec, captured)


def test_masking_names_the_panel_serial_once_and_leaves_short_ids_alone(
    rec: PahoRecorder,
) -> None:
    captured, _ = definition_from_tree(tree_from_retained(_publish(rec, _source())))
    panel = captured.manifest.of_class("panel")[0]
    assert panel.metadata["serial-number"] == "MASKED-PANEL"
    # The example's battery ID is "bess"; its display name keeps its own text.
    assert captured.manifest.of_class("bess")[0].display_name == "Battery"


@pytest.mark.parametrize(
    ("argv", "match"),
    [
        (["--host", "h", "-o", "x.yaml"], "--cafile"),
        (["--host", "h", "--insecure", "--username", "u", "-o", "x.yaml"], "--password"),
        (["--from-snapshot", "s.json", "--ticks-output", "t.yaml", "-o", "x.yaml"], "--record"),
        (["--host", "h", "--insecure", "--record", "-1", "-o", "x.yaml"], "negative"),
    ],
)
def test_the_command_line_rejects_unsafe_or_ignored_options(
    argv: list[str], match: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(argv)
    assert match in capsys.readouterr().err


def test_a_missing_snapshot_is_an_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["--from-snapshot", str(tmp_path / "nope.json"), "-o", str(tmp_path / "p.yaml")]) == 2
    )
    assert "error:" in capsys.readouterr().err


class _FakeDevice:
    def __init__(self, description: dict[str, Any], properties: dict[str, dict[str, str]]):
        self.description = description
        self.properties = properties
        self.is_root = "parent" not in description


class _FakeController:
    """Stands in for ``ebus_sdk.Controller``: serves a fixed set of devices."""

    devices: ClassVar[dict[str, _FakeDevice]] = {}
    complete: ClassVar[bool] = True
    last: ClassVar[_FakeController | None] = None

    def __init__(self, mqtt_cfg: dict[str, Any], root_device_id: str | None = None) -> None:
        self.mqtt_cfg = mqtt_cfg
        self.root_device_id = root_device_id
        type(self).last = self

    def set_on_property_changed_callback(self, callback: Any) -> None:
        del callback

    def start_discovery(self) -> None:
        pass

    def get_all_devices(self) -> dict[str, _FakeDevice]:
        return dict(self.devices)

    def is_tree_complete(self, root_id: str) -> bool:
        # A device that is not a distribution enclosure never completes, as a
        # foreign publisher's partial tree would not.
        enclosure = (
            self.devices[root_id].description.get("type", "").endswith("distribution-enclosure")
        )
        return self.complete and enclosure

    def stop(self) -> None:
        pass


def _fake(monkeypatch: pytest.MonkeyPatch, retained: dict[str, str], complete: bool) -> None:
    import ebus_sdk

    tree = tree_from_retained(retained)
    devices = {}
    for device_id, device in tree.items():
        props: dict[str, dict[str, str]] = {}
        for path, value in device.properties.items():
            node, prop = path.split("/", 1)
            props.setdefault(node, {})[prop] = value
        devices[device_id] = _FakeDevice(device.description, props)
    monkeypatch.setattr(_FakeController, "devices", devices)
    monkeypatch.setattr(_FakeController, "complete", complete)
    monkeypatch.setattr(ebus_sdk, "Controller", _FakeController)


def test_capture_live_reads_the_tree_and_passes_the_root(
    rec: PahoRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = _publish(rec, _source())
    _fake(monkeypatch, retained, complete=True)
    live = capture_live(
        "h", 1883, use_tls=False, root="example-40t-001", settle_s=0, record=2, interval_s=0
    )
    assert live.complete
    assert len(live.samples) == 2
    assert _FakeController.last is not None
    assert _FakeController.last.root_device_id == "example-40t-001"
    captured, _ = definition_from_tree(live.tree, mask=False)
    assert _stable(_publish(rec, captured)) == _stable(retained)


def test_capture_live_reports_an_incomplete_tree(
    rec: PahoRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake(monkeypatch, _publish(rec, _source()), complete=False)
    live = capture_live("h", 1883, use_tls=False, timeout_s=0.3, settle_s=0)
    assert not live.complete


def test_capture_live_on_an_empty_broker_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, {}, complete=False)
    with pytest.raises(CaptureError, match="no devices discovered"):
        capture_live("h", 1883, use_tls=False, timeout_s=0.3, settle_s=0)


def test_generic_names_number_circuits_in_tab_order(rec: PahoRecorder) -> None:
    source = _source()
    captured, _ = definition_from_tree(
        tree_from_retained(_publish(rec, source)), mask=False, generic_names=True
    )
    circuits = captured.manifest.of_class("circuit")
    by_tab = sorted(circuits, key=lambda i: int(i.metadata["tab-numbers"].split(",")[0]))
    assert [c.display_name for c in by_tab] == [f"Circuit {n}" for n in range(1, len(by_tab) + 1)]
    originals = {i.display_name for i in source.manifest.of_class("circuit")}
    assert not originals & {c.display_name for c in circuits}


def test_a_battery_with_no_connection_record_is_noted_not_moved_upstream(
    rec: PahoRecorder,
) -> None:
    retained = {
        t: v for t, v in _publish(rec, _source()).items() if "/connection/fed-by-device-" not in t
    }
    captured, notes = definition_from_tree(tree_from_retained(retained), mask=False)
    bess = captured.manifest.of_class("bess")[0]
    assert bess.metadata["relative-position"] == "IN_PANEL"
    assert "feed" not in bess.metadata
    assert "relative-position" in {n.key for n in notes}
    rebuilt = _publish(rec, captured)
    assert not any("/connection/fed-by-device-" in t for t in rebuilt)


def test_a_feed_outside_the_tree_is_noted_and_recording_survives(rec: PahoRecorder) -> None:
    retained = _publish(rec, _source())
    circuit = next(t.split("/")[2] for t in retained if t.endswith("/info/spaces"))
    retained[f"ebus/5/{circuit}/connection/feeds-device-id"] = "not-in-this-tree"
    tree = tree_from_retained(retained)
    _, notes = definition_from_tree(tree, mask=False)
    assert "feed" in {n.key for n in notes}
    (tick,) = ticks_from_samples(tree, [(0.0, tree)], mask=False)
    assert tick.circuits


def test_completeness_ignores_other_roots_on_the_broker(
    rec: PahoRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    retained = _publish(rec, _source())
    retained["ebus/5/foreign-bridge/$description"] = json.dumps(
        {"homie": "5.0", "type": "energy.ebus.device.bridge", "nodes": {}}
    )
    _fake(monkeypatch, retained, complete=True)
    live = capture_live("h", 1883, use_tls=False, timeout_s=0.5, settle_s=0)
    assert live.complete


def test_capture_live_refuses_a_username_without_a_password() -> None:
    with pytest.raises(CaptureError, match="needs a password"):
        capture_live("h", 1883, use_tls=False, username="u")


def _variant_source() -> PanelDefinition:
    """The span-alpha-test-b2 example with its circuits in the panel (placement is not
    published, and capture places circuits upstream-of-lugs), and its battery
    starting at ``_BESS_CHARGE``."""
    example = load_definition(_EXAMPLES / "span_alpha_test_b2_minimal.yaml")
    return dataclasses.replace(
        example,
        manifest=DeviceManifest(
            instances=tuple(
                DeviceInstance(
                    i.entity_class,
                    i.instance_id,
                    i.display_name,
                    {**i.metadata, "placement": "upstream-of-lugs"}
                    if i.entity_class == "circuit"
                    else {**i.metadata, **_BESS_CHARGE}
                    if i.entity_class == "bess"
                    else i.metadata,
                )
                for i in example.manifest.instances
            )
        ),
    )


def test_a_span_alpha_test_b2_panel_captures_and_rebuilds(rec: PahoRecorder) -> None:
    source = _variant_source()
    original = _publish(rec, source)
    captured, _ = definition_from_tree(tree_from_retained(original), mask=False)
    assert captured.variant == "span-alpha-test-b2"
    assert [i.instance_id for i in captured.manifest.of_class("remote-ct")] == ["remote-ct-1"]
    circuit = next(
        i for i in captured.manifest.of_class("circuit") if i.instance_id == "circuit-4"
    )
    assert circuit.metadata["feeds-role"] == "SOLAR"
    assert _stable(_publish(rec, captured)) == _stable(original)


def test_masking_a_span_alpha_test_b2_panel_masks_the_site_and_remaps_shared_breakers(
    rec: PahoRecorder,
) -> None:
    """The site's facts are replaced by placeholders, so the masked definition
    still values what the panel valued and reveals none of it."""
    source = load_definition(_EXAMPLES / "span_alpha_test_b2_minimal.yaml")
    original = _publish(rec, source)
    captured, _ = definition_from_tree(tree_from_retained(original))
    panel = captured.manifest.of_class("panel")[0]
    real = source.manifest.of_class("panel")[0].metadata
    for key in ("site-name", "address-lines", "locality", "region", "latitude", "longitude"):
        assert panel.metadata[key] != real[key], key
    assert panel.metadata["latitude"] == panel.metadata["longitude"] == "0.0"
    ids = {i.instance_id for i in captured.manifest.instances}
    for inst in captured.manifest.of_class("circuit"):
        for shared in inst.metadata.get("shared-with-device-ids", "").split(","):
            assert not shared or shared in ids
    _publish(rec, captured)


def test_a_reported_wifi_ssid_is_captured_and_masked(rec: PahoRecorder) -> None:
    source = _variant_source()
    panel = source.manifest.of_class("panel")[0]
    with_ssid = dataclasses.replace(
        source,
        manifest=DeviceManifest(
            instances=tuple(
                dataclasses.replace(i, metadata={**i.metadata, "wifi-ssid": "Home Network"})
                if i is panel
                else i
                for i in source.manifest.instances
            )
        ),
    )
    original = _publish(rec, with_ssid)
    assert original[f"ebus/5/{panel.instance_id}/status/wifi-ssid"] == "Home Network"
    unmasked, _ = definition_from_tree(tree_from_retained(original), mask=False)
    masked, _ = definition_from_tree(tree_from_retained(original))
    assert unmasked.manifest.of_class("panel")[0].metadata["wifi-ssid"] == "Home Network"
    assert masked.manifest.of_class("panel")[0].metadata["wifi-ssid"] == "masked-ssid"


@pytest.mark.parametrize("role", ["GENERATOR", "SUBPANEL", "MIXED"])
def test_a_role_the_variant_cannot_book_is_captured_with_a_note(role: str) -> None:
    """A panel with such a breaker is still captured: the definition keeps the role
    the panel published, and a note names the circuit and says the variant cannot
    book it yet, which is also why the definition will not load under it."""
    raw = json.loads(
        (Path(__file__).parent / "fixtures" / "r202639-b-tree-v1.json").read_text(encoding="utf-8")
    )
    tree = tree_from_snapshot(raw)
    branch = next(
        device_id
        for device_id, device in sorted(tree.items())
        if device.type == "circuit" and device.declares("info/spaces")
    )
    tree[branch].properties["connection/feeds-role"] = role

    definition, notes = definition_from_tree(tree)

    circuits = [i for i in definition.manifest.of_class("circuit") if "feeds-role" in i.metadata]
    assert [i.metadata["feeds-role"] for i in circuits] == [role]
    (note,) = [n for n in notes if n.key == "feeds-role"]
    assert note.device == circuits[0].instance_id
    assert role in note.note and "cannot book" in note.note
    with pytest.raises(ManifestValidationError, match=f"{circuits[0].instance_id}.*{role}"):
        Emitter.from_definition(definition, SetterRegistry())
