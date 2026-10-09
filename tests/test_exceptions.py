from ebus_panel_sim.exceptions import (
    EmitterError,
    EmitterStateError,
    ManifestValidationError,
    MissingSetterError,
    ProfileValidationError,
    RuntimeSpecValidationError,
    UnknownReferenceCaptureError,
)


def test_all_exceptions_subclass_emitter_error() -> None:
    for exc in (
        ManifestValidationError,
        RuntimeSpecValidationError,
        MissingSetterError,
        ProfileValidationError,
        EmitterStateError,
        UnknownReferenceCaptureError,
    ):
        assert issubclass(exc, EmitterError)


def test_missing_setter_error_carries_pairs() -> None:
    pairs = [("circuit", "circuit/relay"), ("panel", "core/dominant-power-source")]
    err = MissingSetterError(missing=pairs)
    assert err.missing == pairs
    assert "circuit" in str(err)
    assert "circuit/relay" in str(err)
