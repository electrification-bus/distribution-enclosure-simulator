"""Per-circuit relay state, with strict precedence over commands from multiple sources.

The emitter now owns relay state across ticks. Commands arrive from three sources:

1. **Manifest declaration** — the circuit is *locked*: ``relay-behavior`` is
   ``always-on`` or ``non-controllable``, or ``always-on=true`` metadata says so.
   Absolute: the relay can never be opened, regardless of /set or load-shedding
   decisions. See :func:`manifest_physics.relay_locked`, which derives the bit,
   and which the wire layer reads too so the published ``$settable`` cannot
   disagree with what this resolver does.
2. **/set commands** — operator-driven via Homie ``circuit/.../switch/relay/set``
   topic. Authoritative for unlocked circuits, no debounce.
3. **Load shedding** — emitter's ``LoadSheddingDevice`` decisions. Applies only
   when there's no /set override.

Precedence (highest wins):

    locked > /set override > load-shed > default-CLOSED

Locking gates *both* command paths rather than only /set, because that is what
the capability defines: ``relay-controllable`` is true when the relay "can be
opened and closed by command or automatic shed", and
``devices/distribution-enclosure.md`` states from the shed host's side that the
enclosure "never opens a circuit commissioned as permanently ``OFF_GRID`` /
locked". A circuit that were sheddable but not settable is a state the
specification does not permit.

``relay_requester`` reflects the source of the active decision, using the
canonical eBus ``switch/relay-requester`` domain:
- ``CONFIGURATION`` for a locked circuit (commissioned; the relay cannot open)
- ``USER`` for /set
- ``LOAD_SHED`` for load-shed, including on a circuit commissioned never-backup:
  ``devices/distribution-enclosure.md`` says the requester is ``LOAD_SHED``
  whenever "the enclosure's auto-shed logic drives a circuit's relay", with no
  carve-out for how the circuit came to be ``OFF_GRID``
- ``NONE`` for the default-CLOSED state

The producer never sees /set commands. ``Emitter`` registers internal handlers
for ``circuit.switch/relay`` and ``circuit.load-shed/priority``; those handlers
call ``RelayResolver.set_user_override`` (and the priority equivalent on a
sibling state map).

The circuits a multi-load breaker protects sit behind its one relay
(``RelayResolver.share``). The resolver holds that relay's lock, override and
shed decision once, so a command to any of its circuits, from whichever writer,
is a command to all of them, and they always resolve alike."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class RelayState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class RelayRequester(StrEnum):
    # Canonical eBus ``switch/relay-requester`` domain.
    CONFIGURATION = "CONFIGURATION"  # locked circuit; commissioned, cannot open
    USER = "USER"  # /set override active
    LOAD_SHED = "LOAD_SHED"  # load-shed in effect
    NONE = "NONE"  # default-CLOSED, no active requester
    UNKNOWN = "UNKNOWN"  # unregistered / indeterminate


class RelayResolver:
    """Maintains relay state per relay, for the circuit instances behind each.

    Construct empty, register each circuit with its locked flag, share a relay
    among the circuits on each multi-load breaker, then update overrides and
    shed decisions; query ``state()`` for the resolved final state."""

    def __init__(self) -> None:
        # instance_id -> the key of the relay it sits behind: itself, or the
        # first circuit on its multi-load breaker. The maps below are by that key.
        self._relay_of: dict[str, str] = {}
        # locked map: relay -> bool (manifest declaration; immutable post-register)
        self._always_on: dict[str, bool] = {}
        # /set override map: relay -> RelayState | None (None = no override)
        self._user_overrides: dict[str, RelayState | None] = {}
        # load-shed decision map: relay -> bool (True = wants OPEN)
        self._shed: dict[str, bool] = {}

    def _relay(self, instance_id: str, caller: str) -> str:
        relay = self._relay_of.get(instance_id)
        if relay is None:
            raise KeyError(f"{caller} for unregistered instance_id={instance_id!r}")
        return relay

    def register(self, instance_id: str, *, always_on: bool) -> None:
        """Idempotent — re-registering with a different value updates the manifest
        declaration (typical use: emitter restart with edited manifest).

        ``always_on`` is the locked bit: true for ``relay-behavior`` of
        ``always-on`` *or* ``non-controllable``. The name is the hardware's own
        — SPAN commissions this as ``alwaysOn`` and publishes
        ``relay-controllable = !always-on`` — so it is the flag, not a subset of
        it. Derive it with :func:`manifest_physics.relay_locked`.

        A circuit behind a shared relay updates that relay's lock."""
        relay = self._relay_of.setdefault(instance_id, instance_id)
        self._always_on[relay] = always_on
        self._user_overrides.setdefault(relay, None)
        self._shed.setdefault(relay, False)

    def share(self, instance_ids: Sequence[str]) -> None:
        """Put registered circuits, each behind a relay of its own, behind one
        relay, as a multi-load breaker protects its circuits: an override or shed
        decision for any of them is then one for all of them. The relay keeps the
        first circuit's override and shed decision.

        Raises ``KeyError`` for an unregistered circuit, and ``ValueError`` for a
        circuit named twice or already sharing a relay, or where their locks
        differ, since one relay is locked or it is not."""
        if not instance_ids:
            return
        for instance_id in instance_ids:
            self._relay(instance_id, "share")
        behind = list(self._relay_of.values())
        if len(set(instance_ids)) != len(instance_ids) or any(
            self._relay_of[i] != i or behind.count(i) > 1 for i in instance_ids
        ):
            raise ValueError(
                f"share: {', '.join(instance_ids)} must each sit behind a relay of its own"
            )
        if len({self._always_on[i] for i in instance_ids}) > 1:
            raise ValueError(
                f"share: {', '.join(instance_ids)} differ in their relay's lock; one relay "
                "is locked or it is not"
            )
        first, *others = instance_ids
        for other in others:
            self._relay_of[other] = first
            del self._always_on[other], self._user_overrides[other], self._shed[other]

    def set_user_override(self, instance_id: str, state: RelayState | None) -> None:
        """Operator /set or explicit clear. ``state=None`` clears the override
        and lets load-shed (or default-CLOSED) take effect.

        Locked circuits silently drop the override — operator cannot open them."""
        relay = self._relay(instance_id, "set_user_override")
        if self._always_on[relay]:
            return  # absolute: a locked relay ignores /set
        self._user_overrides[relay] = state

    def clear_user_override(self, instance_id: str) -> None:
        self.set_user_override(instance_id, None)

    def set_shed(self, instance_id: str, *, open_relay: bool) -> None:
        """Load-shedding decision. ``open_relay=True`` means the load-shedding
        policy wants this circuit OPEN.

        Locked circuits silently drop the request: the enclosure never opens a
        circuit commissioned locked (``devices/distribution-enclosure.md``)."""
        relay = self._relay(instance_id, "set_shed")
        if self._always_on[relay]:
            return
        self._shed[relay] = open_relay

    def clear_all_shed(self) -> None:
        """Reset every shed decision to False. Called by the emitter at the
        start of each tick before re-running ``LoadSheddingDevice``."""
        for k in self._shed:
            self._shed[k] = False

    def state(self, instance_id: str) -> tuple[RelayState, RelayRequester]:
        """Resolve the final state for ``instance_id``.

        A shed is attributed to ``LOAD_SHED`` whatever made the circuit
        ``OFF_GRID``, a commissioned never-backup circuit included. The
        specification states it without a carve-out — "when the enclosure's
        auto-shed logic drives a circuit's relay, the circuit publishes
        ``switch/relay-requester = LOAD_SHED``"
        (``devices/distribution-enclosure.md``) — and a consumer depends on it:
        restore is defined as "circuits opened by ``LOAD_SHED`` are re-closed"
        (``integration-guides/bess-and-distribution-enclosure.md``), so any
        other value on a shed circuit leaves it open on rejoin.

        The lock is therefore invisible here. It removes the consumer's ability
        to re-prioritise the circuit, which is a ``$settable`` question on
        ``load-shed/priority``, not an attribution question on the relay."""
        relay = self._relay_of.get(instance_id, instance_id)
        if self._always_on.get(relay, False):
            return RelayState.CLOSED, RelayRequester.CONFIGURATION
        override = self._user_overrides.get(relay)
        if override is not None:
            return override, RelayRequester.USER
        if self._shed.get(relay, False):
            return RelayState.OPEN, RelayRequester.LOAD_SHED
        return RelayState.CLOSED, RelayRequester.NONE

    def known(self, instance_id: str) -> bool:
        return instance_id in self._relay_of
