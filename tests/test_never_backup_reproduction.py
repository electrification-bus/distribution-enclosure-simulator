"""The defect, reproduced on a manifest nobody has to change.

Deliberately a module of its own, and deliberately written against the API as
it stood *before* the fix: no `never-backup` metadata key, no
`CircuitPhysics.never_backup`, no `manifest_physics.never_backup`, nothing this
change introduces. Every name it imports exists on both sides of the fix.

That is what makes it a reproduction rather than a specification. Lifted onto
an unfixed checkout on its own it collects and runs, and fails on the assertion
with the inverted value in the message -- not at import, which would only prove
the file is new. `tests/test_never_backup.py` holds the rest of the suite,
including everything that exercises the new commissioning key.

The circuit here is the ordinary one: `default-priority: NEVER`, meaning "never
shed", which is one of the four values of a settable enum
(`capabilities/load-shed`) and is not a lock of any kind. The panel it models
publishes two such circuits, and publishes `$settable = true` on both.
"""

from __future__ import annotations

import json

import pytest

from ebus_panel_sim import (
    DeviceInstance,
    DeviceManifest,
    Emitter,
    SetterRegistry,
    TickInputs,
)
from ebus_panel_sim.wire.profile_loader import Variant

from .conftest import PahoRecorder


def _panel() -> DeviceInstance:
    return DeviceInstance(
        "panel",
        "p1",
        "Span Panel",
        metadata={
            "vendor-name": "Span",
            "serial-number": "p1",
            "firmware-version": "sim/v0.1.0",
            "hardware-version": "rev2",
            "panel-size": "40",
            "main-breaker-rating-a": "200",
            "panel-model": "MAIN_40",
            "postal-code": "94103",
            "time-zone": "America/Los_Angeles",
        },
    )


def _never_priority_circuit() -> DeviceInstance:
    """An ordinary circuit at `NEVER` priority — the manifest every producer
    already writes. No commissioning key of any kind."""
    return DeviceInstance(
        "circuit",
        "kitchen",
        "Kitchen",
        metadata={
            "tab-numbers": "1",
            "breaker-rating-a": "20",
            "default-priority": "NEVER",
            "relay-behavior": "controllable",
            "placement": "upstream-of-lugs",
        },
    )


@pytest.mark.parametrize("variant", ["span", "reference"])
def test_a_never_priority_circuit_is_settable_and_not_never_backup(
    variant: Variant, rec: PahoRecorder
) -> None:
    """One circuit, one tick, two readings of the same fact that cannot both be
    true.

    The published `$settable = true` says this circuit's priority is the
    consumer's to change. `is_never_backup = true` says the circuit was
    commissioned to receive no backup power and its priority is locked. A panel
    cannot mean both at once, and the capture settles which one is real: both of
    its `NEVER`-priority circuits publish `$settable = true`.

    Asserted through what a consumer actually reads — the snapshot the producer
    reads back, and the `$description` this emitter puts on the broker — rather
    than against the graph builder, so it covers the whole path from manifest to
    wire. Run in both variants because neither publishes a `never-backup`
    property at all: the flat flag is retired in v1.0 and expressed structurally
    through `$settable`, so the snapshot field is the only surface still
    carrying the name, and the only place this defect can hide.
    """
    manifest = DeviceManifest(instances=(_panel(), _never_priority_circuit()))
    em = Emitter(manifest, SetterRegistry(), variant=variant)
    em.start()
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0})
    )

    assert snap.circuits["kitchen"].is_never_backup is False, (
        "a NEVER-priority circuit is not commissioned never-backup: NEVER is an "
        "ordinary settable value meaning 'never shed'"
    )

    retained = rec.retained
    description = json.loads(retained["ebus/5/kitchen/$description"])
    priority = description["nodes"]["load-shed"]["properties"]["priority"]
    assert priority.get("settable") is True, (
        "a NEVER-priority circuit publishes $settable=true on load-shed/priority; "
        f"got {priority.get('settable')!r}"
    )
    assert retained["ebus/5/kitchen/load-shed/priority"] == "NEVER"
    # The flat `never-backup` property is retired in v1.0, so no variant may
    # resurrect it: the lock is published as the absence of `$settable`, and
    # this circuit does not carry the lock in the first place.
    assert not any(t.endswith("/never-backup") for t in retained)
