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
- ``CONFIGURATION`` for a locked circuit (commissioned; the relay cannot open),
  and for a never-backup circuit the enclosure opens at islanding — the same
  attribution, because both states were decided at commissioning
- ``USER`` for /set
- ``LOAD_SHED`` for load-shed
- ``NONE`` for the default-CLOSED state

The producer never sees /set commands. ``Emitter`` registers internal handlers
for ``circuit.switch/relay`` and ``circuit.load-shed/priority``; those handlers
call ``RelayResolver.set_user_override`` (and the priority equivalent on a
sibling state map)."""

from __future__ import annotations

from enum import StrEnum


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
    """Maintains relay state per circuit instance.

    Construct empty, register each circuit with its locked flag, then update
    overrides and shed decisions; query ``state()`` for the resolved final
    state."""

    def __init__(self) -> None:
        # locked map: instance_id -> bool (manifest declaration; immutable post-register)
        self._always_on: dict[str, bool] = {}
        # never-backup map: instance_id -> bool. Attribution only; see register().
        self._priority_locked: dict[str, bool] = {}
        # /set override map: instance_id -> RelayState | None (None = no override)
        self._user_overrides: dict[str, RelayState | None] = {}
        # load-shed decision map: instance_id -> bool (True = wants OPEN)
        self._shed: dict[str, bool] = {}

    def register(
        self, instance_id: str, *, always_on: bool, priority_locked: bool = False
    ) -> None:
        """Idempotent — re-registering with a different value updates the manifest
        declaration (typical use: emitter restart with edited manifest).

        ``always_on`` is the locked bit: true for ``relay-behavior`` of
        ``always-on`` *or* ``non-controllable``. The name is the hardware's own
        — SPAN commissions this as ``alwaysOn`` and publishes
        ``relay-controllable = !always-on`` — so it is the flag, not a subset of
        it. Derive it with :func:`manifest_physics.relay_locked`.

        ``priority_locked`` is the circuit's *other* commissioning lock,
        never-backup, derived with :func:`manifest_physics.never_backup`. It
        does not gate anything here — a never-backup circuit is commissioned
        permanently ``OFF_GRID``, so the enclosure opens it at islanding like
        any other ``OFF_GRID`` circuit — it changes only who that open is
        attributed to. Defaulted so a caller that knows nothing of the flag
        keeps the previous behaviour."""
        self._always_on[instance_id] = always_on
        self._priority_locked[instance_id] = priority_locked
        self._user_overrides.setdefault(instance_id, None)
        self._shed.setdefault(instance_id, False)

    def set_user_override(self, instance_id: str, state: RelayState | None) -> None:
        """Operator /set or explicit clear. ``state=None`` clears the override
        and lets load-shed (or default-CLOSED) take effect.

        Locked circuits silently drop the override — operator cannot open them."""
        if instance_id not in self._always_on:
            raise KeyError(f"set_user_override for unregistered instance_id={instance_id!r}")
        if self._always_on[instance_id]:
            return  # absolute: a locked relay ignores /set
        self._user_overrides[instance_id] = state

    def clear_user_override(self, instance_id: str) -> None:
        self.set_user_override(instance_id, None)

    def set_shed(self, instance_id: str, *, open_relay: bool) -> None:
        """Load-shedding decision. ``open_relay=True`` means the load-shedding
        policy wants this circuit OPEN.

        Locked circuits silently drop the request: the enclosure never opens a
        circuit commissioned locked (``devices/distribution-enclosure.md``)."""
        if instance_id not in self._always_on:
            raise KeyError(f"set_shed for unregistered instance_id={instance_id!r}")
        if self._always_on[instance_id]:
            return
        self._shed[instance_id] = open_relay

    def clear_all_shed(self) -> None:
        """Reset every shed decision to False. Called by the emitter at the
        start of each tick before re-running ``LoadSheddingDevice``."""
        for k in self._shed:
            self._shed[k] = False

    def state(self, instance_id: str) -> tuple[RelayState, RelayRequester]:
        """Resolve the final state for ``instance_id``.

        A shed on a never-backup circuit is attributed to ``CONFIGURATION``
        rather than ``LOAD_SHED``. Both open the relay; they differ in who
        decided, and for this circuit the decision was made at commissioning,
        not by the policy running now. The migration guide maps the flat
        ``NEVER_BACKUP`` requester onto ``CONFIGURATION`` -- "the commissioning
        lock is now expressed structurally via ``load-shed/priority = OFF_GRID``
        with ``$settable = false``; ``CONFIGURATION`` captures the source
        attribution" -- while flat ``BACKUP`` (an ordinary shed) maps to
        ``LOAD_SHED``. Firmware distinguished the two, so collapsing them here
        would lose a distinction the wire vocabulary still carries, and it is
        the same attribution a relay-locked circuit already reports.

        At rest the lock says nothing: it does not hold the relay closed, so a
        closed never-backup circuit reports ``NONE`` like any other."""
        if self._always_on.get(instance_id, False):
            return RelayState.CLOSED, RelayRequester.CONFIGURATION
        override = self._user_overrides.get(instance_id)
        if override is not None:
            return override, RelayRequester.USER
        if self._shed.get(instance_id, False):
            requester = (
                RelayRequester.CONFIGURATION
                if self._priority_locked.get(instance_id, False)
                else RelayRequester.LOAD_SHED
            )
            return RelayState.OPEN, requester
        return RelayState.CLOSED, RelayRequester.NONE

    def known(self, instance_id: str) -> bool:
        return instance_id in self._always_on
