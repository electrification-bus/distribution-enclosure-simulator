"""`never-backup` is a commissioning lock, not a priority value.

Two independent things share the word "never" and were collapsed into one:

* `load-shed/priority = NEVER` is an ordinary *value* of a settable property --
  "never shed this circuit". A consumer may set it, and set it away again.
* `never-backup` is an installer *commissioning input*: the circuit is
  permanently `OFF_GRID` and its priority is not settable at all.

The schema migration guide keeps them apart per-input -- "the three flat
booleans are independent commissioning inputs stored as separate fields in each
circuit's commissioning state, so the derivation is a simple per-input rule" --
and maps only the boolean onto the wire: `never-backup` is "published with
`$settable = !never-backup`", and "locked-priority circuits (commissioned
permanently `OFF_GRID`) appear as `priority = OFF_GRID, $settable = false`;
user-configurable circuits appear as `$settable = true`".

Deriving the flag from the value is therefore inverted twice over: it reports
never-backup for circuits that are merely un-shed, and reports none for the
circuits that actually carry the lock. A masked retained-topic capture of a
production enclosure (16 circuits) refutes the derivation outright: two circuits
publish `load-shed/priority = NEVER` and both publish `$settable = true` on it,
which the derivation makes impossible. Across all 16 circuits `$settable` is
present on `load-shed/priority` without exception.

These tests pin the four surfaces of the one bit -- the `$settable`
declaration, the `/set` path it removes, the published priority value, and the
snapshot flag -- to the commissioning input, and pin the priority *value* to
having no say in any of them.

The reproduction itself lives next door in `test_never_backup_reproduction.py`,
written against the pre-fix API alone so it can be lifted onto an unfixed
checkout and fail on its assertion rather than at import. This module is free to
use the new commissioning key, and does.
"""

from __future__ import annotations

import pytest

from ebus_panel_sim import (
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    Emitter,
    LoadSheddingConfig,
    ManifestValidationError,
    SetterRegistry,
    TickInputs,
)
from ebus_panel_sim.manifest_physics import ManifestPhysicsView
from ebus_panel_sim.wire.graph_builder import BuiltGraph, build_graph
from ebus_panel_sim.wire.mapping_loader import load_mapping_table
from ebus_panel_sim.wire.profile_loader import load_profiles

from .conftest import PahoRecorder


def _panel() -> DeviceInstance:
    return DeviceInstance(
        "panel",
        "p1",
        "Span Panel",
        metadata={
            "vendor-name": "Span",
            "serial-number": "p1",
            "firmware-version": "sim/v0.1.0",
            "hardware-version": "rev2",
            "panel-size": "40",
            "main-breaker-rating-a": "200",
            "panel-model": "MAIN_40",
            "postal-code": "94103",
            "time-zone": "America/Los_Angeles",
        },
    )


def _circuit(
    cid: str,
    *,
    tabs: str = "1",
    priority: str = "NICE_TO_HAVE",
    relay_behavior: str = "controllable",
    never_backup: str | None = None,
) -> DeviceInstance:
    """A circuit instance. ``never_backup=None`` omits the key, which is what
    every manifest written before the key existed does; absent means not
    commissioned as never-backup."""
    metadata = {
        "tab-numbers": tabs,
        "breaker-rating-a": "20",
        "default-priority": priority,
        "relay-behavior": relay_behavior,
        "placement": "upstream-of-lugs",
    }
    if never_backup is not None:
        metadata["never-backup"] = never_backup
    return DeviceInstance("circuit", cid, cid.title(), metadata=metadata)


def _locked(cid: str, *, tabs: str = "1") -> DeviceInstance:
    """A circuit commissioned never-backup: permanently ``OFF_GRID``."""
    return _circuit(cid, tabs=tabs, priority="OFF_GRID", never_backup="true")


def _graph(*circuits: DeviceInstance) -> BuiltGraph:
    manifest = DeviceManifest(instances=(_panel(), *circuits))
    return build_graph(manifest, load_mapping_table(), load_profiles(), mqtt_cfg={})


def _property_declaration(graph: BuiltGraph, cid: str, node: str, prop: str) -> dict[str, object]:
    """One published property declaration, narrowed to a mapping.

    ebus-sdk annotates ``description()`` as a bare ``dict``, so every index into
    it is ``Any`` and a strict build cannot see the shape."""
    declaration = graph.devices[cid].description()["nodes"][node]["properties"][prop]
    assert isinstance(declaration, dict)
    return declaration


def _priority_declaration(graph: BuiltGraph, cid: str) -> dict[str, object]:
    return _property_declaration(graph, cid, "load-shed", "priority")


def _emitter(*circuits: DeviceInstance) -> Emitter:
    """No producer handlers, so the emitter's own internal ones are exercised."""
    return Emitter(DeviceManifest(instances=(_panel(), *circuits)), SetterRegistry())


# --- The priority value has no say ----------------------------------------


def test_a_never_priority_circuit_is_not_never_backup() -> None:
    """The defect, in one assertion. `NEVER` is "never shed", a value the
    consumer chose and may unchoose; the commissioning lock is a separate
    input and this circuit does not carry it."""
    em = _emitter(_circuit("kitchen", priority="NEVER"))
    em.start()
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0})
    )
    assert snap.circuits["kitchen"].is_never_backup is False


def test_a_never_priority_circuit_keeps_its_priority_settable() -> None:
    """Both `NEVER` circuits on the captured enclosure publish `$settable =
    true` on `load-shed/priority`, which the value-derived flag makes
    impossible: a consumer hiding the control on never-backup circuits loses
    every "stays on in an outage" circuit."""
    assert (
        _priority_declaration(_graph(_circuit("kitchen", priority="NEVER")), "kitchen")["settable"]
        is True
    )


def test_a_never_priority_circuit_accepts_a_priority_set() -> None:
    """The value is not a lock: what `NEVER` was set to, `NEVER` can be set
    away from."""
    setters = SetterRegistry()
    em = Emitter(
        DeviceManifest(instances=(_panel(), _circuit("kitchen", priority="NEVER"))), setters
    )
    em.start()
    handler = setters.get("circuit", "load-shed/priority")
    assert handler is not None
    handler("circuit", "kitchen", "load-shed/priority", "SOC_THRESHOLD")

    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0})
    )
    assert snap.circuits["kitchen"].priority == "SOC_THRESHOLD"


# --- The commissioning flag is the lock ------------------------------------


def test_a_never_backup_circuit_is_never_backup() -> None:
    em = _emitter(_locked("well_pump"))
    em.start()
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"well_pump": 500.0})
    )
    assert snap.circuits["well_pump"].is_never_backup is True


def test_a_never_backup_circuit_declares_no_settable_on_its_priority() -> None:
    """The attribute is absent, not `false` -- as on a locked `switch/relay`
    and on the read-only `shed/policy`. Homie 5 defaults it false, so the
    omission and an explicit `false` are the same claim, and the omission is
    what firmware publishes."""
    assert "settable" not in _priority_declaration(_graph(_locked("well_pump")), "well_pump")


def test_a_never_backup_circuit_publishes_off_grid(rec: PahoRecorder) -> None:
    """ "Locked-priority circuits (commissioned permanently `OFF_GRID`) appear as
    `priority = OFF_GRID, $settable = false`" -- the guide. The value and the
    lock are published together or the pair is meaningless."""
    em = _emitter(_locked("well_pump"))
    em.start()
    em.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits={"well_pump": 500.0}))
    assert rec.retained["ebus/5/well_pump/load-shed/priority"] == "OFF_GRID"


def test_a_never_backup_circuit_refuses_a_priority_set() -> None:
    """Defence in depth behind the declaration: a handler reached anyway --
    from a producer's own routing, or a broker that delivers to an
    unsubscribed topic -- must not move a commissioned circuit off
    `OFF_GRID`."""
    setters = SetterRegistry()
    em = Emitter(DeviceManifest(instances=(_panel(), _locked("well_pump"))), setters)
    em.start()
    handler = setters.get("circuit", "load-shed/priority")
    assert handler is not None
    handler("circuit", "well_pump", "load-shed/priority", "NEVER")

    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"well_pump": 500.0})
    )
    assert snap.circuits["well_pump"].priority == "OFF_GRID"
    assert snap.circuits["well_pump"].is_never_backup is True


def test_no_set_subscription_for_a_locked_priority(rec: PahoRecorder) -> None:
    """Pins the MQTT consequence rather than the description JSON: ebus-sdk
    subscribes `/set` only for a settable property, so the declaration is what
    actually removes the command path."""
    manifest = DeviceManifest(
        instances=(_panel(), _circuit("kitchen", tabs="1"), _locked("well_pump", tabs="3"))
    )
    Emitter(manifest, SetterRegistry()).start()
    assert "ebus/5/kitchen/load-shed/priority/set" in rec.subscribed
    assert "ebus/5/well_pump/load-shed/priority/set" not in rec.subscribed


def test_settable_is_declared_exactly_when_a_circuit_is_not_never_backup() -> None:
    """The invariant the captured enclosure holds, asserted over a mixed panel.

    16 circuits there, all with `$settable` present -- including the two at
    `NEVER`. The mix that panel does not happen to contain is the one the
    derivation gets wrong in the other direction, so it is built here."""
    circuits = {
        "kitchen": _circuit("kitchen", tabs="1", priority="SOC_THRESHOLD"),
        "lights": _circuit("lights", tabs="3", priority="NEVER"),
        "well_pump": _locked("well_pump", tabs="5"),
        "hot_tub": _circuit("hot_tub", tabs="7", priority="OFF_GRID"),
    }
    graph = _graph(*circuits.values())
    physics = ManifestPhysicsView(DeviceManifest(instances=(_panel(), *circuits.values())))
    for cid in circuits:
        declared = "settable" in _priority_declaration(graph, cid)
        locked = physics.circuit(cid).never_backup
        assert declared is not locked, f"{cid}: settable={declared} never-backup={locked}"


# --- Shedding: the lock is on the priority, not on the relay ---------------


def test_a_never_backup_circuit_still_sheds() -> None:
    """`never-backup` means exactly "no backup power": the circuit is
    commissioned `OFF_GRID`, so islanding is when it opens. The lock removes
    the consumer's ability to change that, not the shed itself.

    The open is attributed to `CONFIGURATION`: it is the commissioning decision
    taking effect, not a policy call made now."""
    em = Emitter(
        DeviceManifest(
            instances=(
                _panel(),
                _locked("well_pump", tabs="1"),
                DeviceInstance(
                    "bess",
                    "p1-bess",
                    "Battery",
                    metadata={"vendor-name": "Span", "nameplate-capacity-kwh": "13.5"},
                ),
            )
        ),
        SetterRegistry(),
        bess_configs=(
            BESSConfig(
                instance_id="p1-bess",
                nameplate_capacity_kwh=13.5,
                max_charge_w=3500.0,
                max_discharge_w=3500.0,
                initial_soc_pct=80.0,
            ),
        ),
        load_shedding_config=LoadSheddingConfig(soc_threshold_pct=20.0),
    )
    em.start()
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=False, circuits={"well_pump": 1000.0})
    )
    assert snap.circuits["well_pump"].relay_state == "OPEN"
    assert snap.circuits["well_pump"].is_sheddable is True
    assert snap.circuits["well_pump"].relay_requester == "CONFIGURATION"


def test_a_relay_locked_circuit_is_not_sheddable() -> None:
    """`sheddable` is "`load-shed/priority != NEVER` && `relay-controllable`"
    -- the guide's rule for the retired flat flag, both conjuncts. The relay
    half was missing, so a circuit the enclosure "never opens" (
    `devices/distribution-enclosure.md`) reported itself sheddable while
    `RelayResolver` correctly refused to shed it."""
    em = _emitter(_circuit("solar", priority="OFF_GRID", relay_behavior="non-controllable"))
    em.start()
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"solar": -200.0})
    )
    assert snap.circuits["solar"].is_sheddable is False


def test_islanding_separates_a_commissioned_shed_from_a_policy_shed(rec: PahoRecorder) -> None:
    """Two circuits, both `OFF_GRID`, both opened by the same islanding tick,
    attributed to different actors -- which is the whole point of the property.

    The migration guide maps the flat `NEVER_BACKUP` requester to
    `CONFIGURATION`: "the commissioning lock is now expressed structurally via
    `load-shed/priority = OFF_GRID` with `$settable = false`; `CONFIGURATION`
    captures the source attribution". Flat `BACKUP` -- an ordinary shed -- maps
    to `LOAD_SHED` instead. Firmware distinguished the two, so a publisher that
    reported `LOAD_SHED` for both would erase a distinction the canonical
    vocabulary still carries: for the locked circuit the decision was made once,
    at commissioning, and the policy running now is only carrying it out.

    Asserted on the wire, because the requester is a published property and its
    consumer reads it there."""
    manifest = DeviceManifest(
        instances=(
            _panel(),
            _locked("well_pump", tabs="1"),
            _circuit("hot_tub", tabs="3", priority="OFF_GRID"),
            DeviceInstance(
                "bess",
                "p1-bess",
                "Battery",
                metadata={"vendor-name": "Span", "nameplate-capacity-kwh": "13.5"},
            ),
        )
    )
    em = Emitter(
        manifest,
        SetterRegistry(),
        bess_configs=(
            BESSConfig(
                instance_id="p1-bess",
                nameplate_capacity_kwh=13.5,
                max_charge_w=3500.0,
                max_discharge_w=3500.0,
                initial_soc_pct=80.0,
            ),
        ),
        load_shedding_config=LoadSheddingConfig(soc_threshold_pct=20.0),
    )
    em.start()
    snap = em.publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=False,
            circuits={"well_pump": 1000.0, "hot_tub": 3000.0},
        )
    )
    assert snap.circuits["well_pump"].relay_state == "OPEN"
    assert snap.circuits["hot_tub"].relay_state == "OPEN"

    retained = rec.retained
    assert retained["ebus/5/well_pump/switch/relay-requester"] == "CONFIGURATION"
    assert retained["ebus/5/hot_tub/switch/relay-requester"] == "LOAD_SHED"


def test_a_never_backup_circuit_reports_no_requester_at_rest() -> None:
    """The lock speaks only when it acts. It does not hold the relay closed --
    that is what `relay-controllable = false` does, a different flag on a
    different property -- so on-grid, closed, this circuit reports `NONE` like
    any other, and reports `CONFIGURATION` only in the tick where the
    commissioning decision actually opens it."""
    em = _emitter(_locked("well_pump"))
    em.start()
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"well_pump": 500.0})
    )
    assert snap.circuits["well_pump"].relay_requester == "NONE"
    assert snap.circuits["well_pump"].relay_state == "CLOSED"


# --- The manifest input ----------------------------------------------------


def test_the_physics_view_reads_the_commissioning_flag() -> None:
    manifest = DeviceManifest(instances=(_panel(), _locked("well_pump")))
    assert ManifestPhysicsView(manifest).circuit("well_pump").never_backup is True


def test_a_circuit_without_the_key_is_not_never_backup() -> None:
    """Absent metadata is user-configurable, which is what every manifest
    written before this key existed means."""
    manifest = DeviceManifest(instances=(_panel(), _circuit("kitchen", priority="NEVER")))
    assert ManifestPhysicsView(manifest).circuit("kitchen").never_backup is False


@pytest.mark.parametrize("priority", ["NEVER", "SOC_THRESHOLD", "NICE_TO_HAVE"])
def test_never_backup_with_another_priority_is_rejected(priority: str) -> None:
    """A commissioned circuit *is* permanently `OFF_GRID`; a manifest that
    locks one at another value states two contradictory things about the same
    commissioning state. Rejected rather than silently rewritten, so the
    published priority is always the one the producer wrote."""
    manifest = DeviceManifest(
        instances=(_panel(), _circuit("well_pump", priority=priority, never_backup="true")),
    )
    with pytest.raises(ManifestValidationError, match="never-backup"):
        ManifestPhysicsView(manifest)


def test_never_backup_false_leaves_the_priority_free() -> None:
    """Producers write the key out explicitly, `false` included."""
    manifest = DeviceManifest(
        instances=(_panel(), _circuit("kitchen", priority="NEVER", never_backup="false")),
    )
    physics = ManifestPhysicsView(manifest).circuit("kitchen")
    assert physics.never_backup is False
    assert physics.default_priority == "NEVER"


def test_a_malformed_never_backup_is_rejected() -> None:
    manifest = DeviceManifest(
        instances=(_panel(), _circuit("kitchen", never_backup="sometimes")),
    )
    with pytest.raises(ManifestValidationError, match="never-backup"):
        ManifestPhysicsView(manifest)


def test_relay_lock_and_priority_lock_are_independent() -> None:
    """Two commissioning flags, two properties, no crosstalk: locking the relay
    leaves the priority settable (`switch.md` scopes `relay-controllable` to the
    relay) and locking the priority leaves the relay settable."""
    graph = _graph(
        _circuit("solar", tabs="1", priority="OFF_GRID", relay_behavior="non-controllable"),
        _locked("well_pump", tabs="3"),
    )
    assert _priority_declaration(graph, "solar")["settable"] is True
    assert _property_declaration(graph, "well_pump", "switch", "relay")["settable"] is True
