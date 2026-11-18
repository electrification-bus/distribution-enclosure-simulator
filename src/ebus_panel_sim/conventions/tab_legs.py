"""Tab-to-leg assignment convention for US residential split-phase panels.

US residential load centers alternate breaker slots between L1 and L2: odd-numbered
tabs land on L1, even-numbered tabs land on L2. A 240 V dipole circuit occupies
two adjacent tabs (e.g. 1 and 2) — one per leg.

This module is the single source of truth for that convention. The lugs' per-leg
currents in ``PanelMeter`` go through ``legs_for_tabs`` rather than reaching for
``tab % 2`` directly. A circuit's meter current does not depend on legs: a circuit
alone reads its power over its voltage, and circuits sharing a breaker's meter read
it per breaker space (``panel_meter.shared_meter_current_a``).

Future support for non-US panels (European single-phase, 3-phase commercial) lands
here as an additional ``Convention`` enum + dispatch, without touching call sites."""

from __future__ import annotations

from enum import StrEnum


class Leg(StrEnum):
    """A panel power leg. ``L1`` and ``L2`` are the two 120 V legs of a US
    residential split-phase service; line-to-line voltage between them is 240 V."""

    L1 = "L1"
    L2 = "L2"


def legs_for_tabs(tabs: tuple[int, ...]) -> tuple[Leg, ...]:
    """Return the leg assignment for each tab in ``tabs``, US residential
    convention: odd tabs → L1, even tabs → L2.

    Examples:
        >>> legs_for_tabs((1,))
        (<Leg.L1: 'L1'>,)
        >>> legs_for_tabs((2,))
        (<Leg.L2: 'L2'>,)
        >>> legs_for_tabs((1, 2))     # standard dipole
        (<Leg.L1: 'L1'>, <Leg.L2: 'L2'>)
        >>> legs_for_tabs((1, 3))     # both on L1 — invalid for a dipole
        (<Leg.L1: 'L1'>, <Leg.L1: 'L1'>)

    The function does not enforce dipole leg-spanning — that's the validation
    job of ``ManifestPhysicsView``, which sees the ``dipole`` flag.

    Raises ``ValueError`` if any tab is < 1 (panel tabs are 1-indexed)."""
    if any(t < 1 for t in tabs):
        raise ValueError(f"tab numbers must be >= 1; got {tabs!r}")
    return tuple(Leg.L1 if t % 2 == 1 else Leg.L2 for t in tabs)
