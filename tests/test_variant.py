"""The span (SPAN-faithful) vs reference (vendor-neutral, spec-conformant) variant split."""

from __future__ import annotations

import json

from ebus_panel_sim import DeviceInstance, DeviceManifest, Emitter, SetterRegistry, TickInputs
from ebus_panel_sim.wire.profile_loader import load_profiles

from .conftest import PahoRecorder

# The exact minified soc-priority.v1 JSONSchema real SPAN (lc3 nt-2026-c192x)
# publishes in the panel shed/policy $format.
_SOC_PRIORITY_V1_FORMAT = (
    '{"$id":"soc-priority.v1","type":"object","required":["algorithm","parameters"],'
    '"additionalProperties":false,"properties":{"algorithm":{"const":"soc-priority.v1"},'
    '"parameters":{"type":"object","required":["soc-threshold-shed","soc-threshold-release"],'
    '"additionalProperties":false,"properties":'
    '{"soc-threshold-shed":{"type":"integer","minimum":0,"maximum":100,'
    '"description":"SoC percent below which SOC_THRESHOLD circuits shed"},'
    '"soc-threshold-release":{"type":"integer","minimum":0,"maximum":100,'
    '"description":"SoC percent above which shed SOC_THRESHOLD circuits restore"}}}}}'
)


def _manifest() -> DeviceManifest:
    return DeviceManifest(
        instances=(
            DeviceInstance(
                "panel",
                "abc-123",
                "Span Panel",
                metadata={
                    "vendor-name": "Span",
                    "serial-number": "abc-123",
                    "firmware-version": "sim/v0.1.0",
                    "hardware-version": "rev2",
                    "panel-size": "40",
                    "main-breaker-rating-a": "200",
                    "panel-model": "MAIN_40",
                    "postal-code": "94103",
                    "time-zone": "America/Los_Angeles",
                },
            ),
            DeviceInstance(
                "circuit",
                "kitchen",
                "Kitchen",
                metadata={
                    "tab-numbers": "1",
                    "breaker-rating-a": "20",
                    "default-priority": "NICE_TO_HAVE",
                    "relay-behavior": "controllable",
                    "placement": "downstream-of-lugs",
                },
            ),
            # shed is published only with a commissioned BESS.
            DeviceInstance(
                "bess",
                "abc-123-bess",
                "Battery",
                metadata={"vendor-name": "Span", "nameplate-capacity-kwh": "13.5"},
            ),
        )
    )


def test_reference_variant_strips_the_span_overlay() -> None:
    span = load_profiles(variant="span")
    ref = load_profiles(variant="reference")
    # panel status diagnostics are SPAN-vendor-specific
    assert "status" in span["panel"].capabilities
    assert "status" not in ref["panel"].capabilities
    # circuit identity extras are SPAN-specific (they are the whole of circuit
    # info, so the reference circuit has no info capability at all)
    assert "name" in span["circuit"].capabilities["info"].properties
    ref_info = ref["circuit"].capabilities.get("info")
    assert ref_info is None or "name" not in ref_info.properties
    # evse config grab-bag is SPAN-only (spec uses charge-limit)
    assert "config" in span["evse"].capabilities
    assert "config" not in ref["evse"].capabilities
    # shed/policy: read-only on a SPAN panel, settable per the spec
    assert span["panel"].capabilities["shed"].properties["policy"].settable is False
    assert ref["panel"].capabilities["shed"].properties["policy"].settable is True


def _tick() -> TickInputs:
    return TickInputs(current_time=0.0, grid_online=True, circuits={"kitchen": 500.0})


def test_span_emitter_publishes_span_diagnostics(rec: PahoRecorder) -> None:
    em = Emitter(_manifest(), SetterRegistry())  # default variant = span
    em.start()
    em.publish_tick(_tick())
    retained = rec.retained
    assert retained["ebus/5/abc-123/status/postal-code"] == "94103"
    assert retained["ebus/5/kitchen/meter/active-power"] == "-500.0"


def test_reference_emitter_omits_the_span_surface(rec: PahoRecorder) -> None:
    em = Emitter(_manifest(), SetterRegistry(), variant="reference")
    em.start()
    em.publish_tick(_tick())
    retained = rec.retained
    # no SPAN status diagnostics on the reference tree
    assert not any(t.startswith("ebus/5/abc-123/status/") for t in retained)
    assert not any(t.endswith("/info/name") for t in retained)
    # the spec-conformant metering still flows
    assert retained["ebus/5/kitchen/meter/active-power"] == "-500.0"


def test_span_shed_policy_publishes_the_soc_priority_schema(rec: PahoRecorder) -> None:
    """The span variant publishes shed/policy's soc-priority.v1 JSONSchema in
    $format, byte-exact to real SPAN; the reference variant carries no SPAN
    schema (its shed/policy is settable per the spec instead)."""
    span_policy = load_profiles(variant="span")["panel"].capabilities["shed"].properties["policy"]
    assert span_policy.format == _SOC_PRIORITY_V1_FORMAT
    ref_policy = (
        load_profiles(variant="reference")["panel"].capabilities["shed"].properties["policy"]
    )
    assert ref_policy.format is None

    # end to end: the schema reaches the panel $description on the wire
    Emitter(_manifest(), SetterRegistry()).start()
    panel_desc = json.loads(rec.retained["ebus/5/abc-123/$description"])
    assert panel_desc["nodes"]["shed"]["properties"]["policy"]["format"] == _SOC_PRIORITY_V1_FORMAT
