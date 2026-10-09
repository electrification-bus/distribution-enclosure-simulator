"""The ``span-alpha-test-b2`` variant.

It layers ``profiles/span-alpha-test-b2/`` on the span overlay: added commissioning
properties, a narrower ``relay-requester``, ``UNKNOWN`` in panel ``info/model``, a
settable EVSE ``lock-state``, no EVSE ``meter``, and no ``pv`` device."""

from __future__ import annotations

import json

import pytest

from ebus_panel_sim import (
    BESSCommunication,
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    EbusPanelPowerFlows,
    Emitter,
    ManifestValidationError,
    SetterRegistry,
    TickInputs,
    Variant,
)
from ebus_panel_sim.manifest_physics import WIRE_VALUE_PATHS
from ebus_panel_sim.wire.literal import LiteralForm
from ebus_panel_sim.wire.profile_loader import load_profiles

from .conftest import PahoRecorder
from .test_connection import _manifest

_ADDED_CIRCUIT_PROPS = (
    "info/tags",
    "info/locations",
    "info/dedicated",
    "info/nominal-voltage",
    "breaker/protection-functions",
    "meter/shared-with-device-ids",
    "switch/shared-with-device-ids",
    "connection/fed-by-device-id",
    "connection/backed-up",
    "connection/feeds-role",
)


def _paths(variant: Variant, entity_class: str) -> set[str]:
    profile = load_profiles(variant=variant)[entity_class]
    return {f"{c}/{k}" for c, cap in profile.capabilities.items() for k in cap.properties}


def test_the_variant_adds_properties_the_span_variant_lacks() -> None:
    for path in _ADDED_CIRCUIT_PROPS:
        assert path in _paths("span-alpha-test-b2", "circuit")
        assert path not in _paths("span", "circuit")
    for path in ("info/latitude", "meter/busbar-current", "meter/frequency"):
        assert path in _paths("span-alpha-test-b2", "panel")
        assert path not in _paths("span", "panel")
    assert "connection/service-rating" in _paths("span-alpha-test-b2", "lugs")


def test_the_variant_redeclares_the_span_properties_it_changes() -> None:
    profiles = load_profiles(variant="span-alpha-test-b2")
    requester = profiles["circuit"].capabilities["switch"].properties["relay-requester"].format
    assert requester is not None
    assert "CONFIGURATION" not in requester.split(",")
    assert "CIRCUIT_SCHEDULER" in requester.split(",")
    model = profiles["panel"].capabilities["info"].properties["model"].format
    assert model is not None and "UNKNOWN" in model.split(",")
    assert profiles["evse"].capabilities["switch"].properties["lock-state"].settable is True
    assert "meter" not in profiles["evse"].capabilities
    assert "meter" in load_profiles(variant="span")["evse"].capabilities


def test_the_panel_model_format_lists_unknown_first() -> None:
    """As every reference capture declares it."""
    model = load_profiles(variant="span-alpha-test-b2")["panel"].capabilities["info"]
    assert model.properties["model"].format == "UNKNOWN,MAIN_16,MLO_24,MAIN_32,MAIN_40,MLO_48"


def test_every_number_the_variant_computes_has_a_literal_form() -> None:
    """Frequency with two decimals and the busbar current with one, as the reference
    captures show; a commissioning number is published as written instead."""
    profiles = load_profiles(variant="span-alpha-test-b2")
    meter = profiles["panel"].capabilities["meter"].properties
    assert meter["frequency"].literal == LiteralForm("fixed", 2)
    assert meter["busbar-current"].literal == LiteralForm("fixed", 1)
    missing = sorted(
        (entity_class, f"{cap_name}/{key}")
        for entity_class, profile in profiles.items()
        for cap_name, cap in profile.capabilities.items()
        for key, prop in cap.properties.items()
        if prop.datatype in ("integer", "float")
        and prop.literal is None
        and f"{cap_name}/{key}" not in WIRE_VALUE_PATHS.get(entity_class, frozenset())
    )
    assert not missing


def test_the_variant_writes_power_flows_and_the_meter_only_circuit_with_one_decimal() -> None:
    """As every reference capture shows, where it values them: power-flows pv,
    battery and grid, the off-grid import limit, and the meter-only circuit's
    readings."""
    one = LiteralForm("fixed", 1)
    profiles = load_profiles(variant="span-alpha-test-b2")
    for cap, key in (
        ("power-flows", "pv"),
        ("power-flows", "battery"),
        ("power-flows", "grid"),
        ("pcs", "off-grid-import-limit"),
    ):
        assert profiles["panel"].capabilities[cap].properties[key].literal == one, key
    for key, prop in profiles["remote-ct"].capabilities["meter"].properties.items():
        if prop.datatype == "float":
            assert prop.literal == one, key
    span = load_profiles(variant="span")["panel"].capabilities["power-flows"].properties
    assert span["grid"].literal == LiteralForm("integer")


def _variant_manifest() -> DeviceManifest:
    """The connection-test site without its pv device, plus commissioning values."""
    instances = []
    for inst in _manifest().instances:
        if inst.entity_class == "pv":
            continue
        md = dict(inst.metadata)
        if inst.entity_class == "panel":
            md |= {"site-name": "Home", "latitude": "37.77", "country-code": "US"}
        elif inst.instance_id == "solar":
            md |= {"feeds-role": "SOLAR", "dedicated": "true", "nominal-voltage": "240"}
        elif inst.instance_id == "lugs-upstream":
            md |= {"service-rating-a": "200"}
        instances.append(
            DeviceInstance(inst.entity_class, inst.instance_id, inst.display_name, md)
        )
    return DeviceManifest(instances=tuple(instances))


def _started(
    rec: PahoRecorder, manifest: DeviceManifest, variant: Variant = "span-alpha-test-b2"
) -> Emitter:
    em = Emitter(manifest, SetterRegistry(), variant=variant)
    em.start()
    em.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0}))
    return em


def test_commissioning_values_are_published_as_written(rec: PahoRecorder) -> None:
    _started(rec, _variant_manifest())
    retained = rec.retained
    assert retained["ebus/5/abc-123/info/name"] == "Home"
    assert retained["ebus/5/abc-123/info/latitude"] == "37.77"
    assert retained["ebus/5/abc-123/info/country-code"] == "US"
    assert retained["ebus/5/solar/connection/feeds-role"] == "SOLAR"
    assert retained["ebus/5/solar/info/dedicated"] == "true"
    assert retained["ebus/5/solar/info/nominal-voltage"] == "240"
    assert retained["ebus/5/lugs-upstream/connection/service-rating"] == "200"
    # Absent keys stay unpublished.
    assert "ebus/5/kitchen/connection/feeds-role" not in retained
    assert "ebus/5/abc-123/info/locality" not in retained


def test_the_span_variant_ignores_the_commissioning_keys(rec: PahoRecorder) -> None:
    _started(rec, _variant_manifest(), variant="span")
    assert "ebus/5/abc-123/info/name" not in rec.retained
    assert "ebus/5/solar/connection/feeds-role" not in rec.retained


@pytest.mark.parametrize(
    ("instance", "key", "raw"),
    [
        *(
            ("abc-123", "latitude", raw)
            for raw in (
                "north",
                "nan",
                "inf",
                "1e3",
                "-0.0",
                "1_000.5",
                " 120.0",
                "+5",
                "12.",
                ".5",
                "\u0663\u0667.7",  # Arabic-Indic digits, which float() accepts
            )
        ),
        *(
            ("lugs-upstream", "service-rating-a", raw)
            for raw in ("1e3", "1_000", " 7", "+5", "-0", "200.0", "\uff12\uff10\uff10")
        ),
    ],
)
def test_a_commissioning_number_not_in_a_wire_literal_shape_is_rejected(
    instance: str, key: str, raw: str
) -> None:
    """A panel writes ASCII digits only, and never an exponent, a plus sign, a
    digit separator, padding, a bare point or negative zero, so a definition may
    not either."""
    manifest = _with(_variant_manifest(), instance, **{key: raw})
    with pytest.raises(ManifestValidationError, match=key.removesuffix("-a")):
        Emitter(manifest, SetterRegistry(), variant="span-alpha-test-b2")


@pytest.mark.parametrize("raw", ["0.0", "-122.4194", "37.77490", "-3"])
def test_a_commissioning_number_in_a_wire_shape_is_published_as_written(
    rec: PahoRecorder, raw: str
) -> None:
    _started(rec, _with(_variant_manifest(), "abc-123", latitude=raw))
    assert rec.retained["ebus/5/abc-123/info/latitude"] == raw


def test_an_enum_value_outside_the_format_is_rejected() -> None:
    manifest = _variant_manifest()
    bad = tuple(
        DeviceInstance(
            i.entity_class,
            i.instance_id,
            i.display_name,
            {**i.metadata, "feeds-role": "WIND"} if i.instance_id == "kitchen" else i.metadata,
        )
        for i in manifest.instances
    )
    with pytest.raises(ManifestValidationError, match="feeds-role"):
        Emitter(DeviceManifest(instances=bad), SetterRegistry(), variant="span-alpha-test-b2")


def test_a_pv_device_is_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="no pv device"):
        Emitter(_manifest(), SetterRegistry(), variant="span-alpha-test-b2")


def test_the_evse_publishes_no_meter_and_accepts_a_lock_state_set(rec: PahoRecorder) -> None:
    setters = SetterRegistry()
    em = Emitter(_variant_manifest(), setters, variant="span-alpha-test-b2")
    em.start()
    handler = setters.get("evse", "switch/lock-state")
    assert handler is not None
    handler("evse", "evse", "switch/lock-state", "LOCKED")
    em.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0}))

    description = json.loads(rec.retained["ebus/5/evse/$description"])
    assert "meter" not in description["nodes"]
    assert description["nodes"]["switch"]["properties"]["lock-state"]["settable"] is True
    assert rec.retained["ebus/5/evse/switch/lock-state"] == "LOCKED"
    assert not any(t.startswith("ebus/5/evse/meter/") for t in rec.retained)


def _without_main_breaker(manifest: DeviceManifest) -> DeviceManifest:
    return DeviceManifest(
        instances=tuple(
            DeviceInstance(
                i.entity_class,
                i.instance_id,
                i.display_name,
                {k: v for k, v in i.metadata.items() if k != "main-breaker-rating-a"},
            )
            for i in manifest.instances
        )
    )


@pytest.mark.parametrize("variant", ["span", "span-alpha-test-b2"])
def test_no_main_breaker_rating_means_no_breaker_capability(
    rec: PahoRecorder, variant: Variant
) -> None:
    _started(rec, _without_main_breaker(_variant_manifest()), variant=variant)
    description = json.loads(rec.retained["ebus/5/abc-123/$description"])
    assert "breaker" not in description["nodes"]
    assert not any(t.startswith("ebus/5/abc-123/breaker/") for t in rec.retained)


def test_a_main_breaker_rating_publishes_the_breaker(rec: PahoRecorder) -> None:
    _started(rec, _variant_manifest())
    assert rec.retained["ebus/5/abc-123/breaker/rating"] == "200"


@pytest.mark.parametrize("variant", ["span-alpha-test-b2", "span"])
def test_an_unconfigured_requested_import_limit_reads_200(
    rec: PahoRecorder, variant: Variant
) -> None:
    """As every public and reference capture publishes it while UNCONFIGURED."""
    _started(rec, _variant_manifest(), variant=variant)
    assert rec.retained["ebus/5/abc-123/pcs/requested-import-limit"] == "200.0"
    assert rec.retained["ebus/5/abc-123/pcs/requested-import-limit-enablement"] == "UNCONFIGURED"


def test_the_reference_variant_keeps_its_requested_import_limit(rec: PahoRecorder) -> None:
    """200.0 is a SPAN panel's value; the reference variant publishes only what the
    specification says, and is unchanged."""
    reference = DeviceManifest(
        instances=tuple(
            i for i in _variant_manifest().instances if i.entity_class not in ("pv", "evse")
        )
    )
    _started(rec, reference, variant="reference")
    assert float(rec.retained["ebus/5/abc-123/pcs/requested-import-limit"]) == 0.0


def test_the_variant_declares_the_main_relay_and_shed_forecast_unvalued(
    rec: PahoRecorder,
) -> None:
    """Every reference capture leaves status/relay unvalued, and both with a battery
    leave shed-forecast unvalued; the span variant values them."""
    em, _ = _variant_with_battery()
    em.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0}))
    description = json.loads(rec.retained["ebus/5/abc-123/$description"])
    assert "relay" in description["nodes"]["status"]["properties"]
    assert set(description["nodes"]["shed-forecast"]["properties"]) == {
        "total-time-remaining",
        "time-to-priority-shed",
        "full-charge-total-time-remaining",
        "full-charge-time-to-priority-shed",
        "confidence",
    }
    assert "ebus/5/abc-123/status/relay" not in rec.retained
    assert not any(t.startswith("ebus/5/abc-123/shed-forecast/") for t in rec.retained)


def test_the_busbar_current_is_published_at_any_load(rec: PahoRecorder) -> None:
    """Every reference capture publishes it, 0.1 A at a few watts."""
    em = _started(rec, _variant_manifest())
    em.publish_tick(TickInputs(current_time=1.0, grid_online=True, circuits={"kitchen": 0.0}))
    assert rec.retained["ebus/5/abc-123/meter/busbar-current"] == "0.0"


def _loads_only(*, solar: bool) -> DeviceManifest:
    """The variant's site with neither battery nor (unless asked) a SOLAR circuit."""
    instances = []
    for i in _variant_manifest().instances:
        if i.entity_class in ("bess", "evse"):
            continue
        md = dict(i.metadata)
        if not solar:
            md.pop("feeds-role", None)
        instances.append(DeviceInstance(i.entity_class, i.instance_id, i.display_name, md))
    return DeviceManifest(instances=tuple(instances))


def test_a_loads_only_panel_publishes_only_grid_and_site(rec: PahoRecorder) -> None:
    _started(rec, _loads_only(solar=False))
    flows = {t.rsplit("/", 1)[1] for t in rec.retained if "/power-flows/" in t}
    assert flows == {"grid", "site"}


def test_a_panel_with_solar_publishes_all_four(rec: PahoRecorder) -> None:
    _started(rec, _loads_only(solar=True))
    flows = {t.rsplit("/", 1)[1] for t in rec.retained if "/power-flows/" in t}
    assert flows == {"pv", "battery", "grid", "site"}


def test_a_loads_only_span_panel_publishes_all_four(rec: PahoRecorder) -> None:
    _started(rec, _loads_only(solar=False), variant="span")
    flows = {t.rsplit("/", 1)[1] for t in rec.retained if "/power-flows/" in t}
    assert flows == {"pv", "battery", "grid", "site"}


def _with(manifest: DeviceManifest, instance_id: str, **md: str) -> DeviceManifest:
    return DeviceManifest(
        instances=tuple(
            DeviceInstance(
                i.entity_class,
                i.instance_id,
                i.display_name,
                {**i.metadata, **md} if i.instance_id == instance_id else i.metadata,
            )
            for i in manifest.instances
        )
    )


@pytest.mark.parametrize(
    ("variant", "requester"), [("span-alpha-test-b2", "NONE"), ("span", "CONFIGURATION")]
)
def test_a_locked_relay_reports_none_at_rest_under_the_variant(
    rec: PahoRecorder, variant: Variant, requester: str
) -> None:
    manifest = _with(_variant_manifest(), "solar", **{"relay-behavior": "non-controllable"})
    _started(rec, manifest, variant=variant)
    assert rec.retained["ebus/5/solar/switch/relay-controllable"] == "false"
    assert rec.retained["ebus/5/solar/switch/relay-requester"] == requester


def _variant_with_battery() -> tuple[Emitter, SetterRegistry]:
    setters = SetterRegistry()
    cfg = BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
    )
    em = Emitter(_variant_manifest(), setters, bess_configs=(cfg,), variant="span-alpha-test-b2")
    em.start()
    return em, setters


@pytest.mark.parametrize(
    ("link", "published"), [("UNKNOWN", "NONE"), ("LOST", "OFF_GRID"), ("DEGRADED", "OFF_GRID")]
)
def test_the_variant_accepts_an_islanding_assertion_only_while_lost_or_degraded(
    rec: PahoRecorder, link: BESSCommunication, published: str
) -> None:
    em, setters = _variant_with_battery()

    def tick(t: float) -> None:
        em.publish_tick(
            TickInputs(
                current_time=t,
                grid_online=True,
                circuits={"kitchen": 500.0},
                bess_communication={"abc-123-bess": link},
            )
        )

    tick(0.0)
    handler = setters.get("panel", "shed/asserted-islanding-state")
    assert handler is not None
    handler("panel", "abc-123", "shed/asserted-islanding-state", "OFF_GRID")
    tick(1.0)
    assert rec.retained["ebus/5/abc-123/shed/asserted-islanding-state"] == published


def test_busbar_current_and_frequency_are_published(rec: PahoRecorder) -> None:
    _started(rec, _variant_manifest())  # 500 W of site load at 240 V service
    assert float(rec.retained["ebus/5/abc-123/meter/busbar-current"]) == pytest.approx(2.1)
    assert rec.retained["ebus/5/abc-123/meter/frequency"] == "60.00"


def test_an_unknown_link_refuses_an_assertion_but_holds_off_the_clear(
    rec: PahoRecorder,
) -> None:
    em, setters = _variant_with_battery()

    def tick(t: float, link: BESSCommunication) -> None:
        em.publish_tick(
            TickInputs(
                current_time=t,
                grid_online=True,
                circuits={"kitchen": 500.0},
                bess_communication={"abc-123-bess": link},
            )
        )

    topic = "ebus/5/abc-123/shed/asserted-islanding-state"
    handler = setters.get("panel", "shed/asserted-islanding-state")
    assert handler is not None
    tick(0.0, "LOST")
    handler("panel", "abc-123", "shed/asserted-islanding-state", "OFF_GRID")
    tick(10.0, "UNKNOWN")
    tick(50.0, "UNKNOWN")  # 40 s of UNKNOWN: not OK, so no clear
    assert rec.retained[topic] == "OFF_GRID"
    tick(60.0, "OK")
    tick(89.0, "OK")
    assert rec.retained[topic] == "OFF_GRID"
    tick(90.0, "OK")
    assert rec.retained[topic] == "NONE"


def test_a_commissioned_system_circuit_is_rejected() -> None:
    manifest = _with(
        _variant_manifest(), "solar", **{"commissioned-system": "pv", "default-priority": "NEVER"}
    )
    with pytest.raises(ManifestValidationError, match="no commissioned-system circuits"):
        Emitter(manifest, SetterRegistry(), variant="span-alpha-test-b2")


@pytest.mark.parametrize(("variant", "settable"), [("span-alpha-test-b2", False), ("span", True)])
def test_a_locked_relay_locks_the_priority_only_under_the_variant(
    rec: PahoRecorder, variant: Variant, settable: bool
) -> None:
    manifest = _with(_variant_manifest(), "solar", **{"relay-behavior": "non-controllable"})
    setters = SetterRegistry()
    em = Emitter(manifest, setters, variant=variant)
    em.start()
    handler = setters.get("circuit", "load-shed/priority")
    assert handler is not None
    handler("circuit", "solar", "load-shed/priority", "OFF_GRID")
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0})
    )

    description = json.loads(rec.retained["ebus/5/solar/$description"])
    priority = description["nodes"]["load-shed"]["properties"]["priority"]
    assert ("settable" in priority) is settable
    assert (snap.circuits["solar"].priority == "OFF_GRID") is settable


def _with_remote_ct(manifest: DeviceManifest, count: int = 1) -> DeviceManifest:
    extra = tuple(
        DeviceInstance("remote-ct", f"remote-ct-{n}", "Service CT", {})
        for n in range(1, count + 1)
    )
    return DeviceManifest(instances=manifest.instances + extra)


def test_the_remote_ct_is_a_meter_only_circuit_without_spaces(rec: PahoRecorder) -> None:
    _started(rec, _with_remote_ct(_variant_manifest()))
    description = json.loads(rec.retained["ebus/5/remote-ct-1/$description"])
    assert description["type"] == "energy.ebus.device.circuit"
    assert set(description["nodes"]) == {"meter"}
    assert set(description["nodes"]["meter"]["properties"]) == {
        "current",
        "active-power",
        "imported-energy",
        "exported-energy",
        "shared-with-device-ids",
    }
    assert "ebus/5/remote-ct-1/meter/shared-with-device-ids" not in rec.retained
    panel = json.loads(rec.retained["ebus/5/abc-123/$description"])
    assert "remote-ct-1" in panel["children"]


def test_every_branch_circuit_declares_and_values_spaces(rec: PahoRecorder) -> None:
    """Clients tell the remote-ct from a branch circuit by the absence of
    info/spaces, so no branch circuit may lack it."""
    manifest = _with_remote_ct(_variant_manifest())
    _started(rec, manifest)
    for inst in manifest.instances:
        if inst.entity_class != "circuit":
            continue
        description = json.loads(rec.retained[f"ebus/5/{inst.instance_id}/$description"])
        assert "spaces" in description["nodes"]["info"]["properties"]
        assert rec.retained[f"ebus/5/{inst.instance_id}/info/spaces"]
    assert not any(t.startswith("ebus/5/remote-ct-1/info/") for t in rec.retained)


def test_the_remote_ct_reads_positive_on_import_and_imports_energy(rec: PahoRecorder) -> None:
    em = Emitter(
        _with_remote_ct(_variant_manifest()), SetterRegistry(), variant="span-alpha-test-b2"
    )
    em.start()
    for t in (0.0, 3600.0):
        snap = em.publish_tick(
            TickInputs(current_time=t, grid_online=True, circuits={"kitchen": 1200.0})
        )
    grid_w = snap.meter.instant_grid_power_w
    assert grid_w > 0
    assert float(rec.retained["ebus/5/remote-ct-1/meter/active-power"]) == pytest.approx(grid_w)
    assert float(rec.retained["ebus/5/remote-ct-1/meter/imported-energy"]) == pytest.approx(grid_w)
    assert float(rec.retained["ebus/5/remote-ct-1/meter/exported-energy"]) == 0.0


def test_a_remote_ct_is_rejected_outside_the_variant() -> None:
    manifest = _with_remote_ct(_variant_manifest())
    with pytest.raises(ManifestValidationError, match="only by variant 'span-alpha-test-b2'"):
        Emitter(manifest, SetterRegistry(), variant="span")


def test_at_most_one_remote_ct() -> None:
    with pytest.raises(ManifestValidationError, match="at most one remote-ct"):
        Emitter(
            _with_remote_ct(_variant_manifest(), 2), SetterRegistry(), variant="span-alpha-test-b2"
        )


def _solar_and_other_generation() -> DeviceManifest:
    """The loads-only site with its SOLAR circuit, plus a second circuit that also
    reads negative but has no solar role."""
    manifest = _loads_only(solar=True)
    return _with(manifest, "kitchen", **{"feeds-role": "LOADS"})


@pytest.mark.spec_only
@pytest.mark.parametrize(
    ("variant", "pv", "site"),
    [("span-alpha-test-b2", -2000.0, 700.0), ("span", -2300.0, 1000.0)],
)
def test_power_flows_pv_is_the_generation_of_solar_role_circuits(
    rec: PahoRecorder, variant: Variant, pv: float, site: float
) -> None:
    """Assumed, as no reference capture has solar: the eBus catalog's
    connection/feeds-role SOLAR names the circuit feeding a solar source, so the
    variant takes power-flows/pv from those circuits alone and books any other
    circuit's negative reading against the site. The span variant counts every
    negative circuit as solar. The grid is the same either way."""
    em = Emitter(_solar_and_other_generation(), SetterRegistry(), variant=variant)
    em.start()
    snap = em.publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"solar": -2000.0, "kitchen": -300.0, "ev": 1000.0},
        )
    )
    flows = snap.power_flows
    assert flows.pv == pytest.approx(pv)
    assert flows.site == pytest.approx(site)
    assert flows.grid == pytest.approx(1300.0)
    values = [flows.pv, flows.battery or 0.0, flows.grid, flows.site]
    assert sum(v for v in values if v is not None) == pytest.approx(0.0)


@pytest.mark.spec_only
def test_a_solar_circuit_s_standby_draw_counts_toward_pv_not_site(rec: PahoRecorder) -> None:
    """r202639 firmware sums the SOLAR circuits' readings into pv, so an inverter's
    standby draw at night reads as a small positive pv, not as site load."""
    em = Emitter(_loads_only(solar=True), SetterRegistry(), variant="span-alpha-test-b2")
    em.start()
    flows = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"solar": 5.0, "ev": 1000.0})
    ).power_flows
    assert flows.pv == pytest.approx(5.0)
    assert flows.site == pytest.approx(1000.0)
    values = [flows.pv, flows.battery or 0.0, flows.grid, flows.site]
    assert sum(v for v in values if v is not None) == pytest.approx(0.0)


@pytest.mark.spec_only
@pytest.mark.parametrize(("variant", "site"), [("span-alpha-test-b2", 500.0), ("span", 1300.0)])
def test_a_battery_breaker_counts_toward_battery_not_site(
    rec: PahoRecorder, variant: Variant, site: float
) -> None:
    """Under the variant, site is the load circuits alone: a breaker that feeds the
    battery carries the battery's power, which the battery's flow reports. The span
    variant counts it as load. The flows balance either way."""
    manifest = _with(
        _variant_manifest(), "abc-123-bess", feed="kitchen", **{"relative-position": "IN_PANEL"}
    )
    manifest = DeviceManifest(
        instances=tuple(i for i in manifest.instances if i.entity_class not in ("pv", "evse"))
    )
    cfg = BESSConfig(
        instance_id="abc-123-bess",
        nameplate_capacity_kwh=13.5,
        max_charge_w=3500.0,
        max_discharge_w=3500.0,
    )
    em = Emitter(manifest, SetterRegistry(), bess_configs=(cfg,), variant=variant)
    em.start()
    flows = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 800.0, "ev": 500.0})
    ).power_flows
    assert flows.site == pytest.approx(site)
    values = [flows.pv or 0.0, flows.battery or 0.0, flows.grid, flows.site]
    assert sum(v for v in values if v is not None) == pytest.approx(0.0)


def test_with_neither_solar_nor_a_battery_pv_is_unset_not_zero(rec: PahoRecorder) -> None:
    """r202639 firmware leaves pv (and battery) unset on such a panel. A third-party
    backup system that reports pv from its own meter, as capture r202639-c's does,
    would publish it; that case is not modelled yet."""
    em = _started(rec, _loads_only(solar=False))
    assert em.last_snapshot is not None
    assert em.last_snapshot.power_flows.pv is None
    assert em.last_snapshot.power_flows.battery is None
    assert "ebus/5/abc-123/power-flows/pv" not in rec.retained


def _role_site(role: str | None) -> DeviceManifest:
    """The loads-only site with its SOLAR circuit, and the kitchen given ``role``
    (none when ``None``)."""
    manifest = _loads_only(solar=True)
    if role is None:
        return manifest
    return _with(manifest, "kitchen", **{"feeds-role": role})


def _role_flows(manifest: DeviceManifest) -> EbusPanelPowerFlows:
    em = Emitter(manifest, SetterRegistry(), variant="span-alpha-test-b2")
    em.start()
    flows = em.publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"solar": -2000.0, "kitchen": 800.0, "ev": 500.0},
        )
    ).power_flows
    values = [flows.pv, flows.battery, flows.grid, flows.site]
    assert sum(v for v in values if v is not None) == pytest.approx(0.0)
    return flows


@pytest.mark.spec_only
@pytest.mark.parametrize("role", [None, "LOADS", "UNUSED"])
def test_a_load_circuit_counts_toward_site(rec: PahoRecorder, role: str | None) -> None:
    """r202639 firmware books a circuit with no role or the LOADS role to site. An
    UNUSED breaker is surveyed and empty, about 0 W, so any reading it shows is
    load-side and goes to site too."""
    flows = _role_flows(_role_site(role))
    assert flows.site == pytest.approx(1300.0)
    assert flows.pv == pytest.approx(-2000.0)


@pytest.mark.spec_only
def test_a_storage_circuit_counts_toward_battery_not_site(rec: PahoRecorder) -> None:
    """A STORAGE circuit is the battery: its draw is the battery charging, so the
    battery flow is valued even without a BESS device, and site leaves it out."""
    flows = _role_flows(_role_site("STORAGE"))
    assert flows.site == pytest.approx(500.0)
    assert flows.battery == pytest.approx(800.0)


@pytest.mark.spec_only
def test_a_circuit_feeding_the_span_drive_counts_toward_site_whatever_its_role(
    rec: PahoRecorder,
) -> None:
    """Site takes the SPAN Drive's circuit, as firmware books it, even one whose
    role would otherwise be refused."""
    manifest = DeviceManifest(
        instances=tuple(
            i
            for i in _with(_variant_manifest(), "ev", **{"feeds-role": "GENERATOR"}).instances
            if i.entity_class != "pv"
        )
    )
    em = Emitter(manifest, SetterRegistry(), variant="span-alpha-test-b2")
    em.start()
    flows = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"ev": 1500.0, "kitchen": 0.0})
    ).power_flows
    assert flows.site == pytest.approx(1500.0)


@pytest.mark.parametrize("role", ["GENERATOR", "SUBPANEL", "MIXED"])
def test_a_circuit_role_with_no_known_booking_is_rejected(role: str) -> None:
    """No firmware rule says how these roles are booked, so the variant refuses
    them rather than guessing a flow. The span variant does not book by role."""
    manifest = _role_site(role)
    with pytest.raises(ManifestValidationError, match=f"feeds-role.*{role}"):
        Emitter(manifest, SetterRegistry(), variant="span-alpha-test-b2")
    Emitter(manifest, SetterRegistry(), variant="span")
