"""ebus-panel-sim — producer-side Homie wire publisher with native-device runtime.

Architecture (v0.3.0):

- **Wire layer** (``wire/``): vendored Homie 5 device profiles + mapping descriptors,
  graph builder, diff-only publisher, /set setter fan-in, and the property/bag diff
  cache over a thin ebus-sdk seam (ebus-sdk owns $description/$state/LWT/encoding).
- **Native devices** (``native_devices/``): emitter-resident, configured-and-self-driving
  device runtimes (BESS dispatch, load shedding).
- **Manifest physics** (``manifest_physics.py``): typed accessor over
  ``DeviceInstance.metadata`` for physics-relevant fields (voltage, breaker rating,
  tabs/legs, placement, default priority, relay behaviour).
- **Tick pipeline** (``relay_resolver.py`` + ``energy_integrator.py`` + ``panel_meter.py``
  + ``conventions/tab_legs.py``): per-tick state machinery the emitter uses to
  resolve circuit relay state, integrate energy, derive per-leg currents, and
  aggregate panel-level fields.

Producer contract (v0.3.0): build a ``DeviceManifest`` once at startup, then call
``Emitter.publish_tick(TickInputs)`` each tick with signed circuit/EVSE powers,
``current_time``, and ``grid_online``. The emitter does the rest."""

from ebus_panel_sim.conventions.tab_legs import Leg, legs_for_tabs
from ebus_panel_sim.emitter import Emitter
from ebus_panel_sim.exceptions import (
    EmitterError,
    EmitterStateError,
    ManifestValidationError,
    MissingSetterError,
    ProfileValidationError,
    RuntimeSpecValidationError,
)
from ebus_panel_sim.manifest import DeviceInstance, DeviceManifest
from ebus_panel_sim.manifest_physics import (
    BessPhysics,
    CircuitPhysics,
    EvsePhysics,
    LugsPhysics,
    ManifestPhysicsView,
    PanelPhysics,
    PvPhysics,
)
from ebus_panel_sim.native_devices import (
    BESSConfig,
    BESSDevice,
    LoadSheddingConfig,
    LoadSheddingDevice,
    NativeDevice,
    NativeTickContext,
)
from ebus_panel_sim.relay_resolver import RelayRequester, RelayResolver, RelayState
from ebus_panel_sim.snapshot import (
    EbusBatterySnapshot,
    EbusCircuitSnapshot,
    EbusEvseSnapshot,
    EbusLugsSnapshot,
    EbusPanelDoor,
    EbusPanelInfo,
    EbusPanelMeter,
    EbusPanelPcs,
    EbusPanelPowerFlows,
    EbusPanelSnapshot,
    EbusPanelStatus,
    EbusPvSnapshot,
)
from ebus_panel_sim.tick_inputs import PanelEnvelopeTick, TickInputs
from ebus_panel_sim.wire.set_router import SetterHandler, SetterRegistry

# Single source of truth for the distribution version: pyproject reads it from here
# via `[tool.hatch.version]`, and publish.yml refuses to release when the git tag
# disagrees. Bump it in this one place. Note this is the PACKAGE version and is
# distinct from the producer-contract version the docstrings above refer to.
__version__ = "0.2.0"

__all__ = [
    "BESSConfig",
    "BESSDevice",
    "BessPhysics",
    "CircuitPhysics",
    "DeviceInstance",
    "DeviceManifest",
    "EbusBatterySnapshot",
    "EbusCircuitSnapshot",
    "EbusEvseSnapshot",
    "EbusLugsSnapshot",
    "EbusPanelDoor",
    "EbusPanelInfo",
    "EbusPanelMeter",
    "EbusPanelPcs",
    "EbusPanelPowerFlows",
    "EbusPanelSnapshot",
    "EbusPanelStatus",
    "EbusPvSnapshot",
    "Emitter",
    "EmitterError",
    "EmitterStateError",
    "EvsePhysics",
    "Leg",
    "LoadSheddingConfig",
    "LoadSheddingDevice",
    "LugsPhysics",
    "ManifestPhysicsView",
    "ManifestValidationError",
    "MissingSetterError",
    "NativeDevice",
    "NativeTickContext",
    "PanelEnvelopeTick",
    "PanelPhysics",
    "ProfileValidationError",
    "PvPhysics",
    "RelayRequester",
    "RelayResolver",
    "RelayState",
    "RuntimeSpecValidationError",
    "SetterHandler",
    "SetterRegistry",
    "TickInputs",
    "__version__",
    "legs_for_tabs",
]
