"""An EVSE's user charge-current ceiling, `config/user-max-charge-current`.

Unpublished until a user sets it. A `/set` must be an integer, is clamped into
`[6, max-charge-current]`, and anything else is refused with the value left
unchanged. `$format` advertises that range per EVSE."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from ebus_panel_sim import (
    BESSConfig,
    DeviceManifest,
    Emitter,
    PanelDefinition,
    SetterRegistry,
    TickInputs,
    load_definition,
    load_ticks,
)
from ebus_panel_sim.capture import definition_from_tree, tree_from_retained

from .conftest import PahoRecorder
from .test_connection import _manifest

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

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


def _with_user_max(raw: str) -> PanelDefinition:
    example = load_definition(_EXAMPLES / "forty_tab_minimal.yaml")
    return dataclasses.replace(
        example,
        manifest=DeviceManifest(
            instances=tuple(
                dataclasses.replace(i, metadata={**i.metadata, "user-max-charge-current-a": raw})
                if i.entity_class == "evse"
                else i
                for i in example.manifest.instances
            )
        ),
    )


@pytest.mark.parametrize(("raw", "published"), [("24", "24"), ("80", "32"), ("2", "6")])
def test_a_definition_gives_the_user_limit_an_evse_starts_from(
    rec: PahoRecorder, raw: str, published: str
) -> None:
    """Captured state, as a user's setting or a stale retained value would leave
    it; clamped into the advertised range as a /set is."""
    definition = _with_user_max(raw)
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    emitter.publish_tick(load_ticks(_EXAMPLES / "forty_tab_minimal.ticks.yaml")[0])
    evse = definition.manifest.of_class("evse")[0].instance_id
    assert rec.retained[f"ebus/5/{evse}/config/user-max-charge-current"] == published


def test_capture_reads_the_published_user_limit_back(rec: PahoRecorder) -> None:
    definition = _with_user_max("24")
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    emitter.publish_tick(load_ticks(_EXAMPLES / "forty_tab_minimal.ticks.yaml")[0])
    captured, _ = definition_from_tree(tree_from_retained(rec.retained))
    evse = captured.manifest.of_class("evse")[0]
    assert evse.metadata["user-max-charge-current-a"] == "24"
