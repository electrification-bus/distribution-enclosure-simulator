"""The release build read out of a panel's ``firmware-version``, and the
conventions the span variant keys on it."""

from __future__ import annotations

import pytest

from ebus_panel_sim.firmware import (
    CURRENT_CONVENTIONS_RELEASE,
    FirmwareConventions,
    firmware_conventions,
    release_build,
)
from ebus_panel_sim.manifest_physics import ManifestPhysicsView

from .test_connection import _manifest
from .test_firmware_gate import with_panel_firmware


@pytest.mark.parametrize(
    ("firmware", "build"),
    [
        ("spanos2/r202633/02", 202633),
        ("spanos3/r202639/01", 202639),
        ("r202639", 202639),
        (" spanos2/r202640/01 ", 202640),
        # The first release segment wins; nothing after it is read.
        ("spanos2/r202633/r202639", 202633),
    ],
)
def test_a_release_segment_gives_its_build(firmware: str, build: int) -> None:
    assert release_build(firmware) == build


@pytest.mark.parametrize(
    "firmware",
    [
        "example/v0.1.0",
        "",
        None,
        "r20263",
        "xr202639",
        "r2026390",
        "spanos2/r202639x/01",
        "spanos2-r202639-01",
        "rc1",
    ],
)
def test_no_whole_release_segment_gives_none(firmware: str | None) -> None:
    assert release_build(firmware) is None


def test_the_panel_physics_carries_the_release_build() -> None:
    physics = ManifestPhysicsView(with_panel_firmware(_manifest(), "spanos2/r202633/02"))
    assert physics.panel.release_build == 202633
    assert ManifestPhysicsView(_manifest()).panel.release_build is None


_PRE = FirmwareConventions(bess_meter_frame="enclosure", user_max_charge_current_preset=True)
_CURRENT = FirmwareConventions(bess_meter_frame="device", user_max_charge_current_preset=False)


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        (202633, _PRE),
        (CURRENT_CONVENTIONS_RELEASE - 1, _PRE),
        (CURRENT_CONVENTIONS_RELEASE, _CURRENT),
        (202640, _CURRENT),
        # No parseable build: the current conventions.
        (None, _CURRENT),
    ],
)
def test_the_span_variant_keys_on_the_release(
    release: int | None, expected: FirmwareConventions
) -> None:
    assert firmware_conventions("span", release) == expected


@pytest.mark.parametrize("release", [202633, CURRENT_CONVENTIONS_RELEASE, None])
def test_the_reference_variant_always_uses_the_current_conventions(release: int | None) -> None:
    assert firmware_conventions("reference", release) == _CURRENT
