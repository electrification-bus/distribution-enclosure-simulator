"""A panel definition: everything that fixes a simulated panel's makeup, as one file.

The manifest, the variant, the native BESS configs and the load-shedding config
together determine what an ``Emitter`` publishes for a given stream of
``TickInputs``. ``load_definition`` / ``dump_definition`` read and write them as
YAML, and ``Emitter.from_definition`` builds an emitter from one.
``dump_ticks`` / ``load_ticks`` do the same for a recorded sequence of
``TickInputs`` (``schema: panel-sim-ticks/1``).

File shape (``schema: panel-sim-definition/1``)::

    schema: panel-sim-definition/1
    variant: span
    devices:
      - class: panel
        id: abc-123
        name: Span Panel
        metadata: {serial-number: abc-123, ...}
    bess:
      - {instance_id: abc-123-bess, nameplate_capacity_kwh: 13.5, ...}
    load_shedding: {soc_threshold_pct: 20.0}

``bess`` and ``load_shedding`` are optional. Metadata values are written as
strings; on load a YAML scalar is converted to the string the manifest parser
expects (``true`` becomes ``"true"``, ``200`` becomes ``"200"``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar, get_args, get_origin, get_type_hints

import yaml

from ebus_panel_sim.exceptions import ManifestValidationError
from ebus_panel_sim.manifest import DeviceInstance, DeviceManifest
from ebus_panel_sim.native_devices import BESSConfig, LoadSheddingConfig
from ebus_panel_sim.tick_inputs import BESSCommunication, TickInputs
from ebus_panel_sim.wire.profile_loader import Variant

SCHEMA = "panel-sim-definition/1"
TICKS_SCHEMA = "panel-sim-ticks/1"

_Config = TypeVar("_Config", BESSConfig, LoadSheddingConfig)

_TOP_KEYS = frozenset({"schema", "variant", "devices", "bess", "load_shedding"})
_DEVICE_KEYS = frozenset({"class", "id", "name", "metadata"})


@dataclass(frozen=True, slots=True)
class PanelDefinition:
    """A simulated panel's makeup: its manifest and the emitter options that go
    with it."""

    manifest: DeviceManifest
    variant: Variant = "span"
    bess_configs: tuple[BESSConfig, ...] = ()
    load_shedding: LoadSheddingConfig | None = None


def load_definition(path: Path | str) -> PanelDefinition:
    """Read a panel definition file.

    Every scalar is read as written, with no YAML type resolution, so metadata
    such as ``postal-code: 02134`` or ``hardware-version: 2.10`` keeps its exact
    text; config fields are then converted to their declared types."""
    return definition_from_dict(_read_yaml(path), where=str(path))


def dump_definition(definition: PanelDefinition, path: Path | str) -> None:
    """Write a panel definition file."""
    Path(path).write_text(
        yaml.safe_dump(definition_to_dict(definition), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _read_yaml(path: Path | str) -> object:
    # BaseLoader resolves no implicit types: every scalar is a string.
    return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def dump_ticks(ticks: Sequence[TickInputs], path: Path | str) -> None:
    """Write a tick recording (``schema: panel-sim-ticks/1``): each tick's time,
    grid flag, and circuit and EVSE powers."""
    body = {
        "schema": TICKS_SCHEMA,
        "ticks": [
            {
                "current_time": t.current_time,
                "grid_online": t.grid_online,
                "circuits": dict(t.circuits),
                "evse": dict(t.evse),
                **(
                    {"bess_communication": dict(t.bess_communication)}
                    if t.bess_communication
                    else {}
                ),
            }
            for t in ticks
        ],
    }
    Path(path).write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def load_ticks(path: Path | str) -> list[TickInputs]:
    """Read a tick recording written by ``dump_ticks``."""
    where = str(path)
    raw = _read_yaml(path)
    if not isinstance(raw, dict) or raw.get("schema") != TICKS_SCHEMA:
        raise ManifestValidationError(f"{where}: schema must be {TICKS_SCHEMA!r}")
    _no_unknown_keys(raw, frozenset({"schema", "ticks"}), where)
    entries = raw.get("ticks") or []
    if not isinstance(entries, list):
        raise ManifestValidationError(f"{where}: ticks must be a list")
    return [_tick(entry, f"{where}: ticks[{i}]") for i, entry in enumerate(entries)]


_TICK_KEYS = frozenset({"current_time", "grid_online", "circuits", "evse", "bess_communication"})


def _tick(entry: object, at: str) -> TickInputs:
    if not isinstance(entry, dict):
        raise ManifestValidationError(f"{at}: must be a mapping")
    _no_unknown_keys(entry, _TICK_KEYS, at)
    for key in ("current_time", "grid_online"):
        if key not in entry:
            raise ManifestValidationError(f"{at}: missing {key!r}")
    links = _mapping(entry.get("bess_communication"), at, "bess_communication")
    states = get_args(BESSCommunication)
    for battery, state in links.items():
        if state not in states:
            raise ManifestValidationError(
                f"{at}: bess_communication[{battery!r}] must be one of {list(states)}, "
                f"got {state!r}"
            )
    return TickInputs(
        current_time=_coerce(entry["current_time"], float, at, "current_time"),
        grid_online=_coerce(entry["grid_online"], bool, at, "grid_online"),
        circuits={
            str(k): _coerce(v, float, at, f"circuits[{k!r}]")
            for k, v in _mapping(entry.get("circuits"), at, "circuits").items()
        },
        evse={
            str(k): _coerce(v, float, at, f"evse[{k!r}]")
            for k, v in _mapping(entry.get("evse"), at, "evse").items()
        },
        bess_communication={str(k): v for k, v in links.items()},
    )


def _mapping(value: object, at: str, key: str) -> dict[Any, Any]:
    if value is None or value == "":
        return {}
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{at}: {key} must be a mapping")
    return value


def definition_to_dict(definition: PanelDefinition) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema": SCHEMA,
        "variant": definition.variant,
        "devices": [
            {
                "class": inst.entity_class,
                "id": inst.instance_id,
                "name": inst.display_name,
                "metadata": dict(inst.metadata),
            }
            for inst in definition.manifest.instances
        ],
    }
    if definition.bess_configs:
        out["bess"] = [_config_to_dict(cfg) for cfg in definition.bess_configs]
    if definition.load_shedding is not None:
        out["load_shedding"] = _config_to_dict(definition.load_shedding)
    return out


def definition_from_dict(raw: object, *, where: str = "definition") -> PanelDefinition:
    if not isinstance(raw, dict):
        raise ManifestValidationError(f"{where}: must be a mapping")
    if raw.get("schema") != SCHEMA:
        raise ManifestValidationError(
            f"{where}: schema must be {SCHEMA!r}, got {raw.get('schema')!r}"
        )
    _no_unknown_keys(raw, _TOP_KEYS, where)
    variant = raw.get("variant", "span")
    if variant not in get_args(Variant):
        raise ManifestValidationError(
            f"{where}: variant must be one of {list(get_args(Variant))}, got {variant!r}"
        )
    devices = raw.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ManifestValidationError(f"{where}: devices must be a non-empty list")
    bess = raw.get("bess") or []
    if not isinstance(bess, list):
        raise ManifestValidationError(f"{where}: bess must be a list")
    shedding = raw.get("load_shedding") or None
    return PanelDefinition(
        manifest=DeviceManifest(
            instances=tuple(_device(d, f"{where}: devices[{i}]") for i, d in enumerate(devices))
        ),
        variant=variant,
        bess_configs=tuple(
            _config(BESSConfig, b, f"{where}: bess[{i}]") for i, b in enumerate(bess)
        ),
        load_shedding=(
            None
            if shedding is None
            else _config(LoadSheddingConfig, shedding, f"{where}: load_shedding")
        ),
    )


def _device(raw: object, where: str) -> DeviceInstance:
    if not isinstance(raw, dict):
        raise ManifestValidationError(f"{where}: must be a mapping")
    _no_unknown_keys(raw, _DEVICE_KEYS, where)
    for key in ("class", "id"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ManifestValidationError(f"{where}: {key!r} must be a non-empty string")
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ManifestValidationError(f"{where}: metadata must be a mapping")
    return DeviceInstance(
        entity_class=raw["class"],
        instance_id=raw["id"],
        display_name=str(raw.get("name", raw["id"])),
        metadata={str(k): _metadata_str(v, where, str(k)) for k, v in metadata.items()},
    )


def _metadata_str(value: object, where: str, key: str) -> str:
    """A metadata value as the string the manifest expects. A file's scalars are
    already strings; a value built in Python may be a bool or a number."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise ManifestValidationError(f"{where}: metadata {key!r} must be a scalar, got {value!r}")


def _config_to_dict(config: BESSConfig | LoadSheddingConfig) -> dict[str, Any]:
    return {
        f.name: list(v) if isinstance(v := getattr(config, f.name), tuple) else v
        for f in dataclasses.fields(config)
    }


def _config(cls: type[_Config], raw: object, where: str) -> _Config:
    if not isinstance(raw, dict):
        raise ManifestValidationError(f"{where}: must be a mapping")
    hints = get_type_hints(cls)
    _no_unknown_keys(raw, frozenset(f.name for f in dataclasses.fields(cls)), where)
    kwargs = {str(k): _coerce(v, hints[str(k)], where, str(k)) for k, v in raw.items()}
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ManifestValidationError(f"{where}: {exc}") from exc


def _coerce(value: Any, hint: Any, where: str, key: str) -> Any:
    """``value`` (a string from a file, or a Python value) as type ``hint``."""
    origin = get_origin(hint)
    try:
        if hint is bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"
            raise ValueError
        if hint in (int, float) and isinstance(value, bool):
            raise ValueError
        if hint is float:
            return float(value)
        if hint is int:
            return int(value) if isinstance(value, (int, str)) else _exact_int(value)
        if hint is str:
            if isinstance(value, str):
                return value
            raise ValueError
        if origin is Literal:
            if value in get_args(hint):
                return value
            raise ManifestValidationError(
                f"{where}: {key} must be one of {list(get_args(hint))}, got {value!r}"
            )
        if origin is tuple:
            if not isinstance(value, (list, tuple)):
                raise ValueError
            return tuple(_coerce(v, get_args(hint)[0], where, key) for v in value)
    except (TypeError, ValueError) as exc:
        raise ManifestValidationError(
            f"{where}: {key} must be {getattr(hint, '__name__', hint)}, got {value!r}"
        ) from exc
    return value


def _exact_int(value: object) -> int:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError


def _no_unknown_keys(raw: dict[Any, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(k) for k in raw if k not in allowed)
    if unknown:
        raise ManifestValidationError(f"{where}: unknown keys {unknown}")
