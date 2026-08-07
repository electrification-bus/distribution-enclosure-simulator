# Changelog

## [Unreleased]

### Added

- **Bring-your-own-transport.** `Emitter(..., mqttc=client)` publishes the tree through a client the caller already owns, instead of having one built from `mqtt_cfg`. The two are mutually exclusive and passing both raises. This mirrors ebus-sdk's `Device(mqttc=...)` contract, and the case it serves is a host that cannot afford a second connection — a Home Assistant add-on, whose MQTT integration is `single_config_entry` and which forbids background threads (`ebus-mqtt-client` 0.4.0's `asyncio_driver()` covers pumping the loop). (#5)

### Fixed

- `Emitter.start()` no longer blocks waiting for a connection it does not own. With an injected client it returned only after polling `is_connected()` for the full `connect_timeout_s`, which would stall the very event loop such a client is likely driven on; the caller owns that lifecycle and the SDK never starts an injected client, so there is nothing to wait for. Retained values still republish on connect. Unchanged for a client the emitter had built. (#5)
- `Emitter.stop(graceful=False)` no longer calls `stop()` on an injected client, which would tear down a connection the caller owns and may be using for other things. ebus-sdk makes the same guarantee for the devices it holds; the emitter now matches it. (#5)

### Changed

- `ebus-sdk` pin moved from `>=0.12,<0.13` to `>=0.18,<0.19`. The old range excluded the release carrying the [python-sdk#27](https://github.com/electrification-bus/python-sdk/issues/27) fixes — the `battery` capability key removed in favour of `soc`, and `energy` → `energy_storage` / `total_increasing` → `measurement` for `soe`, `total-energy-storage` and `loadup-headroom` — so a downstream needing those could not stay inside the pin. No source changes: the published tree is byte-identical on `0.12.0` and `0.18.0` (197 retained topics, identical topic sets, zero payload differences once the `$description` `version` timestamp is normalised), and the suite is `158 passed` under both. `uv.lock` also moves `ebus-mqtt-client` 0.1.8 → 0.4.0, which `ebus-sdk` 0.18.0 requires. (#4, closes #3)

## [0.2.0] - 2026-08-01

### Fixed

- **BREAKING (wire):** circuit `meter/imported-energy` and `meter/exported-energy` are now published in the enclosure reference frame, matching the already-enclosure-framed `meter/active-power` and real panel firmware. Previously a load circuit published a rising `imported-energy` while its `active-power` was negative, so integrating the published power grew the opposite accumulator (every load looked like it produced energy). Consumers that compensated for the old inverted behaviour must drop the workaround; consumers written against real panel firmware need no change. Lugs metering is unchanged (the frames coincide there). (#2, fixes #1)

## [0.1.0] — 2026-05-02

### Added

- Initial scaffolding of the `panel-sim` package: wire layer (manifest/mapping/profiles,
  graph builder, lifecycle, set router, SDK seam, property bag diff) and schedule runner
  (clock, energy package, simulated circuits, solar curve evaluation, override store,
  tick orchestration).
- Public `Emitter` API: `start()`, `tick()`, `stop()`, `set_property_override()`,
  `clear_property_override()`, `force_grid_state()`, `last_snapshot`, `topology_version`,
  static `lwt_settings()`.
- Vendored Homie 5 device profiles for panel, circuit, lugs, BESS, PV, EVSE (v1_flat).
- Four canonical example manifest + runtime-spec pairs.
- End-to-end mosquitto integration test.
- 132 passing tests across `wire/`, `scheduleRunner/`, and integration suites.

### Deferred

- Full lift of the simulator's `RealisticBehaviorEngine` with cycling state, smart-load
  noise, and HVAC seasonal modulation. v0.1.0 ships a stub that applies the runtime-
  spec's pre-baked hour/monthly factors directly with deterministic noise.
- v2_children topology (parent-child Homie schema). Pending the upstream `ebus-sdk` adding
  parent/child support.
