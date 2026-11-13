import pytest

from ebus_panel_sim.relay_resolver import RelayRequester, RelayResolver, RelayState


def test_default_state_is_closed_unknown() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    assert rr.state("c1") == (RelayState.CLOSED, RelayRequester.NONE)


def test_always_on_resolves_closed_never() -> None:
    rr = RelayResolver()
    rr.register("dryer", always_on=True)
    assert rr.state("dryer") == (RelayState.CLOSED, RelayRequester.CONFIGURATION)


def test_user_override_open_wins_over_default() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_user_override("c1", RelayState.OPEN)
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.USER)


def test_user_override_closed_wins_over_shed() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_shed("c1", open_relay=True)
    rr.set_user_override("c1", RelayState.CLOSED)
    # Operator commanded CLOSED while shed wants OPEN: operator wins.
    assert rr.state("c1") == (RelayState.CLOSED, RelayRequester.USER)


def test_user_override_open_persists_across_shed_clear() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_user_override("c1", RelayState.OPEN)
    rr.set_shed("c1", open_relay=True)
    rr.clear_all_shed()
    # /set persists; only shed was cleared
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.USER)


def test_clear_user_override_falls_back_to_shed() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_shed("c1", open_relay=True)
    rr.set_user_override("c1", RelayState.CLOSED)
    rr.clear_user_override("c1")
    # Without /set override, shed re-asserts.
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.LOAD_SHED)


def test_shed_only_no_override_resolves_open_backup() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_shed("c1", open_relay=True)
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.LOAD_SHED)


def test_always_on_ignores_user_override_open() -> None:
    rr = RelayResolver()
    rr.register("dryer", always_on=True)
    rr.set_user_override("dryer", RelayState.OPEN)
    # Silently dropped. Always-on remains CLOSED with NEVER requester.
    assert rr.state("dryer") == (RelayState.CLOSED, RelayRequester.CONFIGURATION)


def test_always_on_ignores_shed() -> None:
    rr = RelayResolver()
    rr.register("dryer", always_on=True)
    rr.set_shed("dryer", open_relay=True)
    assert rr.state("dryer") == (RelayState.CLOSED, RelayRequester.CONFIGURATION)


def test_clear_all_shed_resets_only_shed() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.register("c2", always_on=False)
    rr.set_shed("c1", open_relay=True)
    rr.set_shed("c2", open_relay=True)
    rr.set_user_override("c1", RelayState.OPEN)
    rr.clear_all_shed()
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.USER)
    assert rr.state("c2") == (RelayState.CLOSED, RelayRequester.NONE)


def test_unregistered_instance_set_user_override_raises() -> None:
    rr = RelayResolver()
    with pytest.raises(KeyError, match="unregistered"):
        rr.set_user_override("ghost", RelayState.OPEN)


def test_unregistered_instance_set_shed_raises() -> None:
    rr = RelayResolver()
    with pytest.raises(KeyError, match="unregistered"):
        rr.set_shed("ghost", open_relay=True)


def test_register_idempotent_keeps_state() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_user_override("c1", RelayState.OPEN)
    rr.register("c1", always_on=False)  # re-register
    # /set override should persist.
    assert rr.state("c1") == (RelayState.OPEN, RelayRequester.USER)


def test_register_can_change_always_on() -> None:
    rr = RelayResolver()
    rr.register("c1", always_on=False)
    rr.set_user_override("c1", RelayState.OPEN)
    rr.register("c1", always_on=True)
    # Newly always-on: /set is silently dropped on next set; existing override
    # remains in the map but state() honors always-on precedence.
    assert rr.state("c1") == (RelayState.CLOSED, RelayRequester.CONFIGURATION)


def test_known_returns_true_after_register() -> None:
    rr = RelayResolver()
    assert rr.known("c1") is False
    rr.register("c1", always_on=False)
    assert rr.known("c1") is True


def _tandem(*, locked: bool = False) -> RelayResolver:
    """``hall`` and ``porch`` behind one relay, ``other`` behind its own."""
    rr = RelayResolver()
    for cid in ("hall", "porch", "other"):
        rr.register(cid, always_on=locked and cid != "other")
    rr.share(("hall", "porch"))
    return rr


@pytest.mark.parametrize("written", ["hall", "porch"])
def test_an_override_of_one_circuit_on_a_shared_relay_is_one_of_all(written: str) -> None:
    rr = _tandem()
    rr.set_user_override(written, RelayState.OPEN)
    assert rr.state("hall") == rr.state("porch") == (RelayState.OPEN, RelayRequester.USER)
    assert rr.state("other") == (RelayState.CLOSED, RelayRequester.NONE)
    rr.clear_user_override("hall" if written == "porch" else "porch")
    assert rr.state("hall") == rr.state("porch") == (RelayState.CLOSED, RelayRequester.NONE)


def test_a_shed_of_one_circuit_on_a_shared_relay_sheds_all() -> None:
    rr = _tandem()
    rr.set_shed("porch", open_relay=True)
    assert rr.state("hall") == rr.state("porch") == (RelayState.OPEN, RelayRequester.LOAD_SHED)
    assert rr.state("other") == (RelayState.CLOSED, RelayRequester.NONE)
    rr.clear_all_shed()
    assert rr.state("hall") == (RelayState.CLOSED, RelayRequester.NONE)


def test_a_locked_shared_relay_ignores_every_circuit_s_commands() -> None:
    rr = _tandem(locked=True)
    rr.set_user_override("hall", RelayState.OPEN)
    rr.set_shed("porch", open_relay=True)
    assert (
        rr.state("hall") == rr.state("porch") == (RelayState.CLOSED, RelayRequester.CONFIGURATION)
    )


def test_a_shared_relay_keeps_the_first_circuit_s_state() -> None:
    rr = RelayResolver()
    rr.register("hall", always_on=False)
    rr.register("porch", always_on=False)
    rr.set_user_override("hall", RelayState.OPEN)
    rr.set_user_override("porch", RelayState.CLOSED)
    rr.share(("hall", "porch"))
    assert rr.state("porch") == (RelayState.OPEN, RelayRequester.USER)


def test_re_registering_a_circuit_on_a_shared_relay_changes_the_relay_s_lock() -> None:
    rr = _tandem()
    rr.register("porch", always_on=True)
    assert rr.state("hall") == (RelayState.CLOSED, RelayRequester.CONFIGURATION)
    assert rr.known("hall") and rr.known("porch")


def test_circuits_that_cannot_share_a_relay_are_refused() -> None:
    rr = RelayResolver()
    rr.register("hall", always_on=False)
    rr.register("porch", always_on=True)
    rr.register("attic", always_on=False)
    with pytest.raises(KeyError, match="nowhere"):
        rr.share(("hall", "nowhere"))
    with pytest.raises(ValueError, match="lock"):
        rr.share(("hall", "porch"))
    with pytest.raises(ValueError, match="of its own"):
        rr.share(("hall", "hall"))
    rr.share(("hall", "attic"))
    rr.register("cellar", always_on=False)
    for group in (("attic", "cellar"), ("hall", "cellar")):
        with pytest.raises(ValueError, match="of its own"):
            rr.share(group)
