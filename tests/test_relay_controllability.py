"""One bit — controllability — governs every surface of a circuit's relay.

`capabilities/switch.md` makes `switch/relay` settable "when `relay-controllable`",
and defines `relay-controllable = false` as "locked": the relay "can be opened and
closed by command or automatic shed" only when true.
`devices/distribution-enclosure.md` says the same from the shed host's side --
"the enclosure never opens a circuit commissioned as permanently `OFF_GRID` /
locked" -- so "sheddable but not settable" is a state the specification does not
permit.

These tests pin all five surfaces to that one bit, because the defect they cover
was five separate readings of `relay-behavior` that disagreed with each other: the
`$settable` declaration said commandable, the published `relay-controllable` said
not, `/set` was obeyed, load-shed opened the relay, and `relay-requester` reported
`NONE` where a locked circuit reports `CONFIGURATION`.

Both real enclosures we can compare against maintain the invariant without
exception across 27 circuits: `$settable` is present on `switch/relay` exactly
when the published `relay-controllable` is `true`.
"""

from __future__ import annotations

import pytest

from ebus_panel_sim import (
    BESSConfig,
    DeviceInstance,
    DeviceManifest,
    Emitter,
    LoadSheddingConfig,
    RelayRequester,
    RelayResolver,
    RelayState,
    SetterRegistry,
    TickInputs,
)
from ebus_panel_sim.manifest_physics import ManifestPhysicsView
from ebus_panel_sim.wire.graph_builder import BuiltGraph, build_graph
from ebus_panel_sim.wire.mapping_loader import load_mapping_table
from ebus_panel_sim.wire.profile_loader import load_profiles

from .conftest import PahoRecorder

_LOCKED = ("non-controllable", "always-on")


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
    relay_behavior: str = "controllable",
    priority: str = "NEVER",
    always_on: str | None = None,
) -> DeviceInstance:
    """A circuit instance. ``always_on=None`` omits the key entirely, which is
    what a hand-written manifest does; the producers we know write it
    explicitly, which is why the tests below cover both."""
    metadata = {
        "tab-numbers": tabs,
        "breaker-rating-a": "20",
        "default-priority": priority,
        "relay-behavior": relay_behavior,
        "placement": "upstream-of-lugs",
    }
    if always_on is not None:
        metadata["always-on"] = always_on
    return DeviceInstance("circuit", cid, cid.title(), metadata=metadata)


def _graph(*circuits: DeviceInstance) -> BuiltGraph:
    manifest = DeviceManifest(instances=(_panel(), *circuits))
    return build_graph(manifest, load_mapping_table(), load_profiles(), mqtt_cfg={})


def _property_declaration(graph: BuiltGraph, cid: str, node: str, prop: str) -> dict[str, object]:
    """One published property declaration, narrowed to a mapping.

    ebus-sdk annotates ``description()`` as a bare ``dict``, so every index into
    it is ``Any`` and a strict build cannot see the shape. Asserting the type on
    the way out keeps the ``Any`` from leaking into the tests, and fails loudly
    if the description ever stops being nested mappings."""
    declaration = graph.devices[cid].description()["nodes"][node]["properties"][prop]
    assert isinstance(declaration, dict)
    return declaration


def _relay_declaration(graph: BuiltGraph, cid: str) -> dict[str, object]:
    return _property_declaration(graph, cid, "switch", "relay")


def _registry() -> SetterRegistry:
    """No producer handlers, so the Emitter's own internal ones are exercised."""
    return SetterRegistry()


# --- The declaration -------------------------------------------------------


@pytest.mark.parametrize("relay_behavior", _LOCKED)
def test_a_locked_circuit_declares_no_settable_on_its_relay(relay_behavior: str) -> None:
    """The attribute is absent, not `false`.

    Both captured enclosures omit it on a locked relay, as they do on the
    read-only `shed/policy`; Homie 5 defaults it false, so an omission and an
    explicit `false` are the same claim, and the omission is what firmware
    publishes."""
    declaration = _relay_declaration(
        _graph(_circuit("locked", relay_behavior=relay_behavior)), "locked"
    )
    assert "settable" not in declaration


def test_a_controllable_circuit_still_declares_its_relay_settable() -> None:
    assert _relay_declaration(_graph(_circuit("kitchen")), "kitchen")["settable"] is True


def test_a_circuit_with_no_relay_behavior_declared_is_controllable() -> None:
    """Absent metadata means controllable, which is what every metadata-less
    fixture and hand-written manifest relies on."""
    bare = DeviceInstance("circuit", "bare", "Bare")
    assert _relay_declaration(_graph(bare), "bare")["settable"] is True


def test_explicit_always_on_metadata_locks_a_controllable_circuit() -> None:
    """`always-on: true` on a `controllable` circuit is the mirror-image defect:
    without the OR rule the relay is locked by the resolver while the
    declaration and the published `relay-controllable` both say commandable."""
    circuit = _circuit("fridge", relay_behavior="controllable", always_on="true")
    assert "settable" not in _relay_declaration(_graph(circuit), "fridge")


def test_explicit_always_on_false_does_not_unlock_a_non_controllable_circuit() -> None:
    """The producers write this key explicitly. A default-chain fix would leave
    every such circuit unlocked, which is every circuit a real-panel clone
    produces for a locked breaker."""
    circuit = _circuit("solar", relay_behavior="non-controllable", always_on="false")
    assert "settable" not in _relay_declaration(_graph(circuit), "solar")


def test_settable_is_declared_exactly_when_relay_controllable_is_published(
    rec: PahoRecorder,
) -> None:
    """The invariant both real enclosures maintain, asserted over a mixed panel.

    This is the whole defect in one assertion: the description and the value are
    two statements about the same fact, and nothing today makes them agree."""
    manifest = DeviceManifest(
        instances=(
            _panel(),
            _circuit("kitchen", tabs="1"),
            _circuit("solar", tabs="3", relay_behavior="non-controllable", always_on="false"),
            _circuit("fridge", tabs="5", relay_behavior="always-on"),
        )
    )
    em = Emitter(manifest, _registry())
    em.start()
    em.publish_tick(
        TickInputs(
            current_time=0.0,
            grid_online=True,
            circuits={"kitchen": 500.0, "solar": -100.0, "fridge": 150.0},
        )
    )
    retained = rec.retained

    graph = _graph(
        _circuit("kitchen", tabs="1"),
        _circuit("solar", tabs="3", relay_behavior="non-controllable", always_on="false"),
        _circuit("fridge", tabs="5", relay_behavior="always-on"),
    )
    for cid in ("kitchen", "solar", "fridge"):
        declared = "settable" in _relay_declaration(graph, cid)
        published = retained[f"ebus/5/{cid}/switch/relay-controllable"] == "true"
        assert declared is published, f"{cid}: settable={declared} relay-controllable={published}"


def test_a_locked_circuit_keeps_its_load_shed_priority_settable() -> None:
    """Scope guard. Never-backup is a separate commissioning flag, mapped to
    `$settable` on `load-shed/priority`; both locked circuits on the captured
    enclosure keep it `true`. Locking the relay must not reach the priority."""
    graph = _graph(_circuit("solar", relay_behavior="non-controllable"))
    priority = _property_declaration(graph, "solar", "load-shed", "priority")
    assert priority["settable"] is True


def test_no_set_subscription_for_a_locked_relay(rec: PahoRecorder) -> None:
    """Pins the MQTT consequence rather than the description JSON: ebus-sdk
    subscribes `/set` only for a settable property, so the declaration is what
    actually removes the command path."""
    manifest = DeviceManifest(
        instances=(
            _panel(),
            _circuit("kitchen", tabs="1"),
            _circuit("solar", tabs="3", relay_behavior="non-controllable", always_on="false"),
        )
    )
    Emitter(manifest, _registry()).start()
    assert "ebus/5/kitchen/switch/relay/set" in rec.subscribed
    assert "ebus/5/solar/switch/relay/set" not in rec.subscribed


# --- The behaviour ---------------------------------------------------------


@pytest.mark.parametrize("relay_behavior", _LOCKED)
def test_a_locked_circuit_is_locked_in_the_physics_view(relay_behavior: str) -> None:
    """The one bit, read where the resolver reads it."""
    manifest = DeviceManifest(
        instances=(_panel(), _circuit("locked", relay_behavior=relay_behavior, always_on="false"))
    )
    assert ManifestPhysicsView(manifest).circuit("locked").always_on is True


def _resolver_for(circuit: DeviceInstance) -> RelayResolver:
    """A resolver registered the way the emitter registers it, from the physics
    view -- so these exercise the derivation and the gate together rather than
    asserting the gate against a hand-passed flag it already honours."""
    manifest = DeviceManifest(instances=(_panel(), circuit))
    physics = ManifestPhysicsView(manifest).circuit(circuit.instance_id)
    resolver = RelayResolver()
    resolver.register(circuit.instance_id, always_on=physics.always_on)
    return resolver


def test_a_locked_relay_refuses_a_user_override() -> None:
    circuit = _circuit("solar", relay_behavior="non-controllable", always_on="false")
    resolver = _resolver_for(circuit)
    resolver.set_user_override("solar", RelayState.OPEN)
    assert resolver.state("solar") == (RelayState.CLOSED, RelayRequester.CONFIGURATION)


def test_a_locked_relay_is_exempt_from_load_shed() -> None:
    """The enclosure never opens a locked circuit -- `distribution-enclosure.md`."""
    circuit = _circuit("solar", relay_behavior="non-controllable", always_on="false")
    resolver = _resolver_for(circuit)
    resolver.set_shed("solar", open_relay=True)
    assert resolver.state("solar") == (RelayState.CLOSED, RelayRequester.CONFIGURATION)


def test_a_non_controllable_circuit_refuses_a_relay_command_end_to_end() -> None:
    manifest = DeviceManifest(
        instances=(
            _panel(),
            _circuit("solar", relay_behavior="non-controllable", always_on="false"),
        )
    )
    setters = SetterRegistry()
    em = Emitter(manifest, setters)
    em.start()

    handler = setters.get("circuit", "switch/relay")
    assert handler is not None
    handler("circuit", "solar", "switch/relay", False)

    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"solar": 1000.0})
    )
    assert snap.circuits["solar"].relay_state == "CLOSED"
    assert snap.circuits["solar"].instant_power_w != 0.0


def test_a_non_controllable_circuit_is_not_shed_off_grid() -> None:
    """A locked circuit at `OFF_GRID` priority stays closed while a controllable
    sibling at the same priority opens -- the shed gate is controllability, not
    priority alone."""
    manifest = DeviceManifest(
        instances=(
            _panel(),
            _circuit("hot_tub", tabs="1", priority="OFF_GRID"),
            _circuit(
                "solar",
                tabs="3",
                priority="OFF_GRID",
                relay_behavior="non-controllable",
                always_on="false",
            ),
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
        _registry(),
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
            circuits={"hot_tub": 3000.0, "solar": -200.0},
        )
    )
    assert snap.circuits["hot_tub"].relay_state == "OPEN"
    assert snap.circuits["solar"].relay_state == "CLOSED"


def test_a_locked_circuit_attributes_its_state_to_configuration(rec: PahoRecorder) -> None:
    """Both captured locked circuits publish `CONFIGURATION` at rest, and every
    controllable one publishes `NONE` (or `PCS`). 27 circuits, no exceptions."""
    manifest = DeviceManifest(
        instances=(
            _panel(),
            _circuit("kitchen", tabs="1"),
            _circuit("solar", tabs="3", relay_behavior="non-controllable", always_on="false"),
        )
    )
    em = Emitter(manifest, _registry())
    em.start()
    em.publish_tick(
        TickInputs(
            current_time=0.0, grid_online=True, circuits={"kitchen": 500.0, "solar": -100.0}
        )
    )
    retained = rec.retained
    assert retained["ebus/5/solar/switch/relay-requester"] == "CONFIGURATION"
    assert retained["ebus/5/kitchen/switch/relay-requester"] == "NONE"


def test_a_locked_circuit_is_not_pcs_managed() -> None:
    """`load-shed` and `pcs` are both policies acting on the relay, and the spec
    scopes both to a controllable switch. `pcs_managed` already keyed off
    `relay_behavior`; it must key off the same one bit so an explicit
    `always-on` on a controllable circuit cannot leave it managed."""
    manifest = DeviceManifest(
        instances=(_panel(), _circuit("fridge", relay_behavior="controllable", always_on="true"))
    )
    em = Emitter(manifest, _registry())
    em.start()
    snap = em.publish_tick(
        TickInputs(current_time=0.0, grid_online=True, circuits={"fridge": 150.0})
    )
    assert snap.circuits["fridge"].pcs_managed is False
    assert snap.circuits["fridge"].is_user_controllable is False
