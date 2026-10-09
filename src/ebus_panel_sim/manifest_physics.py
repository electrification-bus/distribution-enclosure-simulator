"""Typed accessor over ``DeviceInstance.metadata`` for physics-relevant fields.

The producer puts strings in ``DeviceInstance.metadata``; the emitter reads them
through this view. One central place to define every key the emitter consumes,
its parser, its default, and its validation rules. Adding a new physics field
is a one-line addition here plus a docs note in the README.

Validation runs once when ``ManifestPhysicsView`` is constructed (typically at
``Emitter.__init__``). Missing required keys, malformed values, and contradictory
physics (e.g. ``dipole`` flag inconsistent with ``tab-numbers`` count) raise
``ManifestValidationError`` with the offending instance_id."""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from ebus_panel_sim.conventions.tab_legs import Leg, legs_for_tabs
from ebus_panel_sim.exceptions import ManifestValidationError
from ebus_panel_sim.firmware import release_build

if TYPE_CHECKING:
    from ebus_panel_sim.manifest import DeviceInstance, DeviceManifest


# ---------------------------------------------------------------------------
# Per-entity-class typed views — built once, queried many times.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PanelPhysics:
    serial_number: str
    vendor_name: str
    firmware_version: str
    hardware_version: str
    panel_size: int
    main_breaker_rating_a: int | None
    panel_model: str
    postal_code: str
    time_zone: str
    service_voltage_v: float
    line_voltage_v: float
    islandable: bool
    # ``flat`` = legacy single-Homie-device shape (one device, many nodes).
    # ``parent-child`` = post-migration shape where children become separate
    # Homie devices. Producer-overridable via metadata key ``schema-topology``.
    topology: Literal["flat", "parent-child"] = "flat"
    # From `_panel_wire_values`; see `WIRE_VALUE_PATHS`.
    wire_values: dict[str, str] = field(default_factory=dict)
    # The Wi-Fi SSID the panel reports when a tick's envelope gives none.
    wifi_ssid: str | None = None
    # The off-grid import limit as commissioned: its enablement, and the limit,
    # published only while ENABLED. Without an enablement none of the three
    # off-grid properties is published.
    off_grid_import_limit_enablement: str | None = None
    off_grid_import_limit_a: float | None = None

    @property
    def release_build(self) -> int | None:
        """The SPAN release build ``firmware_version`` names (``spanos2/r202633/02``
        is ``202633``), or None when it names none. See
        :func:`ebus_panel_sim.firmware.release_build`."""
        return release_build(self.firmware_version)


@dataclass(frozen=True, slots=True)
class LugsPhysics:
    direction: str  # "upstream" | "downstream"
    # From `_lugs_wire_values`; see `WIRE_VALUE_PATHS`.
    wire_values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CircuitPhysics:
    tabs: tuple[int, ...]
    legs: tuple[Leg, ...]
    dipole: bool
    breaker_rating_a: float
    default_priority: str
    relay_behavior: str  # "controllable" | "always-on" | "non-controllable"
    placement: str  # "upstream-of-lugs" | "downstream-of-lugs"
    # The locked bit, from `relay_locked`: true for either non-controllable
    # spelling. Named for the hardware flag it mirrors (`relay-controllable =
    # !always-on`), not for the one `relay-behavior` value that shares the name.
    always_on: bool
    initial_consumed_wh: float
    initial_produced_wh: float
    pcs_priority: int = 0
    # The *other* commissioning lock, from `never_backup`: this circuit is
    # commissioned permanently `OFF_GRID` and its priority is not settable.
    # Independent of `default_priority`'s value, and of `always_on`.
    #
    # Defaulted, unlike `always_on`, because this dataclass is exported and a
    # caller constructing one by hand predates the field. `False` is the
    # parser's own absent-key answer, so a hand-built instance and a parsed
    # manifest that omits the key agree. It sits last because a defaulted field
    # cannot precede an undefaulted one, not because it belongs here.
    never_backup: bool = False
    # From `commissioned_system`: the circuit the panel adds for a commissioned
    # PV (``"pv"``) or battery (``"backup"``) system, or None.
    commissioned_system: str | None = None
    # From `_circuit_wire_values`; see `WIRE_VALUE_PATHS`.
    wire_values: dict[str, str] = field(default_factory=dict)

    @property
    def priority_locked(self) -> bool:
        """Whether ``load-shed/priority`` is not settable on this circuit."""
        return self.never_backup or self.commissioned_system is not None


@dataclass(frozen=True, slots=True)
class BessPhysics:
    vendor_name: str
    nameplate_capacity_kwh: float
    initial_soe_kwh: float | None
    part_number: str | None
    model: str | None
    serial_number: str | None
    firmware_version: str | None
    relative_position: str | None
    feed: str | None


@dataclass(frozen=True, slots=True)
class PvPhysics:
    vendor_name: str
    nominal_power_w: float
    inverter_type: str  # "hybrid" | "ac-coupled"
    model: str | None
    serial_number: str | None
    firmware_version: str | None
    relative_position: str | None
    feed: str | None


@dataclass(frozen=True, slots=True)
class EvsePhysics:
    vendor_name: str
    model: str
    part_number: str
    serial_number: str
    firmware_version: str
    max_current_a: float
    feed: str | None


@dataclass(frozen=True, slots=True)
class MidPhysics:
    """Microgrid Interconnect Device identity. Grid state (islanding-state /
    grid-state / grid-forming-entity) is dynamic, derived per-tick from the
    grid-online signal rather than parsed here."""

    vendor_name: str | None
    serial_number: str | None
    model: str | None
    firmware_version: str | None
    hardware_version: str | None


# ---------------------------------------------------------------------------
# Top-level view
# ---------------------------------------------------------------------------


_VALID_PRIORITIES = frozenset(
    {
        "MUST_HAVE",
        "NICE_TO_HAVE",
        "NON_ESSENTIAL",
        "NEVER",
        "SOC_THRESHOLD",
        "OFF_GRID",
    }
)
_VALID_RELAY_BEHAVIORS = frozenset({"controllable", "always-on", "non-controllable"})
_VALID_PLACEMENTS = frozenset({"upstream-of-lugs", "downstream-of-lugs"})
_VALID_COMMISSIONED_SYSTEMS = frozenset({"pv", "backup"})
# The lowest user charge-current ceiling an EVSE accepts, in amps.
EVSE_MIN_CHARGE_CURRENT_A = 6
_VALID_LUGS_DIRECTIONS = frozenset({"upstream", "downstream"})
_VALID_INVERTER_TYPES = frozenset({"hybrid", "ac-coupled"})
_VALID_TOPOLOGIES = frozenset({"flat", "parent-child"})


_DEPRECATED_KEYS = {"feed-circuit-id": "feed"}


def _warn_deprecated_keys(manifest: DeviceManifest) -> None:
    """Raise one ``DeprecationWarning`` per manifest naming the instances at fault.

    Deliberately not raised from the leaf that reads the key. Python's default
    filter only *shows* a ``DeprecationWarning`` attributed to ``__main__``, so
    where the warning appears to come from decides whether the producer ever
    sees it at all. Warning from ``_feed`` attributes it to this module, which
    both blames the wrong file and hides it outside a test runner: a deprecation
    nobody can see is worse than none, because it lets us believe we gave notice.

    Raising it here, once, from the public constructor puts the blame on the
    caller's own line. ``stacklevel=3`` walks this frame and ``__init__``'s. A
    producer who goes through ``Emitter`` instead lands on the emitter's
    construction of this view rather than their own line, which is the one case
    this cannot fix from a single site: there is no stack depth that is correct
    for both entry points.
    """
    for key, replacement in _DEPRECATED_KEYS.items():
        culprits = sorted({i.instance_id for i in manifest.instances if key in i.metadata})
        if culprits:
            warnings.warn(
                f"metadata key {key!r} is deprecated; use {replacement!r} instead "
                f"(on {', '.join(culprits)})",
                DeprecationWarning,
                stacklevel=3,
            )


class ManifestPhysicsView:
    """Validated, typed view over a ``DeviceManifest``'s metadata.

    Built once at ``Emitter`` construction time. Holds parsed physics for every
    instance keyed by ``instance_id``. Raises ``ManifestValidationError`` at
    construction if any instance is missing required keys or has malformed
    values; the emitter never sees a partially-validated manifest."""

    def __init__(self, manifest: DeviceManifest) -> None:
        _warn_deprecated_keys(manifest)
        self._panel: PanelPhysics | None = None
        self._lugs: dict[str, LugsPhysics] = {}
        self._circuits: dict[str, CircuitPhysics] = {}
        self._bess: dict[str, BessPhysics] = {}
        self._pv: dict[str, PvPhysics] = {}
        self._evse: dict[str, EvsePhysics] = {}
        self._mid: dict[str, MidPhysics] = {}

        for inst in manifest.instances:
            ec = inst.entity_class
            try:
                if ec == "panel":
                    if self._panel is not None:
                        raise ManifestValidationError(
                            "Multiple panel instances in manifest; expected exactly one",
                        )
                    self._panel = _parse_panel(inst)
                elif ec == "lugs":
                    self._lugs[inst.instance_id] = _parse_lugs(inst)
                elif ec == "circuit":
                    self._circuits[inst.instance_id] = _parse_circuit(inst)
                elif ec == "bess":
                    self._bess[inst.instance_id] = _parse_bess(inst)
                elif ec == "pv":
                    self._pv[inst.instance_id] = _parse_pv(inst)
                elif ec == "evse":
                    self._evse[inst.instance_id] = _parse_evse(inst)
                elif ec == "mid":
                    self._mid[inst.instance_id] = _parse_mid(inst)
                # Unknown entity_class: leave to graph builder to reject.
            except ManifestValidationError as exc:
                raise ManifestValidationError(f"{ec}/{inst.instance_id}: {exc}") from exc

        if self._panel is None:
            raise ManifestValidationError("Manifest has no panel instance")

    # -- accessors -----------------------------------------------------------

    @property
    def panel(self) -> PanelPhysics:
        assert self._panel is not None  # checked in __init__
        return self._panel

    def lugs(self, instance_id: str) -> LugsPhysics:
        return self._lugs[instance_id]

    def circuit(self, instance_id: str) -> CircuitPhysics:
        return self._circuits[instance_id]

    def bess(self, instance_id: str) -> BessPhysics:
        return self._bess[instance_id]

    def pv(self, instance_id: str) -> PvPhysics:
        return self._pv[instance_id]

    def evse(self, instance_id: str) -> EvsePhysics:
        return self._evse[instance_id]

    def all_circuits(self) -> dict[str, CircuitPhysics]:
        return dict(self._circuits)

    def all_lugs(self) -> dict[str, LugsPhysics]:
        return dict(self._lugs)

    def all_bess(self) -> dict[str, BessPhysics]:
        return dict(self._bess)

    def all_pv(self) -> dict[str, PvPhysics]:
        return dict(self._pv)

    def all_evse(self) -> dict[str, EvsePhysics]:
        return dict(self._evse)

    def mid(self, instance_id: str) -> MidPhysics:
        return self._mid[instance_id]

    def all_mid(self) -> dict[str, MidPhysics]:
        return dict(self._mid)


# ---------------------------------------------------------------------------
# Parsers — one per entity_class.
# ---------------------------------------------------------------------------


def relay_locked(md: dict[str, str]) -> bool:
    """Whether this circuit's relay is locked, from its raw metadata.

    Locked means no path opens it: not ``/set``, not the enclosure's load-shed.
    ``capabilities/switch.md`` defines ``relay-controllable`` as true when the
    relay "can be opened and closed by command or automatic shed", and
    ``devices/distribution-enclosure.md`` says the enclosure "never opens a
    circuit commissioned as permanently ``OFF_GRID`` / locked" -- so the two
    paths share one bit rather than having a gate each.

    Read from raw metadata rather than from ``CircuitPhysics`` because the wire
    layer needs the same answer while building a device description, before any
    physics view exists. One derivation, two callers.

    Either spelling locks: ``non-controllable`` and ``always-on`` are one
    commissioning flag on the hardware this models -- SPAN publishes
    ``relay-controllable = !always-on`` -- and they differ here only in the
    operator's intent. The explicit ``always-on`` key is OR'd rather than
    consulted as a default because producers write it out: a clone of a real
    panel emits ``always-on: "false"`` beside ``relay-behavior:
    non-controllable``, and a default chain would let that unlock the circuit.

    Absent metadata is controllable, which is what a manifest that declares no
    relay behaviour at all means.
    """
    return (
        md.get("relay-behavior", "controllable") != "controllable"
        or _opt_bool(md, "always-on", default=False)
        or commissioned_system(md) is not None
    )


def unvalued_paths(md: dict[str, str]) -> frozenset[str]:
    """The ``capability/property`` paths a device declares and never values, from
    its ``unvalued`` metadata (comma-separated), as the panel it reproduces did.

    Any device class takes the key, so it is read here rather than by a class's
    parser."""
    raw = _opt_str(md, "unvalued")
    if raw is None:
        return frozenset()
    return frozenset(path.strip() for path in raw.split(",") if path.strip())


def never_backup(md: dict[str, str]) -> bool:
    """Whether this circuit's load-shed priority is locked, from its raw metadata.

    Locked means the circuit was commissioned to receive no backup power: it is
    permanently ``OFF_GRID`` and no consumer may re-prioritise it. The eBus
    schema migration guide maps the retired flat ``never-backup`` boolean onto
    exactly one thing, the Homie ``$settable`` attribute of
    ``load-shed/priority`` -- "published with ``$settable = !never-backup``" --
    and describes the result as "locked-priority circuits (commissioned
    permanently ``OFF_GRID``) appear as ``priority = OFF_GRID, $settable =
    false``; user-configurable circuits appear as ``$settable = true``".

    It is a commissioning *input*, never derived from the priority value. The
    guide is explicit that "the three flat booleans are independent
    commissioning inputs stored as separate fields in each circuit's
    commissioning state, so the derivation is a simple per-input rule".
    ``NEVER`` in particular is an ordinary settable value meaning "never shed",
    and a captured production enclosure publishes two circuits at ``NEVER``
    with ``$settable = true`` on the same property -- a combination no
    value-derived flag can produce.

    Read from raw metadata rather than from ``CircuitPhysics`` for the same
    reason as :func:`relay_locked`: the wire layer needs the same answer while
    building a device description, before any physics view exists. One
    derivation, two callers.

    Absent metadata is user-configurable, which is what a manifest that declares
    no commissioning lock at all means.
    """
    return _opt_bool(md, "never-backup", default=False)


def main_breaker_rating(md: dict[str, str]) -> int | None:
    """The panel's main-breaker rating in amps, from its raw metadata, or None
    when commissioning records none (absent, empty or 0). Without one the panel
    publishes no ``breaker`` capability."""
    raw = _opt_str(md, "main-breaker-rating-a")
    if raw is None:
        return None
    try:
        return int(raw) or None
    except ValueError as exc:
        raise ManifestValidationError(
            f"key 'main-breaker-rating-a': not an int ({raw!r})"
        ) from exc


def commissioned_system(md: dict[str, str]) -> str | None:
    """Which commissioned system this circuit is the panel's entry for, if any.

    A SPAN panel adds a circuit for an in-panel commissioned PV system
    (``"pv"``) or battery system (``"backup"``). Such a circuit locks both its
    relay and its load-shed priority, which stays ``NEVER``. Read from raw
    metadata for the same reason as :func:`relay_locked`."""
    return _opt_str(md, "commissioned-system")


def priority_locked(md: dict[str, str]) -> bool:
    """Whether ``load-shed/priority`` is not settable, from raw metadata: the
    circuit is commissioned never-backup or is a commissioned-system circuit."""
    return never_backup(md) or commissioned_system(md) is not None


# Commissioning facts published verbatim, keyed by wire property path. Each
# value is published only where the variant's profile declares that path, so a
# variant that does not publish a property ignores its key.


def _panel_wire_values(md: dict[str, str]) -> dict[str, str | None]:
    return {
        "info/name": _opt_str(md, "site-name"),
        "info/address-lines": _opt_str(md, "address-lines"),
        "info/locality": _opt_str(md, "locality"),
        "info/region": _opt_str(md, "region"),
        "info/country-code": _opt_str(md, "country-code"),
        "info/latitude": _opt_str(md, "latitude"),
        "info/longitude": _opt_str(md, "longitude"),
        "info/utility-meter-serial-number": _opt_str(md, "utility-meter-serial-number"),
    }


def _circuit_wire_values(md: dict[str, str]) -> dict[str, str | None]:
    shared = _opt_str(md, "shared-with-device-ids")
    return {
        "info/tags": _opt_str(md, "tags"),
        "info/locations": _opt_str(md, "locations"),
        "info/dedicated": _opt_str(md, "dedicated"),
        "info/nominal-voltage": _opt_str(md, "nominal-voltage"),
        "breaker/protection-functions": _opt_str(md, "protection-functions"),
        "meter/shared-with-device-ids": shared,
        "switch/shared-with-device-ids": shared,
        "connection/feeds-role": _opt_str(md, "feeds-role"),
        "connection/backed-up": _opt_str(md, "backed-up"),
    }


def _lugs_wire_values(md: dict[str, str]) -> dict[str, str | None]:
    return {
        "connection/feeds-role": _opt_str(md, "feeds-role"),
        "connection/backed-up": _opt_str(md, "backed-up"),
        "connection/service-rating": _opt_str(md, "service-rating-a"),
        "connection/overcurrent-protection": _opt_str(md, "overcurrent-protection-a"),
    }


def _present(values: dict[str, str | None]) -> dict[str, str]:
    return {path: value for path, value in values.items() if value is not None}


_OFF_GRID_ENABLEMENTS = frozenset({"UNSPECIFIED", "UNCONFIGURED", "DISABLED", "ENABLED"})


def _off_grid_import_limit(md: dict[str, str]) -> tuple[str | None, float | None]:
    """The commissioned off-grid import limit. An ENABLED limit needs its value."""
    enablement = _opt_str(md, "off-grid-import-limit-enablement")
    if enablement is not None and enablement not in _OFF_GRID_ENABLEMENTS:
        raise ManifestValidationError(
            f"key 'off-grid-import-limit-enablement': must be one of "
            f"{sorted(_OFF_GRID_ENABLEMENTS)}, got {enablement!r}"
        )
    limit = _opt_float(md, "off-grid-import-limit-a", math.nan)
    if enablement == "ENABLED" and math.isnan(limit):
        raise ManifestValidationError(
            "key 'off-grid-import-limit-a': required while the limit is ENABLED"
        )
    return enablement, None if math.isnan(limit) else limit


def _require(md: dict[str, str], key: str) -> str:
    if key not in md:
        raise ManifestValidationError(f"missing required metadata key {key!r}")
    return md[key]


def _opt_float(md: dict[str, str], key: str, default: float) -> float:
    if key not in md:
        return default
    try:
        return float(md[key])
    except ValueError as exc:
        raise ManifestValidationError(f"key {key!r}: not a float ({md[key]!r})") from exc


def _opt_int(md: dict[str, str], key: str, default: int) -> int:
    if key not in md:
        return default
    try:
        return int(md[key])
    except ValueError as exc:
        raise ManifestValidationError(f"key {key!r}: not an int ({md[key]!r})") from exc


def _opt_bool(md: dict[str, str], key: str, default: bool) -> bool:
    if key not in md:
        return default
    raw = md[key].strip().lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ManifestValidationError(f"key {key!r}: not a bool ({md[key]!r})")


def _opt_str(md: dict[str, str], key: str) -> str | None:
    value = md.get(key)
    return value if value not in (None, "") else None


def _req_float(md: dict[str, str], key: str) -> float:
    raw = _require(md, key)
    try:
        return float(raw)
    except ValueError as exc:
        raise ManifestValidationError(f"key {key!r}: not a float ({raw!r})") from exc


def _req_int(md: dict[str, str], key: str) -> int:
    raw = _require(md, key)
    try:
        return int(raw)
    except ValueError as exc:
        raise ManifestValidationError(f"key {key!r}: not an int ({raw!r})") from exc


def _parse_panel(inst: DeviceInstance) -> PanelPhysics:
    md = inst.metadata
    topology_raw = md.get("schema-topology", "flat")
    if topology_raw not in _VALID_TOPOLOGIES:
        raise ManifestValidationError(
            f"key 'schema-topology': must be one of {sorted(_VALID_TOPOLOGIES)}, "
            f"got {topology_raw!r}"
        )
    # ``cast`` would also work here, but a direct comparison keeps the Literal
    # narrow without an explicit import-only typing helper.
    topology: Literal["flat", "parent-child"] = (
        "parent-child" if topology_raw == "parent-child" else "flat"
    )
    off_grid_enablement, off_grid_limit = _off_grid_import_limit(md)
    return PanelPhysics(
        serial_number=_require(md, "serial-number"),
        vendor_name=_require(md, "vendor-name"),
        firmware_version=_opt_str(md, "firmware-version") or _require(md, "software-version"),
        hardware_version=_require(md, "hardware-version"),
        panel_size=_req_int(md, "panel-size"),
        main_breaker_rating_a=main_breaker_rating(md),
        panel_model=_require(md, "panel-model"),
        postal_code=_require(md, "postal-code"),
        time_zone=_require(md, "time-zone"),
        wifi_ssid=_opt_str(md, "wifi-ssid"),
        off_grid_import_limit_enablement=off_grid_enablement,
        off_grid_import_limit_a=off_grid_limit,
        service_voltage_v=_opt_float(md, "service-voltage-v", 240.0),
        line_voltage_v=_opt_float(md, "line-voltage-v", 120.0),
        islandable=_opt_bool(md, "islandable", False),
        topology=topology,
        wire_values=_present(_panel_wire_values(md)),
    )


def _parse_lugs(inst: DeviceInstance) -> LugsPhysics:
    direction = _require(inst.metadata, "direction")
    if direction not in _VALID_LUGS_DIRECTIONS:
        raise ManifestValidationError(
            f"key 'direction': must be one of {sorted(_VALID_LUGS_DIRECTIONS)}, got {direction!r}"
        )
    return LugsPhysics(direction=direction, wire_values=_present(_lugs_wire_values(inst.metadata)))


def _parse_circuit(inst: DeviceInstance) -> CircuitPhysics:
    md = inst.metadata
    raw_tabs = _require(md, "tab-numbers")
    try:
        tabs = tuple(int(t.strip()) for t in raw_tabs.split(",") if t.strip())
    except ValueError as exc:
        raise ManifestValidationError(
            f"key 'tab-numbers': not a comma-separated int list ({raw_tabs!r})"
        ) from exc
    if not tabs:
        raise ManifestValidationError("key 'tab-numbers': must list at least one tab")
    try:
        legs = legs_for_tabs(tabs)
    except ValueError as exc:
        raise ManifestValidationError(f"key 'tab-numbers': {exc}") from exc
    dipole_declared = _opt_bool(md, "dipole", default=len(tabs) > 1)
    # NOTE: ``dipole`` + leg-spanning is not strictly validated. Real SPAN panels
    # use slot numberings where two adjacent breaker positions on the same leg
    # can still be ganged as a "240 V" feed (e.g. tabs 20+22). The convention
    # in ``conventions/tab_legs.py`` is informational for per-leg current
    # calculation; producers are trusted to declare dipole correctly.
    if not dipole_declared and len(tabs) > 1:
        raise ManifestValidationError(
            f"dipole=false but {len(tabs)} tabs declared; single-tab circuits only"
        )

    priority = _require(md, "default-priority")
    if priority not in _VALID_PRIORITIES:
        raise ManifestValidationError(
            f"key 'default-priority': must be one of {sorted(_VALID_PRIORITIES)}, got {priority!r}"
        )

    relay_behavior = _require(md, "relay-behavior")
    if relay_behavior not in _VALID_RELAY_BEHAVIORS:
        raise ManifestValidationError(
            f"key 'relay-behavior': must be one of {sorted(_VALID_RELAY_BEHAVIORS)}, "
            f"got {relay_behavior!r}"
        )

    placement = _require(md, "placement")
    if placement not in _VALID_PLACEMENTS:
        raise ManifestValidationError(
            f"key 'placement': must be one of {sorted(_VALID_PLACEMENTS)}, got {placement!r}"
        )

    always_on = relay_locked(md)
    is_never_backup = never_backup(md)
    # Contradictory physics, rejected rather than silently rewritten. A
    # never-backup circuit *is* commissioned permanently ``OFF_GRID``, so a
    # manifest that locks one at another priority states two incompatible things
    # about one commissioning state, and there is no way to publish both. Having
    # the lock quietly override the declared value would be the same class of
    # defect this key exists to fix -- a published priority nobody wrote -- and
    # would hide the producer's mistake instead of naming it. This way the
    # published value is always the one the manifest declares.
    if is_never_backup and priority != "OFF_GRID":
        raise ManifestValidationError(
            "key 'never-backup': a never-backup circuit is commissioned permanently "
            f"OFF_GRID, so 'default-priority' must be 'OFF_GRID', got {priority!r}"
        )

    system = commissioned_system(md)
    if system is not None and system not in _VALID_COMMISSIONED_SYSTEMS:
        raise ManifestValidationError(
            f"key 'commissioned-system': must be one of "
            f"{sorted(_VALID_COMMISSIONED_SYSTEMS)}, got {system!r}"
        )
    # Rejected for the same reason as never-backup above: a commissioned-system
    # circuit's priority is fixed at NEVER.
    if system is not None and priority != "NEVER":
        raise ManifestValidationError(
            "key 'commissioned-system': a commissioned-system circuit's priority is "
            f"fixed at NEVER, so 'default-priority' must be 'NEVER', got {priority!r}"
        )

    return CircuitPhysics(
        tabs=tabs,
        legs=legs,
        dipole=dipole_declared,
        breaker_rating_a=_req_float(md, "breaker-rating-a"),
        default_priority=priority,
        relay_behavior=relay_behavior,
        placement=placement,
        always_on=always_on,
        never_backup=is_never_backup,
        commissioned_system=system,
        wire_values=_present(_circuit_wire_values(md)),
        pcs_priority=_opt_int(md, "pcs-priority", 0),
        initial_consumed_wh=_opt_float(md, "initial-consumed-wh", 0.0),
        initial_produced_wh=_opt_float(md, "initial-produced-wh", 0.0),
    )


def _feed(md: dict[str, str]) -> str | None:
    """Read the ``feed`` metadata key, honouring the deprecated ``feed-circuit-id``.

    ``feed-circuit-id`` was the original name and is still accepted so existing
    manifests keep working, but it is deliberately absent from the README's
    metadata table: documenting it would entrench two names for one concept.

    Resolution only. The `DeprecationWarning` is raised once per manifest by
    `_warn_deprecated_keys`, not here; see that function for why.
    """
    return _opt_str(md, "feed") or _opt_str(md, "feed-circuit-id")


def _parse_bess(inst: DeviceInstance) -> BessPhysics:
    md = inst.metadata
    initial_soe: float | None = None
    if "initial-soe-kwh" in md:
        initial_soe = _opt_float(md, "initial-soe-kwh", 0.0)
    return BessPhysics(
        vendor_name=_require(md, "vendor-name"),
        nameplate_capacity_kwh=_req_float(md, "nameplate-capacity-kwh"),
        initial_soe_kwh=initial_soe,
        part_number=_opt_str(md, "part-number"),
        model=_opt_str(md, "model"),
        serial_number=_opt_str(md, "serial-number"),
        firmware_version=_opt_str(md, "firmware-version") or _opt_str(md, "software-version"),
        relative_position=_opt_str(md, "relative-position") or "UPSTREAM",
        feed=_feed(md),
    )


def _parse_pv(inst: DeviceInstance) -> PvPhysics:
    md = inst.metadata
    inverter_type = _require(md, "inverter-type")
    if inverter_type not in _VALID_INVERTER_TYPES:
        raise ManifestValidationError(
            f"key 'inverter-type': must be one of {sorted(_VALID_INVERTER_TYPES)}, "
            f"got {inverter_type!r}"
        )
    return PvPhysics(
        vendor_name=_require(md, "vendor-name"),
        nominal_power_w=_req_float(md, "nominal-power-w"),
        inverter_type=inverter_type,
        model=_opt_str(md, "model"),
        serial_number=_opt_str(md, "serial-number"),
        firmware_version=_opt_str(md, "firmware-version") or _opt_str(md, "software-version"),
        relative_position=_opt_str(md, "relative-position") or "IN_PANEL",
        feed=_feed(md),
    )


def _parse_evse(inst: DeviceInstance) -> EvsePhysics:
    md = inst.metadata
    return EvsePhysics(
        vendor_name=_require(md, "vendor-name"),
        model=_require(md, "model"),
        part_number=_require(md, "part-number"),
        serial_number=_require(md, "serial-number"),
        firmware_version=_opt_str(md, "firmware-version") or _require(md, "software-version"),
        max_current_a=_req_float(md, "max-current-a"),
        feed=_feed(md),
    )


def _parse_mid(inst: DeviceInstance) -> MidPhysics:
    md = inst.metadata
    return MidPhysics(
        vendor_name=_opt_str(md, "vendor-name"),
        serial_number=_opt_str(md, "serial-number"),
        model=_opt_str(md, "model"),
        firmware_version=_opt_str(md, "firmware-version") or _opt_str(md, "software-version"),
        hardware_version=_opt_str(md, "hardware-version"),
    )


# The property paths each entity class can take from its metadata verbatim.
WIRE_VALUE_PATHS: dict[str, frozenset[str]] = {
    "panel": frozenset(_panel_wire_values({})),
    "circuit": frozenset(_circuit_wire_values({})),
    "lugs": frozenset(_lugs_wire_values({})),
}
