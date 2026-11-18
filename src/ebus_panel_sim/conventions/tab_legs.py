"""Tab-to-leg assignment convention for US residential split-phase panels.

A US residential load center numbers its spaces odd down the left column and even
down the right. Its bus stabs alternate between L1 and L2 row by row, each stab
serving both spaces of its row: spaces 1 and 2 land on L1, 3 and 4 on L2, 5 and 6
on L1 again. A 240 V two-pole breaker takes two spaces one above the other in a
column (e.g. 1 and 3), so it spans both legs. Every two-pole circuit in the
reference captures sits on spaces n and n + 2, as this layout requires.

Numbering is absolute: a panel whose spaces start at 9, as the MAIN 16 and MAIN 40
captures' do, puts 9 and 10 on L1. The eBus specification leaves the numbering to
the manufacturer, "typically odd positions on the left bus and even on the right,
or sequential" (distribution-enclosure.md, "Enclosure-specific: physical
position"). This module implements the first; a panel numbered sequentially down
one column is not modelled.

This module is the single source of truth for that convention. The lugs' per-leg
currents in ``PanelMeter`` go through ``legs_for_tabs`` rather than computing a
tab's leg directly. A circuit's meter current does not depend on legs: a circuit
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
    """Return the leg of each tab in ``tabs``, US residential convention: rows
    alternate, starting on L1, and the two spaces of a row share a leg.

    Examples:
        >>> legs_for_tabs((1,))
        (<Leg.L1: 'L1'>,)
        >>> legs_for_tabs((2,))       # the same row as 1
        (<Leg.L1: 'L1'>,)
        >>> legs_for_tabs((3,))
        (<Leg.L2: 'L2'>,)
        >>> legs_for_tabs((1, 3))     # a two-pole breaker, one space above the other
        (<Leg.L1: 'L1'>, <Leg.L2: 'L2'>)
        >>> legs_for_tabs((1, 2))     # one row: both on L1, invalid for a two-pole circuit
        (<Leg.L1: 'L1'>, <Leg.L1: 'L1'>)

    The function does not enforce that a two-pole circuit spans both legs; that
    is the validation job of ``ManifestPhysicsView``, which sees the ``dipole``
    flag.

    Raises ``ValueError`` if any tab is < 1 (panel tabs are 1-indexed)."""
    if any(t < 1 for t in tabs):
        raise ValueError(f"tab numbers must be >= 1; got {tabs!r}")
    return tuple(Leg.L1 if (t - 1) // 2 % 2 == 0 else Leg.L2 for t in tabs)
