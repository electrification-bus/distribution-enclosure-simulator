"""The reference captures ship in the package and load through its public accessor.

Each is a masked tree of a real SPAN panel with a definition and ticks that
republish it, read from the installed package rather than from this repository,
so a consumer can replay one from a pinned release without copying files. Most
ship the definition and ticks recorded with the tree; one ships the tree alone
and has both derived from it."""

from __future__ import annotations

from importlib.resources import files

import pytest

from ebus_panel_sim import (
    EmitterError,
    UnknownReferenceCaptureError,
    load_reference_capture,
    reference_capture_names,
)
from ebus_panel_sim.capture import definition_from_tree, ticks_from_samples

_DATA = files("ebus_panel_sim.reference_captures")


def test_the_names_are_the_shipped_captures_sorted() -> None:
    assert reference_capture_names() == (
        "main32_r202633",
        "main32_r202639",
        "main32_r202639-upstream-pv",
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


@pytest.mark.parametrize("name", reference_capture_names())
def test_a_capture_is_derived_exactly_when_it_ships_no_recording(name: str) -> None:
    """A recording is the definition and the ticks together, never one alone."""
    recorded = [(_DATA / f"{name}{suffix}").is_file() for suffix in (".yaml", ".ticks.yaml")]
    assert recorded in ([True, True], [False, False])
    assert load_reference_capture(name).derived_from_tree is not recorded[0]


_RECORDED = [n for n in reference_capture_names() if (_DATA / f"{n}.yaml").is_file()]


@pytest.mark.parametrize("name", _RECORDED)
def test_a_recorded_definition_lists_what_the_capture_tool_leaves_unvalued(name: str) -> None:
    """Each device's unvalued list is the one panel-sim-capture writes from the
    captured tree, so the emitter leaves unvalued exactly what the panel did."""
    capture = load_reference_capture(name)
    written, _ = definition_from_tree(capture.tree)
    assert {
        i.instance_id: i.metadata.get("unvalued") for i in capture.definition.manifest.instances
    } == {i.instance_id: i.metadata.get("unvalued") for i in written.manifest.instances}


def test_the_upstream_pv_definition_names_devices_as_the_capture_tool_does() -> None:
    """Its definition carries the description names panel-sim-capture writes from
    the captured tree, as it carries the tool's unvalued lists."""
    capture = load_reference_capture("main32_r202639-upstream-pv")
    written, _ = definition_from_tree(capture.tree)
    assert {i.instance_id: i.description_name for i in capture.definition.manifest.instances} == {
        i.instance_id: i.description_name for i in written.manifest.instances
    }


def test_only_the_r202633_capture_is_derived() -> None:
    derived = [n for n in reference_capture_names() if load_reference_capture(n).derived_from_tree]
    assert derived == ["main32_r202633"]


def test_a_derived_capture_is_what_panel_sim_capture_writes_from_its_tree() -> None:
    """Unmasked, since the tree is masked already, and one tick: the captured instant."""
    capture = load_reference_capture("main32_r202633")
    definition, notes = definition_from_tree(capture.tree, mask=False)
    assert capture.definition == definition
    assert capture.notes == tuple(notes)
    assert list(capture.ticks) == ticks_from_samples(
        capture.tree, [(0.0, capture.tree)], mask=False
    )
    assert len(capture.ticks) == 1


def test_a_derived_capture_notes_the_upstream_lugs_a_definition_cannot_express() -> None:
    """The r202633 panel's upstream lugs are fed by another enclosure, which the
    derived definition leaves out, so its notes say so."""
    capture = load_reference_capture("main32_r202633")
    (upstream,) = [
        i
        for i in capture.definition.manifest.of_class("lugs")
        if i.metadata["direction"] == "upstream"
    ]
    assert any(n.device == upstream.instance_id and n.key == "fed-by" for n in capture.notes)


def test_only_a_derived_capture_has_notes() -> None:
    captures = [load_reference_capture(n) for n in reference_capture_names()]
    assert [c.name for c in captures if c.notes] == ["main32_r202633"]


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
