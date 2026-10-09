"""The off-grid import limit is commissioned state, never a constant.

Every public and reference capture shows the same rule: without a commissioned
enablement a panel publishes none of the three off-grid properties (both MAIN 32
captures), with one it publishes the enablement and ``-active``, and it values the
limit only while ENABLED (47.9 A on the one capture that has it)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from ebus_panel_sim import (
    DeviceManifest,
    Emitter,
    ManifestValidationError,
    PanelDefinition,
    SetterRegistry,
    load_definition,
    load_ticks,
)
from ebus_panel_sim.capture import definition_from_tree, tree_from_retained

from .conftest import PahoRecorder

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_PANEL = "ebus/5/example-40t-001/pcs"


def _example(**md: str) -> PanelDefinition:
    example = load_definition(_EXAMPLES / "forty_tab_minimal.yaml")
    return dataclasses.replace(
        example,
        manifest=DeviceManifest(
            instances=tuple(
                dataclasses.replace(i, metadata={**i.metadata, **md})
                if i.entity_class == "panel"
                else i
                for i in example.manifest.instances
            )
        ),
    )


def _published(rec: PahoRecorder, definition: PanelDefinition) -> dict[str, str]:
    emitter = Emitter.from_definition(definition, SetterRegistry())
    emitter.start()
    emitter.publish_tick(load_ticks(_EXAMPLES / "forty_tab_minimal.ticks.yaml")[0])
    return rec.retained


def test_without_an_enablement_no_off_grid_property_is_published(rec: PahoRecorder) -> None:
    retained = _published(rec, _example())
    assert not any(t.startswith(f"{_PANEL}/off-grid-import-limit") for t in retained)


def test_an_enabled_limit_is_published_as_commissioned(rec: PahoRecorder) -> None:
    retained = _published(
        rec,
        _example(
            **{"off-grid-import-limit-enablement": "ENABLED", "off-grid-import-limit-a": "47.9"}
        ),
    )
    assert retained[f"{_PANEL}/off-grid-import-limit"] == "47.9"
    assert retained[f"{_PANEL}/off-grid-import-limit-enablement"] == "ENABLED"
    assert retained[f"{_PANEL}/off-grid-import-limit-active"] == "false"


@pytest.mark.parametrize("enablement", ["UNCONFIGURED", "DISABLED"])
def test_a_limit_not_enabled_is_left_unvalued(rec: PahoRecorder, enablement: str) -> None:
    retained = _published(
        rec,
        _example(
            **{"off-grid-import-limit-enablement": enablement, "off-grid-import-limit-a": "47.9"}
        ),
    )
    assert f"{_PANEL}/off-grid-import-limit" not in retained
    assert retained[f"{_PANEL}/off-grid-import-limit-enablement"] == enablement
    assert retained[f"{_PANEL}/off-grid-import-limit-active"] == "false"


@pytest.mark.parametrize(
    ("md", "match"),
    [
        ({"off-grid-import-limit-enablement": "ENABLED"}, "off-grid-import-limit-a"),
        ({"off-grid-import-limit-enablement": "ON"}, "off-grid-import-limit-enablement"),
    ],
)
def test_an_incomplete_or_unknown_enablement_is_rejected(md: dict[str, str], match: str) -> None:
    with pytest.raises(ManifestValidationError, match=match):
        Emitter.from_definition(_example(**md), SetterRegistry())


def test_capture_reads_the_commissioned_limit_back(rec: PahoRecorder) -> None:
    md = {"off-grid-import-limit-enablement": "ENABLED", "off-grid-import-limit-a": "47.9"}
    captured, _ = definition_from_tree(tree_from_retained(_published(rec, _example(**md))))
    panel = captured.manifest.of_class("panel")[0].metadata
    assert {k: panel[k] for k in md} == md
