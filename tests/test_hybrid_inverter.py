"""A battery whose own inverter takes PV: solar DC-coupled into a hybrid inverter.

That solar reaches the panel through the battery's AC terminals, never through a
circuit, so the producer gives it per tick in ``TickInputs.bess_pv``, keyed by a
battery whose manifest says ``inverter-type: hybrid``. Reference capture
``r202639-f`` shows it: a panel reporting 3247 W of solar with no SOLAR circuit and
no pv device, its battery upstream of the lugs."""

from __future__ import annotations

import math
from typing import get_args

import pytest

from ebus_panel_sim import (
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    EbusPanelPowerFlows,
    Emitter,
    EmitterStateError,
    ManifestValidationError,
    SetterRegistry,
    TickInputs,
    Variant,
)

from .conftest import PahoRecorder

_BESS = "bess"


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


def _manifest(*, bess: dict[str, str] | None = None, solar: bool = False) -> DeviceManifest:
    """A panel with two load circuits, its upstream lugs and a battery upstream of
    them whose inverter takes PV, unless ``bess`` replaces the battery's metadata.
    With ``solar``, a SOLAR circuit too."""
    circuits = [_circuit("kitchen", 1), _circuit("laundry", 2), _circuit("garage", 3)]
    if solar:
        circuits.append(_circuit("solar", 5, **{"feeds-role": "SOLAR"}))
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
            *circuits,
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
                    "inverter-type": "hybrid",
                },
            ),
        )
    )


def _emitter(manifest: DeviceManifest, variant: Variant = "span-alpha-test-b2") -> Emitter:
    """Charging and discharging at up to 1 kW, half full."""
    config = BESSConfig(
        instance_id=_BESS, nameplate_capacity_kwh=13.5, max_charge_w=1000.0, max_discharge_w=1000.0
    )
    emitter = Emitter(manifest, SetterRegistry(), bess_configs=(config,), variant=variant)
    emitter.start()
    return emitter


def _sum(flows: EbusPanelPowerFlows) -> float:
    values = [flows.pv, flows.battery, flows.grid, flows.site]
    assert all(v is not None for v in values)
    return sum(v for v in values if v is not None)


@pytest.mark.parametrize("variant", get_args(Variant))
def test_the_inverter_s_pv_is_published_as_pv_with_no_solar_circuit(
    rec: PahoRecorder, variant: Variant
) -> None:
    """1.5 kW of load and 3 kW from the battery's inverter: the battery charges at its
    1 kW limit and the other 500 W is exported, as a node balance in which pv is
    negative, a charging battery and an export positive, and the site its load."""
    emitter = _emitter(_manifest(), variant)
    emitter.publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 1200.0, "laundry": 300.0},
            bess_pv={_BESS: 3000.0},
        )
    )
    retained = rec.retained

    def published(device: str, path: str) -> float:
        return float(retained[f"ebus/5/{device}/{path}"])

    assert published("panel", "power-flows/pv") == -3000.0
    assert published("panel", "power-flows/battery") == 1000.0
    assert published("panel", "power-flows/grid") == 500.0
    assert published("panel", "power-flows/site") == 1500.0
    # The battery's own frame (positive while discharging), so charging is negative.
    assert published(_BESS, "meter/active-power") == -1000.0
    # The upstream lugs, between the battery and the panel, carry only the circuits.
    assert published("lugs-upstream", "meter/active-power") == 1500.0


@pytest.mark.parametrize("variant", get_args(Variant))
def test_on_grid_the_flows_balance_and_the_grid_sees_the_pv_through_the_battery(
    variant: Variant,
) -> None:
    """On grid the grid is derived from the lugs and the battery's AC terminals,
    which carry its dispatch and its inverter's PV together, never back-solved from
    the other three flows, and the four balance."""
    emitter = _emitter(_manifest(), variant)
    for t, pv in enumerate((0.0, 400.0, 2600.0, 6000.0)):
        snapshot = emitter.publish_tick(
            TickInputs(
                current_time=float(t),
                grid_online=True,
                circuits={"kitchen": 1200.0, "laundry": 300.0},
                bess_pv={_BESS: pv},
            )
        )
        (battery,) = snapshot.battery.values()
        upstream = snapshot.lugs["lugs-upstream"].active_power_w
        assert snapshot.meter.instant_grid_power_w == pytest.approx(
            upstream - (battery.active_power_w + pv)
        )
        assert snapshot.power_flows.pv == pytest.approx(-pv)
        assert _sum(snapshot.power_flows) == pytest.approx(0.0, abs=1e-9)


def test_the_battery_charges_from_its_inverter_s_pv() -> None:
    """Self-consumption sees the inverter's PV as solar: with more of it than load
    the battery charges, where without it the battery would cover the load."""
    with_pv = _emitter(_manifest()).publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 500.0},
            bess_pv={_BESS: 1200.0},
        )
    )
    without = _emitter(_manifest()).publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0})
    )
    assert with_pv.battery[_BESS].active_power_w == -700.0
    assert without.battery[_BESS].active_power_w == 500.0
    assert without.power_flows.pv == 0.0


@pytest.mark.spec_only
def test_a_standby_draw_on_the_pv_input_reads_as_a_positive_pv() -> None:
    """At night the inverter's PV side may draw a few watts. pv then reads positive,
    as a SOLAR circuit's standby draw does, and the battery covers it."""
    flows = (
        _emitter(_manifest())
        .publish_tick(
            TickInputs(
                current_time=0.0,
                grid_online=True,
                circuits={"kitchen": 500.0},
                bess_pv={_BESS: -5.0},
            )
        )
        .power_flows
    )
    assert flows.pv == 5.0
    assert flows.battery == -505.0
    assert _sum(flows) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.spec_only
def test_solar_circuits_and_the_inverter_s_pv_add_up() -> None:
    """A SOLAR circuit and a hybrid battery's inverter are two sources of the one pv."""
    flows = (
        _emitter(_manifest(solar=True))
        .publish_tick(
            TickInputs(
                current_time=0.0,
                grid_online=True,
                circuits={"kitchen": 500.0, "solar": -2000.0},
                bess_pv={_BESS: 1000.0},
            )
        )
        .power_flows
    )
    assert flows.pv == -3000.0
    assert flows.site == 500.0
    assert _sum(flows) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.spec_only
def test_a_hybrid_battery_on_a_breaker_balances_on_grid_too() -> None:
    """A battery in the panel, on its own breaker, delivers its inverter's PV through
    that breaker, which reads 0 in ticks as captured ticks leave it: on grid the
    battery's flows account for it."""
    manifest = _manifest(
        bess={
            "vendor-name": "Example",
            "nameplate-capacity-kwh": "13.5",
            "relative-position": "IN_PANEL",
            "feed": "garage",
            "inverter-type": "hybrid",
        }
    )
    flows = (
        _emitter(manifest)
        .publish_tick(
            TickInputs(
                current_time=0.0,
                grid_online=True,
                circuits={"kitchen": 800.0, "garage": 0.0},
                bess_pv={_BESS: 2500.0},
            )
        )
        .power_flows
    )
    assert flows.pv == -2500.0
    assert flows.battery == 1000.0
    assert flows.grid == 700.0
    assert _sum(flows) == pytest.approx(0.0, abs=1e-9)


@pytest.mark.spec_only
def test_off_grid_the_inverter_s_pv_covers_load_before_the_battery() -> None:
    """Off grid the battery only discharges, so the flows balance while the PV is no
    more than the load, as here. The emitter curtails nothing beyond it: that is
    the producer's, which generates the PV."""
    flows = (
        _emitter(_manifest())
        .publish_tick(
            TickInputs(
                current_time=0.0,
                grid_online=False,
                circuits={"kitchen": 1200.0, "laundry": 300.0},
                bess_pv={_BESS: 1100.0},
            )
        )
        .power_flows
    )
    assert flows.pv == -1100.0
    assert flows.battery == -400.0
    assert flows.grid == 0.0
    assert _sum(flows) == pytest.approx(0.0, abs=1e-9)


_AC_COUPLED = {
    "vendor-name": "Example",
    "nameplate-capacity-kwh": "13.5",
    "relative-position": "UPSTREAM",
}


@pytest.mark.parametrize(
    ("manifest", "bess_pv", "match"),
    [
        (_manifest(bess=_AC_COUPLED), {_BESS: 100.0}, "inverter-type is hybrid"),
        (
            _manifest(bess={**_AC_COUPLED, "inverter-type": "ac-coupled"}),
            {_BESS: 100.0},
            "inverter-type is hybrid",
        ),
        (_manifest(), {"no-such-battery": 100.0}, "no-such-battery"),
        (_manifest(), {_BESS: math.nan}, "finite"),
        (_manifest(), {_BESS: math.inf}, "finite"),
    ],
)
def test_pv_for_a_battery_whose_inverter_takes_none_is_refused_before_anything_moves(
    rec: PahoRecorder, manifest: DeviceManifest, bess_pv: dict[str, float], match: str
) -> None:
    """The refused tick publishes nothing and moves nothing: the next tick leaves
    the energy registers and the battery's charge where an emitter that never saw
    the refused tick has them."""

    def tick(t: float, pv: dict[str, float] | None = None) -> TickInputs:
        return TickInputs(
            current_time=t, grid_online=True, circuits={"kitchen": 500.0}, bess_pv=pv or {}
        )

    emitter = _emitter(manifest)
    first = emitter.publish_tick(tick(0.0))
    published = len(rec.published)
    with pytest.raises(EmitterStateError, match=match):
        emitter.publish_tick(tick(3600.0, bess_pv))
    assert emitter.last_snapshot is first
    assert len(rec.published) == published

    after = emitter.publish_tick(tick(7200.0))
    untouched = _emitter(manifest)
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


def test_pv_for_a_hybrid_battery_that_is_not_configured_is_refused() -> None:
    """Without a BESSConfig the battery has no dispatch and does not value the
    battery flow, so its inverter's PV is refused as ``bess_communication`` refuses
    an unconfigured battery, rather than published around a missing battery."""
    emitter = Emitter(_manifest(), SetterRegistry(), variant="span-alpha-test-b2")
    emitter.start()
    with pytest.raises(EmitterStateError, match="configured"):
        emitter.publish_tick(
            TickInputs(
                current_time=0.0,
                grid_online=True,
                circuits={"kitchen": 500.0},
                bess_pv={_BESS: 3000.0},
            )
        )
    assert emitter.last_snapshot is None


@pytest.mark.parametrize("variant", get_args(Variant))
def test_a_night_tick_publishes_pv_as_zero_never_negative_zero(
    rec: PahoRecorder, variant: Variant
) -> None:
    """No circuit producing and the inverter's PV exactly 0.0, as at night: pv reads
    zero, never -0.0, which the reference variant, writing numbers as they are,
    would publish if it negated a sum of zeros."""
    _emitter(_manifest(), variant).publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 500.0},
            bess_pv={_BESS: 0.0},
        )
    )
    pv = rec.retained["ebus/5/panel/power-flows/pv"]
    assert float(pv) == 0.0
    assert not pv.startswith("-")


@pytest.mark.spec_only
def test_inverter_pv_that_cancels_the_circuits_solar_publishes_zero(rec: PahoRecorder) -> None:
    """A circuit producing 5 W while the inverter's PV side draws 5 W: pv is zero,
    which the reference variant would otherwise write as -0.0."""
    _emitter(_manifest(), "reference").publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 500.0, "garage": -5.0},
            bess_pv={_BESS: -5.0},
        )
    )
    assert rec.retained["ebus/5/panel/power-flows/pv"] == "0.0"


@pytest.mark.parametrize(
    "bess", [_AC_COUPLED, None], ids=["ac-coupled battery", "hybrid battery, no PV given"]
)
def test_without_inverter_pv_the_reference_variant_writes_pv_as_before(
    rec: PahoRecorder, bess: dict[str, str] | None
) -> None:
    """The reference variant writes a number as it is, and before batteries took PV
    it wrote the pv of a panel with no producing circuit as ``0``. Where no inverter
    PV is given, it still does."""
    _emitter(_manifest(bess=bess), "reference").publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0})
    )
    assert rec.retained["ebus/5/panel/power-flows/pv"] == "0"


@pytest.mark.parametrize("value", ["dc-coupled", "Hybrid", "true"])
def test_an_inverter_type_outside_the_vocabulary_is_rejected(value: str) -> None:
    with pytest.raises(ManifestValidationError, match="inverter-type"):
        Emitter(_manifest(bess={**_AC_COUPLED, "inverter-type": value}), SetterRegistry())
