# ebus-panel-sim

[![PyPI](https://img.shields.io/pypi/v/ebus-panel-sim)](https://pypi.org/project/ebus-panel-sim/)
[![CI](https://github.com/electrification-bus/distribution-enclosure-simulator/actions/workflows/ci.yaml/badge.svg)](https://github.com/electrification-bus/distribution-enclosure-simulator/actions/workflows/ci.yaml)
[![Python versions](https://img.shields.io/pypi/pyversions/ebus-panel-sim.svg)](https://pypi.org/project/ebus-panel-sim/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![eBus spec](https://img.shields.io/badge/eBus%20spec-6e582c9-green)](https://github.com/electrification-bus/specification/tree/6e582c994fff4c77853a79d8bab26ef9924e22c7)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A fully-loaded, spec-conformant **distribution-enclosure simulator** and producer-side Homie 5 publisher for the eBus convention. It publishes a complete eBus Homie device tree (the enclosure plus a device for every circuit, lugs pair, and integrated DER: BESS, PV, EVSE, and MID) so external developers can build and test their consumers against a realistic SPAN-like panel without beta firmware, a live panel, or the commissioned add-ons (SPAN Drive/EVSE, BESS, PV, MID) a real installation would have.

It serves two roles:

- **Simulator / test fixture.** Drive it from a small YAML definition and it publishes a spec-conformant, fully-commissioned enclosure to any MQTT broker. Consumers (Home Assistant integrations, dashboards, SDK code) validate against it before shipping to the field.
- **Producer library.** The canonical eBus Homie publisher. A producer (a simulator, a real panel gateway, an LLM-driven model) hands the emitter a small per-tick driving signal (signed power per circuit, current time, grid-online flag) via `TickInputs`; the emitter derives all telemetry and publishes Homie-conformant retained MQTT with diff-only updates. The split is **identity = manifest (once at startup), telemetry = derived from TickInputs (per tick)**.

For the internals (the per-tick pipeline, the native BESS/load-shed devices, `/set` handling, the wire model) see [DESIGN.md](https://github.com/electrification-bus/distribution-enclosure-simulator/blob/main/DESIGN.md); for the dev setup see [DEVELOPER.md](https://github.com/electrification-bus/distribution-enclosure-simulator/blob/main/DEVELOPER.md).

## Requirements

- Python >= 3.11
- [uv](https://docs.astral.sh/uv/)
- An MQTT broker reachable at `localhost:1883` (plaintext). The companion [broker-quickstart](https://github.com/electrification-bus/broker-quickstart) bundle brings one up in one command; any `mosquitto` works too.

## Install

```bash
pip install ebus-panel-sim    # or: uv add ebus-panel-sim
```

The import package is `ebus_panel_sim`. During local development, pin a path instead:

```toml
ebus-panel-sim = { path = "../distribution-enclosure-simulator", editable = true }
```

It depends on `ebus-sdk`.

> Before 0.3.0 this package was named `panel-sim`, importing as `panel_sim`, and was
> installable only from git. Update both the dependency and your imports.

## Run

The repo ships a runnable example: it builds an emitter from a YAML definition, publishes a couple of ticks to an MQTT broker, then reads the retained tree back through an ebus-sdk `Controller` and prints it. It expects a plaintext broker on `localhost:1883`.

The quickest broker is the companion [broker-quickstart](https://github.com/electrification-bus/broker-quickstart) in its `open` profile (plaintext, anonymous, port 1883):

```bash
# in a broker-quickstart checkout — a plaintext :1883 broker (anon read + write)
python -m laptop.run --profile open
```

Then, in this repo, publish to it and print the retained tree:

```bash
uv sync --group dev
uv run python examples/run_forty_tab_minimal.py                        # print the retained tree
uv run python examples/run_forty_tab_minimal.py --broker 127.0.0.1:1883 --ticks 2 > tree.txt
```

Any broker that accepts anonymous connections on `localhost:1883` works; `--broker host:port` points the example elsewhere.

The definition is `examples/forty_tab_minimal.yaml`: a fully-commissioned enclosure with circuits, upstream/downstream lugs, a BESS (plus its MID), PV, and SPAN Drive EVSEs. Each node is its own Homie device: the enclosure at `ebus/5/<enclosure-id>/…` and each circuit, lugs pair, and DER at its own topic root, for example `ebus/5/<circuit-id>/switch/relay`, `ebus/5/<lugs-id>/meter/current-a`, `ebus/5/<bess-id>-mid/grid/islanding-state`.

## Configure

The simulator is driven by a config that says which enclosure, which add-ons, and which circuits. There are two entry points.

### 1. Example YAML

`examples/forty_tab_minimal.yaml` is the quickest path. Top-level sections:

- `panel_config` — enclosure identity plus `total_tabs`, `main_size`, `postal_code`, `time_zone`, and `islandable`. A grid-forming BESS in an islandable enclosure automatically exposes an integrated MID (the islanding authority), mirroring a real SPAN panel.
- `circuit_templates` and `circuits` — per-circuit `tabs`, breaker rating, priority, relay behavior, and an optional `device_type` (`evse` or `pv`) to land a DER on a circuit.
- `bess` — nameplate capacity, charge mode, charge/discharge limits.
- `ticks` — the per-tick driving signal: signed watts per circuit and the grid-online flag.

### 2. DeviceManifest (programmatic)

A producer can build `DeviceInstance`s directly instead of using the YAML loader. Each device class's identity and static attributes live in the instance's `metadata`, validated once at startup by `ManifestPhysicsView` (missing required keys or malformed values raise `ManifestValidationError` naming the offending instance). The metadata keys per device class:

| entity_class | required keys | optional keys |
| --- | --- | --- |
| `panel` | `vendor-name`, `serial-number`, `firmware-version` (or `software-version`), `hardware-version`, `panel-size`, `main-breaker-rating-a`, `panel-model`, `postal-code`, `time-zone` | `service-voltage-v` (240), `line-voltage-v` (120), `islandable` (false), `schema-topology` (`flat` \| `parent-child`) |
| `lugs` | `direction` (`upstream` \| `downstream`) | |
| `circuit` | `tab-numbers` (CSV ints), `breaker-rating-a`, `default-priority`, `relay-behavior` (`controllable` \| `non-controllable` \| `always-on`), `placement` (`upstream-of-lugs` \| `downstream-of-lugs`) | `always-on`, `never-backup` (false), `dipole` (defaults to `len(tab-numbers) > 1`), `pcs-priority` (0), `initial-consumed-wh` (0), `initial-produced-wh` (0) |
| `bess` | `vendor-name`, `nameplate-capacity-kwh` | `model`, `part-number`, `serial-number`, `firmware-version`/`software-version`, `relative-position` (`UPSTREAM`), `feed`, `initial-soe-kwh` |
| `pv` | `vendor-name`, `nominal-power-w`, `inverter-type` (`hybrid` \| `ac-coupled`) | `model`, `serial-number`, `firmware-version`/`software-version`, `relative-position` (`IN_PANEL`), `feed` |
| `evse` | `vendor-name`, `model`, `part-number`, `serial-number`, `firmware-version` (or `software-version`), `max-current-a` | `feed` |
| `mid` | (none) | `vendor-name`, `serial-number`, `model`, `firmware-version`/`software-version`, `hardware-version` |

A circuit whose `relay-behavior` is `non-controllable` or `always-on` — or which carries
`always-on: true` — is **locked**: its relay refuses `/set`, is exempt from load-shed, reports
`switch/relay-requester` as `CONFIGURATION`, and declares no `$settable` on `switch/relay`.
The two spellings differ only in the operator's intent; the hardware this models carries one
flag, publishing `switch/relay-controllable` as its inverse. Locking reaches the relay only —
a locked circuit still declares `load-shed/priority` settable.

A circuit carrying `never-backup: true` is commissioned to receive no backup power: it is
permanently `OFF_GRID`, so its `default-priority` must be `OFF_GRID` (any other value is
rejected as contradictory), it declares no `$settable` on `load-shed/priority`, and a `/set`
on that priority is refused. It still sheds when the panel islands — that is what being
`OFF_GRID` means; what the lock removes is the consumer's ability to re-prioritise it.
This is a **separate commissioning input from the priority value**: `default-priority: NEVER`
means "never shed" and stays fully settable, which is what real panels publish.

## Usage (as a producer library)

```python
import time

from ebus_panel_sim import (
    BESSConfig, DeviceInstance, DeviceManifest, Emitter,
    LoadSheddingConfig, SetterRegistry, TickInputs,
)


def main() -> None:
    manifest = DeviceManifest(instances=(
        DeviceInstance("panel", "abc-123", "Span Panel", metadata={
            "vendor-name": "Span", "serial-number": "abc-123",
            "firmware-version": "sim/v0.1.0", "hardware-version": "rev2",
            "panel-size": "40", "main-breaker-rating-a": "200",
            "panel-model": "MAIN_40", "postal-code": "94103",
            "time-zone": "America/Los_Angeles", "islandable": "true",
        }),
        DeviceInstance("lugs", "abc-123-lugs-up", "Upstream lugs", {"direction": "upstream"}),
        DeviceInstance("circuit", "kitchen", "Kitchen", metadata={
            "tab-numbers": "1", "breaker-rating-a": "20",
            "default-priority": "NICE_TO_HAVE", "relay-behavior": "controllable",
            "placement": "downstream-of-lugs",
        }),
        DeviceInstance("bess", "abc-123-bess", "Battery", metadata={
            "vendor-name": "Span", "nameplate-capacity-kwh": "13.5",
        }),
    ))
    bess_cfg = BESSConfig(instance_id="abc-123-bess", nameplate_capacity_kwh=13.5,
                          max_charge_w=3500.0, max_discharge_w=3500.0)

    # With mqtt_cfg the emitter owns the MQTT connection: ebus-sdk builds the
    # client and sets the enclosure's LWT. (Injecting your own client instead
    # moves both of those to you — see "Bring your own transport" below.)
    # Empty SetterRegistry -> the emitter installs internal default /set
    # handlers; register your own before construction to override them.
    emitter = Emitter(
        manifest, SetterRegistry(),
        mqtt_cfg={"host": "127.0.0.1", "port": 1883},
        bess_configs=(bess_cfg,),
        load_shedding_config=LoadSheddingConfig(soc_threshold_pct=20.0),
    )
    emitter.start()
    try:
        while True:
            emitter.publish_tick(TickInputs(
                current_time=time.time(),
                grid_online=True,
                circuits=collect_powers_from_your_model(),  # instance_id -> signed watts
            ))
            time.sleep(1.0)
    finally:
        emitter.stop()


main()
```

Read the most recently published state back through `emitter.last_snapshot`. `mqtt_cfg` is handed straight to ebus-sdk: beyond `host`/`port` it takes the ebus-mqtt-client TLS and authentication keys for secured brokers (e.g. broker-quickstart's mTLS `discovery`/`strict` profiles).

### Bring your own transport

A host that already owns an MQTT connection can publish through it instead of having a second one opened underneath: pass `Emitter(..., mqttc=client)` in place of `mqtt_cfg=`. The two are mutually exclusive. This mirrors ebus-sdk's own `Device(mqttc=...)`, and the case it exists for is a host like a Home Assistant add-on, whose MQTT integration is `single_config_entry` and which forbids background threads (`ebus-mqtt-client` 0.4.0's `asyncio_driver()` pumps paho's loop on yours).

**The emitter never starts or stops a client it did not build.** Two things it consequently cannot do for you — register the Last Will, and re-announce the tree on reconnect — are automatic on the `mqtt_cfg` path and yours here. They are steps 1 and 4 below, and the order is forced rather than stylistic:

```python
from ebus_panel_sim import Emitter, SetterRegistry
from ebus_mqtt_client import MqttClient

# 1. The Last Will must exist before the client connects — it rides the CONNECT
#    packet, so it cannot be attached afterwards. This is why it is a
#    staticmethod: there is no Emitter yet, and cannot be.
lwt = Emitter.lwt_settings(manifest)

# 2. Build your client with it, still unconnected.
client = MqttClient.from_config({"host": "127.0.0.1", "port": 1883}, client_id="my-host", lwt=lwt)

# 3. Now the emitter, publishing through it.
emitter = Emitter(manifest, SetterRegistry(), mqttc=client)

# 4. Re-announce the whole tree on every (re)connect. Assigned after construction
#    rather than passed to from_config, because the callback needs the emitter and
#    the emitter needs the client. Invoked with no arguments.
client.on_connect_callback = emitter.republish_tree

# 5. You connect, not the emitter — it never starts a client it did not build.
client.start()
emitter.start()           # returns immediately; it has no connection to wait for
```

To pump paho on your own event loop instead of its background thread — the case a
Home Assistant add-on needs — replace step 5's `client.start()` with the driver,
which is `async` and mutually exclusive with `start()`:

```python
driver = client.asyncio_driver()   # must be called from inside a running loop
await driver.start()
emitter.start()
```

What each buys, and one obligation that is about timing rather than wiring. All three are silent when they bite:

- **No will means no liveness signal.** Skip step 1 and the tree has no LWT at all: a host that dies leaves every consumer reading a stale retained `ready`, indefinitely. `stop(graceful=False)` publishes `$state=lost` itself, but that only covers an orderly teardown — the case where the process *didn't* die.
- **No re-announce means the tree does not come back.** Skip step 4 and a broker that loses its retained store never sees the tree again; what returns is whatever later ticks happen to republish. Measured after wiping a real broker's retained store: 5 topics of 56, every `$description` missing.
- **Let your loop turn before you close the client.** `stop(graceful=False)` *queues* the `lost` on your loop rather than flushing it — flushing would block the very thread that has to run `loop_write`. Closing the client in the same synchronous breath drops the message and leaves the retained tree on `ready`.

## Layout

- `src/ebus_panel_sim/` — the package (`emitter.py`, `manifest.py`, `wire/` profiles + publishing, `native_devices/`); see [DESIGN.md](https://github.com/electrification-bus/distribution-enclosure-simulator/blob/main/DESIGN.md).
- `examples/` — the runnable example and its YAML definition.
- `tests/` — the pytest suite.

## Tests

```bash
uv run pytest
uv run mypy --strict src/ebus_panel_sim tests
uv run ruff check src tests
```

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](https://github.com/electrification-bus/distribution-enclosure-simulator/blob/main/CONTRIBUTING.md) for how to file issues, start a [discussion](https://github.com/electrification-bus/distribution-enclosure-simulator/discussions), and open pull requests, plus the local quality gates (ruff, mypy `--strict`, pytest).

## Credits

A fork of, and building on, the original simulator created by **Bill Flood** ([@cayossarian](https://github.com/cayossarian)); since updated to track the latest eBus specification. See [AUTHORS](https://github.com/electrification-bus/distribution-enclosure-simulator/blob/main/AUTHORS).

## License

See [LICENSE](https://github.com/electrification-bus/distribution-enclosure-simulator/blob/main/LICENSE).
