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
    MidPhysics,
    PanelPhysics,
    PvPhysics,
)
from ebus_panel_sim.native_devices import (
    BESSConfig,
    BESSDevice,
    ChargeMode,
    DispatchState,
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
)
from ebus_panel_sim.tick_inputs import PanelEnvelopeTick, TickInputs

# `Emitter(mqttc=...)` is public API typed with this, so the name has to be
# nameable from here. Without it a downstream annotating what it passes must
# import from `ebus_sdk` directly, which is exactly the coupling the wire seam
# exists to spare it.
from ebus_panel_sim.wire._sdk_seam import MqttDeviceTransport
from ebus_panel_sim.wire.profile_loader import Variant
from ebus_panel_sim.wire.set_router import SetterHandler, SetterRegistry

# Single source of truth for the distribution version: pyproject reads it from here
# via `[tool.hatch.version]`, and publish.yml refuses to release when the git tag
# disagrees. Bump it in this one place. Note this is the PACKAGE version and is
# distinct from the producer-contract version the docstrings above refer to.
__version__ = "0.6.1"

__all__ = [
    "BESSConfig",
    "BESSDevice",
    "BessPhysics",
    "ChargeMode",
    "CircuitPhysics",
    "DeviceInstance",
    "DeviceManifest",
    "DispatchState",
    "EbusBatterySnapshot",
    "EbusCircuitSnapshot",
    "EbusEvseSnapshot",
    "EbusLugsSnapshot",
    "EbusMidSnapshot",
    "EbusPanelDoor",
    "EbusPanelInfo",
    "EbusPanelMeter",
    "EbusPanelPcs",
    "EbusPanelPowerFlows",
    "EbusPanelShed",
    "EbusPanelShedForecast",
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
    "MidPhysics",
    "MissingSetterError",
    "MqttDeviceTransport",
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
    "Variant",
    "__version__",
    "legs_for_tabs",
]
