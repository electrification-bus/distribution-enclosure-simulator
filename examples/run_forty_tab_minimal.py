"""Run the 40-tab enclosure simulator against an eBus MQTT broker.

Producer-shaped: loads a panel definition and its ticks, builds a (synchronous)
Emitter from the definition, publishes the ticks, then reads the retained tree
back through an ebus-sdk Controller and prints it as a sorted ``topic payload``
map.

Needs a plaintext MQTT broker on localhost:1883. The easiest is the companion
broker-quickstart bundle in its ``open`` profile (see ../broker-quickstart), or
any local broker (e.g. ``mosquitto -p 1883``). Then:

    uv run python examples/run_forty_tab_minimal.py
    uv run python examples/run_forty_tab_minimal.py --broker 127.0.0.1:1883 --ticks 3 \
        > /tmp/ebus-topics.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from ebus_sdk import Controller

from ebus_panel_sim import Emitter, SetterRegistry, load_definition, load_ticks

_HERE = Path(__file__).parent


def main() -> None:
    args = _parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("homie").setLevel(logging.ERROR)
    logging.getLogger("transitions").setLevel(logging.ERROR)
    host, port = _parse_broker(args.broker)
    _run(args.definition, args.tick_file, tick_count=args.ticks, host=host, port=port)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--definition",
        type=Path,
        default=_HERE / "forty_tab_minimal.yaml",
        help="Panel definition file to publish.",
    )
    parser.add_argument(
        "--tick-file",
        type=Path,
        default=_HERE / "forty_tab_minimal.ticks.yaml",
        help="Tick file that drives it.",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        help="Number of ticks from the tick file to publish (default: all).",
    )
    parser.add_argument(
        "--broker",
        default="127.0.0.1:1883",
        help="MQTT broker host:port (plaintext). Default matches broker-quickstart 'open'.",
    )
    return parser.parse_args()


def _parse_broker(spec: str) -> tuple[str, int]:
    host, _, port = spec.partition(":")
    return host or "127.0.0.1", int(port or "1883")


def _run(
    definition_path: Path, ticks_path: Path, *, tick_count: int | None, host: str, port: int
) -> None:
    # The Emitter owns the publishing connection (ebus-sdk builds the MqttClient
    # from mqtt_cfg and sets the root device's LWT automatically).
    emitter = Emitter.from_definition(
        load_definition(definition_path),
        SetterRegistry(),
        mqtt_cfg={"host": host, "port": port},
    )
    emitter.start()
    try:
        for tick in load_ticks(ticks_path)[:tick_count]:
            emitter.publish_tick(tick)
        _print_retained_tree(host, port)
    finally:
        emitter.stop(graceful=True)


def _print_retained_tree(host: str, port: int) -> None:
    """Read the retained tree back through an ebus-sdk Controller and print it as
    a sorted ``topic payload`` map, proving the full round-trip over the broker."""
    controller = Controller(mqtt_cfg={"host": host, "port": port})
    try:
        controller.start_discovery()
        time.sleep(1.5)  # discovery is async; let retained messages arrive
        devices = controller.get_all_devices()
        if not devices:
            print(
                f"# No devices discovered on {host}:{port}. Is a broker running? "
                "Start ../broker-quickstart (open profile) or run `mosquitto -p 1883`.",
            )
            return
        topics: dict[str, str] = {}
        for dev in devices.values():
            topics[f"ebus/5/{dev.device_id}/$state"] = str(dev.state)
            topics[f"ebus/5/{dev.device_id}/$description"] = json.dumps(
                dev.description, sort_keys=True
            )
            for node_id, props in dev.properties.items():
                for prop_id, value in props.items():
                    topics[f"ebus/5/{dev.device_id}/{node_id}/{prop_id}"] = str(value)
        for topic in sorted(topics):
            print(f"{topic} {topics[topic]}")
    finally:
        controller.stop()


if __name__ == "__main__":
    main()
