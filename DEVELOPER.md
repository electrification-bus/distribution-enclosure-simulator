# Developer Guide

## Prerequisites

- Python >= 3.11 (`pyproject.toml` sets `requires-python = ">=3.11"`); developed and CI-tested on 3.11 and 3.14
- [uv](https://docs.astral.sh/uv/) (`brew install uv` on macOS)

## Setup

```bash
# Clone and enter the repo
git clone https://github.com/electrification-bus/distribution-enclosure-simulator.git
cd distribution-enclosure-simulator

# Create venv and install all dependencies (runtime + dev)
# uv reads pyproject.toml and uv.lock, creates .venv/ automatically
uv sync --group dev

# Install pre-commit hooks
uv run pre-commit install
```

That's it. The `.venv/` directory is created in the project root and `uv.lock` pins exact versions for reproducible installs.

## Common Commands

```bash
# Run the standalone example (a producer driving the emitter over a broker)
uv run python examples/run_forty_tab_minimal.py

# Run tests
uv run pytest

# Lint + format
uv run ruff check --fix src tests
uv run ruff format src tests

# Type check (strict; matches the pre-commit hook)
uv run mypy --strict src/ebus_panel_sim tests

# Add a new dependency
uv add <package>          # runtime
uv add --group dev <pkg>  # dev only
```

To check that a change is wire-neutral, compare the retained tree before and after it, but drop `$description.version` first: it is a wall-clock stamp, so two runs never match byte for byte otherwise. `tests/test_definition.py`'s `_stable` does this.

This package is a producer library. Its one console entry point, `panel-sim-capture` (`[project.scripts]` in `pyproject.toml`), writes a panel definition from a published panel tree. The `examples/` directory is the runnable demonstration of correct output.

## Pre-commit Hooks

Every commit is validated by:

| Hook | What it checks |
|---|---|
| **ruff** | Lint rules (E, F, W, I, UP, B, SIM, TCH, RUF) with auto-fix |
| **ruff-format** | Consistent formatting |
| **mypy --strict** | Full strict type checking across `src/ebus_panel_sim` and `tests` |
| **trailing-whitespace** | No trailing whitespace |
| **end-of-file-fixer** | Files end with a newline |
| **check-yaml** | Valid YAML syntax |
| **check-added-large-files** | Prevents accidental large file commits |

## Emitter Internals

The emitter is the **publisher** half of the eBus producer/emitter split: the producer (a simulator, a real gateway, or a modelling agent) computes the generation-side physics (solar curves, HVAC modulation, weather, battery schedules) and hands the emitter a per-tick driving signal via `TickInputs`. The emitter derives wire-facing telemetry from that signal and publishes it. It does **not** run the generation models itself.

### Per-tick flow

Each tick, given `TickInputs` (signed power per circuit, current time, grid-online flag, battery link health):

1. Resolve per-circuit relay state, applying strict precedence across command sources (`relay_resolver.py`).
2. Run native-device behaviours that own their own state (`native_devices/`): BESS dispatch (`bess.py`) and load shedding (`load_shedding.py`).
3. Derive gated per-circuit power, then aggregate panel-level meter values (`panel_meter.py`, a pure function).
4. Integrate energy in watt-hours per instance (`energy_integrator.py`).
5. Translate the resulting snapshot into a `PropertyBag` and diff-publish (`wire/bag_builder.py`, `wire/property_bag.py`, `wire/publisher.py`).

Device identity and static attributes come from the producer once at startup via the `DeviceManifest` (`manifest.py`); dynamic telemetry is derived here. The split is **identity = manifest, telemetry = derived from TickInputs**.

### Energy Accumulation

Energy integrates over time in watt-hours:

```
delta_energy = power_watts * delta_seconds / 3600
```

Consumed and produced energy are tracked separately per circuit, seeded from producer-supplied starting values.

### Diffing

Only changed property values are republished each tick (`wire/property_bag.py` holds the diff cache; `wire/publisher.py` owns the loop). Unchanged values are not retransmitted.

## Directory Layout

```
distribution-enclosure-simulator/
  pyproject.toml               # Package metadata, deps, ruff/mypy/pytest config
  uv.lock                      # Pinned dependency versions
  README.md                    # Overview and usage
  DEVELOPER.md                 # This guide
  CHANGELOG.md
  LICENSE
  AGENTS.md                    # Agent rules (no AI attribution in commits)
  .pre-commit-config.yaml
  .python-version
  .github/workflows/
    ci.yaml                    # CI: ruff, ruff-format, mypy --strict, pytest
  examples/
    forty_tab_minimal.yaml     # Example panel definition file
    forty_tab_minimal.ticks.yaml  # Ticks that drive it
    run_forty_tab_minimal.py   # Minimal standalone producer + emitter demo
  src/ebus_panel_sim/
    __init__.py                # Public surface
    emitter.py                 # Public Emitter facade (wire publisher + native runtime)
    tick_inputs.py             # TickInputs: the producer/emitter per-tick contract
    manifest.py                # Producer-supplied device identity manifest
    manifest_physics.py        # Typed accessor over device metadata (physics fields)
    relay_resolver.py          # Per-circuit relay state with strict command precedence
    energy_integrator.py       # Per-instance watt-hour energy accumulator
    panel_meter.py             # Panel-level aggregator (pure function)
    snapshot.py                # Per-tick snapshot dataclasses (internal model)
    exceptions.py              # Public exception hierarchy
    conventions/
      tab_legs.py              # Tab-to-leg convention for split-phase panels
    native_devices/            # Emitter-native device behaviours
      bess.py                  # Native BESS (configured, self-driving)
      load_shedding.py         # Native load-shedding controller
      protocol.py              # Native-device tick contract
    wire/                      # Homie 5 wire production over ebus-sdk
      graph_builder.py         # Manifest + mappings + profiles -> SDK Device graph
      profile_loader.py        # Load vendored Homie 5 profile JSONs
      mapping_loader.py        # Load vendored mapping descriptor YAMLs
      bag_builder.py           # Snapshot -> PropertyBag translator
      property_bag.py          # Per-tick property values + diff cache
      publisher.py             # Per-tick diff/publish loop
      set_router.py            # Setter registry and /set dispatch
      _sdk_seam.py             # Internal seam over ebus_sdk (property build, owned-client narrowing, will descriptor)
      profiles/                # Vendored Homie 5 device profiles (JSON), per device type
      mapping/                 # Vendored mapping descriptors (YAML), per device type
      catalogs/                # Vendored spec capability catalogs (JSON); the wire type contract
  tests/                       # pytest suite; paho is patched, so no broker and no socket
    conftest.py
    conventions/               # convention tests
    wire/                      # wire-layer tests
    test_*.py                  # unit tests (manifest, energy, relay, panel meter, ...)
```
