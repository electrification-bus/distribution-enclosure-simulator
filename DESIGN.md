# ebus-panel-sim design

Internals of the emitter. For what it is and how to run/configure it, see [README.md](README.md).

## The per-tick pipeline

The producer builds a `DeviceManifest` (identity plus physics keys per device) at startup and hands it to `Emitter` together with a `SetterRegistry`, an `mqtt_cfg` (the broker coordinates ebus-sdk connects with), zero or more `BESSConfig`s, and an optional `LoadSheddingConfig`. Each tick the producer builds a `TickInputs` (signed power per circuit, current time, grid-online flag, panel envelope) and calls `emitter.publish_tick(tick_inputs)`. Inside, the emitter:

1. Resolves BESS dispatch (charge/discharge/idle) for every native BESS.
2. Decides load-shedding (which circuits open when off-grid).
3. Applies relay-state precedence and gates per-circuit power.
4. Integrates energy per circuit/EVSE via `EnergyIntegrator`.
5. Computes per-leg currents and panel meter aggregates via `PanelMeter`.
6. Assembles an `EbusPanelSnapshot` and publishes the Homie diff to MQTT (only changed properties are retransmitted).

## Wire model

The enclosure is a Homie root device (`energy.ebus.device.distribution-enclosure`); every circuit, lugs pair, and integrated DER (BESS, PV, EVSE, MID) is a separate child Homie device with `root` and `parent` back-references to the enclosure. Each device's properties are grouped into capability-typed nodes (`info`, `meter`, `switch`, `breaker`, `load-shed`, `pcs`, `connection`, `status`, `door`, `soc`, `shed`, `shed-forecast`, `grid`, `config`, `power-flows`), whose node `$type` is `energy.ebus.capability.<capability>`. A child device therefore publishes under its own topic root, for example `ebus/5/<circuit-id>/switch/relay` and `ebus/5/<bess-id>-mid/grid/islanding-state`.

Placement is declarative. Each `wire/mapping/*.yaml` descriptor says whether its device class is the `root-device` or a `child-of-parent`; `graph_builder` walks the manifest, mappings, and profiles to build the SDK device graph, and the SDK's `Device` owns the `$state` cascade: `graph_builder` wraps each device's node/property build in a `state_transition()` that coalesces the description republish into a single `init` then `ready` cycle, while `Emitter.start()`/`stop()` drive connect and disconnect. A graceful `stop()` publishes only the root's `$state=disconnected` (by Homie's effective-state rule that covers every child); `stop(graceful=False)` publishes the root's `$state=lost` itself, because the registered LWT cannot deliver it (a will fires only on an *unclean* disconnect, and every teardown path here closes cleanly, deliberately, so an orderly shutdown is not reported as a crash); retained topics are cleared only when `stop(clear_retained=True)` is passed, which is graceful-only, since a producer that died clears nothing. The vendored `wire/profiles/*.json` are the schema (capabilities, properties, datatypes, units, `$format`, settability); `bag_builder` maps each profile-declared property to a snapshot accessor and fails loud at construction if any declared property has no source.

That description assumes the emitter owns the connection. With an injected transport (`Emitter(mqttc=...)`) the same teardown still moves the root to `$state=lost` and publishes it, but three things move to the caller, because the emitter never starts or stops a client it did not build. The LWT is registered by the caller before they connect (`Emitter.lwt_settings(manifest)` answers it without an instance, since the will rides the CONNECT packet); the on-(re)connect whole-tree republish is wired by the caller (`Emitter.republish_tree`), the SDK registering its own only inside `connect_broker()`, below the `if self.mqttc` early return an injected client always takes; and the ungraceful `lost` is *queued* on the caller's loop rather than flushed, since flushing would block the thread running `loop_write`, so the caller must let the loop turn before closing the client. Absent the first, the tree has no will and an unclean death leaves consumers on a stale retained `ready`; absent the second, a broker that loses its retained store never gets the tree back.

## Native devices

Two device classes are not pure publishers: their behaviour runs inside the emitter.

### BESS (`ebus_panel_sim.native_devices.bess`)

Owns the dispatch decision, SOC/SOE accumulation, mode behaviour (self-consumption / backup-only), and the backup-reserve floor. (The `charge_hours` / `discharge_hours` config fields exist but are inert: the dispatch logic never reads them, and hour-of-day / TOU windows are explicitly not modelled.) Instantiated when `Emitter` is constructed with `bess_configs`, a tuple of `BESSConfig` (default empty). One `BESSDevice` is created per config, keyed by `BESSConfig.instance_id`; duplicate instance IDs raise `EmitterStateError`.

Per-tick inputs (from `TickInputs`): `current_time` (received but not consulted by the current dispatch logic; hour-of-day windows are not modelled), `grid_online` (when false the BESS discharges to meet `load_demand - pv_available`), and the derived `load_demand_w` (sum of positive circuit powers) and `pv_available_w` (magnitude of the negative circuit powers).

Per-tick outputs (into `snapshot.battery`): `soe_percentage`, `soe_kwh`, and `active_power_w` (positive = discharging, negative = charging).

Mid-run config changes: `emitter.update_bess_config(new_config)` swaps the `BESSConfig` reference while SOC/SOE state persists (the path for dashboard edits to mode and max charge/discharge rates; the charge/discharge hour-window fields are carried but not yet applied by the dispatch logic). Persistence across restart: call `emitter.seed_bess_soe(instance_id, soe_kwh)` between `__init__` and `start()`, or declare `initial-soe-kwh` in the manifest. Subclassing `BESSDevice` is supported for vendor-variant behaviour without a plugin framework.

### Load shedding (`ebus_panel_sim.native_devices.load_shedding`)

When the grid is offline the policy returns the circuit instance-ids whose priority is `OFF_GRID`, plus those with `SOC_THRESHOLD` priority once the live SOC falls below `soc_threshold_pct`. The emitter writes that decision into the `RelayResolver` shed map; final relay state is then resolved by the precedence rules below. Mid-run config: `emitter.update_load_shedding_config(new_config)`.

## Settable properties (`/set`)

`/set` commands arrive on the child device's settable-property topics (for example `ebus/5/<circuit-id>/switch/relay/set`) and are dispatched through the `SetterRegistry`. The emitter installs internal default handlers for the settable properties when the producer has not supplied one; producer-supplied handlers always win.

| Entity class | Property | Effect |
|---|---|---|
| circuit | `switch/relay` | Updates the `RelayResolver` user override |
| circuit | `load-shed/priority` | Updates the emitter's per-circuit priority override (refused on a never-backup circuit) |
| panel | `shed/asserted-islanding-state` | Updates the consumer-asserted islanding override |
| evse | `config/user-max-charge-current` | Updates the per-EVSE user charge-current ceiling |

### Relay state precedence

```text
locked > /set override > load-shed > default-CLOSED
```

- **Locked** circuits (`relay-behavior` of `always-on` or `non-controllable`, or explicit `always-on: true`) ignore both `/set` and load-shed; the relay is permanently CLOSED and `switch/relay-requester` reports `CONFIGURATION`. One bit governs both paths, because `relay-controllable` is defined as the relay being openable "by command or automatic shed", and the enclosure model says the host never opens a circuit commissioned locked. The same bit suppresses `$settable` on `switch/relay` and sets `switch/relay-controllable`, so the description and the values cannot disagree.
- **Never-backup** circuits (`never-backup: true`) are the *other* commissioning lock, and it is on a different property: the circuit is commissioned permanently `OFF_GRID`, so its `load-shed/priority` carries no `$settable` and a `/set` on it is refused. The relay itself is not locked — a never-backup circuit is shed like any other `OFF_GRID` circuit when the panel islands — but that open is attributed to `CONFIGURATION` rather than `LOAD_SHED`, because the decision was made once at commissioning and the policy running now is only carrying it out (the migration guide maps the flat `NEVER_BACKUP` requester onto `CONFIGURATION`, and flat `BACKUP` onto `LOAD_SHED`). At rest, closed, it reports `NONE`: the lock speaks only when it acts. The two locks are independent: either, both or neither may be commissioned on a circuit. Note that the priority *value* `NEVER` is not a lock at all — it means "never shed" and stays settable.
- A `/set` override takes effect on the next tick, with no debounce, and can override a safety-shed. `switch/relay-requester` reports `USER`.
- Load-shed applies only when there is no `/set` override. `switch/relay-requester` reports `LOAD_SHED`, or `CONFIGURATION` when the circuit shed is one commissioned never-backup.
- Default-CLOSED is the resting state when no decision-maker has spoken. `switch/relay-requester` reports `NONE`.

Relay changes reach the wire on the next `publish_tick`, bounded by the producer's tick cadence (typically 1.0 s).

## Energy integration

`EnergyIntegrator` accumulates per-circuit (and per-EVSE) `consumed_wh` / `produced_wh` across ticks using `dt = current_time - last_tick_time` per instance. Seed values at startup with `emitter.seed_energy("kitchen", consumed_wh=12345.0, produced_wh=0.0)`, or via the manifest `initial-consumed-wh` / `initial-produced-wh` keys. Either path adds a new circuit to a running deployment without zeroing existing accumulators.

## Tab-to-leg convention

`legs_for_tabs((tab, ...)) -> tuple[Leg, ...]` in `ebus_panel_sim.conventions.tab_legs` is the single source of truth for the US residential split-phase convention: odd-numbered tabs land on L1, even-numbered on L2, and a 240 V circuit occupies tabs on both legs. It is isolated so non-US / 3-phase support can land there later without touching `PanelMeter` or the per-leg current calculations.

## Snapshot read-back

`emitter.last_snapshot` returns the most recently published `EbusPanelSnapshot`. Consumers (dashboards, HA-API endpoints) read aggregated state through this property; they do not construct snapshots.

## What lives where

| Concern | Owner |
|---|---|
| Homie wire mechanics (`$description`/`$state`, parent-child arrays, retained topics + value encoding, LWT) | ebus-sdk (emitter builds the `Device` tree + capability nodes and drives the start/stop lifecycle) |
| Device profiles + property graph + diff publishing | emitter |
| Settable-property routing (`/set` to internal state) | emitter |
| Relay state machine (locked > /set > shed > default-CLOSED) | emitter |
| BESS dispatch + SOC/SOE integration | emitter |
| Load-shedding policy (SOC threshold, off-grid priority) | emitter |
| Energy integration + per-leg current + panel meter aggregation | emitter |
| Device identity + static attributes (vendor, serial, ratings, tabs) | producer (via manifest) |
| Per-circuit / per-EVSE signed power, `current_time`, `grid_online`, envelope | producer (per tick) |
| Weather, schedules, rates, modelling, recorder/replay history | producer |
