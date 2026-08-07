"""Bring-your-own-transport: publishing through a client the caller owns.

ebus-sdk supports this at the ``Device`` level (``Device(mqttc=...)``, with an
explicit guarantee that it never starts or stops a client it did not build).
These tests cover the emitter honouring the same contract, so a host that
already owns its MQTT connection — a Home Assistant add-on, say, whose MQTT
integration is ``single_config_entry`` and which forbids background threads —
can publish an eBus tree through it rather than having a second connection
opened underneath it.
"""

from __future__ import annotations

import time

import pytest

from panel_sim import (
    DeviceInstance,
    DeviceManifest,
    Emitter,
    EmitterStateError,
    SetterRegistry,
    TickInputs,
)


class RecordingTransport:
    """Satisfies ebus-sdk's ``MqttDeviceTransport``: publish, subscribe,
    ``is_connected`` and ``is_running``, and nothing else.

    Deliberately has no ``start``/``stop``. The SDK's contract is that it never
    calls them on an injected client, and a transport without them turns a
    violation into an ``AttributeError`` here rather than a silently closed
    connection in production.
    """

    def __init__(self, *, connected: bool = True) -> None:
        self.published: list[tuple[str, str, int, bool]] = []
        self.subscribed: list[str] = []
        self.is_running = True
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected

    def publish(self, topic: str, data: str, qos: int = 1, retain: bool = False) -> object:
        self.published.append((topic, data, qos, retain))
        return None

    def subscribe(self, sub: str, param: object = None, qos: int = 1) -> object:
        self.subscribed.append(sub)
        return None


def _manifest() -> DeviceManifest:
    return DeviceManifest(
        instances=(
            DeviceInstance(
                "panel",
                "p1",
                "Span",
                metadata={
                    "vendor-name": "Span",
                    "serial-number": "p1",
                    "firmware-version": "r2026",
                    "hardware-version": "rev2",
                    "panel-size": "32",
                    "main-breaker-rating-a": "200",
                    "panel-model": "MAIN_32",
                    "postal-code": "94103",
                    "time-zone": "America/Los_Angeles",
                },
            ),
            DeviceInstance(
                "circuit",
                "c1",
                "Kitchen",
                metadata={
                    "tab-numbers": "1",
                    "breaker-rating-a": "20",
                    "default-priority": "NICE_TO_HAVE",
                    "relay-behavior": "controllable",
                    "placement": "downstream-of-lugs",
                },
            ),
        )
    )


def test_injected_transport_receives_the_tree() -> None:
    """The whole point: the caller's client carries the traffic."""
    transport = RecordingTransport()
    emitter = Emitter(_manifest(), SetterRegistry(), mqttc=transport)
    emitter.start()
    emitter.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits={"c1": 200.0}))

    topics = [t for t, _d, _q, _r in transport.published]
    assert topics, "nothing was published through the injected transport"
    for device_id in ("p1", "c1"):
        assert any(f"/{device_id}/$description" in t for t in topics), f"no $description for {device_id}"
        assert any(f"/{device_id}/$state" in t for t in topics), f"no $state for {device_id}"
    assert any("meter/active-power" in t for t in topics), "no property values reached the transport"


def test_mqtt_cfg_and_mqttc_together_is_rejected() -> None:
    """Two answers to "which connection" is a producer-side bug, and silently
    preferring one would hide it until the wrong broker had the traffic."""
    with pytest.raises(EmitterStateError):
        Emitter(
            _manifest(),
            SetterRegistry(),
            mqtt_cfg={"host": "localhost", "port": 1883},
            mqttc=RecordingTransport(),
        )


def test_start_does_not_block_on_an_unconnected_injected_client() -> None:
    """``start()`` waits for the link only for a client it built.

    The caller owns an injected client's lifecycle and its timing, and the SDK
    never starts one — so there is nothing to wait for, and waiting would stall
    the loop such a client is likely being driven on. Retained values republish
    on connect regardless.
    """
    transport = RecordingTransport(connected=False)
    emitter = Emitter(_manifest(), SetterRegistry(), mqttc=transport)

    started = time.monotonic()
    emitter.start(connect_timeout_s=30.0)
    elapsed = time.monotonic() - started

    # Timed rather than merely observed to return: without this the call polls
    # is_connected() for the full timeout, which a slow test would pass and only
    # a stalled event loop in production would reveal.
    assert elapsed < 1.0, f"start() blocked for {elapsed:.1f}s on a client it does not own"

    emitter.publish_tick(TickInputs(current_time=0.0, grid_online=True, circuits={"c1": 200.0}))


def test_stop_never_stops_a_client_it_did_not_build() -> None:
    """Tearing down the caller's connection would take out whatever else they
    were using it for. ``RecordingTransport`` has no ``stop``, so an attempt
    raises rather than passing quietly."""
    transport = RecordingTransport()
    emitter = Emitter(_manifest(), SetterRegistry(), mqttc=transport)
    emitter.start()

    emitter.stop(graceful=False)
    emitter.stop(graceful=True)
