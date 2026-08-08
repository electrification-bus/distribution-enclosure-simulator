# Changelog

## [Unreleased]

### Fixed

- **`grid/grid-forming-entity` published the class name `"BESS"` while islanded, where the catalog asks for a device id.** The grid capability catalog defines the property as `"GRID"` when grid-tied "or the Homie device ID of the grid-forming device … when islanded", and a consumer's use for it is to point at the device now forming the AC reference — to badge it, link to it, attribute the island to it. `"BESS"` resolves to nothing on the wire and defeats all three; in a manifest with a second DER it is not even unambiguous about which battery. It now publishes the BESS instance id, resolved the way the graph builder already resolves a MID's parent, and reports unknown (property absent, which the catalog permits) rather than guessing if a single BESS ever stops answering. No conformance report flagged this: the property's datatype is `string`, so the wrong value was always legal. Three tests pin it, two failing against the previous commit.

## [0.4.0] - 2026-08-08

Adds bring-your-own-transport: a producer that already owns its MQTT connection can publish an eBus tree through it. Additive, so nothing existing changes; the minor bump reflects new public API rather than a break.

The caller obligations are the part to read before using it. An injected client bypasses the SDK's connect path, so the Last Will and the on-(re)connect republish are yours to wire, and the emitter cannot do either on your behalf. `Notes on the bring-your-own-transport path` below states each one and what it costs to skip it; the README carries the wiring order as a recipe that the test suite executes from the file itself.

### Added

- **Bring-your-own-transport.** `Emitter(..., mqttc=client)` publishes the tree through a client the caller already owns, instead of having one built from `mqtt_cfg`. The two are mutually exclusive and passing both raises. This mirrors ebus-sdk's `Device(mqttc=...)` contract, and the case it serves is a host that cannot afford a second connection — a Home Assistant add-on, whose MQTT integration is `single_config_entry` and which forbids background threads (`ebus-mqtt-client` 0.4.0's `asyncio_driver()` covers pumping the loop). See the README's "Bring your own transport" section for the required wiring order, and the notes below for the behaviour that differs from the `mqtt_cfg` path.
- **`Emitter.lwt_settings(manifest)`** returns the Last Will to register on a client you intend to inject. A `staticmethod` because it has to be answerable before an `Emitter` exists: the will rides the MQTT CONNECT packet, so it must be on the client before the client connects, which is before that client can be handed to a constructor. The descriptor comes from the SDK's own `Device.will()` — the same function the SDK passes as `lwt=` when it builds a client itself — so a caller-registered will is identical to an SDK-registered one rather than merely similar, and the shape drops into `MqttClient(lwt=...)` unchanged. Without it an injected tree has no will at all, and an unclean death leaves consumers reading a stale retained `ready` indefinitely.
- **`Emitter.republish_tree()`** re-announces the whole retained tree, for wiring to an injected client's on-connect handler. The SDK registers this itself only for a client it built.

### Fixed

- **`stop(graceful=False)` left the root Device holding `ready` on an injected transport.** 0.3.3 moved the state before publishing but sourced that call through `owned_client()`, which returns None for a caller-supplied client, so the whole ungraceful teardown was a no-op there: object on `ready`, nothing on the wire. Latent in 0.3.3, because `mqttc=` did not exist to reach it; reachable the moment this release adds it. `set_state` now runs before the ownership split. This is the one entry here describing a defect in released code, and it could not be triggered by a 0.3.3 user.

### Notes on the bring-your-own-transport path

Behaviour that is specific to `mqttc=` and easy to get wrong. None of it is a change to the `mqtt_cfg` path, whose wire output is byte-identical to 0.3.3.

- **`start()` does not wait, and there is nothing to wait for.** The SDK never starts a client it did not build, so polling `is_connected()` would stall the very event loop such a client is likely driven on without changing the outcome. Values are retained; the tree goes out on the first `publish_tick`.
- **`stop()` never stops your client, on either path.** Ownership decides, not type — an injected client can itself be an `MqttClient`. It does still go mute: ebus-sdk's `Device.stop()` clears the root's transport reference regardless of ownership, so `republish_tree()` publishes nothing afterwards and an on-connect hook wired to it becomes a silent no-op on a still-live client. Build a new `Emitter` to resume.
- **Nothing re-announces your tree unless you wire it.** The SDK registers its on-(re)connect republish inside `connect_broker()`, below an `if self.mqttc` early return that an injected client always takes. Measured against a real broker with the retained store wiped: an injected tree came back 5 topics of 56, every `$description` missing, where an owned one came back all 56. `republish_tree()` is the remedy; the README recipe wires it.
- **Let your event loop turn before closing the client.** `stop(graceful=False)` queues the `lost` rather than flushing it — `wait_for_publish` would block the very thread that has to run `loop_write` for a client pumped by `asyncio_driver`. Closing the client in the same synchronous breath drops the message and leaves the retained tree on `ready`: deterministic, not a race. There is nothing to await; one turn of the loop is the whole remedy.

## [0.3.3] - 2026-08-07

### Fixed

- **`stop(graceful=False)` published `$state=lost` but left the root Device still holding `ready`.** The wire and the object disagreed, so anything re-announcing from that object republished `ready` straight over the `lost`. It now goes through the SDK's public `Device.set_state(DeviceState.LOST)` first, mirroring what `Device.stop()` already does for `disconnected`. The flushed publish that follows is deliberately the same retained value a second time: `set_state` uses the ordinary unflushed path, and on the owned path the connection closes immediately behind the call, so only a flushed publish is guaranteed to land. Costs one message at teardown. The observable retained outcome is unchanged (confirmed against a real broker: `p1=lost`); what changes is that it now survives a subsequent re-announce. Two tests pin it, both failing against 0.3.2. Found by [@cayossarian](https://github.com/cayossarian) while building on this code in [#17](https://github.com/electrification-bus/distribution-enclosure-simulator/pull/17).

## [0.3.2] - 2026-08-07

Metadata-only. No source changes, so the published tree and the public API are identical to 0.3.1; this release exists to get the packaging metadata below onto PyPI, where it only takes effect on a publish.

### Fixed

- **The PyPI project page was blank.** `pyproject.toml` declared no `readme`, so the published metadata carried no description at all: the page showed the summary line and nothing else. It now carries the README (`description_content_type: text/markdown`). The README's repo-relative links (`DESIGN.md`, `DEVELOPER.md`, `CONTRIBUTING.md`, `LICENSE`, `AUTHORS`) are absolutised, since those resolve on GitHub but 404 when the same Markdown is rendered on PyPI.
- **No licence was declared.** The README badge claimed MIT and a `LICENSE` file was present, but nothing said so in the metadata. Now `license = "MIT"` (an SPDX expression) with `license-files = ["LICENSE"]`, so the wheel carries `License-Expression: MIT` and `License-File: LICENSE`. No `License :: OSI Approved` classifier is paired with it, per PEP 639.
- **No classifiers.** PyPI indexed nothing about the package. Now covers 3.11 through 3.14, audience, topic, and `Typing :: Typed` — the last being how PyPI surfaces the `py.typed` marker shipped in 0.3.0, on the page and in the "Typed" search filter.
- The `hatchling` build requirement is floored at `>=1.27`, the version that supports PEP 639. Below it the build **succeeds** and quietly emits legacy metadata instead (measured on 1.26.3: `License: MIT`, no `License-Expression`, no `License-File`), so an unpinned backend would have degraded the published artifact without failing anything. ([#15](https://github.com/electrification-bus/distribution-enclosure-simulator/pull/15))

### Changed

- The README's Python badge is now the dynamic `pypi/pyversions` one used across the eBus family, replacing a hardcoded `3.11+`. It reads classifiers from the published release, so it could not be adopted until this release carried them.

## [0.3.1] - 2026-08-07

### Fixed

- **`Emitter.stop(graceful=False)` now leaves the tree at `$state=lost`, as it always claimed to.** It documented itself as "leaving the LWT to fire `$state=lost`", which cannot happen: a Last Will fires only on an *unclean* disconnect, and `MqttClient.stop()` sends a clean DISCONNECT deliberately, so that an orderly shutdown is not reported to consumers as a crash. The will was therefore suppressed on every teardown, and an ungraceful stop published nothing at all: verified against a real broker, a consumer joining afterwards read the entire retained tree as `ready`, indefinitely. For a simulator this is the mode's whole purpose, since "act like a producer that died" is exactly what a consumer test needs, so the fix makes the behaviour real rather than deleting the promise. The root's `$state=lost` is now published retained before the connection drops, with topic and payload taken from the SDK's own `Device.will()` descriptor so it cannot drift from what a broker-delivered will would have carried. The difference from a real will is timing and delivery, not content: this lands immediately over the live connection, where a broker-delivered will waits on keepalive expiry, so a consumer exercising the *retained* view sees the same thing either way while one exercising live will delivery does not. `clear_retained` is documented as graceful-only, since a producer that died clears nothing.

### Added

- `tests/test_teardown.py`: both teardown modes now have tests, which neither had before. That absence is why a docstring could promise behaviour the code had never performed. Three of the five fail against 0.3.0.

## [0.3.0] - 2026-08-07

First release published to PyPI, as **`ebus-panel-sim`**.

### Added

- **Published to PyPI.** Releases are tag-triggered and use PyPI trusted publishing (OIDC), so no API token is stored anywhere. The workflow re-runs the full gate set against the tag rather than trusting a green branch run, since a tag can point at any commit, and it refuses to publish when the tag disagrees with `__version__` or when the built wheel is missing its wire data. Consumers pinning by git URL can now `pip install ebus-panel-sim` instead. (#10)
- **`py.typed`.** The package is `mypy --strict` throughout but shipped no PEP 561 marker, so none of its annotations reached consumers: an installed `panel_sim` resolved to `Any`. This is the same class of gap that let the `Device.mqttc` teardown bug below sit unnoticed here until `ebus-sdk` shipped its own marker. Verified from outside: a consumer installing the wheel now types `Emitter.publish_tick` as `(TickInputs) -> EbusPanelSnapshot`. (#10)

### Changed

- **BREAKING (package): renamed to `ebus-panel-sim`, importing as `ebus_panel_sim`.** Was `panel-sim`/`panel_sim`. Update imports and any git-URL or path pin. Across the eBus family the repository name and the distribution name differ freely, but the distribution name always equals the import package under an `ebus` prefix (`ebus-sdk`/`ebus_sdk`, `ebus-tools`/`ebus_tools`, `ebus-service-discovery`/`ebus_service_discovery`), and this package was the outlier. The unprefixed name was also actively misleading: PyPI's `panel` is HoloViz's dashboard framework, with an established `panel-*` plugin ecosystem, so `panel-sim` read as a plugin for it. Done now because it is free before the first release and a breaking change for every consumer after it. (#8)
- `ebus-sdk` pin moved from `>=0.18,<0.19` to `>=0.19,<0.20`. The motivating fix is in **0.18.1**, which the old range already permitted but `uv.lock` had never picked up: `Device.refresh_tree()` published a device's own `$state` before recursing to its children, so a device could announce `ready` while the children it vouches for had published nothing ([python-sdk#31](https://github.com/electrification-bus/python-sdk/issues/31)). That matters here specifically because this package publishes a parent/child tree, which is the shape that exhibits it. **0.19.0** additionally makes the `refresh_tree()` cascade best-effort per child, so one raising descendant can no longer abort the rest of a reconnect republish; with an enclosure plus a device per circuit, lugs pair and DER, this tree has many descendants to abort. Its other changes do not reach us: `Controller.is_tree_complete()`/`on_tree_ready` is consumer-side, and the `Node.delete_property()` `$description` fix applies to an API this package never calls. No source changes. The emitted wire surface is unchanged between `0.18.0` and `0.19.0`: identical publish order, identical subscriptions, and identical retained payloads once the `$description` `version` wall-clock stamp is normalised (it differs run to run regardless of SDK version). Suite is `158 passed` under both.
- `ebus-sdk` pin moved from `>=0.12,<0.13` to `>=0.18,<0.19`. The old range excluded the release carrying the [python-sdk#27](https://github.com/electrification-bus/python-sdk/issues/27) fixes — the `battery` capability key removed in favour of `soc`, and `energy` → `energy_storage` / `total_increasing` → `measurement` for `soe`, `total-energy-storage` and `loadup-headroom` — so a downstream needing those could not stay inside the pin. No source changes: the published tree is byte-identical on `0.12.0` and `0.18.0` (197 retained topics, identical topic sets, zero payload differences once the `$description` `version` timestamp is normalised), and the suite is `158 passed` under both. `uv.lock` also moves `ebus-mqtt-client` 0.1.8 → 0.4.0, which `ebus-sdk` 0.18.0 requires. (#4, closes #3)
- The package version is now single-sourced from `ebus_panel_sim.__version__` and read by `[tool.hatch.version]`, rather than being restated in `pyproject.toml`. Note this is the *package* version, which is distinct from the producer-contract version the module docstrings refer to. (#10)
- `pre-commit` runs mypy from the project venv instead of a pre-commit-managed one. The isolated environment could never see `ebus-sdk` at all, so the hook silently checked less than CI did; restating the pin in `additional_dependencies` would have put a second copy of it somewhere nothing keeps in sync. (#7)

### Fixed

- **The package could not be built at all.** `packages` already carries everything under the package directory, so the `force-include` table naming the profiles/mapping/catalogs trees re-added each file at a path the wheel already held, which hatchling treats as fatal. Every `uv build` failed, on every commit this project has ever had. Nothing caught it because the test suite exercises the source tree rather than the built artifact, so `publish.yml` now asserts the wheel's contents directly. The sdist additionally excludes the agent and issue-tracker symlinks, which are untracked and absent from a clean checkout but break a local build on their absolute link targets. (#8)
- `Emitter.stop(graceful=False)` did not type-check, leaving `main` red from the 0.18 pin bump onward. `ebus-sdk` 0.18 ships a `py.typed` marker, so mypy stopped resolving the SDK to `Any` and started reading its real types, and `Device.mqttc` is typed `MqttDeviceTransport`, which deliberately omits `start`/`stop`: that omission *is* the SDK's no-start/no-stop guarantee, expressed as a type. `stop` resolves only on the concrete client the SDK builds and owns. Now narrowed at runtime in the SDK seam. Thanks to [@cayossarian](https://github.com/cayossarian), who found this independently and contributed the fix. (#7)

## [0.2.0] - 2026-08-01

### Fixed

- **BREAKING (wire):** circuit `meter/imported-energy` and `meter/exported-energy` are now published in the enclosure reference frame, matching the already-enclosure-framed `meter/active-power` and real panel firmware. Previously a load circuit published a rising `imported-energy` while its `active-power` was negative, so integrating the published power grew the opposite accumulator (every load looked like it produced energy). Consumers that compensated for the old inverted behaviour must drop the workaround; consumers written against real panel firmware need no change. Lugs metering is unchanged (the frames coincide there). (#2, fixes #1)

## [0.1.0] — 2026-05-02

### Added

- Initial scaffolding of the `ebus-panel-sim` package: wire layer (manifest/mapping/profiles,
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
