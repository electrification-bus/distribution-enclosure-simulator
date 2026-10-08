"""Load device profiles by hydrating a light selection against the vendored spec catalogs.

The base ``profiles/*.json`` are *selections*: per device, which capabilities + which
property keys it publishes, each carrying its presentation ``name`` (+ optional
``settable`` override, or a ``datatype`` override for a legal narrowing). A property's
``datatype``/``unit``/``format`` come from the vendored spec capability catalogs
(``catalogs/*.json``, copies of ``../specification`` ``capabilities/*.json``), so the
wire type contract is single-sourced from the spec and cannot silently drift. A
property with no spec-catalog home (pv ``nominal-power``, the SPAN-vendor extras)
carries a full inline definition instead, which hydration uses as-is.

``variant='span'`` (the default) deep-merges the ``profiles/span/*.json`` overlay (the
SPAN-vendor-specific surface + conformance-latitude overrides) onto the base, where an
overlay ``null`` removes a base capability or property; ``variant='reference'`` loads the
spec-conformant base only.

A numeric property may carry a ``literal`` entry, ``"integer"``, ``"<n>dp"`` or
``"shortest"``: the form its numbers are written in on the wire
(``ebus_panel_sim.wire.literal``). The span overlay gives one to every number the span
variant publishes; a property without one is published as its value converts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ebus_panel_sim.exceptions import ProfileValidationError
from ebus_panel_sim.wire.literal import LiteralForm, parse_literal_form

_DEFAULT_DIR = Path(__file__).parent / "profiles"
_CATALOG_DIR = Path(__file__).parent / "catalogs"
_OVERLAY_SUBDIR = "span"

Variant = Literal["span", "reference"]


@dataclass(frozen=True, slots=True)
class ProfileProperty:
    name: str
    datatype: str
    unit: str | None
    format: str | None
    settable: bool
    literal: LiteralForm | None = None


@dataclass(frozen=True, slots=True)
class ProfileCapability:
    type: str
    properties: dict[str, ProfileProperty]


@dataclass(frozen=True, slots=True)
class Profile:
    entity_class: str
    version: int
    type: str
    capabilities: dict[str, ProfileCapability]

    def settable_properties(self) -> list[tuple[str, str]]:
        """Return [(capability, property_key), ...] for every settable=True property."""
        out: list[tuple[str, str]] = []
        for cap_name, cap in self.capabilities.items():
            for prop_key, prop in cap.properties.items():
                if prop.settable:
                    out.append((cap_name, prop_key))
        return out


class ProfileTable(dict[str, Profile]):
    """Mapping of entity_class → Profile."""


def load_profiles(
    directory: Path = _DEFAULT_DIR,
    *,
    variant: Variant = "span",
    catalog_dir: Path = _CATALOG_DIR,
) -> ProfileTable:
    """Hydrate the base selections against the vendored catalogs, merging the SPAN
    overlay when ``variant='span'``."""
    catalogs = _load_catalogs(catalog_dir)
    overlay_dir = directory / _OVERLAY_SUBDIR
    table = ProfileTable()
    for path in sorted(directory.glob("*.json")):  # base only; the span/ subdir is not matched
        entity_class = path.stem
        raw = json.loads(path.read_text())
        if variant == "span":
            overlay_path = overlay_dir / f"{entity_class}.json"
            if overlay_path.exists():
                raw = _merge_overlay(raw, json.loads(overlay_path.read_text()), overlay_path)
        table[entity_class] = _hydrate_profile(entity_class, path, raw, catalogs)
    return table


def _load_catalogs(directory: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Load vendored capability catalogs → {capability_type: {property_key: definition}},
    with ``property_patterns`` expanded into concrete keys (voltage-{a,b,c} → voltage-a…)."""
    catalogs: dict[str, dict[str, dict[str, Any]]] = {}
    if not directory.is_dir():
        return catalogs
    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text())
        cap_type = raw["capability"]
        props: dict[str, dict[str, Any]] = {
            k: dict(v) for k, v in raw.get("properties", {}).items()
        }
        for pattern, body in raw.get("property_patterns", {}).items():
            props.update(_expand_pattern(pattern, body))
        catalogs[cap_type] = props
    return catalogs


def _expand_pattern(pattern: str, body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tokens = body.get("expand", [])
    prefix = pattern[: pattern.index("{")] if "{" in pattern else pattern
    suffix = pattern[pattern.index("}") + 1 :] if "}" in pattern else ""
    slim = {k: v for k, v in body.items() if k != "expand"}
    return {f"{prefix}{t}{suffix}": dict(slim) for t in tokens}


def _merge_overlay(base: dict[str, Any], overlay: dict[str, Any], path: Path) -> dict[str, Any]:
    """Deep-merge a partial overlay profile onto a base profile (raw dicts).

    An overlay capability absent from the base is added whole; one that exists has
    its properties merged in, overlay entries overriding the base entry key-by-key
    (so an overlay can flip ``settable`` on an existing property without restating
    its definition). A ``null`` capability or property removes it from the base, as
    in JSON Merge Patch (RFC 7396); removing one the base lacks, or every property
    of a capability, is an error. Top-level ``$version``/``type`` stay the base's."""
    merged: dict[str, Any] = {**base, "capabilities": {}}
    for cap_name, cap in base.get("capabilities", {}).items():
        merged["capabilities"][cap_name] = {**cap, "properties": dict(cap.get("properties", {}))}
    for cap_name, ov_cap in overlay.get("capabilities", {}).items():
        if ov_cap is None:
            if merged["capabilities"].pop(cap_name, None) is None:
                raise ProfileValidationError(
                    f"{path}: removes capability {cap_name!r}, which the base lacks"
                )
            continue
        target = merged["capabilities"].setdefault(cap_name, {**ov_cap, "properties": {}})
        for prop_key, ov_prop in ov_cap.get("properties", {}).items():
            if ov_prop is None:
                if target["properties"].pop(prop_key, None) is None:
                    raise ProfileValidationError(
                        f"{path}: removes {cap_name}/{prop_key}, which the base lacks"
                    )
                continue
            target["properties"][prop_key] = {**target["properties"].get(prop_key, {}), **ov_prop}
        if not target["properties"]:
            raise ProfileValidationError(
                f"{path}: leaves capability {cap_name!r} with no properties; "
                "remove the capability instead"
            )
    return merged


def _hydrate_profile(
    entity_class: str,
    path: Path,
    raw: dict[str, Any],
    catalogs: dict[str, dict[str, dict[str, Any]]],
) -> Profile:
    if "$version" not in raw or "type" not in raw or "capabilities" not in raw:
        raise ProfileValidationError(f"profile {path} missing required top-level keys")
    capabilities: dict[str, ProfileCapability] = {}
    for cap_name, cap in raw["capabilities"].items():
        cap_type = cap["type"]
        catalog = catalogs.get(cap_type, {})
        properties = {
            prop_key: _hydrate_property(path, cap_name, prop_key, sel, catalog.get(prop_key))
            for prop_key, sel in cap["properties"].items()
        }
        capabilities[cap_name] = ProfileCapability(type=cap_type, properties=properties)
    return Profile(
        entity_class=entity_class,
        version=raw["$version"],
        type=raw["type"],
        capabilities=capabilities,
    )


def _resolve_format(sel: dict[str, Any], catalog_def: dict[str, Any] | None) -> str | None:
    """Resolve a property's Homie ``$format`` string.

    A selection (or its catalog entry) may carry ``format`` as a ready wire
    string (e.g. an enum's ``"A,B,C"``), or ``format_json`` as a JSON object
    (e.g. the shed/policy JSONSchema) that is serialized to the minified Homie
    form. The selection wins over the catalog; within either, ``format`` wins
    over ``format_json``."""
    for source in (sel, catalog_def):
        if source is None:
            continue
        fmt = source.get("format")
        if isinstance(fmt, str):
            return fmt
        fmt_json = source.get("format_json")
        if fmt_json is not None:
            return json.dumps(fmt_json, separators=(",", ":"))
    return None


def _hydrate_property(
    path: Path, cap_name: str, key: str, sel: dict[str, Any], catalog_def: dict[str, Any] | None
) -> ProfileProperty:
    """Resolve a property entry to a ProfileProperty.

    An entry carrying its own ``datatype`` is a self-contained definition (a legacy
    full profile, or a non-cataloged property like pv ``nominal-power`` or a SPAN
    extra) and the catalog is ignored. An entry WITHOUT a ``datatype`` is a light
    selection: ``datatype``/``unit``/``format``/``settable`` come from the catalog,
    with ``name`` (and any explicit ``settable`` override) taken from the selection.
    A selection with no catalog home is an error."""
    if "datatype" in sel:
        datatype = sel["datatype"]
        return ProfileProperty(
            name=sel["name"],
            datatype=datatype,
            unit=sel.get("unit"),
            format=_resolve_format(sel, None),
            settable=bool(sel.get("settable", False)),
            literal=_literal(path, cap_name, key, sel, datatype),
        )
    if catalog_def is None:
        raise ProfileValidationError(
            f"{path}: {cap_name}/{key} is selected but absent from the "
            "catalog and has no inline datatype"
        )
    datatype = catalog_def["datatype"]
    return ProfileProperty(
        name=sel.get("name") or catalog_def.get("name") or key,
        datatype=datatype,
        unit=sel.get("unit", catalog_def.get("unit")),
        format=_resolve_format(sel, catalog_def),
        settable=bool(sel.get("settable", catalog_def.get("settable", False))),
        literal=_literal(path, cap_name, key, sel, datatype),
    )


def _literal(
    path: Path, cap_name: str, key: str, sel: dict[str, Any], datatype: str
) -> LiteralForm | None:
    """The selection's ``literal`` form, which only an integer or float property
    may carry, and an integer property only as ``"integer"``."""
    if "literal" not in sel:
        return None
    form = parse_literal_form(sel["literal"])
    if form is None:
        raise ProfileValidationError(
            f"{path}: {cap_name}/{key} literal must be 'integer', '<n>dp' or 'shortest', "
            f"got {sel['literal']!r}"
        )
    if datatype not in ("integer", "float") or (datatype == "integer" and form.kind != "integer"):
        raise ProfileValidationError(
            f"{path}: {cap_name}/{key} literal {sel['literal']!r} does not fit a "
            f"{datatype} property"
        )
    return form
