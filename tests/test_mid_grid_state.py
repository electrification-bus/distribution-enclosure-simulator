"""``grid/grid-forming-entity`` names a device, not a device class.

The grid capability catalog defines the property as ``"GRID"`` when grid-tied,
"or the Homie device ID of the grid-forming device (typically the DER parent
device: a BESS, a V2H EVSE, a generator) when islanded". The emitter published
the literal string ``"BESS"`` off grid — a class name no consumer can resolve to
anything on the wire, and the only device id it could have meant is sitting in
the manifest.

A consumer's use for this property is to point at the device now forming the AC
reference: to badge it, to link to it, to attribute the island to it. A class
name defeats all three, and in a manifest that grows a second DER it is not even
unambiguous about which battery.
"""

from __future__ import annotations

import pytest

from ebus_panel_sim import (
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    Emitter,
    ManifestValidationError,
    SetterRegistry,
    TickInputs,
)

from .conftest import PahoRecorder

_MID_TOPIC = "ebus/5/abc-123-bess-mid/grid/grid-forming-entity"

_PANEL = DeviceInstance(
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
)

_MID = DeviceInstance(
    "mid",
    "abc-123-bess-mid",
    "Microgrid Interconnect Device",
    metadata={"vendor-name": "Span", "serial-number": "abc-123-bess-mid"},
)


def _bess(instance_id: str) -> DeviceInstance:
    return DeviceInstance(
        "bess",
        instance_id,
        "Battery",
        metadata={"vendor-name": "Span", "nameplate-capacity-kwh": "13.5"},
    )


def _started_emitter(*bess_ids: str) -> Emitter:
    manifest = DeviceManifest(instances=(_PANEL, *(_bess(b) for b in bess_ids), _MID))
    configs = tuple(
        BESSConfig(
            instance_id=b,
            nameplate_capacity_kwh=13.5,
            max_charge_w=3500.0,
            max_discharge_w=3500.0,
        )
        for b in bess_ids
    )
    emitter = Emitter(manifest, SetterRegistry(), bess_configs=configs)
    emitter.start()
    return emitter


def _tick(*, grid_online: bool) -> TickInputs:
    return TickInputs(current_time=0.0, grid_online=grid_online, circuits={})


def test_grid_forming_entity_is_grid_while_grid_tied(rec: PahoRecorder) -> None:
    _started_emitter("abc-123-bess").publish_tick(_tick(grid_online=True))

    assert rec.retained[_MID_TOPIC] == "GRID"


def test_grid_forming_entity_is_the_bess_device_id_while_islanded(rec: PahoRecorder) -> None:
    """The regression. This published ``"BESS"`` before the fix."""
    _started_emitter("abc-123-bess").publish_tick(_tick(grid_online=False))

    assert rec.retained[_MID_TOPIC] == "abc-123-bess"


def test_the_islanded_value_resolves_to_a_device_that_exists(rec: PahoRecorder) -> None:
    """What made the old value wrong, stated as the property a consumer needs.

    Asserting the literal id would still pass if the emitter published any other
    plausible-looking constant. This fails for anything that is not a device on
    the wire, which is what the catalog actually requires.
    """
    _started_emitter("abc-123-bess").publish_tick(_tick(grid_online=False))
    retained = rec.retained

    former = retained[_MID_TOPIC]
    assert f"ebus/5/{former}/$description" in retained


@pytest.mark.parametrize("bess_ids", [(), ("bess-one", "bess-two")], ids=["none", "two"])
def test_a_mid_without_exactly_one_bess_is_refused_at_build(bess_ids: tuple[str, ...]) -> None:
    """Pins the guarantee the resolver leans on rather than re-deriving it.

    ``_grid_forming_device_id`` reports unknown when a single BESS does not
    answer, and that branch is unreachable today precisely because the graph
    builder rejects the manifest first — a MID is placed ``child-of-parent``
    under ``bess`` and an ambiguous parent is an error, not a default. If that
    ever relaxes, this fails and points at the resolver.
    """
    with pytest.raises(ManifestValidationError, match="expected exactly one 'bess'"):
        _started_emitter(*bess_ids)
