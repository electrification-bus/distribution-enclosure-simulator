"""Teardown semantics: what the retained tree looks like after ``Emitter.stop()``.

Both modes were previously untested. The non-graceful path had documented itself as
"leaving the LWT to fire ``$state=lost``", which cannot happen: a Last Will fires
only on an *unclean* disconnect, and ``MqttClient.stop()`` sends a clean DISCONNECT
(deliberately, so an orderly shutdown is not reported as a crash). Verified against
a real mosquitto broker before this suite existed: a consumer joining after an
ungraceful teardown read the whole tree as ``ready``.

These tests pin the observable outcome, the effective retained ``$state`` a
late-joining consumer sees, rather than the mechanism.
"""

from __future__ import annotations

from ebus_panel_sim import Emitter
from ebus_panel_sim.wire.set_router import SetterRegistry

from .conftest import PahoRecorder
from .test_emitter_public_surface import _manifest

ROOT_STATE = "ebus/5/p1/$state"


def _started() -> Emitter:
    emitter = Emitter(_manifest(), SetterRegistry())
    emitter.start()
    return emitter


def test_ungraceful_stop_leaves_the_root_lost(rec: PahoRecorder) -> None:
    """A producer that died must not leave the tree claiming ``ready``."""
    _started().stop(graceful=False)
    assert rec.retained[ROOT_STATE] == "lost"


def test_graceful_stop_leaves_the_root_disconnected(rec: PahoRecorder) -> None:
    """An orderly shutdown is a different signal from a crash, and consumers act
    on the difference: ``disconnected`` is expected, ``lost`` is a fault."""
    _started().stop(graceful=True)
    assert rec.retained[ROOT_STATE] == "disconnected"


def test_the_two_modes_do_not_agree(rec: PahoRecorder) -> None:
    """Guards against a future change collapsing the two teardown modes into one.

    Note this test passed before the fix too, for the wrong reason: ungraceful left
    the root on ``ready``, which does differ from ``disconnected``. The bug was
    never that the modes agreed, it was that one of them lied. The two tests above
    are what pin the actual values; this one only pins that they stay distinct."""
    _started().stop(graceful=False)
    ungraceful = rec.retained[ROOT_STATE]
    rec.reset()
    _started().stop(graceful=True)
    assert ungraceful != rec.retained[ROOT_STATE]


def test_ungraceful_stop_uses_the_sdk_will_descriptor(rec: PahoRecorder) -> None:
    """The synthesised ``lost`` must be byte-identical to what the broker would
    have delivered from the registered will, or the simulation is a fiction."""
    emitter = _started()
    will = emitter._root.will()
    emitter.stop(graceful=False)
    assert rec.retained[will["topic"]] == will["payload"]


def test_ungraceful_stop_publishes_lost_retained(rec: PahoRecorder) -> None:
    """Retained, or a consumer joining after the death sees nothing at all and
    cannot distinguish a dead producer from one that never existed."""
    _started().stop(graceful=False)
    lost = [(t, d, q, r) for (t, d, q, r) in rec.published if t == ROOT_STATE and d == "lost"]
    assert lost, "no $state=lost publish reached the transport"
    assert all(retain for (_t, _d, _q, retain) in lost)


def test_ungraceful_stop_moves_the_device_state_not_just_the_wire(rec: PahoRecorder) -> None:
    """Publishing `lost` while the Device object still holds `ready` leaves the two
    disagreeing, and anything that re-announces from the object undoes the publish.
    `Device.stop()` sets `_state` for `disconnected`; this must match."""
    emitter = _started()
    emitter.stop(graceful=False)
    assert rec.retained[ROOT_STATE] == "lost"
    assert emitter._root.state().value == "lost"


def test_a_later_refresh_tree_re_announces_lost_not_ready(rec: PahoRecorder) -> None:
    """`refresh_tree()` republishes from the Device's own state, and the SDK asks a
    bring-your-own-transport caller to wire it onto their client's on-connect
    handler. If the ungraceful stop left `_state` on `ready`, that hook would
    resurrect `ready` over the `lost` and restore the very stale-tree bug 0.3.1
    removed."""
    emitter = _started()
    emitter.stop(graceful=False)
    emitter._root.refresh_tree()
    assert rec.retained[ROOT_STATE] == "lost"


def test_ungraceful_stop_publishes_the_root_state_exactly_once(rec: PahoRecorder) -> None:
    """One publish, not two.

    Until this went through the SDK, the owned path published `$state` twice: once
    via `set_state` and again as a deliberate byte-identical duplicate, because
    only a flushed publish is guaranteed to land before the socket closes and
    `set_state` uses the ordinary unflushed path. `Device.declare_lost()` does both
    jobs in one flushed publish, so the duplicate is gone.

    Worth pinning rather than leaving implicit: the duplicate was harmless but
    deliberate, so a future reader seeing one publish might reasonably re-add it.
    """
    emitter = _started()
    rec.reset()
    emitter.stop(graceful=False)
    state_publishes = [p for p in rec.published if p[0] == ROOT_STATE]
    assert len(state_publishes) == 1, f"expected one $state publish, got {state_publishes}"
    _topic, payload, _qos, retain = state_publishes[0]
    assert payload == "lost", f"payload should be the wire string, got {payload!r}"
    assert retain is True
    assert rec.retained[ROOT_STATE] == "lost"


def test_clear_retained_removes_each_device_state_first(rec: PahoRecorder) -> None:
    """Homie 5 removes a device by clearing its ``$state`` before its other topics.
    A controller that sees an empty ``$description`` while the device still has a
    ``$state`` reads it as a malformed description, not a removal."""
    emitter = _started()
    devices = list(emitter._graph.devices)
    assert len(devices) > 1
    rec.reset()
    emitter.stop(clear_retained=True)
    cleared = [t for (t, d, _q, r) in rec.published if d == "" and r]
    for device_id in devices:
        state, description = f"ebus/5/{device_id}/$state", f"ebus/5/{device_id}/$description"
        assert state in cleared and description in cleared, device_id
        assert cleared.index(state) < cleared.index(description), device_id


def test_clear_retained_leaves_nothing_retained(rec: PahoRecorder) -> None:
    """A trailing ``disconnected`` would re-create the root as a ``$state`` with no
    ``$description``."""
    _started().stop(clear_retained=True)
    assert rec.retained == {}
