"""Circuits on one multi-load breaker, behind its one meter and one relay.

A tandem or quad breaker protects several circuits through one CT and one relay.
Some SPAN panels publish such a breaker as one circuit device per protected
circuit, each naming the others in ``meter/shared-with-device-ids`` and
``switch/shared-with-device-ids`` (spanio/SPAN-API-Client-Docs#21). That is the
eBus shared-hardware declaration (``framework.md`` §"Capability coverage and
shared hardware"): every member names every other, and the shared ``meter``'s
values describe the whole group, its energy registers accumulating the group's
flow once (``capabilities/meter.md`` §"Shared metering hardware"). The emitter
models it under a variant whose circuit profile declares both lists
(``declares_shared_breakers``).

The specification also models the same hardware as a feed circuit carrying the
meter and relay above load circuits carrying only their breakers
(``devices/circuit.md`` §"Multi-load breakers (tandem and quad)"). SPAN plans to
move to that model; this module is the declaration SPAN publishes today."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ebus_panel_sim.exceptions import ManifestValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ebus_panel_sim.manifest_physics import CircuitPhysics
    from ebus_panel_sim.wire.profile_loader import ProfileTable

# What one CT, one relay and one breaker position fix for every circuit behind
# them, with what the definition gives it as: a metadata key, or the relay's lock,
# which `relay-behavior`, `always-on` or `commissioned-system` sets.
_SHARED_FACTS: tuple[tuple[str, Callable[[CircuitPhysics], object]], ...] = (
    ("'initial-consumed-wh'", lambda c: c.initial_consumed_wh),
    ("'initial-produced-wh'", lambda c: c.initial_produced_wh),
    ("the relay's lock", lambda c: c.always_on),
    ("'default-priority'", lambda c: c.default_priority),
    ("'never-backup'", lambda c: c.never_backup),
    ("'placement'", lambda c: c.placement),
)


def declares_shared_breakers(profiles: ProfileTable) -> bool:
    """Whether a variant publishes a multi-load breaker as one circuit device per
    protected circuit, declaring the group on each: its circuit profile declares
    both ``meter/shared-with-device-ids`` and ``switch/shared-with-device-ids``,
    since the breaker shares its meter and its relay alike.

    The span and reference variants do not, their circuit profiles declaring
    neither list: MAIN 32, which the span variant models, publishes one circuit
    per breaker space and no shared lists (spanio/SPAN-API-Client-Docs#21)."""
    circuit = profiles.get("circuit")
    return circuit is not None and all(
        (cap := circuit.capabilities.get(node)) is not None
        and "shared-with-device-ids" in cap.properties
        for node in ("meter", "switch")
    )


def shared_breakers(circuits: Mapping[str, CircuitPhysics]) -> dict[str, tuple[str, ...]]:
    """Each circuit on a multi-load breaker, mapped to every circuit on that breaker
    (itself included) in manifest order. A circuit on a breaker of its own is left
    out.

    Raises ``ManifestValidationError`` where the circuits cannot be one breaker: a
    list naming the circuit itself or anything but another circuit, members that do
    not all name each other, members that disagree on what the shared hardware
    fixes (the register's seed, the relay's lock and priority, the placement), or
    members that do not sit where one breaker can. A breaker sits on one space, as a
    tandem does, or on the two spaces of a two-pole circuit on it, as a quad and a
    tandem-quad do, with every circuit on it inside them."""
    order = list(circuits)
    breakers: dict[str, tuple[str, ...]] = {}
    for cid, circuit in circuits.items():
        if not circuit.shared_with:
            continue
        where = f"{cid} key 'shared-with-device-ids'"
        if cid in circuit.shared_with:
            raise ManifestValidationError(f"{where}: names the circuit itself")
        unknown = [other for other in circuit.shared_with if other not in circuits]
        if unknown:
            raise ManifestValidationError(
                f"{where}: names {', '.join(unknown)}, which is not a circuit in the manifest"
            )
        members = {cid, *circuit.shared_with}
        for other in circuit.shared_with:
            if {other, *circuits[other].shared_with} != members:
                raise ManifestValidationError(
                    f"{where}: lists {', '.join(circuit.shared_with)}, but {other} lists "
                    f"{', '.join(circuits[other].shared_with) or 'none'}; every circuit on a "
                    "shared breaker names all the others"
                )
        breakers[cid] = tuple(sorted(members, key=order.index))
    for breaker in dict.fromkeys(breakers.values()):
        for what, fact in _SHARED_FACTS:
            values = {m: fact(circuits[m]) for m in breaker}
            if len(set(values.values())) > 1:
                raise ManifestValidationError(
                    f"{', '.join(breaker)} share a breaker but differ in {what} "
                    f"({', '.join(f'{m}: {v}' for m, v in values.items())}); one meter, "
                    "relay and position have one"
                )
        _check_footprint(breaker, circuits)
    return breakers


def _check_footprint(breaker: tuple[str, ...], circuits: Mapping[str, CircuitPhysics]) -> None:
    """Refuse circuits that no one breaker could hold: one breaker sits on one
    space, or on two that a two-pole circuit on it spans."""
    spaces = sorted({tab for m in breaker for tab in circuits[m].tabs})
    if len(spaces) == 1 or (
        len(spaces) == 2 and any(sorted(circuits[m].tabs) == spaces for m in breaker)
    ):
        return
    raise ManifestValidationError(
        f"{', '.join(breaker)} share a breaker but sit on spaces "
        f"{', '.join(str(t) for t in spaces)}; a breaker sits on one space, or on the two "
        "spaces of a two-pole circuit on it"
    )
