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
                        ``BESSCommunication``, raises ``EmitterStateError``.
        bess_pv:        Mapping of a configured BESS ``instance_id`` whose
                        manifest gives ``inverter-type: hybrid`` → the power its
                        inverter's PV input produces, in watts: positive while
                        producing (the PV's own frame), negative for a standby
                        draw. Solar DC-coupled into a battery's inverter reaches
                        the panel through the battery, never through a circuit,
                        so no circuit's power carries it. It is published in
                        ``power-flows/pv``, offered to the battery's dispatch as
                        solar, and leaves the battery's AC terminals with its
                        dispatch, which is how the grid sees it. Off grid the
                        battery only discharges and the emitter curtails
                        nothing, so there the four flows balance only while the
                        solar is no more than the load and the battery covers
                        the rest; curtailing any surplus is the producer's, as
                        generating it is. A battery left out produces 0 W. An
                        id that is not such a BESS, or a value that is not a
                        finite number, raises ``EmitterStateError``."""

    current_time: float
    grid_online: bool
    circuits: dict[str, float]
    evse: dict[str, float] = field(default_factory=dict)
    envelope: PanelEnvelopeTick = field(default_factory=PanelEnvelopeTick)
    bess_communication: dict[str, BESSCommunication] = field(default_factory=dict)
    bess_pv: dict[str, float] = field(default_factory=dict)
