"""A panel definition: everything that fixes a simulated panel's makeup, as one file.

The manifest, the variant, the native BESS configs and the load-shedding config
together determine what an ``Emitter`` publishes for a given stream of
``TickInputs``. ``load_definition`` / ``dump_definition`` read and write them as
YAML, and ``Emitter.from_definition`` builds an emitter from one.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, get_args

import yaml

from ebus_panel_sim.exceptions import ManifestValidationError
from ebus_panel_sim.manifest import DeviceInstance, DeviceManifest
from ebus_panel_sim.native_devices import BESSConfig, ChargeMode, LoadSheddingConfig
from ebus_panel_sim.wire.profile_loader import Variant

SCHEMA = "panel-sim-definition/1"

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
    """Read a panel definition file."""
    raw = yaml.safe_load(Path(path).read_text())
    return definition_from_dict(raw, where=str(path))


def dump_definition(definition: PanelDefinition, path: Path | str) -> None:
    """Write a panel definition file."""
    Path(path).write_text(
        yaml.safe_dump(definition_to_dict(definition), sort_keys=False, allow_unicode=True)
    )


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
    _no_unknown_keys(raw, _TOP_KEYS, where)
    if raw.get("schema") != SCHEMA:
        raise ManifestValidationError(
            f"{where}: schema must be {SCHEMA!r}, got {raw.get('schema')!r}"
        )
    variant = raw.get("variant", "span")
    if variant not in get_args(Variant):
        raise ManifestValidationError(
            f"{where}: variant must be one of {list(get_args(Variant))}, got {variant!r}"
        )
    devices = raw.get("devices")
    if not isinstance(devices, list) or not devices:
        raise ManifestValidationError(f"{where}: devices must be a non-empty list")
    bess = raw.get("bess", [])
    if not isinstance(bess, list):
        raise ManifestValidationError(f"{where}: bess must be a list")
    shedding = raw.get("load_shedding")
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
        metadata={str(k): _metadata_str(v) for k, v in metadata.items()},
    )


def _metadata_str(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _config_to_dict(config: BESSConfig | LoadSheddingConfig) -> dict[str, Any]:
    return {
        f.name: list(v) if isinstance(v := getattr(config, f.name), tuple) else v
        for f in dataclasses.fields(config)
    }


def _config(cls: type[_Config], raw: object, where: str) -> _Config:
    if not isinstance(raw, dict):
        raise ManifestValidationError(f"{where}: must be a mapping")
    names = {f.name for f in dataclasses.fields(cls)}
    _no_unknown_keys(raw, frozenset(names), where)
    kwargs: dict[str, Any] = {
        str(k): tuple(v) if isinstance(v, list) else v for k, v in raw.items()
    }
    mode = kwargs.get("charge_mode")
    if mode is not None and mode not in get_args(ChargeMode):
        raise ManifestValidationError(
            f"{where}: charge_mode must be one of {list(get_args(ChargeMode))}, got {mode!r}"
        )
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ManifestValidationError(f"{where}: {exc}") from exc


def _no_unknown_keys(raw: dict[Any, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(k) for k in raw if k not in allowed)
    if unknown:
        raise ManifestValidationError(f"{where}: unknown keys {unknown}")
