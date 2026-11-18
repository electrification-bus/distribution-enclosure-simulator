"""A PV inverter no circuit feeds: AC-coupled solar upstream of the panel.

That production reaches the site beside the main lugs, never through a circuit, so
the producer gives it per tick in ``TickInputs.pv``, keyed by a ``pv`` instance whose
manifest has no ``feed``. Values are in the PV's own frame, positive while
producing."""

from __future__ import annotations

import math

import pytest

from ebus_panel_sim import (
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    EbusPanelPowerFlows,
    Emitter,
    EmitterStateError,
    RelayState,
    SetterRegistry,
    TickInputs,
    Variant,
)

from .conftest import PahoRecorder

_BESS = "bess"

PV_VARIANTS: tuple[Variant, ...] = ("reference", "span")
"""The variants that allow ``pv`` devices."""


def _circuit(cid: str, tab: int, **md: str) -> DeviceInstance:
    return DeviceInstance(
        "circuit",
        cid,
        cid.title(),
        metadata={
            "tab-numbers": str(tab),
            "breaker-rating-a": "20",
            "default-priority": "SOC_THRESHOLD",
            "relay-behavior": "controllable",
            "placement": "upstream-of-lugs",
            **md,
        },
    )


def _manifest(
    *, bess: dict[str, str] | None = None, battery: bool = True, pv_devices: bool = True
) -> DeviceManifest:
    """A panel with two load circuits and a SOLAR circuit, its upstream lugs, an
    AC-coupled battery upstream of them (unless ``bess`` replaces its metadata), and
    two ``pv`` devices: ``pv-up``, which no circuit feeds, and ``pv-roof``, which the
    SOLAR circuit does. Without ``battery`` there is no ``bess`` instance, and without
    ``pv_devices`` no ``pv`` instance."""
    pv = (
        [
            DeviceInstance(
                "pv",
                "pv-up",
                "Upstream PV",
                metadata={
                    "vendor-name": "Example",
                    "nominal-power-w": "7600",
                    "inverter-type": "ac-coupled",
                },
            ),
            DeviceInstance(
                "pv",
                "pv-roof",
                "Roof PV",
                metadata={
                    "vendor-name": "Example",
                    "nominal-power-w": "7600",
                    "inverter-type": "ac-coupled",
                    "feed": "solar",
                },
            ),
        ]
        if pv_devices
        else []
    )
    return DeviceManifest(
        instances=(
            DeviceInstance(
                "panel",
                "panel",
                "Panel",
                metadata={
                    "vendor-name": "Span",
                    "serial-number": "panel",
                    "firmware-version": "sim/v0.1.0",
                    "hardware-version": "rev2",
                    "panel-size": "40",
                    "panel-model": "MAIN_40",
                    "postal-code": "94103",
                    "time-zone": "America/Los_Angeles",
                    "islandable": "true",
                },
            ),
            DeviceInstance("lugs", "lugs-upstream", "Upstream lugs", {"direction": "upstream"}),
            _circuit("kitchen", 1),
            _circuit("laundry", 2),
            _circuit("solar", 5, **{"feeds-role": "SOLAR"}),
            *(
                [
                    DeviceInstance(
                        "bess",
                        _BESS,
                        "Battery",
                        metadata=bess
                        if bess is not None
                        else {
                            "vendor-name": "Example",
                            "nameplate-capacity-kwh": "13.5",
                            "relative-position": "UPSTREAM",
                            "inverter-type": "ac-coupled",
                        },
                    )
                ]
                if battery
                else []
            ),
            *pv,
        )
    )


def _emitter(manifest: DeviceManifest, variant: Variant = "span") -> Emitter:
    """Its battery, where ``manifest`` has one, charging and discharging at up to
    1 kW, half full."""
    config = BESSConfig(
        instance_id=_BESS, nameplate_capacity_kwh=13.5, max_charge_w=1000.0, max_discharge_w=1000.0
    )
    emitter = Emitter(
        manifest,
        SetterRegistry(),
        bess_configs=(config,) if manifest.of_class("bess") else (),
        variant=variant,
    )
    emitter.start()
    return emitter


def _sum(flows: EbusPanelPowerFlows) -> float:
    values = [flows.pv, flows.battery, flows.grid, flows.site]
    assert all(v is not None for v in values)
    return sum(v for v in values if v is not None)


@pytest.mark.parametrize("variant", PV_VARIANTS)
def test_an_uncircuited_inverter_s_production_is_published_as_pv(
    rec: PahoRecorder, variant: Variant
) -> None:
    """1.5 kW of load and 3 kW from an inverter upstream of the panel: the battery
    charges at its 1 kW limit and the other 500 W is exported, as a node balance in
    which pv is negative, a charging battery and an export positive, and the site its
    load. The lugs carry only the circuits."""
    _emitter(_manifest(), variant).publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 1200.0, "laundry": 300.0},
            pv={"pv-up": 3000.0},
        )
    )
    retained = rec.retained

    def published(device: str, path: str) -> float:
        return float(retained[f"ebus/5/{device}/{path}"])

    assert published("panel", "power-flows/pv") == -3000.0
    assert published("panel", "power-flows/battery") == 1000.0
    assert published("panel", "power-flows/grid") == 500.0
    assert published("panel", "power-flows/site") == 1500.0
    assert published("lugs-upstream", "meter/active-power") == 1500.0


@pytest.mark.parametrize("variant", PV_VARIANTS)
def test_on_grid_the_grid_sees_the_uncircuited_pv_beside_the_battery(variant: Variant) -> None:
    """On grid the grid is derived from the lugs and what reaches the site beside them,
    the battery's dispatch and this PV, never back-solved from the other three flows,
    and the four balance."""
    emitter = _emitter(_manifest(), variant)
    for t, pv in enumerate((0.0, 400.0, 2600.0, 6000.0)):
        snapshot = emitter.publish_tick(
            TickInputs(
                current_time=float(t),
                grid_online=True,
                circuits={"kitchen": 1200.0, "laundry": 300.0},
                pv={"pv-up": pv},
            )
        )
        (battery,) = snapshot.battery.values()
        upstream = snapshot.lugs["lugs-upstream"].active_power_w
        assert snapshot.meter.instant_grid_power_w == pytest.approx(
            upstream - (battery.active_power_w + pv)
        )
        assert snapshot.power_flows.pv == pytest.approx(-pv)
        assert _sum(snapshot.power_flows) == pytest.approx(0.0, abs=1e-9)


def test_the_battery_charges_from_an_uncircuited_inverter() -> None:
    """Self-consumption sees the inverter's production as solar: with more of it than
    load the battery charges, where without it the battery would cover the load."""
    with_pv = _emitter(_manifest()).publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 500.0},
            pv={"pv-up": 1200.0},
        )
    )
    without = _emitter(_manifest()).publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0})
    )
    assert with_pv.battery[_BESS].active_power_w == -700.0
    assert without.battery[_BESS].active_power_w == 500.0


@pytest.mark.parametrize("variant", PV_VARIANTS)
def test_uncircuited_pv_and_a_hybrid_battery_s_pv_add_up(variant: Variant) -> None:
    """A hybrid battery's inverter and an upstream inverter are two sources of the
    one pv, and both leave beside the lugs."""
    manifest = _manifest(
        bess={
            "vendor-name": "Example",
            "nameplate-capacity-kwh": "13.5",
            "relative-position": "UPSTREAM",
            "inverter-type": "hybrid",
        }
    )
    snapshot = _emitter(manifest, variant).publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 1200.0, "laundry": 300.0},
            bess_pv={_BESS: 1000.0},
            pv={"pv-up": 500.0},
        )
    )
    (battery,) = snapshot.battery.values()
    upstream = snapshot.lugs["lugs-upstream"].active_power_w
    assert snapshot.power_flows.pv == pytest.approx(-1500.0)
    assert snapshot.meter.instant_grid_power_w == pytest.approx(
        upstream - (battery.active_power_w + 1500.0)
    )
    assert _sum(snapshot.power_flows) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.parametrize("variant", PV_VARIANTS)
@pytest.mark.parametrize(
    ("pv", "match"),
    [
        ({"no-such-pv": 100.0}, "no-such-pv"),
        ({"kitchen": 100.0}, "kitchen"),
        ({"pv-roof": 100.0}, "fed by circuit 'solar'"),
        ({"pv-up": math.nan}, "finite"),
        ({"pv-up": math.inf}, "finite"),
    ],
)
def test_uncircuited_pv_that_is_refused_moves_nothing(
    rec: PahoRecorder, pv: dict[str, float], match: str, variant: Variant
) -> None:
    """The refused tick publishes nothing and moves nothing: the next tick leaves
    the energy registers and the battery's charge where an emitter that never saw
    the refused tick has them."""

    def tick(t: float, given: dict[str, float] | None = None) -> TickInputs:
        return TickInputs(
            current_time=t, grid_online=True, circuits={"kitchen": 500.0}, pv=given or {}
        )

    manifest = _manifest()
    emitter = _emitter(manifest, variant)
    first = emitter.publish_tick(tick(0.0))
    published = len(rec.published)
    with pytest.raises(EmitterStateError, match=match):
        emitter.publish_tick(tick(3600.0, pv))
    assert emitter.last_snapshot is first
    assert len(rec.published) == published

    after = emitter.publish_tick(tick(7200.0))
    untouched = _emitter(manifest, variant)
    untouched.publish_tick(tick(0.0))
    expected = untouched.publish_tick(tick(7200.0))
    assert after.circuits["kitchen"].consumed_energy_wh == pytest.approx(1000.0)
    assert (
        after.circuits["kitchen"].consumed_energy_wh
        == expected.circuits["kitchen"].consumed_energy_wh
    )
    assert after.battery[_BESS].soe_kwh == expected.battery[_BESS].soe_kwh
    assert after.lugs["lugs-upstream"].imported_energy_wh == (
        expected.lugs["lugs-upstream"].imported_energy_wh
    )
    assert after == expected


def test_uncircuited_pv_is_refused_where_the_variant_has_no_pv_device() -> None:
    """``span-alpha-test-b2`` rejects ``pv`` devices, so a manifest for it has none
    and there is no inverter for a tick to name."""
    emitter = _emitter(_manifest(pv_devices=False), "span-alpha-test-b2")
    with pytest.raises(EmitterStateError, match="pv-up"):
        emitter.publish_tick(
            TickInputs(
                current_time=0.0,
                grid_online=True,
                circuits={"kitchen": 500.0},
                pv={"pv-up": 100.0},
            )
        )
    assert emitter.last_snapshot is None


@pytest.mark.parametrize("variant", PV_VARIANTS)
@pytest.mark.parametrize(
    ("circuits", "pv"),
    [
        ({"kitchen": 300.0, "solar": -300.0}, {"pv-up": -300.0}),
        ({"kitchen": 300.0}, {"pv-up": 0.0}),
    ],
    ids=["cancelling", "zero"],
)
def test_a_zero_or_cancelling_uncircuited_pv_never_publishes_negative_zero(
    rec: PahoRecorder, variant: Variant, circuits: dict[str, float], pv: dict[str, float]
) -> None:
    """The solar circuit's output and the inverter's standby draw cancel, or the
    inverter is given 0 W beside no other solar: pv reads zero, never -0.0, which
    the reference variant, writing numbers as they are, would publish if it negated
    a sum that came to zero."""
    _emitter(_manifest(), variant).publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits=circuits, pv=pv)
    )
    published = rec.retained["ebus/5/panel/power-flows/pv"]
    assert published != "-0.0"
    assert float(published) == 0.0


@pytest.mark.parametrize("variant", PV_VARIANTS)
def test_without_a_battery_the_grid_takes_what_the_uncircuited_pv_leaves(
    rec: PahoRecorder, variant: Variant
) -> None:
    """With no battery to charge, the 1.5 kW the inverter produces beyond the load is
    exported: the grid is the lugs less this PV alone, and the four flows balance.
    The lugs carry only the circuits."""
    emitter = _emitter(_manifest(battery=False), variant)
    emitter.publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 1200.0, "laundry": 300.0},
            pv={"pv-up": 3000.0},
        )
    )
    retained = rec.retained

    def published(device: str, path: str) -> float:
        return float(retained[f"ebus/5/{device}/{path}"])

    flows = {
        flow: published("panel", f"power-flows/{flow}")
        for flow in ("pv", "battery", "grid", "site")
    }
    assert flows == {"pv": -3000.0, "battery": 0.0, "grid": 1500.0, "site": 1500.0}
    assert sum(flows.values()) == 0.0
    assert published("lugs-upstream", "meter/active-power") == 1500.0


def test_off_grid_uncircuited_pv_covers_load_before_the_battery() -> None:
    """Off grid the battery only discharges, so the flows balance while the PV is no
    more than the load, as here: the PV covers part of it and the battery the rest.
    Curtailing any surplus is the producer's, as generating it is."""
    flows = (
        _emitter(_manifest())
        .publish_tick(
            TickInputs(
                current_time=0.0,
                grid_online=False,
                circuits={"kitchen": 900.0},
                pv={"pv-up": 400.0},
            )
        )
        .power_flows
    )
    assert flows.pv == -400.0
    assert flows.battery == -500.0
    assert flows.grid == 0.0
    assert flows.site == 900.0
    assert _sum(flows) == pytest.approx(0.0, abs=1e-9)


def test_an_open_relay_does_not_gate_uncircuited_pv() -> None:
    """No circuit carries this PV, so no circuit's relay gates it: with the only
    load's relay open, the PV still counts, the site reads nothing and the four
    flows balance."""
    emitter = _emitter(_manifest())
    emitter.relays.set_user_override("kitchen", RelayState.OPEN)
    flows = emitter.publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 500.0},
            pv={"pv-up": 800.0},
        )
    ).power_flows
    assert flows.pv == -800.0
    assert flows.site == 0.0
    assert _sum(flows) == pytest.approx(0.0, abs=1e-9)
