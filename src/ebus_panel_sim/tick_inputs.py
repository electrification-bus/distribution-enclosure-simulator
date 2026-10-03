"""``TickInputs`` — the v0.3.0 producer/emitter per-tick contract.

The producer's job each tick is reduced to: collect the signed power for each
known circuit (and EVSE) from the modelled world, fill in a small panel
envelope, and call ``Emitter.publish_tick(tick_inputs)``. The emitter does the
rest: BESS dispatch, load shedding, relay state resolution, energy integration,
per-leg currents, panel meter aggregation, and Homie-diff publication.

Sign convention for circuit / EVSE powers:
    power_w > 0  → consume (load)
    power_w < 0  → produce (PV / V2G)
    power_w == 0 → idle

The emitter does NOT consult ``power_w`` to discover what kind of device an
instance is; it learns that from the manifest's ``entity_class``. The sign
purely tells direction within that class."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

BESSCommunication: TypeAlias = Literal["OK", "DEGRADED", "LOST", "UNKNOWN"]
"""Health of the panel's link to a battery: the ``status`` catalog's
``communication-state`` values."""


@dataclass(slots=True)
class PanelEnvelopeTick:
    """Producer-supplied panel envelope facts that aren't derivable from
    circuit-level state. Most have sensible defaults; the producer overrides
    only what its model represents."""

    door_state: str = "CLOSED"
    proximity_proven: bool = True
    eth0_link: bool = True
    wlan_link: bool = True
    wwan_link: bool = False
    uptime_s: int = 0
    wifi_ssid: str | None = None
    cloud_connection: str = "CONNECTED"


@dataclass(slots=True)
class TickInputs:
    """Single-tick driving signal handed to ``Emitter.publish_tick``.

    Fields:
        current_time:   UNIX epoch seconds. Used by BESS for charge/discharge
                        window evaluation, by EnergyIntegrator for ``dt``, and
                        by per-property update timestamps. The producer is
                        responsible for picking a clock (real-time vs sim-time);
                        the emitter only requires monotonic-ish progression.
        grid_online:    Whether the utility grid is electrically connected. False
                        triggers BESS islanding behaviour, opens the main relay,
                        zeros published grid power, and may activate load
                        shedding.
        circuits:       Mapping of circuit ``instance_id`` → signed instant
                        power in watts. Every circuit in the manifest should
                        appear; missing entries are treated as 0 W.
        evse:           Mapping of EVSE ``instance_id`` → signed instant power
                        in watts. Status (``CHARGING``/``AVAILABLE``) is
                        derived from this signal in the emitter.
        envelope:       Panel envelope facts; defaults are sensible for most
                        producers.
        bess_communication:
                        Mapping of configured BESS ``instance_id`` → health of
                        the panel's link to that battery. A battery left out is
                        ``OK``. Published as the battery's
                        ``status/communication-state`` and on the connection
                        status of the circuit or lugs that connects it, and it
                        decides whether the panel accepts an
                        ``asserted-islanding-state`` write. An id that is not a
                        configured BESS, or a value outside
                        ``BESSCommunication``, raises ``EmitterStateError``."""

    current_time: float
    grid_online: bool
    circuits: dict[str, float]
    evse: dict[str, float] = field(default_factory=dict)
    envelope: PanelEnvelopeTick = field(default_factory=PanelEnvelopeTick)
    bess_communication: dict[str, BESSCommunication] = field(default_factory=dict)
