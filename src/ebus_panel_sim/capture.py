"""Generate a panel definition from a published distribution-enclosure tree.

A tree comes from a live broker (``capture_live``), from a ``tree-v1`` snapshot
file (``tree_from_snapshot``), or from a retained-topic map (``tree_from_retained``).
``definition_from_tree`` maps it onto manifest metadata and returns the
definition together with notes on every value it had to default, since a
published tree does not carry everything a definition holds (BESS dispatch
settings, panel size, an inverter's coupling).

Command line::

    panel-sim-capture --host span-<serial>.local --username <serial> --password <pw> -o panel.yaml
    panel-sim-capture --from-snapshot snapshot.json -o panel.yaml

Masking is on by default: serial numbers, device IDs and the postal code are
replaced. ``--no-mask`` keeps them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from ebus_panel_sim.definition import PanelDefinition, dump_definition, dump_ticks
from ebus_panel_sim.manifest import DeviceInstance, DeviceManifest
from ebus_panel_sim.native_devices import BESSConfig
from ebus_panel_sim.tick_inputs import TickInputs
from ebus_panel_sim.wire.profile_loader import Variant

_DOMAIN = "ebus/5"
_TYPE_PREFIX = "energy.ebus.device."


@dataclass(slots=True)
class Device:
    """One published device: its ``$description`` and ``node/property`` values."""

    description: dict[str, Any]
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def type(self) -> str:
        return str(self.description.get("type", "")).removeprefix(_TYPE_PREFIX)

    def declares(self, path: str) -> bool:
        return self.declaration(path) is not None

    def declaration(self, path: str) -> dict[str, Any] | None:
        node, _, prop = path.partition("/")
        decl = self.description.get("nodes", {}).get(node, {}).get("properties", {}).get(prop)
        return decl if isinstance(decl, dict) else None

    def value(self, path: str) -> str | None:
        raw = self.properties.get(path)
        return None if raw in (None, "") else raw


Tree = dict[str, Device]


@dataclass(frozen=True, slots=True)
class CaptureNote:
    """A value the definition could not take from the tree."""

    device: str
    key: str
    note: str


# ---------------------------------------------------------------------------
# Tree sources
# ---------------------------------------------------------------------------


def tree_from_retained(retained: Mapping[str, str], domain: str = _DOMAIN) -> Tree:
    """A tree from retained ``<domain>/<device>/...`` topics."""
    tree: Tree = {}
    values: dict[str, dict[str, str]] = {}
    prefix = domain.rstrip("/") + "/"
    for topic, payload in retained.items():
        if not topic.startswith(prefix):
            continue
        parts = topic[len(prefix) :].split("/")
        if len(parts) == 2 and parts[1] == "$description":
            tree[parts[0]] = Device(description=json.loads(payload))
        elif len(parts) == 3 and not parts[1].startswith("$"):
            values.setdefault(parts[0], {})[f"{parts[1]}/{parts[2]}"] = payload
    for device_id, props in values.items():
        if device_id in tree:
            tree[device_id].properties = props
    return tree


def tree_from_snapshot(raw: Mapping[str, Any]) -> Tree:
    """A tree from a ``tree-v1`` snapshot: ``{"metadata": {"schema": "tree-v1"},
    "devices": {<id>: {"description": ..., "properties": ..., "numeric_properties": ...}}}``,
    with string/enum/boolean values under ``properties`` and numbers under
    ``numeric_properties``."""
    if raw.get("metadata", {}).get("schema") != "tree-v1":
        raise ValueError("not a tree-v1 snapshot (metadata.schema)")
    tree: Tree = {}
    for device_id, entry in raw.get("devices", {}).items():
        merged = {**entry.get("properties", {}), **entry.get("numeric_properties", {})}
        tree[device_id] = Device(
            description=entry.get("description", {}),
            properties={k: _wire_str(v) for k, v in merged.items()},
        )
    return tree


def capture_live(
    host: str,
    port: int,
    *,
    username: str | None = None,
    password: str | None = None,
    use_tls: bool = True,
    tls_insecure: bool = False,
    tls_ca_cert: str | None = None,
    timeout_s: float = 30.0,
    settle_s: float = 5.0,
    record: int = 0,
    interval_s: float = 1.0,
) -> tuple[Tree, list[tuple[float, Tree]]]:
    """Read a live tree through ``ebus_sdk.Controller``.

    Waits until every advertised device has a ``$description`` and no new
    property has appeared for ``settle_s``, or until ``timeout_s``. Then takes
    ``record`` further samples, ``interval_s`` apart, each stamped with the wall
    clock. Returns the settled tree and the samples."""
    from ebus_sdk import Controller

    mqtt_cfg: dict[str, Any] = {
        "host": host,
        "port": port,
        "use_tls": use_tls,
        "tls_insecure": tls_insecure,
    }
    if tls_ca_cert is not None:
        mqtt_cfg["tls_ca_cert"] = tls_ca_cert
    if username is not None:
        mqtt_cfg["authentication"] = {
            "type": "USER_PASS",
            "username": username,
            "password": password or "",
        }
    controller = Controller(mqtt_cfg=mqtt_cfg)
    seen: set[tuple[str, str, str]] = set()
    last_new = time.monotonic()

    def on_property(device_id: str, node: str, prop: str, new: str, old: str | None) -> None:
        nonlocal last_new
        del new, old
        if (device_id, node, prop) not in seen:
            seen.add((device_id, node, prop))
            last_new = time.monotonic()

    def snapshot() -> Tree:
        return {
            device_id: Device(
                description=dict(d.description or {}),
                properties={
                    f"{node}/{prop}": _wire_str(value)
                    for node, props in d.properties.items()
                    for prop, value in props.items()
                },
            )
            for device_id, d in controller.get_all_devices().items()
            if d.description
        }

    controller.set_on_property_changed_callback(on_property)
    controller.start_discovery()
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            time.sleep(0.25)
            devices = controller.get_all_devices()
            complete = bool(devices) and all(
                d.description and all(c in devices for c in d.children_ids)
                for d in devices.values()
            )
            if complete and time.monotonic() - last_new >= settle_s:
                break
        tree = snapshot()
        samples: list[tuple[float, Tree]] = []
        for _ in range(record):
            time.sleep(interval_s)
            samples.append((time.time(), snapshot()))
        return tree, samples
    finally:
        controller.stop()


def _wire_str(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ---------------------------------------------------------------------------
# Tree -> definition
# ---------------------------------------------------------------------------


def definition_from_tree(
    tree: Tree, *, variant: Variant | None = None, mask: bool = True
) -> tuple[PanelDefinition, list[CaptureNote]]:
    """Map a published tree onto a panel definition.

    ``variant`` defaults to ``span`` for a SPAN panel and ``reference``
    otherwise."""
    return _Mapper(tree, mask).run(variant)


def ticks_from_samples(
    tree: Tree, samples: Sequence[tuple[float, Tree]], *, mask: bool = True
) -> list[TickInputs]:
    """Replayable ticks from timestamped samples of the same tree.

    Device IDs match ``definition_from_tree(tree, mask=mask)``. A circuit's power
    is its published ``meter/active-power`` negated back to the producer's sign
    (positive = consuming); an EVSE draws what its feeding circuit does; the grid
    is offline when a MID reports ``grid/islanding-state`` ``OFF_GRID`` or
    ``grid/grid-state`` ``DOWN`` or, without a MID, the panel's main relay is
    ``OPEN``."""
    mapper = _Mapper(tree, mask)
    feeds = mapper._feeds()
    ticks: list[TickInputs] = []
    for stamp, sample in samples:
        circuits: dict[str, float] = {}
        for device_id, device in sample.items():
            power = device.value("meter/active-power")
            if device.type == "circuit" and device_id in mapper.ids and power is not None:
                circuits[mapper.ids[device_id]] = -float(power) + 0.0
        evse = {
            mapper.ids[evse_id]: circuits.get(feed, 0.0)
            for evse_id, feed in feeds.items()
            if evse_id in mapper.ids and tree[evse_id].type == "evse"
        }
        ticks.append(
            TickInputs(
                current_time=stamp,
                grid_online=_grid_online(sample),
                circuits=circuits,
                evse=evse,
            )
        )
    return ticks


def _grid_online(sample: Tree) -> bool:
    mids = [d for d in sample.values() if d.type == "mid"]
    for mid in mids:
        if (
            mid.value("grid/islanding-state") == "OFF_GRID"
            or mid.value("grid/grid-state") == "DOWN"
        ):
            return False
    if mids:
        return True
    panel = next((d for d in sample.values() if d.type == "distribution-enclosure"), None)
    return panel is None or panel.value("status/relay") != "OPEN"


class _Mapper:
    def __init__(self, tree: Tree, mask: bool) -> None:
        self.tree = tree
        self.mask = mask
        self.notes: list[CaptureNote] = []
        roots = [i for i, d in tree.items() if d.type == "distribution-enclosure"]
        if len(roots) != 1:
            raise ValueError(f"expected one distribution-enclosure, found {len(roots)}")
        self.panel_id = roots[0]
        self.ids = self._id_map()
        # Strings a masked definition must not contain: every original device ID
        # and published serial number.
        self.secrets = {i for i in tree if i not in self.ids.values()} | {
            serial for d in tree.values() if (serial := d.value("info/serial-number"))
        }
        # Where tabs are not published, circuits get the next free ones.
        self.next_tab = 1 + max(
            (t for d in tree.values() for t in _tabs(d.value("info/spaces"))), default=0
        )

    # -- ids -------------------------------------------------------------

    def _id_map(self) -> dict[str, str]:
        """Original device ID -> published ID. Identity unless masking."""
        if not self.mask:
            return {i: i for i in self.tree}
        out: dict[str, str] = {self.panel_id: "masked-panel"}
        counters: dict[str, int] = {}
        circuits = sorted(
            (i for i, d in self.tree.items() if d.type == "circuit"),
            key=lambda i: (_first_tab(self.tree[i].value("info/spaces")), i),
        )
        for n, cid in enumerate(circuits, start=1):
            out[cid] = f"circuit-{n}"
        for device_id, device in self.tree.items():
            if device_id in out:
                continue
            kind = device.type
            if kind == "lugs":
                out[device_id] = device_id if device_id.startswith("lugs-") else _lugs_id(device)
            else:
                counters[kind] = counters.get(kind, 0) + 1
                out[device_id] = f"{kind}-{counters[kind]}"
        return out

    def _serial(self, entity_class: str, device_id: str, raw: str | None) -> str | None:
        if raw is None or not self.mask:
            return raw
        return f"MASKED-{self.ids[device_id].upper()}"

    def _name(self, device_id: str, raw: str) -> str:
        """A display name, replaced by the masked ID if it embeds an original ID
        or serial."""
        if self.mask and any(secret.lower() in raw.lower() for secret in self.secrets):
            return self.ids[device_id]
        return raw

    def note(self, device_id: str, key: str, text: str) -> None:
        self.notes.append(CaptureNote(self.ids.get(device_id, device_id), key, text))

    # -- run -------------------------------------------------------------

    def run(self, variant: Variant | None) -> tuple[PanelDefinition, list[CaptureNote]]:
        panel = self.tree[self.panel_id]
        chosen = variant or _infer_variant(panel)
        feeds = self._feeds()
        instances = [self._panel()]
        bess_configs: list[BESSConfig] = []
        for device_id in self._tree_order():
            device = self.tree[device_id]
            kind = device.type
            if kind == "circuit":
                instances.append(self._circuit(device_id, device))
            elif kind == "lugs":
                instances.append(self._lugs(device_id, device))
            elif kind == "bess":
                inst, cfg = self._bess(device_id, device, feeds)
                instances.append(inst)
                bess_configs.append(cfg)
            elif kind == "pv":
                instances.append(self._pv(device_id, device, feeds))
            elif kind == "evse":
                instances.append(self._evse(device_id, device, feeds))
            elif kind == "mid":
                instances.append(self._mid(device_id, device))
            elif kind != "distribution-enclosure":
                self.note(device_id, "type", f"unsupported device type {kind!r}; skipped")
        return (
            PanelDefinition(
                manifest=DeviceManifest(instances=tuple(instances)),
                variant=chosen,
                bess_configs=tuple(bess_configs),
            ),
            self.notes,
        )

    def _tree_order(self) -> list[str]:
        """The panel's device tree, breadth-first in each ``children`` order, so
        the rebuilt panel lists its children as the captured one does. Other
        devices on the broker are not part of it."""
        order: list[str] = []
        queue = [self.panel_id]
        while queue:
            device_id = queue.pop(0)
            if device_id in order or device_id not in self.tree:
                continue
            order.append(device_id)
            queue.extend(self.tree[device_id].description.get("children", []))
        return order

    def _feeds(self) -> dict[str, str]:
        """DER device ID -> the published ID of the circuit or lugs feeding it."""
        out: dict[str, str] = {}
        for device_id, device in self.tree.items():
            target = device.value("connection/feeds-device-id")
            if target is not None and device.type == "circuit":
                out[target] = self.ids[device_id]
        return out

    # -- per class -------------------------------------------------------

    def _panel(self) -> DeviceInstance:
        pid, d = self.panel_id, self.tree[self.panel_id]
        md: dict[str, str] = {}
        self._put(md, pid, "vendor-name", d.value("info/vendor-name"), "unknown")
        md["serial-number"] = (
            self._serial("panel", pid, d.value("info/serial-number")) or (self.ids[pid])
        )
        self._put(md, pid, "firmware-version", d.value("info/firmware-version"), "unknown")
        self._put(md, pid, "hardware-version", d.value("info/hardware-version"), "unknown")
        model = d.value("info/model") or "UNKNOWN"
        md["panel-model"] = model
        tabs = [
            t
            for dev in self.tree.values()
            if dev.type == "circuit"
            for t in _tabs(dev.value("info/spaces"))
        ]
        size = re.search(r"(\d+)$", model)
        if size:
            md["panel-size"] = size.group(1)
        else:
            md["panel-size"] = str(max(tabs, default=40))
            self.note(pid, "panel-size", f"not published; using {md['panel-size']}")
        rating = d.value("breaker/rating")
        if rating is not None:
            md["main-breaker-rating-a"] = _int_str(rating)
        postal = d.value("status/postal-code")
        md["postal-code"] = "00000" if self.mask or postal is None else postal
        if postal is None:
            self.note(pid, "postal-code", "not published; using 00000")
        self._put(md, pid, "time-zone", d.value("status/time-zone"), "UTC")
        voltage = d.value("meter/voltage-a")
        if voltage is not None and float(voltage) > 0:
            md["line-voltage-v"] = voltage
            md["service-voltage-v"] = str(2 * float(voltage))
        if any(dev.type == "mid" for dev in self.tree.values()):
            md["islandable"] = "true"
        return DeviceInstance(
            "panel", self.ids[pid], self._name(pid, str(d.description.get("name", pid))), md
        )

    def _circuit(self, device_id: str, d: Device) -> DeviceInstance:
        name = d.value("info/name") or str(d.description.get("name", device_id))
        tabs = _tabs(d.value("info/spaces"))
        if not tabs:
            width = 2 if d.value("breaker/poles") == "2" else 1
            tabs = list(range(self.next_tab, self.next_tab + width))
            self.next_tab += width
            self.note(device_id, "tab-numbers", f"info/spaces not published; using {tabs}")
        md: dict[str, str] = {
            "tab-numbers": ",".join(str(t) for t in tabs),
            "placement": "upstream-of-lugs",
        }
        rating = d.value("breaker/rating")
        if rating is None:
            md["breaker-rating-a"] = "20"
            self.note(device_id, "breaker-rating-a", "not published; using 20")
        else:
            md["breaker-rating-a"] = rating
        poles = d.value("breaker/poles")
        if poles is not None:
            md["dipole"] = "true" if int(float(poles)) > 1 else "false"
        priority = d.value("load-shed/priority")
        if priority is None:
            priority = "NEVER"
            self.note(device_id, "default-priority", "not published; using NEVER")
        elif priority == "UNKNOWN":
            # No manifest value publishes UNKNOWN except the legacy REST-era
            # priorities, which all do.
            priority = "NICE_TO_HAVE"
            self.note(device_id, "default-priority", "published UNKNOWN; using NICE_TO_HAVE")
        md["default-priority"] = priority
        locked = d.value("switch/relay-controllable") == "false"
        md["relay-behavior"] = "non-controllable" if locked else "controllable"
        # A never-backup circuit publishes OFF_GRID with no $settable, on a relay
        # that is otherwise controllable.
        priority_settable = bool((d.declaration("load-shed/priority") or {}).get("settable"))
        if (
            not locked
            and priority == "OFF_GRID"
            and d.declares("load-shed/priority")
            and not priority_settable
        ):
            md["never-backup"] = "true"
        pcs = d.value("pcs/priority")
        if pcs is not None:
            md["pcs-priority"] = _int_str(pcs)
        # Hosted-circuit registers are enclosure-framed: consumption accumulates
        # exported-energy, backfeed accumulates imported-energy.
        if (consumed := d.value("meter/exported-energy")) is not None:
            md["initial-consumed-wh"] = consumed
        if (produced := d.value("meter/imported-energy")) is not None:
            md["initial-produced-wh"] = produced
        return DeviceInstance("circuit", self.ids[device_id], self._name(device_id, name), md)

    def _lugs(self, device_id: str, d: Device) -> DeviceInstance:
        direction = (d.value("info/direction") or "").lower()
        if direction not in ("upstream", "downstream"):
            direction = "downstream" if "down" in device_id else "upstream"
            self.note(device_id, "direction", f"not published; using {direction}")
        md = {"direction": direction}
        if d.value("connection/fed-by-device-type") == _TYPE_PREFIX + "distribution-enclosure":
            self.note(
                device_id,
                "fed-by",
                "fed by another distribution enclosure, which a definition cannot express",
            )
        return DeviceInstance("lugs", self.ids[device_id], f"{direction.title()} lugs", md)

    def _identity(self, entity_class: str, device_id: str, d: Device) -> dict[str, str]:
        md: dict[str, str] = {}
        for key in ("vendor-name", "model", "part-number", "firmware-version"):
            if (value := d.value(f"info/{key}")) is not None:
                md[key] = value
        serial = self._serial(entity_class, device_id, d.value("info/serial-number"))
        if serial is not None:
            md["serial-number"] = serial
        return md

    def _bess(
        self, device_id: str, d: Device, feeds: dict[str, str]
    ) -> tuple[DeviceInstance, BESSConfig]:
        md = self._identity("bess", device_id, d)
        md.setdefault("vendor-name", "unknown")
        capacity = d.value("info/nameplate-capacity")
        if capacity is None:
            capacity = "13.5"
            self.note(device_id, "nameplate-capacity-kwh", "not published; using 13.5")
        md["nameplate-capacity-kwh"] = capacity
        upstream = any(
            dev.type == "lugs" and dev.value("connection/fed-by-device-id") == device_id
            for dev in self.tree.values()
        )
        if upstream:
            md["relative-position"] = "UPSTREAM"
        elif device_id in feeds:
            md["feed"] = feeds[device_id]
        if (soe := d.value("soc/soe")) is not None:
            md["initial-soe-kwh"] = soe
        self.note(
            device_id,
            "bess dispatch",
            "charge/discharge limits and charge mode are not published; using 5000 W "
            "each way, self-consumption",
        )
        soc = d.value("soc/soc")
        config = BESSConfig(
            instance_id=self.ids[device_id],
            nameplate_capacity_kwh=float(capacity),
            max_charge_w=5000.0,
            max_discharge_w=5000.0,
            initial_soc_pct=float(soc) if soc is not None else 50.0,
        )
        name = self._name(device_id, str(d.description.get("name", "Battery")))
        return DeviceInstance("bess", self.ids[device_id], name, md), config

    def _pv(self, device_id: str, d: Device, feeds: dict[str, str]) -> DeviceInstance:
        md = self._identity("pv", device_id, d)
        md.setdefault("vendor-name", "unknown")
        power = d.value("info/nominal-power")
        if power is None:
            power = "5000"
            self.note(device_id, "nominal-power-w", "not published; using 5000")
        md["nominal-power-w"] = power
        md["inverter-type"] = "ac-coupled"
        self.note(device_id, "inverter-type", "not published; using ac-coupled")
        md["relative-position"] = "IN_PANEL"
        if device_id in feeds:
            md["feed"] = feeds[device_id]
        return DeviceInstance(
            "pv",
            self.ids[device_id],
            self._name(device_id, str(d.description.get("name", "PV"))),
            md,
        )

    def _evse(self, device_id: str, d: Device, feeds: dict[str, str]) -> DeviceInstance:
        md = self._identity("evse", device_id, d)
        for key in ("vendor-name", "model", "part-number", "firmware-version"):
            if key not in md:
                md[key] = "unknown"
                self.note(device_id, key, "not published; using unknown")
        md.setdefault("serial-number", self._serial("evse", device_id, device_id) or device_id)
        current = d.value("config/max-charge-current") or d.value("meter/advertised-current")
        if current is None:
            current = "32"
            self.note(device_id, "max-current-a", "not published; using 32")
        md["max-current-a"] = current
        if device_id in feeds:
            md["feed"] = feeds[device_id]
        name = self._name(device_id, str(d.description.get("name", "EV Charger")))
        return DeviceInstance("evse", self.ids[device_id], name, md)

    def _mid(self, device_id: str, d: Device) -> DeviceInstance:
        md = self._identity("mid", device_id, d)
        if (hw := d.value("info/hardware-version")) is not None:
            md["hardware-version"] = hw
        name = self._name(device_id, str(d.description.get("name", "MID")))
        return DeviceInstance("mid", self.ids[device_id], name, md)

    # -- helpers ---------------------------------------------------------

    def _put(
        self, md: dict[str, str], device_id: str, key: str, value: str | None, default: str
    ) -> None:
        if value is None:
            md[key] = default
            self.note(device_id, key, f"not published; using {default}")
        else:
            md[key] = value


def _infer_variant(panel: Device) -> Variant:
    if "span" in (panel.value("info/vendor-name") or "").lower():
        return "span"
    return "reference"


def _tabs(spaces: str | None) -> list[int]:
    return [int(t) for t in (spaces or "").split(",") if t.strip().isdigit()]


def _first_tab(spaces: str | None) -> int:
    tabs = _tabs(spaces)
    return tabs[0] if tabs else 1 << 30


def _int_str(value: str) -> str:
    try:
        return str(int(float(value)))
    except ValueError:
        return value


def _lugs_id(device: Device) -> str:
    direction = (device.value("info/direction") or "upstream").lower()
    return f"lugs-{direction}"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="panel-sim-capture",
        description="Generate a panel-sim definition file from a published panel tree.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--host", help="MQTT broker host (live capture)")
    source.add_argument("--from-snapshot", type=Path, help="a tree-v1 snapshot JSON file")
    parser.add_argument("--port", type=int, default=8883)
    parser.add_argument("--username")
    parser.add_argument("--password")
    parser.add_argument("--no-tls", action="store_true", help="plaintext MQTT")
    parser.add_argument("--cafile", help="CA certificate to verify the broker against")
    parser.add_argument(
        "--insecure", action="store_true", help="skip broker certificate verification"
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="live capture limit, s")
    parser.add_argument("--variant", choices=get_args(Variant))
    parser.add_argument("--no-mask", action="store_true", help="keep serials, IDs and site")
    parser.add_argument("--record", type=int, default=0, help="live samples to record as ticks")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between samples")
    parser.add_argument("--ticks-output", type=Path, help="where --record writes its ticks")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.record and (args.from_snapshot is not None or args.ticks_output is None):
        parser.error("--record needs a live capture (--host) and --ticks-output")

    samples: list[tuple[float, Tree]] = []
    if args.from_snapshot is not None:
        tree = tree_from_snapshot(json.loads(args.from_snapshot.read_text()))
    else:
        tree, samples = capture_live(
            args.host,
            args.port,
            username=args.username,
            password=args.password,
            use_tls=not args.no_tls,
            tls_insecure=args.insecure,
            tls_ca_cert=args.cafile,
            timeout_s=args.timeout,
            record=args.record,
            interval_s=args.interval,
        )
    definition, notes = definition_from_tree(tree, variant=args.variant, mask=not args.no_mask)
    dump_definition(definition, args.output)
    if samples:
        dump_ticks(ticks_from_samples(tree, samples, mask=not args.no_mask), args.ticks_output)
        print(f"wrote {args.ticks_output}: {len(samples)} ticks", file=sys.stderr)
    for n in notes:
        print(f"{n.device}: {n.key}: {n.note}", file=sys.stderr)
    print(
        f"wrote {args.output}: {len(definition.manifest.instances)} devices, "
        f"variant {definition.variant}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
