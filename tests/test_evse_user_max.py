"""An EVSE's user charge-current ceiling, `config/user-max-charge-current`.

Unpublished until a user sets it. A `/set` must be an integer, is clamped into
`[6, max-charge-current]`, and anything else is refused with the value left
unchanged. `$format` advertises that range per EVSE."""

from __future__ import annotations

import json

import pytest

from ebus_panel_sim import BESSConfig, Emitter, SetterRegistry, TickInputs

from .conftest import PahoRecorder
from .test_connection import _manifest

_TOPIC = "ebus/5/evse/config/user-max-charge-current"


class _Panel:
    def __init__(self) -> None:
        cfg = BESSConfig(
            instance_id="abc-123-bess",
            nameplate_capacity_kwh=13.5,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
        )
        self.setters = SetterRegistry()
        self.em = Emitter(_manifest(), self.setters, bess_configs=(cfg,))
        self.em.start()

    def tick(self, t: float) -> None:
        self.em.publish_tick(
            TickInputs(current_time=t, grid_online=True, circuits={"kitchen": 500.0})
        )

    def set(self, entity_class: str, instance_id: str, prop: str, value: object) -> None:
        handler = self.setters.get(entity_class, prop)
        assert handler is not None
        handler(entity_class, instance_id, prop, value)


def test_unpublished_until_a_user_sets_it(rec: PahoRecorder) -> None:
    panel = _Panel()
    panel.tick(0.0)
    assert _TOPIC not in rec.retained
    assert rec.retained["ebus/5/evse/config/max-charge-current"] == "32"

    panel.set("evse", "evse", "config/user-max-charge-current", "24")
    panel.tick(1.0)
    assert rec.retained[_TOPIC] == "24"


@pytest.mark.parametrize(("written", "published"), [("2", "6"), ("40", "32"), ("32", "32")])
def test_a_set_is_clamped_into_range(rec: PahoRecorder, written: str, published: str) -> None:
    panel = _Panel()
    panel.set("evse", "evse", "config/user-max-charge-current", written)
    panel.tick(0.0)
    assert rec.retained[_TOPIC] == published


@pytest.mark.parametrize("written", ["", "abc", "12.5"])
def test_a_non_integer_set_is_refused(rec: PahoRecorder, written: str) -> None:
    panel = _Panel()
    panel.set("evse", "evse", "config/user-max-charge-current", "20")
    panel.set("evse", "evse", "config/user-max-charge-current", written)
    panel.tick(0.0)
    assert rec.retained[_TOPIC] == "20"


def test_format_advertises_the_range_up_to_the_commissioned_max(rec: PahoRecorder) -> None:
    _Panel().tick(0.0)
    description = json.loads(rec.retained["ebus/5/evse/$description"])
    declaration = description["nodes"]["config"]["properties"]["user-max-charge-current"]
    assert declaration["format"] == "6:32"
