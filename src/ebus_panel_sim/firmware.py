"""Which side of a SPAN firmware change the simulated panel publishes.

SPAN panels in the field run on both sides of release 202639, and two of the
conventions this emitter publishes changed there. Before it, the BESS child's
``meter/active-power`` was the panel's reading of the battery, equal to
``power-flows/battery`` (positive while charging), and the panel published an
EVSE's ``config/user-max-charge-current`` at the commissioned maximum before any
user had set one. From it, the BESS meter is the battery's own frame, positive
while discharging as ``devices/bess.md`` defines it, and the user limit is
unpublished until a user sets one.

The span variant impersonates whichever side the panel's own
``firmware-version`` names, so one emitter can stand in for either and a
consumer can be tested across the change. A firmware string with no release
build in it, such as the examples' ``example/v0.1.0``, gets the current
conventions, and so does every other variant: the span-alpha-test-b2 and
reference variants publish the specification's frame whatever firmware they
report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from ebus_panel_sim.wire.profile_loader import Variant

CURRENT_CONVENTIONS_RELEASE: Final = 202639
"""The first SPAN release build that publishes the current conventions."""

_RELEASE_SEGMENT: Final = re.compile(r"r(\d{6})")

# The variants that key their conventions on the panel's release build. Any
# other variant always publishes the current ones.
_FIRMWARE_KEYED_VARIANTS: Final[frozenset[Variant]] = frozenset({"span"})

BessMeterFrame = Literal["device", "enclosure"]
"""Whose frame a hosted BESS's ``meter/active-power`` is published in:
``device`` is the battery's own (positive while discharging), ``enclosure`` the
panel's reading of it (positive while charging, equal to
``power-flows/battery``)."""


def release_build(firmware_version: str | None) -> int | None:
    """The six-digit release build in a SPAN firmware string, or ``None``.

    SPAN firmware publishes ``info/firmware-version`` as ``/``-separated
    segments, for example ``spanos2/r202639/03``; the ``r`` segment names the
    release, and the respin after it never changes a wire convention. Only a
    whole segment of ``r`` plus exactly six digits counts, so ``r2026390``,
    ``xr202639`` and ``example/v0.1.0`` all give ``None``. The first such
    segment wins.
    """
    if firmware_version is None:
        return None
    for segment in firmware_version.strip().split("/"):
        match = _RELEASE_SEGMENT.fullmatch(segment)
        if match is not None:
            return int(match.group(1))
    return None


@dataclass(frozen=True, slots=True)
class FirmwareConventions:
    """The wire conventions that differ across release 202639."""

    bess_meter_frame: BessMeterFrame
    # Whether ``config/user-max-charge-current`` is published at the
    # commissioned maximum before a user sets it, rather than left unpublished.
    user_max_charge_current_preset: bool


_CURRENT: Final = FirmwareConventions(
    bess_meter_frame="device", user_max_charge_current_preset=False
)
_EARLIER: Final = FirmwareConventions(
    bess_meter_frame="enclosure", user_max_charge_current_preset=True
)


def firmware_conventions(variant: Variant, release: int | None) -> FirmwareConventions:
    """The conventions a panel of ``variant`` reporting ``release`` publishes.

    Only a firmware-keyed variant with a release before
    :data:`CURRENT_CONVENTIONS_RELEASE` gets the earlier ones; an unknown release is
    taken to be current."""
    keyed = variant in _FIRMWARE_KEYED_VARIANTS
    if keyed and release is not None and release < CURRENT_CONVENTIONS_RELEASE:
        return _EARLIER
    return _CURRENT
