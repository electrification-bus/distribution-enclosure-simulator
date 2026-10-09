"""Profile-driven snapshot → ``PropertyBag`` translator.

The wire-layer graph + mapping table together declare every property the
emitter can publish. The bag builder walks that declared set and pulls each
value out of an ``EbusPanelSnapshot`` using a static lookup table that maps
``(entity_class, capability/property)`` → snapshot accessor.

Two-purpose design:

1. **Mechanical and complete.** Every profile-declared property is considered
   on every tick; if its snapshot value is non-None, it goes into the bag.
   This eliminates the silent-drop bug where the old hand-rolled
   ``_snapshot_to_bag`` published only the ~25% of properties someone happened
   to remember.
2. **Fail loud on schema drift.** If the profile declares a property the
   builder has no source for, construction raises ``EmitterStateError``.
   Adding a new profile property without a corresponding snapshot field is a
   loud build failure rather than a silent missing topic.

Property values that resolve to ``None`` are skipped (Homie 5 allows missing
properties — the property's retained topic just isn't updated this tick).

A number whose profile property carries a ``literal`` form goes into the bag
already written in it (``ebus_panel_sim.wire.literal``), so the diff and the
wire see the panel's text, not the physics model's full-precision float."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from ebus_panel_sim.exceptions import EmitterStateError
from ebus_panel_sim.firmware import BessMeterFrame
from ebus_panel_sim.manifest_physics import WIRE_VALUE_PATHS
from ebus_panel_sim.snapshot import EbusPanelSnapshot
from ebus_panel_sim.wire.graph_builder import BuiltGraph
from ebus_panel_sim.wire.literal import LiteralForm, format_literal
from ebus_panel_sim.wire.mapping_loader import MappingTable
from ebus_panel_sim.wire.profile_loader import ProfileTable
from ebus_panel_sim.wire.property_bag import PropertyBag

# A ``Resolver`` is a function that pulls a single property's value from the
# snapshot. It receives the snapshot and the per-instance id and returns the
# value (or ``None`` to skip publication this tick). Resolvers are pure
# functions over the snapshot; no side effects.
Resolver = Callable[[EbusPanelSnapshot, str], object]


def _panel_resolver(getter: Callable[[EbusPanelSnapshot], object]) -> Resolver:
    """Wrap a panel-level getter so it ignores the per-instance id."""

    def _resolve(snapshot: EbusPanelSnapshot, _instance_id: str) -> object:
        return getter(snapshot)

    return _resolve


# ---------------------------------------------------------------------------
# Static resolver table — one entry per profile-declared property.
#
# Keyed by (entity_class, "<capability>/<property-key>"), matching the canonical
# eBus capability decomposition (info/meter/switch/breaker/load-shed/pcs/
# connection/status/door/soc/shed/shed-forecast/grid/config/power-flows).
#
# Adding a new profile property: add it here AND add a snapshot field. The
# constructor's coverage check raises if you only do one half.
# ---------------------------------------------------------------------------


def _circuit_field(field: str) -> Resolver:
    def _resolve(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
        circuit = snapshot.circuits.get(instance_id)
        if circuit is None:
            return None
        return getattr(circuit, field)

    return _resolve


def _bess_field(field: str) -> Resolver:
    def _resolve(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
        bess = snapshot.battery.get(instance_id)
        if bess is None:
            return None
        return getattr(bess, field)

    return _resolve


def _pv_field(field: str) -> Resolver:
    def _resolve(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
        pv = snapshot.pv.get(instance_id)
        if pv is None:
            return None
        return getattr(pv, field)

    return _resolve


def _evse_field(field: str) -> Resolver:
    def _resolve(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
        evse = snapshot.evse.get(instance_id)
        if evse is None:
            return None
        return getattr(evse, field)

    return _resolve


def _lugs_field(field: str) -> Resolver:
    def _resolve(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
        lugs = snapshot.lugs.get(instance_id)
        if lugs is None:
            return None
        return getattr(lugs, field)

    return _resolve


def _mid_field(field: str) -> Resolver:
    def _resolve(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
        mid = snapshot.mid.get(instance_id)
        if mid is None:
            return None
        return getattr(mid, field)

    return _resolve


def _remote_ct_field(field: str) -> Resolver:
    def _resolve(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
        rct = snapshot.remote_ct.get(instance_id)
        if rct is None:
            return None
        return getattr(rct, field)

    return _resolve


def _circuit_spaces(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
    """``info/spaces`` — the position(s) the breaker occupies, as a comma list.

    A two-pole/tandem breaker lands on multiple tabs (e.g. ``"32,34"``); the
    multi-valued string preserves every position a single scalar would lose."""
    circuit = snapshot.circuits.get(instance_id)
    if circuit is None or not circuit.tabs:
        return None
    return ",".join(str(t) for t in circuit.tabs)


def _circuit_poles(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
    """``breaker/poles`` — 2 for a 240 V (two-pole) circuit, else 1."""
    circuit = snapshot.circuits.get(instance_id)
    if circuit is None:
        return None
    return 2 if circuit.is_240v else 1


def _circuit_breaker(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
    circuit = snapshot.circuits.get(instance_id)
    if circuit is None or circuit.breaker_rating_a is None:
        return None
    return int(circuit.breaker_rating_a)


def _circuit_shed_priority(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
    circuit = snapshot.circuits.get(instance_id)
    if circuit is None:
        return None
    if circuit.priority in ("OFF_GRID", "SOC_THRESHOLD", "NEVER"):
        return circuit.priority
    return "UNKNOWN"


def _circuit_wire_active_power(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
    """Circuit ``active-power`` in the enclosure reference frame.

    The snapshot's ``instant_power_w`` is device-frame (positive = the circuit is
    consuming). The wire is enclosure-frame: positive means power flowing *into*
    the enclosure busbar (a circuit backfeeding, e.g. a PV inverter), negative
    means power flowing *out* of the busbar to a load. Hence the negation.

    The energy accumulators below must be relabelled for the same reason — see
    ``_circuit_wire_imported_energy``."""
    circuit = snapshot.circuits.get(instance_id)
    if circuit is None:
        return None
    return 0.0 if circuit.instant_power_w == 0 else -circuit.instant_power_w


def _circuit_wire_imported_energy(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
    """Circuit ``imported-energy`` — energy imported *by the enclosure* from the
    circuit, i.e. the circuit backfeeding the busbar. That is the snapshot's
    ``produced_energy_wh``."""
    circuit = snapshot.circuits.get(instance_id)
    if circuit is None:
        return None
    return circuit.produced_energy_wh


def _circuit_wire_exported_energy(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
    """Circuit ``exported-energy`` — energy exported *by the enclosure* to the
    circuit, i.e. normal load consumption. That is the snapshot's
    ``consumed_energy_wh``."""
    circuit = snapshot.circuits.get(instance_id)
    if circuit is None:
        return None
    return circuit.consumed_energy_wh


def _bess_wire_active_power(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
    """BESS ``meter/active-power`` in the battery's own frame: positive while
    discharging, as ``devices/bess.md`` defines it. That makes it the negative
    of the panel's ``power-flows/battery`` (positive = toward the battery), and
    ``test_bess_meter_active_power_is_the_negative_of_power_flows_battery``
    pins the pair. ``-0.0`` is normalized so an idle battery never publishes a
    negative zero.

    The default: the span-alpha-test-b2 and reference variants always publish it,
    and the span variant does unless its panel reports a SPAN release before 202639 (see
    :func:`_bess_enclosure_frame_active_power`)."""
    bess = snapshot.battery.get(instance_id)
    if bess is None:
        return None
    return 0.0 if bess.active_power_w == 0 else bess.active_power_w


def _bess_enclosure_frame_active_power(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
    """BESS ``meter/active-power`` in the panel's frame, as a span-variant panel
    on a SPAN release before 202639 publishes it (``ebus_panel_sim.firmware``).

    The snapshot's ``active_power_w`` stays device-frame (positive = the battery
    is discharging); that firmware published the panel's reading of the battery
    instead, positive while *charging*, which is the same quantity and sign as
    the panel's ``power-flows/battery``. Hence the negation, and hence
    ``test_before_202639_the_bess_meter_equals_power_flows_battery``.
    ``-0.0`` is normalized as above."""
    bess = snapshot.battery.get(instance_id)
    if bess is None:
        return None
    return 0.0 if bess.active_power_w == 0 else -bess.active_power_w


# The ``meter/active-power`` resolver for each frame a hosted BESS can publish in.
_BESS_ACTIVE_POWER: Final[Mapping[BessMeterFrame, Resolver]] = MappingProxyType(
    {
        "device": _bess_wire_active_power,
        "enclosure": _bess_enclosure_frame_active_power,
    }
)


def _constant(value: object) -> Resolver:
    def _resolve(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
        del snapshot, instance_id
        return value

    return _resolve


def _upper_lugs_direction(snapshot: EbusPanelSnapshot, instance_id: str) -> object:
    lugs = snapshot.lugs.get(instance_id)
    if lugs is None:
        return None
    return lugs.direction.upper()


# Wire property path ``"<capability>/<property-key>"`` → resolver.
# Keyed by (entity_class, "<cap>/<prop>"). One entry per profile-declared
# property; the constructor verifies coverage at startup.
_RESOLVERS: dict[tuple[str, str], Resolver] = {
    # ---- panel (distribution-enclosure) ---------------------------------
    ("panel", "info/vendor-name"): _panel_resolver(lambda s: s.info.vendor_name),
    ("panel", "info/model"): _panel_resolver(lambda s: s.info.panel_model),
    ("panel", "info/serial-number"): _panel_resolver(lambda s: s.info.serial_number),
    ("panel", "info/hardware-version"): _panel_resolver(lambda s: s.info.hardware_version),
    ("panel", "info/firmware-version"): _panel_resolver(lambda s: s.info.firmware_version),
    ("panel", "info/data-model-version"): _panel_resolver(lambda s: s.info.data_model_version),
    ("panel", "door/state"): _panel_resolver(lambda s: s.door.state),
    ("panel", "meter/busbar-current"): _panel_resolver(lambda s: s.meter.busbar_current_a),
    ("panel", "meter/frequency"): _panel_resolver(lambda s: s.meter.frequency_hz),
    ("panel", "meter/voltage-a"): _panel_resolver(lambda s: s.meter.l1_voltage),
    ("panel", "meter/voltage-b"): _panel_resolver(lambda s: s.meter.l2_voltage),
    ("panel", "breaker/rating"): _panel_resolver(lambda s: s.pcs.main_breaker_rating_a),
    ("panel", "pcs/enabled"): _panel_resolver(lambda s: s.pcs.enabled),
    ("panel", "pcs/active"): _panel_resolver(lambda s: s.pcs.active),
    ("panel", "pcs/import-limit"): _panel_resolver(lambda s: s.pcs.import_limit_a),
    ("panel", "pcs/binding-constraint"): _panel_resolver(lambda s: s.pcs.binding_constraint),
    ("panel", "pcs/feed-import-limit"): _panel_resolver(lambda s: s.pcs.feed_import_limit_a),
    ("panel", "pcs/feed-import-limit-enablement"): _panel_resolver(
        lambda s: s.pcs.feed_import_limit_enablement,
    ),
    ("panel", "pcs/feed-import-limit-active"): _panel_resolver(
        lambda s: s.pcs.feed_import_limit_active,
    ),
    ("panel", "pcs/operator-import-limit"): _panel_resolver(
        lambda s: s.pcs.operator_import_limit_a,
    ),
    ("panel", "pcs/operator-import-limit-enablement"): _panel_resolver(
        lambda s: s.pcs.operator_import_limit_enablement,
    ),
    ("panel", "pcs/operator-import-limit-active"): _panel_resolver(
        lambda s: s.pcs.operator_import_limit_active,
    ),
    ("panel", "pcs/off-grid-import-limit"): _panel_resolver(
        lambda s: s.pcs.off_grid_import_limit_a,
    ),
    ("panel", "pcs/off-grid-import-limit-enablement"): _panel_resolver(
        lambda s: s.pcs.off_grid_import_limit_enablement,
    ),
    ("panel", "pcs/off-grid-import-limit-active"): _panel_resolver(
        lambda s: s.pcs.off_grid_import_limit_active,
    ),
    ("panel", "pcs/requested-import-limit"): _panel_resolver(
        lambda s: s.pcs.requested_import_limit_a,
    ),
    ("panel", "pcs/requested-import-limit-enablement"): _panel_resolver(
        lambda s: s.pcs.requested_import_limit_enablement,
    ),
    ("panel", "pcs/requested-import-limit-active"): _panel_resolver(
        lambda s: s.pcs.requested_import_limit_active,
    ),
    ("panel", "status/relay"): _panel_resolver(lambda s: s.status.main_relay_state),
    ("panel", "status/ethernet"): _panel_resolver(lambda s: s.status.eth0_link),
    ("panel", "status/wifi"): _panel_resolver(lambda s: s.status.wlan_link),
    ("panel", "status/wifi-ssid"): _panel_resolver(lambda s: s.status.wifi_ssid),
    ("panel", "status/cloud-connection"): _panel_resolver(lambda s: s.status.cloud_connection),
    ("panel", "status/postal-code"): _panel_resolver(lambda s: s.status.postal_code),
    ("panel", "status/time-zone"): _panel_resolver(lambda s: s.status.time_zone),
    ("panel", "shed-forecast/total-time-remaining"): _panel_resolver(
        lambda s: s.shed_forecast.total_time_remaining,
    ),
    ("panel", "shed-forecast/time-to-priority-shed"): _panel_resolver(
        lambda s: s.shed_forecast.time_to_priority_shed,
    ),
    ("panel", "shed-forecast/full-charge-total-time-remaining"): _panel_resolver(
        lambda s: s.shed_forecast.full_charge_total_time_remaining,
    ),
    ("panel", "shed-forecast/full-charge-time-to-priority-shed"): _panel_resolver(
        lambda s: s.shed_forecast.full_charge_time_to_priority_shed,
    ),
    ("panel", "shed-forecast/confidence"): _panel_resolver(lambda s: s.shed_forecast.confidence),
    ("panel", "shed/asserted-islanding-state"): _panel_resolver(
        lambda s: s.shed.asserted_islanding_state,
    ),
    ("panel", "shed/policy"): _panel_resolver(lambda s: s.shed.policy),
    ("panel", "power-flows/pv"): _panel_resolver(lambda s: s.power_flows.pv),
    ("panel", "power-flows/battery"): _panel_resolver(lambda s: s.power_flows.battery),
    ("panel", "power-flows/grid"): _panel_resolver(lambda s: s.power_flows.grid),
    ("panel", "power-flows/site"): _panel_resolver(lambda s: s.power_flows.site),
    # ---- circuit --------------------------------------------------------
    ("circuit", "info/name"): _circuit_field("name"),
    ("circuit", "info/spaces"): _circuit_spaces,
    ("circuit", "switch/relay"): _circuit_field("relay_state"),
    ("circuit", "switch/relay-requester"): _circuit_field("relay_requester"),
    ("circuit", "switch/relay-controllable"): _circuit_field("is_user_controllable"),
    ("circuit", "breaker/rating"): _circuit_breaker,
    ("circuit", "breaker/poles"): _circuit_poles,
    ("circuit", "meter/current"): _circuit_field("current_a"),
    ("circuit", "meter/active-power"): _circuit_wire_active_power,
    ("circuit", "meter/imported-energy"): _circuit_wire_imported_energy,
    ("circuit", "meter/exported-energy"): _circuit_wire_exported_energy,
    ("circuit", "load-shed/priority"): _circuit_shed_priority,
    ("circuit", "pcs/managed"): _circuit_field("pcs_managed"),
    ("circuit", "pcs/priority"): _circuit_field("pcs_priority"),
    ("circuit", "connection/feeds-device-id"): _circuit_field("feeds_device_id"),
    ("circuit", "connection/feeds-device-type"): _circuit_field("feeds_device_type"),
    ("circuit", "connection/feeds-device-status"): _circuit_field("feeds_device_status"),
    ("circuit", "connection/fed-by-device-id"): _circuit_field("fed_by_device_id"),
    ("circuit", "connection/fed-by-device-type"): _circuit_field("fed_by_device_type"),
    ("circuit", "connection/fed-by-device-status"): _circuit_field("fed_by_device_status"),
    # ---- lugs -----------------------------------------------------------
    ("lugs", "info/direction"): _upper_lugs_direction,
    ("lugs", "meter/current-a"): _lugs_field("l1_current_a"),
    ("lugs", "meter/current-b"): _lugs_field("l2_current_a"),
    ("lugs", "meter/active-power"): _lugs_field("active_power_w"),
    ("lugs", "meter/imported-energy"): _lugs_field("imported_energy_wh"),
    ("lugs", "meter/exported-energy"): _lugs_field("exported_energy_wh"),
    ("lugs", "connection/fed-by-device-id"): _lugs_field("fed_by_device_id"),
    ("lugs", "connection/fed-by-device-type"): _lugs_field("fed_by_device_type"),
    ("lugs", "connection/fed-by-device-status"): _lugs_field("fed_by_device_status"),
    ("lugs", "connection/feeds-device-id"): _lugs_field("feeds_device_id"),
    ("lugs", "connection/feeds-device-type"): _lugs_field("feeds_device_type"),
    ("lugs", "connection/feeds-device-status"): _lugs_field("feeds_device_status"),
    # ---- bess -----------------------------------------------------------
    ("bess", "info/vendor-name"): _bess_field("vendor_name"),
    ("bess", "info/part-number"): _bess_field("part_number"),
    ("bess", "info/model"): _bess_field("model"),
    ("bess", "info/serial-number"): _bess_field("serial_number"),
    ("bess", "info/firmware-version"): _bess_field("firmware_version"),
    ("bess", "info/nameplate-capacity"): _bess_field("nameplate_capacity_kwh"),
    ("bess", "soc/soc"): _bess_field("soe_percentage"),
    ("bess", "soc/soe"): _bess_field("soe_kwh"),
    ("bess", "meter/active-power"): _bess_wire_active_power,
    ("bess", "status/communication-state"): _bess_field("communication"),
    # ---- pv -------------------------------------------------------------
    ("pv", "info/vendor-name"): _pv_field("vendor_name"),
    ("pv", "info/model"): _pv_field("model"),
    ("pv", "info/serial-number"): _pv_field("serial_number"),
    ("pv", "info/firmware-version"): _pv_field("firmware_version"),
    ("pv", "info/nominal-power"): _pv_field("nominal_power_w"),
    # ---- evse -----------------------------------------------------------
    ("evse", "info/vendor-name"): _evse_field("vendor_name"),
    ("evse", "info/model"): _evse_field("model"),
    ("evse", "info/part-number"): _evse_field("part_number"),
    ("evse", "info/serial-number"): _evse_field("serial_number"),
    ("evse", "info/firmware-version"): _evse_field("firmware_version"),
    ("evse", "switch/lock-state"): _evse_field("lock_state"),
    ("evse", "status/status"): _evse_field("status"),
    ("evse", "meter/advertised-current"): _evse_field("advertised_current_a"),
    ("evse", "config/user-max-charge-current"): _evse_field("user_max_charge_current_a"),
    ("evse", "config/max-charge-current"): _evse_field("max_charge_current_a"),
    # ---- mid ------------------------------------------------------------
    # ---- remote-ct ------------------------------------------------------
    ("remote-ct", "meter/current"): _remote_ct_field("current_a"),
    ("remote-ct", "meter/active-power"): _remote_ct_field("active_power_w"),
    ("remote-ct", "meter/imported-energy"): _remote_ct_field("imported_energy_wh"),
    ("remote-ct", "meter/exported-energy"): _remote_ct_field("exported_energy_wh"),
    # Declared and left unpublished, as a SPAN panel's meter-only circuit does.
    ("remote-ct", "meter/shared-with-device-ids"): lambda snapshot, instance_id: None,
    ("mid", "info/vendor-name"): _mid_field("vendor_name"),
    ("mid", "info/serial-number"): _mid_field("serial_number"),
    ("mid", "info/model"): _mid_field("model"),
    ("mid", "info/firmware-version"): _mid_field("firmware_version"),
    ("mid", "info/hardware-version"): _mid_field("hardware_version"),
    ("mid", "grid/islanding-state"): _mid_field("islanding_state"),
    ("mid", "grid/grid-state"): _mid_field("grid_state"),
    ("mid", "grid/grid-forming-entity"): _mid_field("grid_forming_entity"),
}


# ---------------------------------------------------------------------------
# Bag builder
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _BoundProperty:
    """Resolved property binding ready for per-tick evaluation."""

    entity_class: str
    instance_id: str
    property_path: str
    resolver: Resolver
    literal: LiteralForm | None


class BagBuilder:
    """Walk the wire graph + profiles to populate a ``PropertyBag`` per tick.

    Construction validates that every profile-declared property has a
    resolver; this is the structural check that catches silent-drop drift
    between profile JSONs and the snapshot dataclasses."""

    def __init__(
        self,
        graph: BuiltGraph,
        mapping: MappingTable,
        profiles: ProfileTable,
        wire_values: dict[tuple[str, str, str], object] | None = None,
        *,
        bess_meter_frame: BessMeterFrame = "device",
    ) -> None:
        """``wire_values`` holds the constant commissioning values, keyed by
        ``(entity_class, instance_id, property_path)``, for the paths in
        ``WIRE_VALUE_PATHS``; such a path without an entry stays unpublished.

        ``bess_meter_frame`` picks the frame a hosted BESS publishes
        ``meter/active-power`` in, fixed for the emitter's life by the panel's
        firmware (``ebus_panel_sim.firmware``). It overrides that one entry in
        this instance's copy of the resolver table; the coverage check and the
        binding below read only that copy, never ``_RESOLVERS`` itself. In the
        enclosure frame it is also written in ``power-flows/battery``'s literal
        form, as that firmware did."""
        del mapping  # accepted for API symmetry; not consulted today.
        constants = wire_values or {}
        self._bound: list[_BoundProperty] = []
        resolvers = {
            **_RESOLVERS,
            ("bess", "meter/active-power"): _BESS_ACTIVE_POWER[bess_meter_frame],
        }

        # First pass: structural coverage check. Every profile property the
        # graph references must have a resolver entry. A missing resolver is a
        # programmer error (profile JSON updated without bag-builder follow-up)
        # and we want to fail at construction rather than silently drop topics.
        missing: list[tuple[str, str]] = []
        for ec, profile in profiles.items():
            for cap_name, cap in profile.capabilities.items():
                for prop_key in cap.properties:
                    full = f"{cap_name}/{prop_key}"
                    if (ec, full) not in resolvers and full not in WIRE_VALUE_PATHS.get(ec, ()):
                        missing.append((ec, full))
        if missing:
            joined = ", ".join(f"{ec}.{p}" for ec, p in missing)
            raise EmitterStateError(
                f"BagBuilder: profile-declared properties have no snapshot "
                f"resolver: {joined}. Snapshot dataclasses and wire profiles are "
                f"out of sync.",
            )

        # Properties a profile declares and never values are never bound.
        unvalued = {
            (entity_class, f"{cap_name}/{prop_key}")
            for entity_class, profile in profiles.items()
            for cap_name, cap in profile.capabilities.items()
            for prop_key, prop in cap.properties.items()
            if prop.unvalued
        }

        # Each property's literal form, from its profile declaration.
        literals = {
            (entity_class, f"{cap_name}/{prop_key}"): prop.literal
            for entity_class, profile in profiles.items()
            for cap_name, cap in profile.capabilities.items()
            for prop_key, prop in cap.properties.items()
        }
        if bess_meter_frame == "enclosure":
            # The panel's own reading of the battery, equal to power-flows/battery,
            # and written as that is. The public r202633 capture's only sample is an
            # idle battery's integer 0.
            literals[("bess", "meter/active-power")] = literals.get(
                ("panel", "power-flows/battery")
            )

        # Second pass: bind resolvers to the (entity_class, instance_id,
        # property_path) keys actually present in the graph. The graph already
        # encodes which instances exist for each entity_class.
        for entity_class, instance_id, property_path in graph.properties:
            if (entity_class, property_path) in unvalued:
                continue
            key = (entity_class, instance_id, property_path)
            resolver = (
                _constant(constants.get(key))
                if property_path in WIRE_VALUE_PATHS.get(entity_class, ())
                else resolvers[(entity_class, property_path)]
            )
            self._bound.append(
                _BoundProperty(
                    entity_class=entity_class,
                    instance_id=instance_id,
                    property_path=property_path,
                    resolver=resolver,
                    literal=literals[(entity_class, property_path)],
                ),
            )

    def build(self, snapshot: EbusPanelSnapshot) -> PropertyBag:
        """Pull a value for every bound property; skip ``None`` values
        (Homie 5 lets a property's retained topic stay unchanged when the
        emitter has nothing fresh to say)."""
        bag = PropertyBag(values={})
        for bound in self._bound:
            value = bound.resolver(snapshot, bound.instance_id)
            if value is None:
                continue
            if (
                bound.literal is not None
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                value = format_literal(value, bound.literal)
            bag.set(bound.entity_class, bound.instance_id, bound.property_path, value)
        return bag
