"""The operator import limit's enablement is commissioned state.

The reference captures publish it DISABLED on three panels and UNCONFIGURED on
three, ``main32_r202639`` included, and the MAIN 32 captures whose PCS is off
leave it unvalued. So a definition carries it and the emitter publishes it as
given; without one it reads UNCONFIGURED."""

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
_TOPIC = "ebus/5/example-40t-001/pcs/operator-import-limit-enablement"


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


@pytest.mark.parametrize(
    ("md", "published"),
    [
        ({}, "UNCONFIGURED"),
        ({"operator-import-limit-enablement": "DISABLED"}, "DISABLED"),
        ({"operator-import-limit-enablement": "ENABLED"}, "ENABLED"),
    ],
)
def test_the_enablement_is_published_as_commissioned(
    rec: PahoRecorder, md: dict[str, str], published: str
) -> None:
    assert _published(rec, _example(**md))[_TOPIC] == published


def test_an_unknown_enablement_is_rejected() -> None:
    with pytest.raises(ManifestValidationError, match="operator-import-limit-enablement"):
        Emitter.from_definition(
            _example(**{"operator-import-limit-enablement": "ON"}), SetterRegistry()
        )


def test_capture_reads_the_enablement_back(rec: PahoRecorder) -> None:
    md = {"operator-import-limit-enablement": "DISABLED"}
    captured, _ = definition_from_tree(tree_from_retained(_published(rec, _example(**md))))
    assert captured.manifest.of_class("panel")[0].metadata["operator-import-limit-enablement"] == (
        "DISABLED"
    )
