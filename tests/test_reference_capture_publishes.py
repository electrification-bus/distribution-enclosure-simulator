"""Each reference capture, republished, publishes what it published before, tick by tick.

The fidelity test holds a republished capture to the capture by shape and sign,
which is the right bar against hardware but lets a value move unnoticed. This pins
the values. A capture's definition, driven by its ticks, leaves a retained tree
after every tick, and ``reference_capture_publishes.json`` records a digest of
each of those trees and, for each device node, a digest of that node's topics
across every tick. A change to what a capture publishes fails here, naming the
capture, the first tick whose tree differs and the device nodes whose topics do.
It is a regression, or a deliberate change to the wire, which rewrites the record
in the same commit (``uv run python -m tests.test_reference_capture_publishes``)
and says why in its message.

A retained tree here leaves out each ``$description``'s ``version``, a wall-clock
stamp, as ``DEVELOPER.md`` describes for a wire-neutral check."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ebus_panel_sim import Emitter, SetterRegistry, load_reference_capture, reference_capture_names
from ebus_panel_sim.capture import _RetainedTransport

_RECORD = Path(__file__).with_name("reference_capture_publishes.json")
_PREFIX = "ebus/5/"

Record = dict[str, dict[str, object]]


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _stable(retained: dict[str, str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for topic, payload in retained.items():
        if topic.endswith("/$description"):
            description = json.loads(payload)
            description.pop("version", None)
            out[topic] = description
        else:
            out[topic] = payload
    return out


def _node(topic: str) -> str:
    """``<device>/<node>`` for a topic under the eBus prefix."""
    return "/".join(topic.removeprefix(_PREFIX).split("/")[:2])


def _published(name: str) -> dict[str, object]:
    """What the capture publishes: each tick's tree, digested, and each device
    node's topics across the ticks, digested."""
    capture = load_reference_capture(name)
    transport = _RetainedTransport()
    emitter = Emitter.from_definition(capture.definition, SetterRegistry(), mqttc=transport)
    emitter.start()
    trees: list[dict[str, object]] = []
    for tick in capture.ticks:
        emitter.publish_tick(tick)
        trees.append(_stable(transport.retained))
    by_node: dict[str, list[dict[str, object]]] = {}
    for n, tree in enumerate(trees):
        for topic, value in tree.items():
            series = by_node.setdefault(_node(topic), [{} for _ in trees])
            series[n][topic] = value
    return {
        "ticks": [_digest(tree) for tree in trees],
        "nodes": {node: _digest(by_node[node]) for node in sorted(by_node)},
    }


def _diagnosis(name: str, recorded: dict[str, object], published: dict[str, object]) -> str:
    ticks_r, ticks_p = recorded["ticks"], published["ticks"]
    nodes_r, nodes_p = recorded["nodes"], published["nodes"]
    assert isinstance(ticks_r, list) and isinstance(ticks_p, list)
    assert isinstance(nodes_r, dict) and isinstance(nodes_p, dict)
    first = next(
        (n for n, (r, p) in enumerate(zip(ticks_r, ticks_p, strict=False)) if r != p),
        min(len(ticks_r), len(ticks_p)),
    )
    differing = sorted(n for n in set(nodes_r) | set(nodes_p) if nodes_r.get(n) != nodes_p.get(n))
    return (
        f"{name} publishes differently from its record: {len(ticks_p)} ticks against "
        f"{len(ticks_r)} recorded, first differing at tick {first} (from 0); device nodes "
        f"whose topics differ: {', '.join(differing) or 'none'}. If the change is "
        f"deliberate, rewrite {_RECORD.name} with `uv run python -m "
        f"tests.test_reference_capture_publishes` and say why in the commit."
    )


def _record() -> Record:
    loaded = json.loads(_RECORD.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_every_capture_has_a_record() -> None:
    assert sorted(_record()) == sorted(reference_capture_names())


@pytest.mark.parametrize("name", reference_capture_names())
def test_a_capture_publishes_what_it_published_before(name: str) -> None:
    recorded = _record().get(name)
    assert recorded is not None, f"{name} has no record in {_RECORD.name}"
    published = _published(name)
    assert published == recorded, _diagnosis(name, recorded, published)


if __name__ == "__main__":
    _RECORD.write_text(
        json.dumps({name: _published(name) for name in reference_capture_names()}, indent=1)
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {_RECORD}")
