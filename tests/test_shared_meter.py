"""Circuits that share a breaker's meter publish one reading (#68).

Under a variant whose circuit profile declares both ``meter/shared-with-device-ids``
and ``switch/shared-with-device-ids``, a multi-load breaker is published as one
circuit device per protected circuit, each naming its siblings in both lists, as
some SPAN panels publish it (spanio/SPAN-API-Client-Docs#21). The breaker has one
CT, so every one of those circuits publishes the same reading: the breaker's
combined power, current and energy. That is the eBus shared-hardware declaration
(``capabilities/meter.md`` §"Shared metering hardware"): the values describe the
whole group and its energy registers accumulate the group's flow once.

A producer gives each circuit its own load. A shared meter's power is their sum.
Its current, in one space, is the sum of their currents; across two spaces it is
the larger leg, a single-pole circuit's current lying on its own space and a
two-pole circuit's on both, as SPAN publishes a two-pole breaker's meter wherever
it publishes the shared lists (spanio/SPAN-API-Client-Docs#21). Each circuit keeps
its own ``breaker/rating`` and ``info/spaces``."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, get_args

import pytest

from ebus_panel_sim import (
    DeviceInstance,
    DeviceManifest,
    Emitter,
    ManifestValidationError,
    SetterRegistry,
    TickInputs,
    Variant,
    load_reference_capture,
    load_ticks,
)
from ebus_panel_sim.capture import (
    CaptureError,
    LiveCapture,
    definition_from_tree,
    main,
    ticks_from_samples,
    tree_from_retained,
)
from ebus_panel_sim.shared_hardware import declares_shared_breakers
from ebus_panel_sim.wire.profile_loader import ProfileTable, load_profiles

from .conftest import PahoRecorder
from .test_capture import _snapshot
from .test_definition import _stable

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _circuit(cid: str, tabs: str, rating: str, *shared: str, **md: str) -> DeviceInstance:
    return DeviceInstance(
        "circuit",
        cid,
        cid.title(),
        metadata={
            "tab-numbers": tabs,
            "breaker-rating-a": rating,
            "default-priority": "SOC_THRESHOLD",
            "relay-behavior": "controllable",
            "placement": "upstream-of-lugs",
            **({"shared-with-device-ids": ",".join(shared)} if shared else {}),
            **md,
        },
    )


def _manifest(*circuits: DeviceInstance) -> DeviceManifest:
    return DeviceManifest(
        instances=(
            DeviceInstance(
                "panel",
                "panel",
                "Panel",
                metadata={
                    "vendor-name": "SPAN",
                    "serial-number": "panel",
                    "firmware-version": "spanos3/r202639/03",
                    "hardware-version": "3.0",
                    "panel-size": "40",
                    "panel-model": "MAIN_40",
                    "postal-code": "00000",
                    "time-zone": "UTC",
                },
            ),
            DeviceInstance("lugs", "lugs-upstream", "Upstream lugs", {"direction": "upstream"}),
            *circuits,
            _circuit("other", "1", "20"),
        )
    )


def _tandem(rating_a: str = "20", rating_b: str = "20") -> DeviceManifest:
    """A tandem in space 16: two single-pole circuits behind one CT."""
    return _manifest(
        _circuit("hall", "16", rating_a, "porch"),
        _circuit("porch", "16", rating_b, "hall"),
    )


def _quad() -> DeviceManifest:
    """A quad across spaces 34 and 36: two two-pole circuits, each listing both."""
    return _manifest(
        _circuit("dryer", "34,36", "30", "range"),
        _circuit("range", "34,36", "20", "dryer"),
    )


def _tandem_quad() -> DeviceManifest:
    """A tandem-quad across spaces 34 and 36: a single-pole circuit on each space
    and a two-pole circuit on both."""
    return _manifest(
        _circuit("left", "34", "15", "right", "heater"),
        _circuit("right", "36", "15", "left", "heater"),
        _circuit("heater", "34,36", "20", "left", "right"),
    )


def _published(
    rec: PahoRecorder,
    manifest: DeviceManifest,
    *ticks: dict[str, float],
    variant: Variant = "span-alpha-test-b2",
) -> dict[str, str]:
    emitter = Emitter(manifest, SetterRegistry(), variant=variant)
    emitter.start()
    for n, circuits in enumerate(ticks):
        emitter.publish_tick(
            TickInputs(current_time=3600.0 * n, grid_online=True, circuits=circuits)
        )
    return rec.retained


def _meter(retained: dict[str, str], cid: str) -> tuple[str, str]:
    return retained[f"ebus/5/{cid}/meter/active-power"], retained[f"ebus/5/{cid}/meter/current"]


@pytest.mark.parametrize(("rating_a", "rating_b"), [("20", "20"), ("15", "20")])
def test_a_tandem_s_circuits_publish_the_breaker_s_one_reading(
    rec: PahoRecorder, rating_a: str, rating_b: str
) -> None:
    """1.2 kW and 600 W behind one CT in one space: both circuits read 1.8 kW and
    15 A, each keeps its own rating and space, and the site counts the load once."""
    retained = _published(
        rec, _tandem(rating_a, rating_b), {"hall": 1200.0, "porch": 600.0, "other": 300.0}
    )
    assert _meter(retained, "hall") == _meter(retained, "porch") == ("-1800.0", "15.0")
    assert _meter(retained, "other") == ("-300.0", "2.5")
    assert retained["ebus/5/hall/breaker/rating"] == rating_a
    assert retained["ebus/5/porch/breaker/rating"] == rating_b
    for cid, sibling in (("hall", "porch"), ("porch", "hall")):
        assert retained[f"ebus/5/{cid}/info/spaces"] == "16"
        assert retained[f"ebus/5/{cid}/breaker/poles"] == "1"
        assert retained[f"ebus/5/{cid}/meter/shared-with-device-ids"] == sibling
        assert retained[f"ebus/5/{cid}/switch/shared-with-device-ids"] == sibling
    assert retained["ebus/5/panel/power-flows/site"] == "2100.0"


def test_a_shared_meter_s_energy_accumulates_once_and_is_published_on_every_circuit(
    rec: PahoRecorder,
) -> None:
    """An hour at 1.8 kW is 1.8 kWh on the breaker's one register, published on
    both circuits, not 1.2 and 0.6 kWh on two, and the panel's total of its
    circuits' registers counts it once."""
    emitter = Emitter(_tandem(), SetterRegistry(), variant="span-alpha-test-b2")
    emitter.start()
    loads = {"hall": 1200.0, "porch": 600.0, "other": 300.0}
    for t in (0.0, 3600.0):
        snapshot = emitter.publish_tick(
            TickInputs(current_time=t, grid_online=True, circuits=loads)
        )
    for cid in ("hall", "porch"):
        assert rec.retained[f"ebus/5/{cid}/meter/exported-energy"] == "1800.0"
        assert rec.retained[f"ebus/5/{cid}/meter/imported-energy"] == "0.0"
    assert rec.retained["ebus/5/other/meter/exported-energy"] == "300.0"
    assert snapshot.meter.main_meter_energy_consumed_wh == pytest.approx(2100.0)


@pytest.mark.parametrize("sign", [1.0, -1.0], ids=["consuming", "backfeeding"])
def test_a_shared_meter_counts_once_in_the_panel_s_totals(rec: PahoRecorder, sign: float) -> None:
    """An hour at 1.8 kW through a tandem downstream of the lugs is 1.8 kWh in the
    panel's consumed or produced total, and in its feedthrough's, whichever way it
    flows: the breaker's one register, counted once."""
    downstream = {"placement": "downstream-of-lugs"}
    manifest = _with(_with(_tandem(), "hall", **downstream), "porch", **downstream)
    emitter = Emitter(manifest, SetterRegistry(), variant="span-alpha-test-b2")
    emitter.start()
    loads = {"hall": sign * 1200.0, "porch": sign * 600.0}
    for t in (0.0, 3600.0):
        meter = emitter.publish_tick(
            TickInputs(current_time=t, grid_online=True, circuits=loads)
        ).meter
    flowed = (1800.0, 0.0) if sign > 0 else (0.0, 1800.0)
    assert (
        meter.main_meter_energy_consumed_wh,
        meter.main_meter_energy_produced_wh,
    ) == pytest.approx(flowed)
    assert (
        meter.feedthrough_energy_consumed_wh,
        meter.feedthrough_energy_produced_wh,
    ) == pytest.approx(flowed)


def test_seeding_one_circuit_seeds_the_meter_every_circuit_on_it_publishes(
    rec: PahoRecorder,
) -> None:
    emitter = Emitter(_tandem(), SetterRegistry(), variant="span-alpha-test-b2")
    emitter.seed_energy("porch", consumed_wh=750.0)
    emitter.start()
    emitter.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits={}))
    for cid in ("hall", "porch"):
        assert rec.retained[f"ebus/5/{cid}/meter/exported-energy"] == "750.0"


def test_a_shared_meter_s_register_starts_from_its_circuits_common_seed(
    rec: PahoRecorder,
) -> None:
    """Each circuit's seed is the breaker's register, as a captured panel publishes
    it on every circuit, so the register starts from it once, not from their sum."""
    seeded = _manifest(
        _circuit("hall", "16", "20", "porch", **{"initial-consumed-wh": "500.0"}),
        _circuit("porch", "16", "20", "hall", **{"initial-consumed-wh": "500.0"}),
    )
    loads = {"hall": 1200.0, "porch": 600.0}
    retained = _published(rec, seeded, loads, loads)
    for cid in ("hall", "porch"):
        assert retained[f"ebus/5/{cid}/meter/exported-energy"] == "2300.0"


@pytest.mark.spec_only
def test_a_quad_publishes_its_combined_power_and_the_larger_leg(rec: PahoRecorder) -> None:
    """Two two-pole circuits across 34 and 36 carry 2.4 kW and 1.2 kW at 240 V:
    each leg carries 15 A, which both circuits publish, each listing both spaces."""
    retained = _published(rec, _quad(), {"dryer": 2400.0, "range": 1200.0})
    assert _meter(retained, "dryer") == _meter(retained, "range") == ("-3600.0", "15.0")
    for cid in ("dryer", "range"):
        assert retained[f"ebus/5/{cid}/info/spaces"] == "34,36"
        assert retained[f"ebus/5/{cid}/breaker/poles"] == "2"
    assert retained["ebus/5/dryer/breaker/rating"] == "30"
    assert retained["ebus/5/range/breaker/rating"] == "20"


@pytest.mark.spec_only
@pytest.mark.parametrize(
    ("left", "right", "current"),
    [(1200.0, 600.0, "20.0"), (600.0, 1200.0, "20.0"), (0.0, 0.0, "10.0")],
)
def test_a_tandem_quad_publishes_the_larger_leg(
    rec: PahoRecorder, left: float, right: float, current: str
) -> None:
    """The two-pole circuit's 2.4 kW puts 10 A on each leg; each single-pole
    circuit's load adds to its own space's leg only. The meter reads the larger leg,
    whichever it is, not the sum of the two."""
    retained = _published(rec, _tandem_quad(), {"left": left, "right": right, "heater": 2400.0})
    power = f"-{2400.0 + left + right:.1f}"
    for cid in ("left", "right", "heater"):
        assert _meter(retained, cid) == (power, current)
    assert retained["ebus/5/left/info/spaces"] == "34"
    assert retained["ebus/5/right/info/spaces"] == "36"
    assert retained["ebus/5/heater/info/spaces"] == "34,36"
    assert retained["ebus/5/heater/meter/shared-with-device-ids"] == "left,right"


@pytest.mark.spec_only
def test_currents_on_one_space_add_with_their_signs(rec: PahoRecorder) -> None:
    """One CT sees the net current through it: a tandem circuit backfeeding 400 W
    offsets its sibling's 1.2 kW load rather than adding to it."""
    retained = _published(rec, _tandem(), {"hall": 1200.0, "porch": -400.0})
    for cid in ("hall", "porch"):
        assert _meter(retained, cid) == ("-800.0", "6.7")


@pytest.mark.parametrize("variant", [v for v in get_args(Variant) if v != "span-alpha-test-b2"])
def test_a_variant_that_declares_no_sharing_publishes_each_circuit_s_own_load(
    rec: PahoRecorder, variant: Variant
) -> None:
    """The span variant models MAIN 32, which publishes one circuit per space and no
    shared lists, and the reference variant declares none either: the metadata
    changes nothing they publish."""
    retained = _published(rec, _tandem(), {"hall": 1200.0, "porch": 600.0}, variant=variant)
    assert float(retained["ebus/5/hall/meter/active-power"]) == -1200.0
    assert float(retained["ebus/5/porch/meter/active-power"]) == -600.0
    assert not any("shared-with-device-ids" in topic for topic in retained)


@pytest.mark.parametrize(
    ("without", "declared"),
    [((), True), (("switch",), False), (("meter",), False), (("meter", "switch"), False)],
)
def test_a_variant_declares_shared_breakers_only_with_both_lists(
    without: tuple[str, ...], declared: bool
) -> None:
    """A multi-load breaker shares its meter and its relay, so a variant publishes
    one only if its circuit profile declares both lists."""
    profiles = load_profiles(variant="span-alpha-test-b2")
    circuit = profiles["circuit"]
    capabilities = {
        node: dataclasses.replace(
            cap,
            properties={
                k: v
                for k, v in cap.properties.items()
                if not (node in without and k == "shared-with-device-ids")
            },
        )
        for node, cap in circuit.capabilities.items()
    }
    table = ProfileTable(
        {**profiles, "circuit": dataclasses.replace(circuit, capabilities=capabilities)}
    )
    assert declares_shared_breakers(table) is declared


def _with(manifest: DeviceManifest, cid: str, **md: str) -> DeviceManifest:
    return DeviceManifest(
        instances=tuple(
            DeviceInstance(i.entity_class, i.instance_id, i.display_name, {**i.metadata, **md})
            if i.instance_id == cid
            else i
            for i in manifest.instances
        )
    )


@pytest.mark.parametrize(
    ("manifest", "match"),
    [
        (_with(_tandem(), "hall", **{"shared-with-device-ids": "nowhere"}), "nowhere"),
        (_with(_tandem(), "hall", **{"shared-with-device-ids": "lugs-upstream"}), "lugs-upstream"),
        (_with(_tandem(), "hall", **{"shared-with-device-ids": "porch,hall"}), "itself"),
        (
            _with(_tandem(), "hall", **{"shared-with-device-ids": "porch,other"}),
            "but porch lists hall",
        ),
        (_with(_tandem(), "porch", **{"initial-consumed-wh": "10.0"}), "initial-consumed-wh"),
        (_with(_tandem(), "porch", **{"initial-produced-wh": "10.0"}), "initial-produced-wh"),
        (_with(_tandem(), "porch", **{"default-priority": "NEVER"}), "default-priority"),
        (_with(_tandem(), "porch", **{"relay-behavior": "non-controllable"}), "relay's lock"),
        (_with(_tandem(), "porch", **{"always-on": "true"}), "relay's lock"),
        (_with(_tandem(), "porch", **{"placement": "downstream-of-lugs"}), "placement"),
        (
            _with(
                _with(_tandem(), "hall", **{"default-priority": "OFF_GRID"}),
                "porch",
                **{"default-priority": "OFF_GRID", "never-backup": "true"},
            ),
            "never-backup",
        ),
        (
            _manifest(
                _circuit("hall", "16", "20", "porch"), _circuit("porch", "22", "20", "hall")
            ),
            "spaces 16, 22",
        ),
        (
            _manifest(
                _circuit("left", "34", "15", "right"), _circuit("right", "36", "15", "left")
            ),
            "spaces 34, 36",
        ),
        (
            _manifest(
                _circuit("hall", "16", "20", "dryer"), _circuit("dryer", "22,24", "30", "hall")
            ),
            "spaces 16, 22, 24",
        ),
        (
            _manifest(
                _circuit("left", "34", "15", "right", "heater"),
                _circuit("right", "38", "15", "left", "heater"),
                _circuit("heater", "34,36", "20", "left", "right"),
            ),
            "spaces 34, 36, 38",
        ),
        (
            _manifest(
                _circuit("left", "34", "15", "wide"), _circuit("wide", "34,36,38", "30", "left")
            ),
            "spaces 34, 36, 38",
        ),
    ],
)
def test_a_shared_breaker_that_cannot_be_one_device_is_rejected(
    manifest: DeviceManifest, match: str
) -> None:
    """Every circuit names every other, as the declaration's symmetry requires,
    agrees on what one CT, one relay and one position fix (the register's seed, the
    relay's lock and priority, and the placement), and sits where one breaker can: in
    one space, or in the two spaces of a two-pole circuit on it."""
    with pytest.raises(ManifestValidationError, match=match):
        Emitter(manifest, SetterRegistry(), variant="span-alpha-test-b2")
    Emitter(manifest, SetterRegistry(), variant="span")


def test_a_shared_reading_is_split_equally_among_the_circuits_that_publish_it() -> None:
    """A panel publishes the breaker's reading on every circuit and nothing of each
    circuit's own load, so a recorded tick gives each an equal share, which add
    back to the reading."""
    tree = load_reference_capture("r202639-e").tree
    assert tree["circuit-52"].value("meter/active-power") == "0.1"
    (tick,) = ticks_from_samples(tree, [(0.0, tree)], mask=False)
    assert tick.circuits["circuit-52"] == tick.circuits["circuit-53"] == -0.05


def test_a_panel_with_tandems_republishes_each_breaker_s_reading(rec: PahoRecorder) -> None:
    """Captured, the r202639-e panel's three tandems republish the reading each
    published, on both of its circuits."""
    tree = load_reference_capture("r202639-e").tree
    definition, _ = definition_from_tree(tree, mask=False)
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    for tick in ticks_from_samples(tree, [(0.0, tree)], mask=False):
        emitter.publish_tick(tick)
    published = tree_from_retained(rec.retained)
    shared = [i for i, d in tree.items() if d.value("meter/shared-with-device-ids")]
    assert len(shared) == 6
    for cid in shared:
        for path in ("meter/active-power", "meter/current", "meter/exported-energy"):
            assert published[cid].value(path) == tree[cid].value(path), (cid, path)
        description = json.loads(rec.retained[f"ebus/5/{cid}/$description"])
        assert "shared-with-device-ids" in description["nodes"]["meter"]["properties"]


def _round_trip(
    rec: PahoRecorder, manifest: DeviceManifest, loads: dict[str, float]
) -> dict[str, tuple[object, object]]:
    """What a panel publishing ``loads`` and its capture, rebuilt and driven by the
    tick sampled from it, publish differently: topic -> (panel's, rebuilt's)."""
    emitter = Emitter(manifest, SetterRegistry(), variant="span-alpha-test-b2")
    emitter.start()
    emitter.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits=loads))
    original = _stable(rec.retained)
    tree = tree_from_retained(rec.retained)
    definition, _ = definition_from_tree(tree, mask=False)
    (tick,) = ticks_from_samples(tree, [(0.0, tree)], mask=False)
    rec.reset()
    rebuilt = Emitter.from_definition(definition, SetterRegistry())
    rebuilt.start()
    rebuilt.publish_tick(tick)
    republished = _stable(rec.retained)
    return {
        topic: (original.get(topic), republished.get(topic))
        for topic in original.keys() | republished.keys()
        if original.get(topic) != republished.get(topic)
    }


@pytest.mark.parametrize(
    ("manifest", "loads"),
    [
        pytest.param(_tandem(), {"hall": 1200.0, "porch": 600.0, "other": 300.0}, id="tandem"),
        pytest.param(
            _quad(),
            {"dryer": 2400.0, "range": 1200.0, "other": 300.0},
            id="quad",
            marks=pytest.mark.spec_only,
        ),
    ],
)
def test_a_panel_with_a_shared_breaker_captures_and_rebuilds(
    rec: PahoRecorder, manifest: DeviceManifest, loads: dict[str, float]
) -> None:
    """Each circuit's share of the breaker's reading republishes it exactly: in one
    space, or across two where every circuit is two-pole, the meter reads the same
    however the load is split, and with both circuits drawing the same way so do the
    flows and lugs."""
    assert _round_trip(rec, manifest, loads) == {}


@pytest.mark.spec_only
def test_a_tandem_drawing_both_ways_rebuilds_its_meter_but_not_the_lugs_current(
    rec: PahoRecorder,
) -> None:
    """121 W drawn and 300 W backfed behind one CT read 179 W backfed, which the
    split gives as 89.5 W on each circuit. The meters and the flows republish, but
    the lugs, adding each circuit's current whatever its direction, read the net
    1.5 A on the tandem's leg instead of the 1.0 A and 2.5 A it carried."""
    loads = {"hall": 121.0, "porch": -300.0, "other": 0.0}
    assert _round_trip(rec, _tandem(), loads) == {
        "ebus/5/lugs-upstream/meter/current-b": ("3.5", "1.5")
    }


def _tandem_tree(rec: PahoRecorder, **seeds: str) -> dict[str, str]:
    """The retained tree of a panel publishing a tandem, each circuit seeded with
    ``seeds``."""
    manifest = _manifest(
        _circuit("hall", "16", "20", "porch", **seeds),
        _circuit("porch", "16", "20", "hall", **seeds),
    )
    return dict(_published(rec, manifest, {"hall": 1200.0, "porch": 600.0}))


@pytest.mark.parametrize(
    ("variant", "seeds"),
    [
        ("span-alpha-test-b2", {"hall": ("500.0", "1000.0"), "porch": ("500.0", "1000.0")}),
        ("span", {"hall": ("500.0", "999.9"), "porch": ("499.9", "1000.0")}),
    ],
)
def test_circuits_read_a_moment_apart_are_seeded_with_their_breaker_s_latest_register(
    rec: PahoRecorder, variant: Variant, seeds: dict[str, tuple[str, str]]
) -> None:
    """Circuits on one breaker publish its one register, so a capture reading them
    a moment apart can see it 0.1 Wh apart; it only rises, so each is seeded with the
    largest any of them published, by value rather than as text. Under a variant
    that gives each circuit a register of its own, each keeps the one it published."""
    retained = _tandem_tree(
        rec, **{"initial-consumed-wh": "500.0", "initial-produced-wh": "1000.0"}
    )
    retained["ebus/5/porch/meter/exported-energy"] = "499.9"
    retained["ebus/5/hall/meter/imported-energy"] = "999.9"
    definition, _ = definition_from_tree(tree_from_retained(retained), variant=variant, mask=False)
    assert {
        i.instance_id: (i.metadata["initial-consumed-wh"], i.metadata["initial-produced-wh"])
        for i in definition.manifest.of_class("circuit")
        if i.instance_id in seeds
    } == seeds


@pytest.mark.parametrize(
    ("variant", "share"),
    [("span-alpha-test-b2", 900.0), ("span", 1800.0), ("reference", 1800.0)],
)
def test_only_a_variant_that_shares_breakers_splits_a_shared_reading(
    rec: PahoRecorder, variant: Variant, share: float
) -> None:
    """Under a variant declaring both ``shared-with-device-ids`` lists, each circuit on
    a tandem publishes the sum of their loads, so a capture splits the reading
    between them. A variant that publishes each circuit's own load gets each
    circuit's reading as it published it, as 0.10.0 recorded it, and republishes it
    on each."""
    tree = tree_from_retained(_tandem_tree(rec))
    definition, _ = definition_from_tree(tree, variant=variant, mask=False)
    (tick,) = ticks_from_samples(tree, [(0.0, tree)], mask=False, variant=variant)
    assert tick.circuits["hall"] == tick.circuits["porch"] == share
    rec.reset()
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    emitter.publish_tick(tick)
    for cid in ("hall", "porch"):
        assert rec.retained[f"ebus/5/{cid}/meter/active-power"] == "-1800.0", cid


@pytest.mark.parametrize(("argv", "share"), [((), 900.0), (("--variant", "span"), 1800.0)])
def test_the_command_line_records_ticks_for_the_definition_s_variant(
    rec: PahoRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
    share: float,
) -> None:
    """``--record`` splits a shared reading only for a definition whose variant
    shares breakers, as ``--variant`` chooses it."""
    tree = tree_from_retained(_tandem_tree(rec))
    live = LiveCapture(tree=tree, samples=[(0.0, tree)], complete=True)
    monkeypatch.setattr("ebus_panel_sim.capture.capture_live", lambda *args, **kwargs: live)
    ticks = tmp_path / "ticks.yaml"
    out = tmp_path / "panel.yaml"
    argv_all = ["--host", "h", "--insecure", "--no-mask", *argv, "--record", "1"]
    assert main([*argv_all, "--ticks-output", str(ticks), "-o", str(out)]) == 0
    (tick,) = load_ticks(ticks)
    assert tick.circuits["hall"] == tick.circuits["porch"] == share


def test_a_listed_id_is_read_without_the_spaces_around_it(rec: PahoRecorder) -> None:
    """As a definition's own ``shared-with-device-ids`` is read."""
    retained = _tandem_tree(rec)
    for cid, entries in (("hall", " porch"), ("porch", "hall ")):
        for node in ("meter", "switch"):
            retained[f"ebus/5/{cid}/{node}/shared-with-device-ids"] = entries
    definition, notes = definition_from_tree(tree_from_retained(retained), mask=False)
    assert {
        i.instance_id: i.metadata.get("shared-with-device-ids")
        for i in definition.manifest.of_class("circuit")
    } == {"hall": "porch", "porch": "hall", "other": None}
    assert not [n for n in notes if n.key == "shared-with-device-ids"]


def test_a_listed_circuit_missing_from_the_tree_is_dropped_with_a_note(
    rec: PahoRecorder,
) -> None:
    """A tandem whose circuits each list a circuit the tree does not hold, or
    themselves, is captured as the two it does hold, and a circuit listing only
    such entries as on a breaker of its own. Each note counts what was dropped
    rather than naming it, since the name may be an unmasked ID."""
    retained = _tandem_tree(rec)
    listed = {"hall": "porch,ghost", "porch": "hall,porch", "other": "ghost"}
    for cid, entries in listed.items():
        for node in ("meter", "switch"):
            retained[f"ebus/5/{cid}/{node}/shared-with-device-ids"] = entries
    tree = tree_from_retained(retained)
    definition, notes = definition_from_tree(tree)
    # Masked, so known by their space.
    by_tab = {i.metadata["tab-numbers"]: i for i in definition.manifest.of_class("circuit")}
    hall, porch = (
        i for i in definition.manifest.of_class("circuit") if i.metadata["tab-numbers"] == "16"
    )
    other = by_tab["1"]
    assert hall.metadata["shared-with-device-ids"] == porch.instance_id
    assert porch.metadata["shared-with-device-ids"] == hall.instance_id
    assert "shared-with-device-ids" not in other.metadata
    dropped = "1 of the 2 listed is not another circuit in the tree; dropped"
    assert sorted(
        (n.device, n.note) for n in notes if n.key == "shared-with-device-ids"
    ) == sorted(
        [
            (hall.instance_id, dropped),
            (porch.instance_id, dropped),
            (other.instance_id, "1 of the 1 listed is not another circuit in the tree; dropped"),
        ]
    )
    assert "ghost" not in repr(definition)
    (tick,) = ticks_from_samples(tree, [(0.0, tree)])
    assert tick.circuits[hall.instance_id] == tick.circuits[porch.instance_id] == 900.0


def test_a_circuit_missing_from_a_sample_leaves_its_breaker_s_reading_to_the_others(
    rec: PahoRecorder,
) -> None:
    """A circuit whose reading a sample lacks takes no share: the others on its
    breaker carry the reading, which the breaker's meter then republishes."""
    retained = _tandem_tree(rec)
    tree = tree_from_retained(retained)
    sample = tree_from_retained(_without(retained, "porch", "meter/active-power"))
    (tick,) = ticks_from_samples(tree, [(0.0, sample)], mask=False)
    assert tick.circuits == {"hall": 1800.0, "other": 0.0}


def _without(retained: dict[str, str], cid: str, *paths: str) -> dict[str, str]:
    return {k: v for k, v in retained.items() if k not in {f"ebus/5/{cid}/{p}" for p in paths}}


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (
            lambda r: _without(
                r, "porch", "meter/shared-with-device-ids", "switch/shared-with-device-ids"
            ),
            "disagree about which circuits share their breaker",
        ),
        (lambda r: {**r, "ebus/5/porch/load-shed/priority": "NEVER"}, "default-priority"),
    ],
    ids=["one-sided", "two-priorities"],
)
def test_a_tree_whose_breaker_cannot_be_one_device_is_a_capture_error(
    rec: PahoRecorder,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[dict[str, str]], dict[str, str]],
    match: str,
) -> None:
    """Circuits that do not name each other, or disagree on what one relay fixes,
    cannot be one breaker: the capture says so, and the command line exits 2. The
    span variant, which publishes no shared breaker, captures the same tree as
    0.10.0 did."""
    retained = change(_tandem_tree(rec))
    with pytest.raises(CaptureError, match=match):
        definition_from_tree(tree_from_retained(retained), mask=False)
    definition_from_tree(tree_from_retained(retained), variant="span", mask=False)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(_snapshot(retained)))
    assert main(["--from-snapshot", str(snapshot), "-o", str(tmp_path / "panel.yaml")]) == 2
    assert match in capsys.readouterr().err
