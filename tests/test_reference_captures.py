"""The reference captures ship in the package and load through its public accessor.

Each is a masked tree of a real SPAN panel with the definition and ticks recorded
with it, read from the installed package rather than from this repository, so a
consumer can replay one from a pinned release without copying files."""

from __future__ import annotations

import pytest

from ebus_panel_sim import (
    EmitterError,
    UnknownReferenceCaptureError,
    load_reference_capture,
    reference_capture_names,
)


def test_the_names_are_the_shipped_captures_sorted() -> None:
    assert reference_capture_names() == (
        "main32_r202639",
        "r202639-a",
        "r202639-b",
        "r202639-c",
        "r202639-d",
        "r202639-e",
    )


@pytest.mark.parametrize("name", reference_capture_names())
def test_every_capture_loads_its_definition_ticks_and_tree(name: str) -> None:
    """The three parts belong together: the definition describes the captured
    panel, and every tick drives only circuits the definition declares."""
    capture = load_reference_capture(name)
    assert capture.name == name
    (panel,) = capture.definition.manifest.of_class("panel")
    (enclosure,) = [d for d in capture.tree.values() if d.type == "distribution-enclosure"]
    for key in ("firmware-version", "hardware-version"):
        assert enclosure.value(f"info/{key}") == panel.metadata[key]
    ids = {i.instance_id for i in capture.definition.manifest.instances}
    assert capture.ticks
    assert all(set(tick.circuits) <= ids for tick in capture.ticks)


def test_each_load_reads_afresh() -> None:
    """A caller may change what it gets without changing the next caller's copy."""
    first = load_reference_capture("r202639-a")
    first.tree.clear()
    assert load_reference_capture("r202639-a").tree


def test_an_unknown_name_is_refused_with_the_known_names() -> None:
    with pytest.raises(UnknownReferenceCaptureError, match="r202639-a") as raised:
        load_reference_capture("no-such-capture")
    assert isinstance(raised.value, EmitterError)
    assert isinstance(raised.value, LookupError)
