"""Public Emitter facade — wire-layer publisher with native-device runtime.

The producer hands the emitter a small per-tick driving signal via
``publish_tick(TickInputs)``: signed power per circuit/EVSE, current_time,
grid_online, panel envelope, battery link health. The emitter resolves BESS
dispatch, gates circuit power through ``RelayResolver``, integrates energy via
``EnergyIntegrator``, aggregates panel-level fields via ``PanelMeter``, builds
the internal snapshot, and publishes the Homie diff to MQTT.

The internal snapshot type (``EbusPanelSnapshot`` and friends) is used for the
diff cache and read-back via ``last_snapshot``; producers do not construct it."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Mapping
from typing import Any, Final, get_args

from ebus_panel_sim.definition import PanelDefinition
from ebus_panel_sim.energy_integrator import EnergyIntegrator
from ebus_panel_sim.exceptions import (
    EmitterStateError,
    ManifestValidationError,
    ProfileValidationError,
)
from ebus_panel_sim.firmware import firmware_conventions
from ebus_panel_sim.manifest import DeviceManifest
from ebus_panel_sim.manifest_physics import (
    EVSE_MIN_CHARGE_CURRENT_A,
    ManifestPhysicsView,
    unvalued_paths,
)
from ebus_panel_sim.native_devices import (
    BESSConfig,
    BESSDevice,
    LoadSheddingConfig,
    LoadSheddingDevice,
    NativeTickContext,
)
from ebus_panel_sim.panel_meter import circuit_current_a
from ebus_panel_sim.panel_meter import resolve as resolve_panel
from ebus_panel_sim.relay_resolver import RelayResolver, RelayState
from ebus_panel_sim.snapshot import (
    EbusBatterySnapshot,
    EbusCircuitSnapshot,
    EbusEvseSnapshot,
    EbusLugsSnapshot,
    EbusMidSnapshot,
    EbusPanelDoor,
    EbusPanelInfo,
    EbusPanelMeter,
    EbusPanelPcs,
    EbusPanelPowerFlows,
    EbusPanelShed,
    EbusPanelShedForecast,
    EbusPanelSnapshot,
    EbusPanelStatus,
    EbusPvSnapshot,
    EbusRemoteCtSnapshot,
)
from ebus_panel_sim.tick_inputs import BESSCommunication, TickInputs
from ebus_panel_sim.wire._sdk_seam import (
    MqttDeviceTransport,
    owned_client,
    will_for_root_id,
)
from ebus_panel_sim.wire.bag_builder import BagBuilder
from ebus_panel_sim.wire.graph_builder import build_graph, root_instance_of
from ebus_panel_sim.wire.mapping_loader import load_mapping_table
from ebus_panel_sim.wire.profile_loader import ProfileTable, Variant, load_profiles
from ebus_panel_sim.wire.publisher import Publisher
from ebus_panel_sim.wire.set_router import (
    SetterRegistry,
    check_setter_coverage,
    make_set_callback,
)

_LOG = logging.getLogger(__name__)
_DEFAULT_MQTT_CFG: dict[str, Any] = {"host": "127.0.0.1", "port": 1883}

ASSERTION_CLEAR_AFTER_S: Final = 30.0
"""Tick-time seconds every battery link must have been ``OK`` before the panel
clears an active ``shed/asserted-islanding-state`` back to ``NONE``."""

_ASSERTABLE_ISLANDING_STATES: Final = frozenset({"ON_GRID", "OFF_GRID"})
_BESS_COMMUNICATION_STATES: Final[frozenset[str]] = frozenset(get_args(BESSCommunication))
# A SPAN panel declares the catalog's full enum on a connection status but reports
# only these values there (SPAN-API-Client-Docs specs/r202633/homie-schema.json).
_SPAN_REPORTED_LINK_STATUSES: Final = frozenset({"OK", "LOST"})
_NOMINAL_FREQUENCY_HZ = 60.0


def _busbar_current(site_w: float, service_voltage_v: float) -> float | None:
    """Estimated busbar current: the site's power over the service voltage, in A,
    or None without a service voltage. A SPAN panel estimates it from apparent
    power and publishes it on every reference capture, 0.1 A at a few watts;
    this model has active power only."""
    return abs(site_w) / service_voltage_v if service_voltage_v > 0 else None


def _declared_enum(
    profiles: ProfileTable, entity_class: str, capability: str, prop: str
) -> frozenset[str]:
    """The values a profile's enum property declares in its ``$format``."""
    profile = profiles.get(entity_class)
    cap = profile.capabilities.get(capability) if profile is not None else None
    declared = cap.properties.get(prop) if cap is not None else None
    if declared is None or not declared.format:
        raise ProfileValidationError(
            f"{entity_class} profile declares no enum {capability}/{prop}"
        )
    return frozenset(declared.format.split(","))


def _reported_link_statuses(
    profiles: ProfileTable, variant: Variant, entity_class: str, prop: str
) -> frozenset[str]:
    """The values a connection status reports: what its profile declares, narrowed
    to ``OK`` and ``LOST`` for the SPAN variants, as a SPAN panel reports."""
    declared = _declared_enum(profiles, entity_class, "connection", prop)
    return declared & _SPAN_REPORTED_LINK_STATUSES if variant != "reference" else declared


def _link_status(communication: BESSCommunication, reported: frozenset[str]) -> str:
    """The connection status a battery link of this health publishes: the health
    itself where the status reports it, and ``LOST`` otherwise. No connection catalog
    declares ``UNKNOWN``, and a SPAN panel reports ``DEGRADED`` as ``LOST`` too."""
    return communication if communication in reported else "LOST"


def _typed(raw: str, datatype: str, fmt: str | None, *, where: str) -> object:
    """A metadata string checked against the property's Homie datatype.

    A number is published exactly as written: a commissioning fact keeps the
    digits it was recorded with (``120.0`` stays ``120.0``), so no literal form
    applies to it. A boolean is published in lower case."""
    try:
        if datatype == "float" and math.isfinite(float(raw)):
            return raw
        if datatype == "integer":
            int(raw)
            return raw
    except ValueError as exc:
        raise ManifestValidationError(f"{where}: not a {datatype} ({raw!r})") from exc
    if datatype == "float":
        raise ManifestValidationError(f"{where}: not a finite {datatype} ({raw!r})")
    if datatype == "boolean":
        if raw.lower() not in ("true", "false"):
            raise ManifestValidationError(f"{where}: not a boolean ({raw!r})")
        return raw.lower() == "true"
    if datatype == "enum" and fmt is not None and raw not in fmt.split(","):
        raise ManifestValidationError(f"{where}: must be one of {fmt}, got {raw!r}")
    return raw


def _declared_unvalued(
    manifest: DeviceManifest, profiles: ProfileTable
) -> dict[tuple[str, str], frozenset[str]]:
    """Each device's ``unvalued`` metadata, keyed ``(entity_class, instance_id)``.

    A path the device's profile does not declare is an error, so a typo cannot
    silently leave a property valued."""
    out: dict[tuple[str, str], frozenset[str]] = {}
    for inst in manifest.instances:
        paths = unvalued_paths(inst.metadata)
        if not paths:
            continue
        profile = profiles.get(inst.entity_class)
        declared = (
            {f"{cap}/{key}" for cap, c in profile.capabilities.items() for key in c.properties}
            if profile is not None
            else set()
        )
        if unknown := sorted(paths - declared):
            raise ManifestValidationError(
                f"{inst.instance_id} key 'unvalued': its profile declares no {', '.join(unknown)}"
            )
        out[(inst.entity_class, inst.instance_id)] = paths
    return out


class Emitter:
    """One emitter per logical panel/clone."""

    def __init__(
        self,
        manifest: DeviceManifest,
        setter_registry: SetterRegistry,
        *,
        mqtt_cfg: dict[str, Any] | None = None,
        mqttc: MqttDeviceTransport | None = None,
        bess_configs: tuple[BESSConfig, ...] = (),
        load_shedding_config: LoadSheddingConfig | None = None,
        variant: Variant = "span",
    ) -> None:
        self._manifest = manifest
        # Bring-your-own-transport: a caller that already owns a connected client
        # passes it as ``mqttc`` and the SDK publishes through it, never starting
        # or stopping it. Mutually exclusive with ``mqtt_cfg``, which has the SDK
        # build a client it owns.
        #
        # Registering the Last Will on an injected client is the CALLER'S job, and
        # has to happen before they connect it: the will rides the MQTT CONNECT
        # packet, so neither the SDK nor this emitter can attach one to a client
        # handed over already connected (``Device.will()`` is exposed for exactly
        # this, and returns the descriptor to register). Without it the tree can
        # still reach ``$state=lost`` through ``stop(graceful=False)``, which
        # publishes the same payload itself — but that covers orderly teardown
        # only. A caller that wants a *dropped* process to show as lost, which is
        # what a will is for, must register it.
        if mqttc is not None and mqtt_cfg is not None:
            raise EmitterStateError("Emitter takes mqtt_cfg= or mqttc=, not both")
        self._mqttc = mqttc
        self._owns_client = mqttc is None
        if mqttc is not None:
            self._mqtt_cfg = None
        else:
            self._mqtt_cfg = dict(mqtt_cfg) if mqtt_cfg is not None else dict(_DEFAULT_MQTT_CFG)

        # variant="span" (default) publishes the SPAN-faithful surface (status
        # diagnostics, read-only shed/policy, the legacy evse config);
        # "span-alpha-test-b2" layers on it site, busbar and frequency properties,
        # commissioning facts and a meter-only circuit device; "reference" is the
        # vendor-neutral spec-conformant tree.
        self._variant = variant
        if variant == "span-alpha-test-b2" and manifest.of_class("pv"):
            raise ManifestValidationError(
                "variant 'span-alpha-test-b2' publishes no pv device; mark the circuit that feeds "
                "the inverter with feeds-role SOLAR instead"
            )
        remote_cts = manifest.of_class("remote-ct")
        if remote_cts and variant != "span-alpha-test-b2":
            raise ManifestValidationError(
                "a remote-ct is published only by variant 'span-alpha-test-b2'"
            )
        if len(remote_cts) > 1:
            raise ManifestValidationError(
                f"at most one remote-ct per panel, got {len(remote_cts)}"
            )
        if variant == "span-alpha-test-b2" and any(
            i.metadata.get("commissioned-system") for i in manifest.of_class("circuit")
        ):
            raise ManifestValidationError(
                "variant 'span-alpha-test-b2' has no commissioned-system circuits; a PV or "
                "battery breaker is an ordinary circuit with a locked relay at priority NEVER"
            )
        self._profiles = load_profiles(variant=variant)
        self._mapping = load_mapping_table()
        self._mapping.validate_against(self._profiles)
        self._unvalued = _declared_unvalued(manifest, self._profiles)

        # The root device holds the shared connection — built from mqtt_cfg, or
        # the caller's when injected — and children publish through it either
        # way. Construction opens no socket: an owned client connects in start(),
        # an injected one is already the caller's to connect.
        self._graph = build_graph(
            manifest,
            self._mapping,
            self._profiles,
            mqtt_cfg=self._mqtt_cfg,
            mqttc=self._mqttc,
            priority_locked_by_relay=variant == "span-alpha-test-b2",
        )
        self._root = self._graph.devices[self._graph.root_id]

        # ---- native-device + physics state (must exist before internal /set
        # handlers bind, which must happen before setter-coverage validation). ----
        # BESS is pluralized: a panel can host multiple battery devices (e.g. a
        # Powerwall plus an Enphase IQ, or two Powerwalls). Keyed by
        # ``BESSConfig.instance_id``; duplicate IDs are a producer-side bug.
        self._bess: dict[str, BESSDevice] = {}
        for cfg in bess_configs:
            if cfg.instance_id in self._bess:
                raise EmitterStateError(f"duplicate bess_config instance_id={cfg.instance_id!r}")
            self._bess[cfg.instance_id] = BESSDevice(config=cfg)
        self._load_shedding: LoadSheddingDevice | None = (
            LoadSheddingDevice(config=load_shedding_config)
            if load_shedding_config is not None
            else None
        )
        # ManifestPhysicsView raises ManifestValidationError if the manifest
        # is missing required physics keys. publish_tick is the only publish
        # path now, so a malformed manifest is a hard error at construction.
        self._physics = ManifestPhysicsView(manifest)
        # The conventions that changed across SPAN releases, fixed once for the
        # emitter's life by the variant and the panel's own firmware-version.
        self._conventions = firmware_conventions(variant, self._physics.panel.release_build)
        self._relays = RelayResolver()
        self._energy = EnergyIntegrator()
        self._priority_overrides: dict[str, str] = {}
        # The circuits whose priority is locked (never-backup or a
        # commissioned-system circuit), resolved once from the manifest the way
        # `RelayResolver` registers the relay lock once. An override never enters
        # the map above, so the published value stays the commissioned one.
        self._priority_locked: frozenset[str] = frozenset(
            cid
            for cid, cphys in self._physics.all_circuits().items()
            if cphys.priority_locked or (variant == "span-alpha-test-b2" and cphys.always_on)
        )
        self._name_overrides: dict[str, str] = {}
        self._dominant_power_source_override: str | None = None
        self._asserted_islanding_override: str | None = None
        # Health of the panel's link to each battery as of the last tick, which
        # the assertion handler reads between ticks (None until the first tick
        # has observed one), and the tick time since which every link has been OK.
        self._bess_links: dict[str, BESSCommunication] | None = None
        self._links_healthy_since: float | None = None
        self._feeds_status_values = _reported_link_statuses(
            self._profiles, variant, "circuit", "feeds-device-status"
        )
        self._fed_by_status_values = _reported_link_statuses(
            self._profiles, variant, "lugs", "fed-by-device-status"
        )
        # The link states that make an assertion acceptable once a link has been
        # observed. Under span-alpha-test-b2 UNKNOWN does not count; otherwise any
        # state but OK does.
        self._assertion_link_states: frozenset[str] | None = (
            frozenset({"LOST", "DEGRADED"}) if variant == "span-alpha-test-b2" else None
        )
        self._shed_policy_override: str | None = None
        self._evse_user_max_override: dict[str, int] = {}
        self._evse_lock_override: dict[str, str] = {}

        self._remote_ct_ids = tuple(i.instance_id for i in manifest.of_class("remote-ct"))
        for rct_id in self._remote_ct_ids:
            self._energy.register(rct_id)
        for cid, cphys in self._physics.all_circuits().items():
            self._relays.register(cid, always_on=cphys.always_on)
            self._energy.register(cid)
            if cphys.initial_consumed_wh or cphys.initial_produced_wh:
                self._energy.seed(
                    cid,
                    consumed_wh=cphys.initial_consumed_wh,
                    produced_wh=cphys.initial_produced_wh,
                )
        for eid in self._physics.all_evse():
            self._energy.register(eid)
        # Lugs are metered points, so their energy registers integrate the power
        # THEIR OWN meter reports. They used to be handed the sum of the circuits
        # behind them instead, which is a different quantity: with 7 kW of PV and
        # 6 kW of load the lugs carry ~1 kW in one direction, but the gross sum
        # advanced `imported-energy` AND `exported-energy` in the same tick. A
        # capture of a live panel never does that -- `meter.md` calls
        # `imported-energy` "the energy counterpart of positive `active-power`",
        # and a counterpart that integrates a different signal is not one.
        for lugs_id in self._physics.all_lugs():
            self._energy.register(lugs_id)
        # Seed any configured BESS whose manifest physics declares an initial SOE.
        for bess_id, bphys in self._physics.all_bess().items():
            if bphys.initial_soe_kwh is not None and bess_id in self._bess:
                self._bess[bess_id].set_soe(bphys.initial_soe_kwh)

        # Internal default /set handlers — registered BEFORE the setter-coverage
        # check so it passes. Producer-supplied handlers always win (the helper
        # checks .get() first).
        self._register_internal_setters(setter_registry)

        # ---- /set wiring: fail loud on any settable without a handler, then
        # bind each settable Property's SDK set-callback to its registry handler.
        # (ebus-sdk owns the /set subscription + payload decode.) ----
        instances = [(i.entity_class, i.instance_id) for i in manifest.instances]
        settables_by_class = {
            ec: profile.settable_properties() for ec, profile in self._profiles.items()
        }
        check_setter_coverage(
            instances=instances,
            settables_by_class=settables_by_class,
            registry=setter_registry,
        )
        self._wire_set_callbacks(setter_registry, instances, settables_by_class)

        self._publisher = Publisher(self._graph)
        self._bag_builder = BagBuilder(
            self._graph,
            self._mapping,
            self._profiles,
            self._typed_wire_values(),
            bess_meter_frame=self._conventions.bess_meter_frame,
            unvalued=self._unvalued,
        )
        self._last_snapshot: EbusPanelSnapshot | None = None
        self._started = False

    def _typed_wire_values(self) -> dict[tuple[str, str, str], object]:
        """The manifest's verbatim commissioning values, checked against the profile.

        A value for a path the variant's profile does not declare is dropped."""
        sources: list[tuple[str, str, dict[str, str]]] = [
            ("panel", self._graph.root_id, self._physics.panel.wire_values),
            *(("circuit", cid, c.wire_values) for cid, c in self._physics.all_circuits().items()),
            *(("lugs", lid, lg.wire_values) for lid, lg in self._physics.all_lugs().items()),
        ]
        out: dict[tuple[str, str, str], object] = {}
        for entity_class, instance_id, values in sources:
            for path, raw in values.items():
                cap_name, prop_key = path.split("/", 1)
                cap = self._profiles[entity_class].capabilities.get(cap_name)
                prop = cap.properties.get(prop_key) if cap is not None else None
                if prop is not None:
                    out[(entity_class, instance_id, path)] = _typed(
                        raw, prop.datatype, prop.format, where=f"{instance_id} {path}"
                    )
        return out

    def _wire_set_callbacks(
        self,
        registry: SetterRegistry,
        instances: list[tuple[str, str]],
        settables_by_class: dict[str, list[tuple[str, str]]],
    ) -> None:
        """Bind every settable property's ebus-sdk set-callback to its registry
        handler. The callback coerces the ``/set`` payload per datatype and fans
        in to the handler; ebus-sdk owns the subscription + decode."""
        for ec, iid in instances:
            for cap, key in settables_by_class.get(ec, []):
                prop_path = f"{cap}/{key}"
                handler = registry.get(ec, prop_path)
                sdk_prop = self._graph.properties.get((ec, iid, prop_path))
                if handler is None or sdk_prop is None:
                    continue
                datatype = self._profiles[ec].capabilities[cap].properties[key].datatype
                sdk_prop.set_set_callback(
                    make_set_callback(
                        handler,
                        entity_class=ec,
                        instance_id=iid,
                        property_path=prop_path,
                        datatype=datatype,
                    )
                )

    @staticmethod
    def lwt_settings(manifest: DeviceManifest) -> dict[str, str]:
        """The Last Will to register on a client you intend to inject.

        A ``staticmethod`` taking the manifest, because it has to be answerable
        *before* there is an ``Emitter`` to ask: the will rides the MQTT CONNECT
        packet, so it must be on the client before the client connects, which is
        before you can hand that client to a constructor. An instance method here
        would be unusable by definition.

        The root is derived from the same mapping table ``build_graph`` uses, so
        the will names the device the tree will actually publish as, and the
        descriptor itself comes from the SDK's ``Device.will()`` — the very
        function the SDK passes as ``lwt=`` when it builds a client of its own.
        A caller-registered will is therefore identical to an SDK-registered one
        rather than merely similar.

        The shape drops into ``MqttClient(lwt=...)`` unchanged. The full wiring,
        in the order it has to happen::

            from ebus_mqtt_client import MqttClient

            from ebus_panel_sim import Emitter, SetterRegistry

            lwt = Emitter.lwt_settings(manifest)
            client = MqttClient.from_config(
                {"host": "127.0.0.1", "port": 1883}, client_id="my-host", lwt=lwt
            )
            emitter = Emitter(manifest, SetterRegistry(), mqttc=client)
            client.on_connect_callback = emitter.republish_tree
            client.start()

        ``on_connect_callback`` is assigned after construction rather than passed
        to ``from_config`` because the callback needs the emitter and the emitter
        needs the client; omitting it leaves the tree unable to come back after a
        broker restart. See the README's "Bring your own transport" section for
        what each step buys.

        Without the will the tree has none at all on an injected transport, and an
        ungraceful death leaves consumers reading a stale retained ``ready``
        indefinitely. ``stop(graceful=False)`` covers only orderly teardown.
        """
        root = root_instance_of(manifest, load_mapping_table())
        return will_for_root_id(root.instance_id)

    def republish_tree(self) -> None:
        """Re-announce the whole retained tree. Wire this to your client's
        on-connect handler when you inject a transport.

        For a client the SDK builds, it registers this itself inside
        ``connect_broker()`` and every reconnect republishes automatically. That
        registration sits *below* an ``if self.mqttc: return``, so an injected
        client never reaches it — the SDK says as much, and asks the caller to
        call it from their own on-connect handler.

        Nothing else covers the gap. Retained values survive on the broker, but a
        broker that loses its retained store (restart, eviction, a fresh
        deployment) drops the tree, and only a re-announce brings it back. What
        comes back without this is whatever later ticks happen to republish:
        measured against a real broker, 5 topics of 56, with every
        ``$description`` missing.
        """
        self._root.refresh_tree()

    @classmethod
    def from_definition(
        cls,
        definition: PanelDefinition,
        setter_registry: SetterRegistry,
        *,
        mqtt_cfg: dict[str, Any] | None = None,
        mqttc: MqttDeviceTransport | None = None,
    ) -> Emitter:
        """Build an emitter for a panel definition (see ``load_definition``)."""
        return cls(
            definition.manifest,
            setter_registry,
            mqtt_cfg=mqtt_cfg,
            mqttc=mqttc,
            bess_configs=definition.bess_configs,
            load_shedding_config=definition.load_shedding,
            variant=definition.variant,
        )

    def start(self, *, connect_timeout_s: float = 5.0) -> None:
        """Mark the emitter ready to publish; with ``mqtt_cfg=``, open the connection too.

        The two paths differ in almost everything this method does, so the
        summary above deliberately promises only what both deliver.

        **Owned (``mqtt_cfg=``)** — opens the MQTT connection and publishes the
        retained device tree. ebus-sdk publishes each device's ``$description`` +
        ``$state`` and subscribes the ``/set`` topics itself once the link comes
        up (on_connect -> refresh_tree). We start the root's shared client and
        wait, bounded, for the link so the first ``publish_tick`` lands live;
        publishing before connect is still safe, since values are retained and
        the SDK's own on-connect hook republishes the tree.

        **Injected (``mqttc=``)** — opens nothing and publishes nothing. The
        caller connects their own client, and the tree goes out on the first
        ``publish_tick``. Returns immediately; see the comment below for why
        waiting would be wrong rather than merely pointless."""
        if not self._owns_client:
            # The caller owns the connection and its timing. Blocking here would
            # stall the very loop an injected client is likely being driven on,
            # and the SDK never starts a client it did not build, so there is
            # nothing to wait for.
            #
            # Note what does NOT rescue an early publish here: the SDK registers
            # its on-connect republish inside connect_broker(), which returns at
            # `if self.mqttc` before reaching that registration for an injected
            # client. So nothing republishes on reconnect unless the caller wired
            # `republish_tree` themselves -- see that method, and the README's
            # "Bring your own transport" wiring order. Measured against a real
            # broker: after wiping the retained store, an injected tree came back
            # 5 topics of 56, an owned one all 56.
            self._started = True
            return
        self._root.start_mqtt_client()
        deadline = time.monotonic() + connect_timeout_s
        while not self._root.is_connected() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self._root.is_connected():
            _LOG.warning(
                "Emitter.start(): MQTT link not up after %.1fs; publishing anyway "
                "(values are retained and republished on connect)",
                connect_timeout_s,
            )
        self._started = True

    def publish_tick(self, tick_inputs: TickInputs) -> EbusPanelSnapshot:
        """The producer-facing publish path. Builds the snapshot from the tick,
        publishes the Homie diff, and returns the snapshot for read-back."""
        if not self._started:
            raise EmitterStateError("Emitter.publish_tick() called before start()")
        snapshot = self._build_snapshot_from_tick(tick_inputs)
        self._publish_diff(snapshot)
        return snapshot

    def seed_energy(
        self,
        instance_id: str,
        *,
        consumed_wh: float = 0.0,
        produced_wh: float = 0.0,
    ) -> None:
        """Overwrite an instance's energy accumulators. Typical use: producer
        reads last-known values from persistent storage and seeds at startup
        before the first ``publish_tick`` call. Raises ``KeyError`` for unknown
        instance IDs (caught typos before they cause silent data loss)."""
        self._energy.seed(instance_id, consumed_wh=consumed_wh, produced_wh=produced_wh)

    def seed_bess_soe(self, instance_id: str, soe_kwh: float) -> None:
        """Overwrite a BESS device's stored SOE. Raises ``EmitterStateError``
        if no BESS is configured or if ``instance_id`` is not among the
        configured BESS instances — both are producer-side programming
        errors."""
        if not self._bess:
            raise EmitterStateError(
                f"seed_bess_soe({instance_id!r}, ...): no BESS configured on this emitter"
            )
        if instance_id not in self._bess:
            known = sorted(self._bess.keys())
            raise EmitterStateError(
                f"seed_bess_soe: instance_id={instance_id!r} not among configured "
                f"BESS instances {known!r}"
            )
        self._bess[instance_id].set_soe(soe_kwh)

    def stop(self, *, graceful: bool = True, clear_retained: bool = False) -> None:
        """Take the tree down. With ``mqtt_cfg=``, tear down the connection too.

        Graceful (default): publish the root's ``$state=disconnected``; per
        Homie's effective-state rule the root going disconnected covers every
        child. For a client this emitter built, that is followed by ebus-sdk's
        bounded teardown, which stops it.

        A caller-injected client is **never** stopped — the emitter did not build
        it and may not be its only user. Two consequences a BYO caller has to
        know, because neither is visible:

        * The connection stays open and connected after this returns. Closing it
          is yours to do, and yours to time (see the non-graceful note below).
        * The emitter goes mute regardless. ebus-sdk's ``Device.stop()`` clears
          the root's transport reference on both paths, so ``republish_tree()``
          silently publishes nothing afterwards. If you wired it to your client's
          on-connect handler, as the README's recipe does, that hook is still
          attached to a live client and is now a no-op. Build a new ``Emitter``
          to resume publishing.

        Non-graceful: leave the tree looking like a producer that died, by
        publishing the root's ``$state=lost`` retained before dropping the
        connection. This is what a consumer test wants from a simulator, and it
        is emphatically NOT what a bare disconnect gives you: the Last Will fires
        only on an *unclean* disconnect, and every teardown here closes cleanly,
        so relying on the will would leave the whole retained tree claiming
        ``ready`` forever. This goes through ebus-sdk's ``Device.declare_lost()``,
        which publishes exactly the topic and payload ``Device.will()`` describes,
        so the declared and will-driven paths cannot drift.

        The difference from a real will is timing and delivery, not content: this
        lands immediately over the live connection, where a broker-delivered will
        waits on keepalive expiry. A consumer exercising the *retained* view sees
        the same thing either way; one exercising live will delivery does not.

        **On an injected transport, let the loop turn before you close the
        client.** The ``lost`` is queued on the caller's loop, not flushed — the
        emitter cannot flush it, and would not want to: ``wait_for_publish``
        blocks the very thread that has to run ``loop_write`` for a client pumped
        by ``asyncio_driver``. Closing the client in the same synchronous breath
        as this call therefore drops the message and leaves the retained tree on
        ``ready``, deterministically. There is nothing to await: letting the loop
        turn once before you close the client is the whole remedy.

        ``clear_retained`` additionally clears every device's retained values +
        ``$description`` before disconnecting, for a clean-slate re-run. It
        applies to the graceful path only, since a producer that died clears
        nothing."""
        if not graceful:
            # Only ever stop a client this emitter had built for it. Stopping an
            # injected one would tear down a connection the caller owns and may
            # be using for other things. Ownership is what decides, not the type:
            # an injected client can itself be an ``MqttClient`` (a caller driving
            # one on its own event loop via ``asyncio_driver``), so the narrowing
            # below only makes the ``stop()`` call well-typed.
            client = owned_client(self._root.mqttc) if self._owns_client else None
            # Before the stop, not after: an owned client's connection is gone
            # once it returns. ``declare_lost`` handles the ownership split
            # itself — flushed on a client the SDK owns, queued on the caller's
            # loop on an injected one — so this call is the whole publish on
            # both paths.
            self._root.declare_lost()
            if client is not None:
                client.stop()
            return
        if clear_retained:
            for device in self._graph.devices.values():
                device.delete_all_from_mqtt()
        self._root.stop()

    def update_bess_config(self, config: BESSConfig) -> None:
        """Replace (or add) a BESS device's configuration keyed by
        ``config.instance_id``. Takes effect on the next publish call.
        SOC/SOE state persists across in-place config swaps; freshly added
        BESS instances start from their config's ``initial_soc_pct``."""
        existing = self._bess.get(config.instance_id)
        if existing is None:
            self._bess[config.instance_id] = BESSDevice(config=config)
        else:
            existing.update_config(config)

    def update_load_shedding_config(self, config: LoadSheddingConfig) -> None:
        if self._load_shedding is None:
            self._load_shedding = LoadSheddingDevice(config=config)
        else:
            self._load_shedding.update_config(config)

    @property
    def last_snapshot(self) -> EbusPanelSnapshot | None:
        return self._last_snapshot

    @property
    def topology_version(self) -> int:
        return next(iter(self._mapping.values())).profile_version

    @property
    def relays(self) -> RelayResolver:
        """Read-write access to the per-circuit relay resolver. Used by /set
        handlers (registered by the emitter for ``circuit.switch/relay``) to
        update operator overrides."""
        return self._relays

    @property
    def dominant_power_source_override(self) -> str | None:
        """Operator-set dominant power source override, or None if not set.
        Set via /set ``panel.pcs/dominant-power-source`` topic."""
        return self._dominant_power_source_override

    # ---- internal --------------------------------------------------------

    def _register_internal_setters(self, registry: SetterRegistry) -> None:
        """Register default handlers for the settable properties when the
        producer hasn't already supplied one. The handlers update emitter-
        internal state (RelayResolver, the priority override map, the panel
        asserted-islanding override). The next ``publish_tick`` call reflects
        the change on the wire.

        Producers needing custom routing register their own handler before
        constructing the ``Emitter`` and the registry's existing entry wins."""

        def on_circuit_relay(
            entity_class: str,
            instance_id: str,
            prop_path: str,
            value: object,
        ) -> None:
            del entity_class, prop_path
            # Homie boolean: True = relay closed (energized), False = open.
            closed = (
                bool(value)
                if isinstance(value, bool)
                else (str(value).strip().lower() in ("true", "1", "closed", "on"))
            )
            new_state = RelayState.CLOSED if closed else RelayState.OPEN
            if self._relays.known(instance_id):
                self._relays.set_user_override(instance_id, new_state)

        def on_shed_priority(
            entity_class: str,
            instance_id: str,
            prop_path: str,
            value: object,
        ) -> None:
            del entity_class, prop_path
            if instance_id in self._priority_locked:
                # Absolute, exactly as `RelayResolver` drops a `/set` on a locked
                # relay. The declaration already removes the `/set` subscription,
                # so this covers the paths that do not go through it: a producer
                # routing commands into the registry itself, or a broker
                # delivering to a topic nothing subscribed.
                return
            self._priority_overrides[instance_id] = str(value).upper()

        def on_asserted_islanding(
            entity_class: str,
            instance_id: str,
            prop_path: str,
            value: object,
        ) -> None:
            del entity_class, instance_id, prop_path
            # A SPAN panel accepts ON_GRID or OFF_GRID only while its link to a
            # battery is not known to be healthy, and ignores everything else --
            # NONE included, since only the panel clears an assertion -- leaving
            # the published value unchanged, which is how a consumer sees the
            # refusal.
            asserted = str(value).upper()
            if asserted in _ASSERTABLE_ISLANDING_STATES and self._assertion_eligible():
                self._asserted_islanding_override = asserted

        def on_shed_policy(
            entity_class: str,
            instance_id: str,
            prop_path: str,
            value: object,
        ) -> None:
            del entity_class, instance_id, prop_path
            # A json-datatype /set delivers a parsed object; store the policy as JSON text.
            self._shed_policy_override = value if isinstance(value, str) else json.dumps(value)

        def on_evse_user_max(
            entity_class: str,
            instance_id: str,
            prop_path: str,
            value: object,
        ) -> None:
            del entity_class, prop_path
            # An integer, clamped into [minimum, commissioned max]; anything
            # else is refused and the value left unchanged.
            try:
                requested = value if isinstance(value, int) else int(str(value).strip())
            except ValueError:
                _LOG.warning("evse %s: refusing user-max-charge-current %r", instance_id, value)
                return
            ceiling = int(self._physics.evse(instance_id).max_current_a)
            self._evse_user_max_override[instance_id] = max(
                EVSE_MIN_CHARGE_CURRENT_A, min(requested, ceiling)
            )

        def on_evse_lock(
            entity_class: str,
            instance_id: str,
            prop_path: str,
            value: object,
        ) -> None:
            del entity_class, prop_path
            requested = str(value).upper()
            if requested not in ("LOCKED", "UNLOCKED"):
                _LOG.warning("evse %s: refusing lock-state %r", instance_id, value)
                return
            self._evse_lock_override[instance_id] = requested

        if registry.get("circuit", "switch/relay") is None:
            registry.register("circuit", "switch/relay", on_circuit_relay)
        if registry.get("circuit", "load-shed/priority") is None:
            registry.register("circuit", "load-shed/priority", on_shed_priority)
        if registry.get("panel", "shed/asserted-islanding-state") is None:
            registry.register("panel", "shed/asserted-islanding-state", on_asserted_islanding)
        if registry.get("panel", "shed/policy") is None:
            registry.register("panel", "shed/policy", on_shed_policy)
        if registry.get("evse", "switch/lock-state") is None:
            registry.register("evse", "switch/lock-state", on_evse_lock)
        if registry.get("evse", "config/user-max-charge-current") is None:
            registry.register("evse", "config/user-max-charge-current", on_evse_user_max)

    def _publish_diff(self, snapshot: EbusPanelSnapshot) -> None:
        bag = self._bag_builder.build(snapshot)
        self._publisher.publish(bag)
        self._last_snapshot = snapshot

    def _build_snapshot_from_tick(self, tick: TickInputs) -> EbusPanelSnapshot:
        self._validate_bess_communication(tick.bess_communication)
        panel_phys = self._physics.panel
        circuits_phys = self._physics.all_circuits()

        # Step 1: aggregate inputs for BESS dispatch (pre-shed: use raw producer
        # power, not gated, since shedding decisions DEPEND on BESS SOC).
        load_demand_w = sum(p for p in tick.circuits.values() if p > 0)
        pv_available_w = -sum(p for p in tick.circuits.values() if p < 0)

        # Step 2: BESS dispatch + battery snapshots — one per configured BESS.
        # ``battery_w`` is the SUM of signed dispatch across all batteries; it
        # feeds the panel meter aggregation as a single combined contribution.
        # The min-SOC across batteries is what drives load-shedding decisions
        # (the most-depleted BESS is the binding constraint).
        battery_snapshots: dict[str, EbusBatterySnapshot] = {}
        battery_w = 0.0
        for bess_id, bess_dev in self._bess.items():
            bphys = self._physics.bess(bess_id)
            snap = bess_dev.tick(
                NativeTickContext(
                    current_time=tick.current_time,
                    grid_online=tick.grid_online,
                    load_demand_w=load_demand_w,
                    pv_available_w=pv_available_w,
                )
            )
            snap.instance_id = bess_id
            snap.vendor_name = bphys.vendor_name
            snap.part_number = bphys.part_number
            snap.model = bphys.model
            snap.serial_number = bphys.serial_number
            snap.firmware_version = bphys.firmware_version
            snap.relative_position = bphys.relative_position
            snap.feed_circuit_id = bphys.feed
            snap.communication = tick.bess_communication.get(bess_id, snap.communication)
            snap.connected = snap.communication == "OK"
            snap.grid_state = "ON_GRID" if tick.grid_online else "OFF_GRID"
            battery_snapshots[bess_id] = snap
            battery_w += snap.active_power_w
        has_battery = bool(battery_snapshots)
        self._bess_links = {bid: s.communication or "OK" for bid, s in battery_snapshots.items()}
        self._expire_assertion(tick.current_time)
        # ``min_soc`` is None when no BESS reports a SOE value (all uninitialised);
        # ``decide_shed`` then treats SOC as unknown.
        soc_values = [
            s.soe_percentage for s in battery_snapshots.values() if s.soe_percentage is not None
        ]
        min_soc: float | None = min(soc_values) if soc_values else None

        # Step 3: load-shedding decisions written into RelayResolver. Always
        # cleared first so a previous tick's shed state doesn't linger when the
        # grid comes back online or SOC recovers. Operator-set priority
        # overrides take precedence over manifest defaults.
        self._relays.clear_all_shed()
        if self._load_shedding is not None:
            effective_priorities = {
                cid: self._priority_overrides.get(cid, cphys.default_priority)
                for cid, cphys in circuits_phys.items()
            }
            shed_ids = self._load_shedding.decide_shed(
                grid_online=tick.grid_online,
                bess_soc_pct=min_soc,
                priorities=effective_priorities,
            )
            for cid in shed_ids:
                self._relays.set_shed(cid, open_relay=True)

        # Step 4: resolve final relay state per circuit (locked > /set > shed
        # > default-CLOSED) and gate producer-reported power.
        gated_powers: dict[str, float] = {}
        for cid in circuits_phys:
            raw_power = tick.circuits.get(cid, 0.0)
            relay_state, _requester = self._relays.state(cid)
            gated_powers[cid] = 0.0 if relay_state == RelayState.OPEN else raw_power

        # Step 4: integrate energy per circuit using gated power (if relay open,
        # no energy flows).
        for cid, gated in gated_powers.items():
            self._energy.observe(cid, gated, tick.current_time)
        for eid, evse_power in tick.evse.items():
            if self._energy.known(eid):
                self._energy.observe(eid, evse_power, tick.current_time)

        # Step 5: panel-level aggregation.
        meter = resolve_panel(
            panel=panel_phys,
            circuits=circuits_phys,
            gated_powers=gated_powers,
            battery_w=battery_w,
            grid_online=tick.grid_online,
            has_battery=has_battery,
        )

        # Cross-device connection edges. Real SPAN owns the connection index on
        # the panel-side device (the circuit or lugs that feeds a DER), never on
        # the DER child: each DER's feed circuit publishes the feeds-* triple, and
        # an upstream BESS's fed-by triple lands on the upstream lugs. Status is a
        # link-health enum; PV/EVSE have no comms model so they report OK, a BESS
        # reports its battery's link health in the values that status reports.
        feeds_by_circuit: dict[str, tuple[str, str, str]] = {}
        for pv_id, pv_phys in self._physics.all_pv().items():
            if pv_phys.feed:
                feeds_by_circuit[pv_phys.feed] = (pv_id, self._profiles["pv"].type, "OK")
        for evse_id, evse_phys in self._physics.all_evse().items():
            if evse_phys.feed:
                feeds_by_circuit[evse_phys.feed] = (evse_id, self._profiles["evse"].type, "OK")
        upstream_fed_by: tuple[str, str, str] | None = None
        for bess_id, bess_phys in self._physics.all_bess().items():
            bsnap = battery_snapshots.get(bess_id)
            link = (bsnap.communication if bsnap else None) or "OK"
            if bess_phys.relative_position == "UPSTREAM":
                upstream_fed_by = (
                    bess_id,
                    self._profiles["bess"].type,
                    _link_status(link, self._fed_by_status_values),
                )
            elif bess_phys.feed:
                feeds_by_circuit[bess_phys.feed] = (
                    bess_id,
                    self._profiles["bess"].type,
                    _link_status(link, self._feeds_status_values),
                )

        # Step 6: build per-circuit snapshots — applying any operator name and
        # priority overrides on top of manifest defaults.
        circuit_snaps: dict[str, EbusCircuitSnapshot] = {}
        for cid, cphys in circuits_phys.items():
            relay_state, requester = self._relays.state(cid)
            gated_p = gated_powers[cid]
            estate = self._energy.state(cid)
            effective_priority = self._priority_overrides.get(cid, cphys.default_priority)
            effective_name = self._name_overrides.get(
                cid,
                self._manifest.get("circuit", cid).display_name,
            )
            edge = feeds_by_circuit.get(cid)
            circuit_snaps[cid] = EbusCircuitSnapshot(
                circuit_id=cid,
                name=effective_name,
                relay_state=str(relay_state),
                instant_power_w=gated_p,
                produced_energy_wh=estate.produced_wh,
                consumed_energy_wh=estate.consumed_wh,
                tabs=list(cphys.tabs),
                priority=effective_priority,
                is_user_controllable=not cphys.always_on,
                # Both conjuncts of the retired flat `sheddable`, which the
                # migration guide defines as "`load-shed/priority != NEVER` &&
                # `switch/relay-controllable`". The relay half was missing, so a
                # circuit `RelayResolver` refuses to shed -- the enclosure "never
                # opens a circuit commissioned as permanently OFF_GRID / locked"
                # -- reported itself sheddable. The priority half is narrowed to
                # the values this emitter's own policy acts on
                # (`native_devices/load_shedding.py`), which are the shed-eligible
                # members of the v1.0 enum.
                is_sheddable=(
                    effective_priority in ("OFF_GRID", "SOC_THRESHOLD") and not cphys.always_on
                ),
                # The commissioning lock, not the priority value: `NEVER` means
                # "never shed" and is an ordinary settable value, while
                # never-backup is an installer input that pins the circuit to
                # OFF_GRID and removes `$settable` from its priority.
                is_never_backup=cphys.never_backup,
                is_240v=cphys.dipole,
                current_a=circuit_current_a(
                    gated_p,
                    dipole=cphys.dipole,
                    line_voltage_v=panel_phys.line_voltage_v,
                ),
                breaker_rating_a=cphys.breaker_rating_a,
                always_on=cphys.always_on,
                pcs_managed=not cphys.always_on,
                pcs_priority=cphys.pcs_priority,
                # CONFIGURATION is not in span-alpha-test-b2's format; a locked relay
                # with no disconnect reason held reports NONE there.
                relay_requester=(
                    "NONE"
                    if self._variant == "span-alpha-test-b2" and requester == "CONFIGURATION"
                    else str(requester)
                ),
                energy_accum_update_time_s=int(tick.current_time),
                instant_power_update_time_s=int(tick.current_time),
                feeds_device_id=edge[0] if edge else None,
                feeds_device_type=edge[1] if edge else None,
                feeds_device_status=edge[2] if edge else None,
            )

        # Step 7: PV snapshots — one entry per PV instance in the manifest.
        # Per-PV power telemetry comes from the producer's circuit feed; the
        # snapshot here carries the static identity from manifest physics.
        pv_snaps: dict[str, EbusPvSnapshot] = {}
        for pv_id, pv_phys in self._physics.all_pv().items():
            pv_snaps[pv_id] = EbusPvSnapshot(
                node_id=pv_id,
                feed_circuit_id=pv_phys.feed,
                vendor_name=pv_phys.vendor_name,
                model=pv_phys.model,
                serial_number=pv_phys.serial_number,
                nominal_power_w=pv_phys.nominal_power_w,
                firmware_version=pv_phys.firmware_version,
                relative_position=pv_phys.relative_position,
            )

        # Step 7b: Lugs snapshots — one per declared lugs instance. Per-leg
        # currents and power/energy come from the panel meter aggregation;
        # ``direction`` and ``feed`` come from manifest physics. Producers that
        # only model a single lugs (most US split-phase setups) get a single
        # entry here; OPNsense-fed multi-lugs panels get one per device.
        lugs_snaps: dict[str, EbusLugsSnapshot] = {}
        for lugs_id, lphys in self._physics.all_lugs().items():
            if lphys.direction == "upstream":
                l1 = meter.upstream_l1_current_a
                l2 = meter.upstream_l2_current_a
                # Upstream lugs are panel-side. With an upstream BESS, utility
                # grid flow is computed beyond the BESS and can differ.
                active_w = meter.upstream_active_power_w
            else:  # downstream
                l1 = meter.downstream_l1_current_a
                l2 = meter.downstream_l2_current_a
                active_w = meter.feedthrough_power_w
            # Integrate what this meter reads, in this meter's own frame: a lugs
            # meter takes the default reference direction, so positive is power
            # arriving through it and accrues `imported-energy`. One direction can
            # accrue per tick, which is the property the gross sum broke.
            self._energy.observe(lugs_id, active_w, tick.current_time)
            lugs_energy = self._energy.state(lugs_id)
            imported_wh = lugs_energy.consumed_wh
            exported_wh = lugs_energy.produced_wh
            fed_by = upstream_fed_by if lphys.direction == "upstream" else None
            lugs_snaps[lugs_id] = EbusLugsSnapshot(
                instance_id=lugs_id,
                direction=("upstream" if lphys.direction == "upstream" else "downstream"),
                feed=None,
                l1_current_a=l1,
                l2_current_a=l2,
                active_power_w=active_w,
                imported_energy_wh=imported_wh,
                exported_energy_wh=exported_wh,
                fed_by_device_id=fed_by[0] if fed_by else None,
                fed_by_device_type=fed_by[1] if fed_by else None,
                fed_by_device_status=fed_by[2] if fed_by else None,
            )

        # Step 8: EVSE snapshots derived from per-tick power.
        evse_snaps: dict[str, EbusEvseSnapshot] = {}
        for eid, ephys in self._physics.all_evse().items():
            power = tick.evse.get(eid, 0.0)
            charging = power > 100.0
            # Unpublished until a user sets it, max-charge-current being the
            # ceiling; SPAN firmware before release 202639 published the
            # commissioned maximum until then (`ebus_panel_sim.firmware`).
            user_max_unset = (
                int(ephys.max_current_a)
                if self._conventions.user_max_charge_current_preset
                else None
            )
            evse_snaps[eid] = EbusEvseSnapshot(
                node_id=eid,
                feed_circuit_id=ephys.feed,
                status="CHARGING" if charging else "AVAILABLE",
                lock_state=self._evse_lock_override.get(eid, "LOCKED" if charging else "UNLOCKED"),
                advertised_current_a=ephys.max_current_a,
                max_charge_current_a=int(ephys.max_current_a),
                user_max_charge_current_a=self._evse_user_max_override.get(eid, user_max_unset),
                vendor_name=ephys.vendor_name,
                model=ephys.model,
                part_number=ephys.part_number,
                serial_number=ephys.serial_number,
                firmware_version=ephys.firmware_version,
            )

        # Step 8b: MID snapshots — the grid-forming interconnect device that a
        # commissioned islanding BESS exposes. Identity is static; grid state is
        # derived from the grid-online signal this tick.
        #
        # Off grid, the grid catalog wants the Homie device id of the device
        # forming the reference, not its class. Left absent when no single BESS
        # answers to that, which the catalog permits for "unknown".
        islanded_former = self._grid_forming_device_id()
        mid_snaps: dict[str, EbusMidSnapshot] = {}
        for mid_id, mphys in self._physics.all_mid().items():
            mid_snaps[mid_id] = EbusMidSnapshot(
                instance_id=mid_id,
                vendor_name=mphys.vendor_name,
                serial_number=mphys.serial_number,
                model=mphys.model,
                firmware_version=mphys.firmware_version,
                hardware_version=mphys.hardware_version,
                islanding_state="ON_GRID" if tick.grid_online else "OFF_GRID",
                grid_state="UP" if tick.grid_online else "DOWN",
                grid_forming_entity="GRID" if tick.grid_online else islanded_former,
            )

        # Step 9: assemble the panel snapshot from capability sub-dataclasses.
        info = EbusPanelInfo(
            serial_number=panel_phys.serial_number,
            firmware_version=panel_phys.firmware_version,
            vendor_name=panel_phys.vendor_name,
            hardware_version=panel_phys.hardware_version,
            panel_size=panel_phys.panel_size,
            panel_model=panel_phys.panel_model,
            schema_topology=panel_phys.topology,
        )
        door = EbusPanelDoor(
            state=tick.envelope.door_state,
            proximity_proven=tick.envelope.proximity_proven,
        )
        consumed_total = sum(s.consumed_energy_wh for s in circuit_snaps.values())
        produced_total = sum(s.produced_energy_wh for s in circuit_snaps.values())
        feedthrough_consumed = sum(
            s.consumed_energy_wh
            for cid, s in circuit_snaps.items()
            if circuits_phys[cid].placement == "downstream-of-lugs"
        )
        feedthrough_produced = sum(
            s.produced_energy_wh
            for cid, s in circuit_snaps.items()
            if circuits_phys[cid].placement == "downstream-of-lugs"
        )
        meter_section = EbusPanelMeter(
            instant_grid_power_w=meter.instant_grid_power_w,
            main_meter_energy_consumed_wh=consumed_total,
            main_meter_energy_produced_wh=produced_total,
            feedthrough_power_w=meter.feedthrough_power_w,
            feedthrough_energy_consumed_wh=feedthrough_consumed,
            feedthrough_energy_produced_wh=feedthrough_produced,
            l1_voltage=meter.line_voltage_v,
            l2_voltage=meter.line_voltage_v,
            upstream_l1_current_a=meter.upstream_l1_current_a,
            upstream_l2_current_a=meter.upstream_l2_current_a,
            downstream_l1_current_a=meter.downstream_l1_current_a,
            downstream_l2_current_a=meter.downstream_l2_current_a,
            busbar_current_a=_busbar_current(meter.power_flow_site, panel_phys.service_voltage_v),
            frequency_hz=_NOMINAL_FREQUENCY_HZ if meter.line_voltage_v > 0 else None,
        )
        status = EbusPanelStatus(
            main_relay_state=meter.main_relay_state,
            eth0_link=tick.envelope.eth0_link,
            wlan_link=tick.envelope.wlan_link,
            wwan_link=tick.envelope.wwan_link,
            wifi_ssid=tick.envelope.wifi_ssid,
            cloud_connection=tick.envelope.cloud_connection,
            postal_code=panel_phys.postal_code,
            time_zone=panel_phys.time_zone,
            uptime_s=tick.envelope.uptime_s,
        )
        pcs = EbusPanelPcs(
            main_breaker_rating_a=panel_phys.main_breaker_rating_a,
            dominant_power_source=(
                self._dominant_power_source_override
                if self._dominant_power_source_override is not None
                else meter.dominant_power_source
            ),
            grid_state=meter.grid_state,
            dsm_state=meter.dsm_state,
            current_run_config=meter.current_run_config,
        )
        # Under span-alpha-test-b2, a panel with neither a battery nor solar (a
        # circuit commissioned feeds-role SOLAR) leaves pv and battery unset.
        loads_only = (
            self._variant == "span-alpha-test-b2"
            and not has_battery
            and not any(
                c.wire_values.get("connection/feeds-role") == "SOLAR"
                for c in circuits_phys.values()
            )
        )
        power_flows = EbusPanelPowerFlows(
            pv=None if loads_only else meter.power_flow_pv,
            battery=None if loads_only else meter.power_flow_battery,
            grid=meter.power_flow_grid,
            site=meter.power_flow_site,
        )
        shed = EbusPanelShed(
            asserted_islanding_state=self._asserted_islanding_override or "NONE",
            policy=self._shed_policy_override
            or (
                '{"algorithm": "soc-priority.v1", '
                '"parameters": {"soc-threshold-shed": 20, "soc-threshold-release": 30}}'
            ),
        )
        # Battery Time Remaining forecast: representative values matching real SPAN
        # (lc3 nt-2026-c192x), published only when a BESS is present. Minutes for the
        # four time fields; confidence is a LOW/MEDIUM/HIGH enum.
        shed_forecast = (
            EbusPanelShedForecast(
                total_time_remaining=4320,
                time_to_priority_shed=3037,
                full_charge_total_time_remaining=4320,
                full_charge_time_to_priority_shed=3038,
                confidence="HIGH",
            )
            if has_battery
            else EbusPanelShedForecast()
        )

        # A remote-ct meters the service conductor: utility-side grid power,
        # positive on import.
        remote_ct_snaps: dict[str, EbusRemoteCtSnapshot] = {}
        for rct_id in self._remote_ct_ids:
            self._energy.observe(rct_id, meter.instant_grid_power_w, tick.current_time)
            estate = self._energy.state(rct_id)
            remote_ct_snaps[rct_id] = EbusRemoteCtSnapshot(
                active_power_w=meter.instant_grid_power_w,
                current_a=(
                    abs(meter.instant_grid_power_w) / panel_phys.service_voltage_v
                    if panel_phys.service_voltage_v > 0
                    else 0.0
                ),
                imported_energy_wh=estate.consumed_wh,
                exported_energy_wh=estate.produced_wh,
            )

        return EbusPanelSnapshot(
            info=info,
            door=door,
            meter=meter_section,
            status=status,
            pcs=pcs,
            power_flows=power_flows,
            shed=shed,
            shed_forecast=shed_forecast,
            circuits=circuit_snaps,
            battery=battery_snapshots,
            pv=pv_snaps,
            evse=evse_snaps,
            lugs=lugs_snaps,
            mid=mid_snaps,
            remote_ct=remote_ct_snaps,
        )

    def _validate_bess_communication(self, links: Mapping[str, str]) -> None:
        """Refuse a tick naming a battery that is not configured, or a link health
        outside ``BESSCommunication``, before any state moves."""
        unknown = sorted(set(links) - set(self._bess))
        if unknown:
            raise EmitterStateError(
                f"TickInputs.bess_communication names {unknown!r}, which are not among "
                f"the configured BESS instances {sorted(self._bess)!r}"
            )
        invalid = {bid: v for bid, v in links.items() if v not in _BESS_COMMUNICATION_STATES}
        if invalid:
            raise EmitterStateError(
                f"TickInputs.bess_communication {invalid!r}: expected one of "
                f"{sorted(_BESS_COMMUNICATION_STATES)!r}"
            )

    def _assertion_eligible(self) -> bool:
        """Whether the panel would accept an assertion now: while its link to some
        battery is not known to be healthy. Before the first tick no link has been
        observed, so a panel with a battery accepts; one without a battery has no
        link to lose and never does."""
        if self._bess_links is None:
            return bool(self._bess)
        if self._assertion_link_states is not None:
            return any(link in self._assertion_link_states for link in self._bess_links.values())
        return any(link != "OK" for link in self._bess_links.values())

    def _expire_assertion(self, now: float) -> None:
        """Clear an active assertion once every battery link has been ``OK`` for
        ``ASSERTION_CLEAR_AFTER_S``, as the panel does when it reclaims authority.
        Any unhealthy tick restarts the wait, so a flapping link keeps it."""
        if any(link != "OK" for link in (self._bess_links or {}).values()):
            self._links_healthy_since = None
            return
        if self._links_healthy_since is None:
            self._links_healthy_since = now
        if (
            self._asserted_islanding_override is not None
            and now - self._links_healthy_since >= ASSERTION_CLEAR_AFTER_S
        ):
            self._asserted_islanding_override = None

    def _grid_forming_device_id(self) -> str | None:
        """Homie id of the device forming the AC reference while islanded.

        The MID mapping places it ``child-of-parent`` under ``bess``, and the
        graph builder refuses a manifest where that parent is ambiguous, so a
        single BESS is the answer wherever a MID exists at all. Anything else is
        reported as unknown rather than guessed at.
        """
        bess_ids = tuple(self._physics.all_bess())
        if len(bess_ids) != 1:
            return None
        return bess_ids[0]
