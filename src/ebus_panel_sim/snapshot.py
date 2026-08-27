"""Per-tick snapshot dataclasses — emitter-internal data model.

The emitter constructs these inside ``publish_tick`` from ``TickInputs`` plus
manifest physics, native device state, and integrated accumulators. Producers
do not build snapshots directly; they push ``TickInputs`` and read state back
via ``Emitter.last_snapshot``. The snapshot is the cache key for the wire-layer
property differ: only fields that change between ticks are republished. The
``Ebus-`` prefix reflects the producer-side data model for the eBus convention;
the shape is residential-energy-panel-generic (panel + circuits + battery + PV
+ EVSE + PCS) and is decoupled from any specific transport profile.

Phase 2 reshape: panel-level state is split into capability sub-dataclasses
(``info``, ``door``, ``meter``, ``status``, ``pcs``, ``power_flows``) that
mirror the wire profile's capability nodes. BESS and PV are pluralized into
``dict[str, ...]`` keyed by ``instance_id``. ``EbusLugsSnapshot`` is added so
the lugs profile can be populated cleanly. ``EbusPcsSnapshot`` is folded into
``EbusPanelPcs`` and removed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(slots=True)
class EbusCircuitSnapshot:
    """Transport-agnostic circuit state."""

    circuit_id: str
    name: str

    relay_state: str  # OPEN | CLOSED | UNKNOWN
    instant_power_w: float  # Positive = consumption, negative = production
    produced_energy_wh: float
    consumed_energy_wh: float

    tabs: list[int]
    priority: str  # MUST_HAVE | NICE_TO_HAVE | NON_ESSENTIAL | NEVER | SOC_THRESHOLD | OFF_GRID
    is_user_controllable: bool
    # `priority` is shed-eligible AND the relay is controllable -- both conjuncts
    # of the retired flat `sheddable`. A relay-locked circuit never opens, so it
    # is not sheddable whatever its priority says.
    is_sheddable: bool
    # The installer commissioning lock, not a priority value: this circuit is
    # permanently `OFF_GRID` and its `load-shed/priority` is not settable
    # (published as the absence of `$settable`). `priority == "NEVER"` is the
    # unrelated, ordinary, settable value meaning "never shed" -- deriving this
    # flag from it is inverted in both directions.
    is_never_backup: bool

    device_type: str = "circuit"
    is_240v: bool = False
    current_a: float | None = None
    breaker_rating_a: float | None = None
    always_on: bool = False
    pcs_managed: bool = True
    pcs_priority: int = 0
    relay_requester: str = "UNKNOWN"
    energy_accum_update_time_s: int = 0
    instant_power_update_time_s: int = 0

    # ``connection`` capability — downstream topology edge, populated once the
    # parent-child wire flip assembles the site graph; None in the flat model.
    feeds_device_id: str | None = None
    feeds_device_type: str | None = None
    feeds_device_status: str | None = None


@dataclass(slots=True)
class EbusPvSnapshot:
    """PV inverter metadata."""

    node_id: str = ""
    feed_circuit_id: str | None = ""
    vendor_name: str | None = None
    model: str | None = None
    nominal_power_w: float | None = None
    firmware_version: str | None = None
    serial_number: str | None = None
    relative_position: str | None = None


@dataclass(slots=True)
class EbusEvseSnapshot:
    """EV Charger (EVSE) state."""

    node_id: str
    feed_circuit_id: str | None
    status: str = "UNKNOWN"
    lock_state: str = "UNKNOWN"
    advertised_current_a: float | None = None
    max_charge_current_a: int | None = None
    user_max_charge_current_a: int | None = None

    vendor_name: str | None = None
    model: str | None = None
    part_number: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None


@dataclass(slots=True)
class EbusBatterySnapshot:
    """Battery state."""

    instance_id: str = ""
    soe_percentage: float | None = None
    soe_kwh: float | None = None
    active_power_w: float = 0.0  # Positive = discharging, negative = charging

    vendor_name: str | None = None
    part_number: str | None = None
    model: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    relative_position: str | None = None
    feed_circuit_id: str | None = None
    nameplate_capacity_kwh: float | None = None
    connected: bool | None = None
    grid_state: str | None = None
    communication: Literal["OK", "LOST", "DEGRADED"] | None = None


@dataclass(slots=True)
class EbusLugsSnapshot:
    """Lugs (upstream / downstream) — ``info`` + ``meter`` capability subset."""

    instance_id: str
    direction: Literal["upstream", "downstream"]
    feed: str | None = None
    l1_current_a: float | None = None
    l2_current_a: float | None = None
    active_power_w: float = 0.0
    imported_energy_wh: float = 0.0
    exported_energy_wh: float = 0.0

    # ``connection`` capability — upstream (fed-by) and downstream (feeds) edges.
    fed_by_device_id: str | None = None
    fed_by_device_type: str | None = None
    fed_by_device_status: str | None = None
    feeds_device_id: str | None = None
    feeds_device_type: str | None = None
    feeds_device_status: str | None = None


@dataclass(slots=True)
class EbusMidSnapshot:
    """Microgrid Interconnect Device (MID) — ``info`` + ``grid`` capabilities."""

    instance_id: str
    vendor_name: str | None = None
    serial_number: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    hardware_version: str | None = None
    islanding_state: str = "UNKNOWN"
    grid_state: str = "UNKNOWN"
    grid_forming_entity: str | None = None


# ---------------------------------------------------------------------------
# Panel capability sub-dataclasses — one per capability node on the panel
# device profile (panel.json). Each sub-dataclass owns the fields that map to
# its capability's properties, so the bag builder can iterate mechanically.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EbusPanelInfo:
    """Identity + topology — ``info`` capability node on the panel device."""

    serial_number: str
    firmware_version: str
    vendor_name: str | None = None
    hardware_version: str | None = None
    panel_size: int = 0
    panel_model: str | None = None
    schema_topology: Literal["flat", "parent-child"] = "flat"
    data_model_version: str = "1.0"


@dataclass(slots=True)
class EbusPanelDoor:
    """``door`` capability node."""

    state: str = "CLOSED"
    proximity_proven: bool = True


@dataclass(slots=True)
class EbusPanelMeter:
    """``meter`` capability node — voltage + grid power + main-meter energies."""

    instant_grid_power_w: float = 0.0
    main_meter_energy_consumed_wh: float = 0.0
    main_meter_energy_produced_wh: float = 0.0
    feedthrough_power_w: float = 0.0
    feedthrough_energy_consumed_wh: float = 0.0
    feedthrough_energy_produced_wh: float = 0.0
    l1_voltage: float | None = None
    l2_voltage: float | None = None
    upstream_l1_current_a: float | None = None
    upstream_l2_current_a: float | None = None
    downstream_l1_current_a: float | None = None
    downstream_l2_current_a: float | None = None


@dataclass(slots=True)
class EbusPanelStatus:
    """``status`` capability node — networking, cloud, location, relay state."""

    main_relay_state: str = "CLOSED"
    eth0_link: bool = True
    wlan_link: bool = True
    wwan_link: bool = False
    wifi_ssid: str | None = None
    cloud_connection: str = "CONNECTED"
    postal_code: str | None = None
    time_zone: str | None = None
    uptime_s: int = 0


@dataclass(slots=True)
class EbusPanelPcs:
    """``pcs`` capability node — power-control system + grid-topology flags.

    Folds in everything that lived on the (now-removed) standalone
    ``EbusPcsSnapshot`` so panel PCS state has exactly one home."""

    main_breaker_rating_a: int | None = None
    binding_constraint: str = "NONE"
    dominant_power_source: str | None = None
    grid_state: str | None = None
    dsm_state: str = "DSM_ON_GRID"
    current_run_config: str = ""
    enabled: bool = False
    active: bool = False
    import_limit_a: float = 0.0
    feed_import_limit_a: float = 0.0
    feed_import_limit_enablement: str = "UNCONFIGURED"
    feed_import_limit_active: bool = False
    operator_import_limit_a: float = 0.0
    operator_import_limit_enablement: str = "UNCONFIGURED"
    operator_import_limit_active: bool = False
    off_grid_import_limit_a: float = 0.0
    off_grid_import_limit_enablement: str = "UNCONFIGURED"
    off_grid_import_limit_active: bool = False
    requested_import_limit_a: float = 0.0
    requested_import_limit_enablement: str = "UNCONFIGURED"
    requested_import_limit_active: bool = False


@dataclass(slots=True)
class EbusPanelPowerFlows:
    """``power-flows`` capability node."""

    pv: float | None = None
    battery: float | None = None
    grid: float | None = None
    site: float | None = None


@dataclass(slots=True)
class EbusPanelShed:
    """``shed`` capability node — consumer islanding override + shed policy."""

    asserted_islanding_state: str = "NONE"
    policy: str | None = None


@dataclass(slots=True)
class EbusPanelShedForecast:
    """``shed-forecast`` capability node — off-grid runtime estimates.

    These are simulator dynamics: None until the runtime model drives them, in
    which case the property's retained topic is simply not published (Homie 5
    permits a property to have no value yet)."""

    total_time_remaining: int | None = None
    time_to_priority_shed: int | None = None
    full_charge_total_time_remaining: int | None = None
    full_charge_time_to_priority_shed: int | None = None
    confidence: str | None = None


@dataclass(slots=True)
class EbusPanelSnapshot:
    """Complete panel state — single point-in-time view.

    Top-level fields hold capability sub-dataclasses that mirror the panel
    wire profile's capability nodes. Per-instance children (circuits,
    batteries, PV, EVSE, lugs) live in dicts keyed by ``instance_id``."""

    info: EbusPanelInfo
    door: EbusPanelDoor = field(default_factory=EbusPanelDoor)
    meter: EbusPanelMeter = field(default_factory=EbusPanelMeter)
    status: EbusPanelStatus = field(default_factory=EbusPanelStatus)
    pcs: EbusPanelPcs = field(default_factory=EbusPanelPcs)
    power_flows: EbusPanelPowerFlows = field(default_factory=EbusPanelPowerFlows)
    shed: EbusPanelShed = field(default_factory=EbusPanelShed)
    shed_forecast: EbusPanelShedForecast = field(default_factory=EbusPanelShedForecast)
    circuits: dict[str, EbusCircuitSnapshot] = field(default_factory=dict)
    battery: dict[str, EbusBatterySnapshot] = field(default_factory=dict)
    pv: dict[str, EbusPvSnapshot] = field(default_factory=dict)
    evse: dict[str, EbusEvseSnapshot] = field(default_factory=dict)
    lugs: dict[str, EbusLugsSnapshot] = field(default_factory=dict)
    mid: dict[str, EbusMidSnapshot] = field(default_factory=dict)
