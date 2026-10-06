"""The span variant publishes either side of a SPAN firmware change, chosen by
the panel's own ``firmware-version``.

Before release 202639 a SPAN panel published the BESS child's
``meter/active-power`` in its own frame, equal to ``power-flows/battery``, and
published ``config/user-max-charge-current`` at the commissioned maximum before
anyone set it. From 202639 the BESS meter is the battery's own frame
(``devices/bess.md``: positive while discharging), the negative of
``power-flows/battery``, and the user limit is unpublished until a user sets it.
A firmware string with no release build, as the examples carry, gets the
current conventions; the reference variant always does."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from ebus_panel_sim import (
    BESSConfig,
    DeviceManifest,
    Emitter,
    SetterRegistry,
    TickInputs,
    Variant,
    load_definition,
    load_ticks,
)

from .conftest import PahoRecorder
from .test_connection import _manifest
from .test_definition import _stable

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

_PRE = "spanos2/r202633/02"
_CURRENT = "spanos2/r202639/01"
_UNVERSIONED = "example/v0.1.0"

_BESS_METER = "ebus/5/abc-123-bess/meter/active-power"
_POWER_FLOWS_BATTERY = "ebus/5/abc-123/power-flows/battery"
_USER_MAX = "ebus/5/evse/config/user-max-charge-current"


def with_panel_firmware(manifest: DeviceManifest, firmware: str) -> DeviceManifest:
    """``manifest`` with only the panel's ``firmware-version`` replaced."""
    return DeviceManifest(
        instances=tuple(
            dataclasses.replace(inst, metadata={**inst.metadata, "firmware-version": firmware})
            if inst.entity_class == "panel"
            else inst
            for inst in manifest.instances
        )
    )


class _Panel:
    """The connection test's panel (a BESS and a 32 A SPAN Drive) on ``firmware``."""

    def __init__(self, firmware: str, *, variant: Variant = "span") -> None:
        cfg = BESSConfig(
            instance_id="abc-123-bess",
            nameplate_capacity_kwh=13.5,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
        )
        self.setters = SetterRegistry()
        self.em = Emitter(
            with_panel_firmware(_manifest(), firmware),
            self.setters,
            bess_configs=(cfg,),
            variant=variant,
        )
        self.em.start()

    def tick(self, t: float, kitchen_w: float = 3000.0) -> float:
        """Publish a tick; return the battery's device-frame power."""
        snap = self.em.publish_tick(
            TickInputs(current_time=t, grid_online=True, circuits={"kitchen": kitchen_w})
        )
        return snap.battery["abc-123-bess"].active_power_w

    def set_user_max(self, value: str) -> None:
        handler = self.setters.get("evse", "config/user-max-charge-current")
        assert handler is not None
        handler("evse", "evse", "config/user-max-charge-current", value)


# ---- BESS meter/active-power --------------------------------------------------


def test_before_202639_the_bess_meter_equals_power_flows_battery(rec: PahoRecorder) -> None:
    panel = _Panel(_PRE)
    device_frame = panel.tick(0.0)
    assert device_frame > 0.0, "the kitchen load should discharge the battery"

    wire = float(rec.retained[_BESS_METER])
    assert wire == pytest.approx(float(rec.retained[_POWER_FLOWS_BATTERY]))
    # Pinned absolutely too: the panel's frame is negative while discharging, and
    # the snapshot keeps the battery's own frame.
    assert wire == pytest.approx(-device_frame)


@pytest.mark.parametrize("firmware", [_CURRENT, _UNVERSIONED])
def test_otherwise_the_bess_meter_is_the_negative_of_power_flows_battery(
    rec: PahoRecorder, firmware: str
) -> None:
    panel = _Panel(firmware)
    device_frame = panel.tick(0.0)
    assert device_frame > 0.0, "the kitchen load should discharge the battery"

    wire = float(rec.retained[_BESS_METER])
    assert wire == pytest.approx(-float(rec.retained[_POWER_FLOWS_BATTERY]))
    assert wire == pytest.approx(device_frame)


def test_the_reference_variant_ignores_the_firmware(rec: PahoRecorder) -> None:
    panel = _Panel(_PRE, variant="reference")
    device_frame = panel.tick(0.0)
    assert device_frame > 0.0
    assert float(rec.retained[_BESS_METER]) == pytest.approx(device_frame)


def test_an_idle_battery_publishes_zero_before_202639(rec: PahoRecorder) -> None:
    panel = _Panel(_PRE)
    assert panel.tick(0.0, kitchen_w=0.0) == 0.0
    # Never ``-0.0``: an idle battery has no direction to report.
    assert not rec.retained[_BESS_METER].startswith("-")
    assert float(rec.retained[_BESS_METER]) == 0.0


# ---- SPAN Drive config/user-max-charge-current --------------------------------


def test_before_202639_the_user_limit_starts_at_the_commissioned_max(rec: PahoRecorder) -> None:
    panel = _Panel(_PRE)
    panel.tick(0.0)
    assert rec.retained[_USER_MAX] == "32"
    assert rec.retained[_USER_MAX] == rec.retained["ebus/5/evse/config/max-charge-current"]

    panel.set_user_max("24")
    panel.tick(1.0)
    assert rec.retained[_USER_MAX] == "24"

    # Still clamped into [6, max].
    panel.set_user_max("2")
    panel.tick(2.0)
    assert rec.retained[_USER_MAX] == "6"
    panel.set_user_max("40")
    panel.tick(3.0)
    assert rec.retained[_USER_MAX] == "32"


@pytest.mark.parametrize("firmware", [_CURRENT, _UNVERSIONED])
def test_otherwise_the_user_limit_is_unpublished_until_set(
    rec: PahoRecorder, firmware: str
) -> None:
    panel = _Panel(firmware)
    panel.tick(0.0)
    assert _USER_MAX not in rec.retained

    panel.set_user_max("24")
    panel.tick(1.0)
    assert rec.retained[_USER_MAX] == "24"


@pytest.mark.parametrize("firmware", [_PRE, _CURRENT, _UNVERSIONED])
def test_the_user_limit_format_is_the_same_on_every_firmware(
    rec: PahoRecorder, firmware: str
) -> None:
    _Panel(firmware).tick(0.0)
    description = json.loads(rec.retained["ebus/5/evse/$description"])
    declaration = description["nodes"]["config"]["properties"]["user-max-charge-current"]
    assert declaration["format"] == "6:32"


# ---- end to end: the shipped example on either firmware -----------------------


def _run_example(rec: PahoRecorder, firmware: str | None) -> dict[str, object]:
    """The shipped example's retained tree after its ticks, with the panel's
    ``firmware-version`` replaced when ``firmware`` is given."""
    definition = load_definition(_EXAMPLES / "forty_tab_minimal.yaml")
    if firmware is not None:
        definition = dataclasses.replace(
            definition, manifest=with_panel_firmware(definition.manifest, firmware)
        )
    rec.reset()
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    for tick in load_ticks(_EXAMPLES / "forty_tab_minimal.ticks.yaml"):
        emitter.publish_tick(tick)
    emitter.stop()
    return _stable(rec.retained)


def test_the_example_before_202639_differs_only_in_the_gated_topics(rec: PahoRecorder) -> None:
    shipped = _run_example(rec, None)
    pre = _run_example(rec, _PRE)

    firmware = "ebus/5/example-40t-001/info/firmware-version"
    bess = "ebus/5/bess/meter/active-power"
    flows = "ebus/5/example-40t-001/power-flows/battery"
    user_max = {f"ebus/5/{e}/config/user-max-charge-current" for e in ("evse", "evse-2")}

    # The shipped example keeps the current conventions.
    assert shipped[firmware] == _UNVERSIONED
    assert float(str(shipped[bess])) == pytest.approx(-float(str(shipped[flows])))
    assert float(str(shipped[bess])) != 0.0
    assert user_max.isdisjoint(shipped)

    # The same panel before 202639: the old sign and the preset user limit.
    assert pre[firmware] == _PRE
    assert pre[bess] == pre[flows]
    for topic in user_max:
        max_topic = topic.replace("user-max-charge-current", "max-charge-current")
        assert pre[topic] == pre[max_topic]

    changed = {t for t in shipped.keys() | pre.keys() if shipped.get(t) != pre.get(t)}
    assert changed == {firmware, bess} | user_max


def test_two_panels_on_either_side_of_202639_coexist(rec: PahoRecorder) -> None:
    """One process can host both, so a consumer can be tested against a panel
    crossing the change by restarting it on the other firmware."""
    pre = _Panel(_PRE)
    current = _Panel(_CURRENT)
    # Same device ids, so each tick overwrites the other's retained values; read
    # the wire after each.
    pre.tick(0.0)
    pre_wire = float(rec.retained[_BESS_METER])
    current.tick(0.0)
    current_wire = float(rec.retained[_BESS_METER])
    assert pre_wire == pytest.approx(-current_wire)
    assert pre_wire < 0.0


def test_only_the_panels_firmware_selects(rec: PahoRecorder) -> None:
    """An EVSE's or a battery's own ``firmware-version`` selects nothing."""
    manifest = _manifest()
    em = Emitter(
        DeviceManifest(
            instances=tuple(
                dataclasses.replace(inst, metadata={**inst.metadata, "firmware-version": _PRE})
                if inst.entity_class in ("evse", "bess")
                else inst
                for inst in manifest.instances
            )
        ),
        SetterRegistry(),
        bess_configs=(
            BESSConfig(
                instance_id="abc-123-bess",
                nameplate_capacity_kwh=13.5,
                max_charge_w=3500.0,
                max_discharge_w=3500.0,
            ),
        ),
    )
    em.start()
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 3000.0})
    )
    assert float(rec.retained[_BESS_METER]) == pytest.approx(
        snap.battery["abc-123-bess"].active_power_w
    )
    assert _USER_MAX not in rec.retained
