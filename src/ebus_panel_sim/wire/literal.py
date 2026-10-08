"""The literal form of a published number, as the panel writes it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

LiteralKind = Literal["integer", "fixed", "shortest"]

_DECIMAL_PLACES = re.compile(r"^(\d+)dp$")


@dataclass(frozen=True, slots=True)
class LiteralForm:
    """How a property's numbers are written.

    ``integer``: rounded to a whole number. ``fixed``: rounded to ``places``
    digits after the point, trailing zeros kept. ``shortest``: not rounded, and
    written with only the digits the value needs, so an integral value has no
    point at all.

    The span profile writes a number in the shortest form wherever the public
    MAIN 32 captures show no rounding rule for it, so a value the panel may not
    round is not rounded here: the BESS ``nameplate-capacity`` and the PV
    ``nominal-power``, whose only samples (``81``, ``4640``) are integral, and the
    EVSE ``meter/advertised-current`` and ``pcs/off-grid-import-limit``, which
    neither capture publishes."""

    kind: LiteralKind
    places: int = 0
    """Digits after the point; only a ``fixed`` form has any."""

    def __post_init__(self) -> None:
        if self.places < 0 or (self.kind != "fixed" and self.places):
            raise ValueError(f"a {self.kind} literal form takes no {self.places} places")


def format_literal(value: float, form: LiteralForm) -> str:
    """Write `value` in `form`: rounded half away from zero, and never as negative zero.

    The panel never publishes `-0.0` or an exponent, so a reading that rounds to
    zero from below is written as zero.
    """
    exact = Decimal(repr(value))
    if form.kind == "shortest":
        rounded = exact.quantize(Decimal(1)) if exact == exact.to_integral_value() else exact
    else:
        quantum = Decimal(1).scaleb(-form.places)
        rounded = exact.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded.is_zero():
        rounded = abs(rounded)
    return f"{rounded:f}"


def parse_literal_form(raw: object) -> LiteralForm | None:
    """A profile's ``literal`` entry: ``"integer"``, ``"shortest"``, or ``"<n>dp"``
    for n fixed decimal places. ``None`` for anything else, which the caller reports."""
    if raw == "integer":
        return LiteralForm("integer")
    if raw == "shortest":
        return LiteralForm("shortest")
    match = _DECIMAL_PLACES.match(raw) if isinstance(raw, str) else None
    return LiteralForm("fixed", int(match.group(1))) if match else None
