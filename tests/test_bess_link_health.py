"""The panel's link to each battery is a per-tick input, and the islanding assertion
follows it.

A SPAN panel publishes its link health as the battery's ``status/communication-state``
and, from the same observation, on the connection status of the circuit or lugs that
connects the battery, where it reports only ``OK`` or ``LOST`` while declaring the
catalog's full enum. It accepts an ``asserted-islanding-state`` of ``ON_GRID`` or
``OFF_GRID`` only while that link is not known to be healthy, ignores ``NONE`` always,
and clears an assertion once the link has been healthy for about 30 seconds."""

from __future__ import annotations

import json
from typing import cast

import pytest

from ebus_panel_sim import (
    BESSCommunication,
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    Emitter,
    EmitterStateError,
    SetterRegistry,
    TickInputs,
    Variant,
)

from .conftest import PahoRecorder

_UPSTREAM = "bess-up"
_IN_PANEL = "bess-in"
_ASSERTION = "ebus/5/abc-123/shed/asserted-islanding-state"


def _circuit(cid: str) -> DeviceInstance:
    return DeviceInstance(
        "circuit",
        cid,
        cid.title(),
        metadata={
            "tab-numbers": "1",
            "breaker-rating-a": "20",
            "default-priority": "NICE_TO_HAVE",
            "relay-behavior": "controllable",
            "placement": "downstream-of-lugs",
        },
    )


def _bess(instance_id: str, **placement: str) -> DeviceInstance:
    return DeviceInstance(
        "bess",
        instance_id,
        "Battery",
        metadata={"vendor-name": "Span", "nameplate-capacity-kwh": "13.5", **placement},
    )


def _manifest() -> DeviceManifest:
    """An upstream battery, connected through the upstream lugs, and an in-panel one
    on its own circuit, so both connection statuses are exercised."""
    return DeviceManifest(
        instances=(
            DeviceInstance(
                "panel",
                "abc-123",
                "Span Panel",
                metadata={
                    "vendor-name": "Span",
                    "serial-number": "abc-123",
                    "firmware-version": "sim/v0.1.0",
                    "hardware-version": "rev2",
                    "panel-size": "40",
                    "main-breaker-rating-a": "200",
                    "panel-model": "MAIN_40",
                    "postal-code": "94103",
                    "time-zone": "America/Los_Angeles",
                },
            ),
            _circuit("kitchen"),
            _circuit("battery"),
            DeviceInstance("lugs", "lugs-upstream", "Upstream lugs", {"direction": "upstream"}),
            DeviceInstance(
                "lugs", "lugs-downstream", "Downstream lugs", {"direction": "downstream"}
            ),
            _bess(_UPSTREAM, **{"relative-position": "UPSTREAM"}),
            _bess(_IN_PANEL, **{"relative-position": "IN_PANEL", "feed": "battery"}),
        )
    )


def _config(instance_id: str) -> BESSConfig:
    return BESSConfig(
        instance_id=instance_id,
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
    )


def _emitter(setters: SetterRegistry | None = None, variant: Variant = "span") -> Emitter:
    """No producer handlers, so the emitter's own assertion handler is exercised."""
    em = Emitter(
        _manifest(),
        setters if setters is not None else SetterRegistry(),
        bess_configs=(_config(_UPSTREAM), _config(_IN_PANEL)),
        variant=variant,
    )
    em.start()
    return em


def _batteryless_emitter(setters: SetterRegistry) -> Emitter:
    batteryless = tuple(i for i in _manifest().instances if i.entity_class != "bess")
    em = Emitter(DeviceManifest(instances=batteryless), setters)
    em.start()
    return em


def _tick(
    current_time: float,
    *,
    up: BESSCommunication | None = None,
    in_panel: BESSCommunication | None = None,
) -> TickInputs:
    """A tick reporting the given link health for each battery; one left as None is
    left out of the tick."""
    reported = ((_UPSTREAM, up), (_IN_PANEL, in_panel))
    return TickInputs(
        current_time=current_time,
        grid_online=True,
        circuits={"kitchen": 500.0, "battery": 0.0},
        bess_communication={bid: link for bid, link in reported if link is not None},
    )


def _assert_islanding(setters: SetterRegistry, value: str) -> None:
    handler = setters.get("panel", "shed/asserted-islanding-state")
    assert handler is not None
    handler("panel", "abc-123", "shed/asserted-islanding-state", value)


# --- Link health on the wire ------------------------------------------------


def test_a_battery_left_out_of_the_tick_is_ok(rec: PahoRecorder) -> None:
    em = _emitter()
    snap = em.publish_tick(_tick(0.0))
    retained = rec.retained
    assert retained[f"ebus/5/{_UPSTREAM}/status/communication-state"] == "OK"
    assert retained["ebus/5/lugs-upstream/connection/fed-by-device-status"] == "OK"
    assert retained["ebus/5/battery/connection/feeds-device-status"] == "OK"
    assert snap.battery[_UPSTREAM].connected is True


def test_a_lost_link_is_published_on_the_battery_and_its_connection(rec: PahoRecorder) -> None:
    em = _emitter()
    snap = em.publish_tick(_tick(0.0, up="LOST", in_panel="LOST"))
    retained = rec.retained
    assert retained[f"ebus/5/{_UPSTREAM}/status/communication-state"] == "LOST"
    assert retained[f"ebus/5/{_IN_PANEL}/status/communication-state"] == "LOST"
    assert retained["ebus/5/lugs-upstream/connection/fed-by-device-status"] == "LOST"
    assert retained["ebus/5/battery/connection/feeds-device-status"] == "LOST"
    assert snap.battery[_UPSTREAM].connected is False


@pytest.mark.parametrize(
    ("variant", "link", "connection"),
    [
        ("span", "DEGRADED", "LOST"),
        ("span", "UNKNOWN", "LOST"),
        ("reference", "DEGRADED", "DEGRADED"),
        ("reference", "UNKNOWN", "LOST"),
    ],
)
def test_the_connection_status_publishes_what_its_profile_declares(
    rec: PahoRecorder, variant: Variant, link: BESSCommunication, connection: str
) -> None:
    """The battery reports its link health as is; the connection status reports it
    where it can and as ``LOST`` otherwise. A SPAN panel's connection status carries
    only ``OK`` and ``LOST``, and no connection catalog carries ``UNKNOWN``."""
    em = _emitter(variant=variant)
    em.publish_tick(_tick(0.0, up=link, in_panel=link))
    retained = rec.retained
    assert retained[f"ebus/5/{_UPSTREAM}/status/communication-state"] == link
    assert retained["ebus/5/lugs-upstream/connection/fed-by-device-status"] == connection
    assert retained["ebus/5/battery/connection/feeds-device-status"] == connection


@pytest.mark.parametrize("variant", ["span", "reference"])
def test_the_connection_status_declares_the_catalogs_full_enum(
    rec: PahoRecorder, variant: Variant
) -> None:
    """A SPAN panel declares ``OK,LOST,DEGRADED`` on these statuses even though it
    reports only ``OK`` or ``LOST`` (SPAN-API-Client-Docs
    ``specs/r202633/homie-schema.json``), so the span variant declares it too."""
    em = _emitter(variant=variant)
    em.publish_tick(_tick(0.0))
    for device_id, prop in (
        ("battery", "feeds-device-status"),
        ("lugs-upstream", "fed-by-device-status"),
        ("lugs-upstream", "feeds-device-status"),
    ):
        description = json.loads(rec.retained[f"ebus/5/{device_id}/$description"])
        assert (
            description["nodes"]["connection"]["properties"][prop]["format"] == "OK,LOST,DEGRADED"
        )


def test_a_tick_naming_an_unconfigured_battery_is_refused() -> None:
    em = _emitter()
    before = em.publish_tick(_tick(0.0))
    tick = _tick(1.0)
    tick.bess_communication["not-a-battery"] = "LOST"
    with pytest.raises(EmitterStateError, match="not-a-battery"):
        em.publish_tick(tick)
    assert em.last_snapshot is before


def test_a_tick_with_an_unknown_link_health_is_refused() -> None:
    em = _emitter()
    before = em.publish_tick(_tick(0.0))
    with pytest.raises(EmitterStateError, match="FLAKY"):
        em.publish_tick(_tick(1.0, up=cast("BESSCommunication", "FLAKY")))
    assert em.last_snapshot is before


# --- The assertion follows the link -----------------------------------------


def test_an_assertion_is_ignored_while_every_link_is_ok(rec: PahoRecorder) -> None:
    setters = SetterRegistry()
    em = _emitter(setters)
    em.publish_tick(_tick(0.0))
    _assert_islanding(setters, "ON_GRID")
    em.publish_tick(_tick(1.0))
    assert rec.retained[_ASSERTION] == "NONE"


def test_an_assertion_is_accepted_before_the_first_tick(rec: PahoRecorder) -> None:
    """No link has been observed yet, so none is known to be healthy, and the panel
    accepts. A healthy first tick then starts the wait that clears it."""
    setters = SetterRegistry()
    em = _emitter(setters)
    _assert_islanding(setters, "ON_GRID")
    em.publish_tick(_tick(0.0))
    assert rec.retained[_ASSERTION] == "ON_GRID"
    em.publish_tick(_tick(30.0))
    assert rec.retained[_ASSERTION] == "NONE"


@pytest.mark.parametrize("ticked", [False, True])
def test_a_panel_without_a_battery_never_accepts_an_assertion(ticked: bool) -> None:
    """With no battery there is no link to lose, before the first tick or after."""
    setters = SetterRegistry()
    em = _batteryless_emitter(setters)
    if ticked:
        em.publish_tick(_tick(0.0))
    _assert_islanding(setters, "OFF_GRID")
    snap = em.publish_tick(_tick(1.0))
    assert snap.shed.asserted_islanding_state == "NONE"


@pytest.mark.parametrize("link", ["LOST", "DEGRADED", "UNKNOWN"])
def test_an_assertion_is_accepted_while_a_link_is_unhealthy(
    rec: PahoRecorder, link: BESSCommunication
) -> None:
    setters = SetterRegistry()
    em = _emitter(setters)
    em.publish_tick(_tick(0.0, up=link))
    _assert_islanding(setters, "on_grid")
    em.publish_tick(_tick(1.0, up=link))
    assert rec.retained[_ASSERTION] == "ON_GRID"


def test_one_unhealthy_battery_is_enough(rec: PahoRecorder) -> None:
    setters = SetterRegistry()
    em = _emitter(setters)
    em.publish_tick(_tick(0.0, up="OK", in_panel="LOST"))
    _assert_islanding(setters, "OFF_GRID")
    em.publish_tick(_tick(1.0, up="OK", in_panel="LOST"))
    assert rec.retained[_ASSERTION] == "OFF_GRID"


@pytest.mark.parametrize("value", ["NONE", "MAYBE"])
def test_an_assertion_in_force_ignores_what_a_consumer_cannot_assert(
    rec: PahoRecorder, value: str
) -> None:
    """``NONE`` is panel-authored: a consumer does not clear an assertion."""
    setters = SetterRegistry()
    em = _emitter(setters)
    em.publish_tick(_tick(0.0, up="LOST"))
    _assert_islanding(setters, "ON_GRID")
    _assert_islanding(setters, value)
    em.publish_tick(_tick(1.0, up="LOST"))
    assert rec.retained[_ASSERTION] == "ON_GRID"


def test_an_assertion_clears_after_thirty_healthy_seconds(rec: PahoRecorder) -> None:
    setters = SetterRegistry()
    em = _emitter(setters)
    em.publish_tick(_tick(0.0, up="LOST"))
    _assert_islanding(setters, "ON_GRID")
    em.publish_tick(_tick(5.0, up="LOST"))
    em.publish_tick(_tick(10.0))  # healthy from here
    em.publish_tick(_tick(39.0))
    assert rec.retained[_ASSERTION] == "ON_GRID"
    em.publish_tick(_tick(40.0))
    assert rec.retained[_ASSERTION] == "NONE"


def test_an_unhealthy_tick_restarts_the_wait(rec: PahoRecorder) -> None:
    setters = SetterRegistry()
    em = _emitter(setters)
    em.publish_tick(_tick(0.0, up="LOST"))
    _assert_islanding(setters, "ON_GRID")
    em.publish_tick(_tick(10.0))
    em.publish_tick(_tick(20.0, in_panel="DEGRADED"))
    em.publish_tick(_tick(25.0))  # healthy again from here
    em.publish_tick(_tick(54.0))
    assert rec.retained[_ASSERTION] == "ON_GRID"
    em.publish_tick(_tick(55.0))
    assert rec.retained[_ASSERTION] == "NONE"
