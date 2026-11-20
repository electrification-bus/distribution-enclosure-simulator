import pytest

from ebus_panel_sim.conventions.tab_legs import Leg, legs_for_tabs


def test_the_two_spaces_of_a_row_share_a_leg() -> None:
    assert legs_for_tabs((1,)) == (Leg.L1,)
    assert legs_for_tabs((2,)) == (Leg.L1,)
    assert legs_for_tabs((3,)) == (Leg.L2,)
    assert legs_for_tabs((4,)) == (Leg.L2,)


def test_rows_alternate_down_the_panel() -> None:
    assert legs_for_tabs((5,)) == (Leg.L1,)
    assert legs_for_tabs((6,)) == (Leg.L1,)
    assert legs_for_tabs((39,)) == (Leg.L2,)
    assert legs_for_tabs((40,)) == (Leg.L2,)
    assert legs_for_tabs((41,)) == (Leg.L1,)
    assert legs_for_tabs((42,)) == (Leg.L1,)


def test_a_panel_whose_spaces_start_at_9_puts_9_and_10_on_l1() -> None:
    assert legs_for_tabs((9,)) == (Leg.L1,)
    assert legs_for_tabs((10,)) == (Leg.L1,)
    assert legs_for_tabs((11,)) == (Leg.L2,)


def test_a_two_pole_breaker_one_above_the_other_spans_both_legs() -> None:
    assert legs_for_tabs((1, 3)) == (Leg.L1, Leg.L2)
    assert legs_for_tabs((2, 4)) == (Leg.L1, Leg.L2)
    assert legs_for_tabs((20, 22)) == (Leg.L2, Leg.L1)
    assert legs_for_tabs((43, 45)) == (Leg.L2, Leg.L1)


def test_the_two_spaces_of_a_row_do_not_span_both_legs() -> None:
    # The convention is mechanical; ManifestPhysicsView refuses a two-pole
    # circuit on one leg. legs_for_tabs just reports the legs.
    assert legs_for_tabs((1, 2)) == (Leg.L1, Leg.L1)
    assert legs_for_tabs((1, 5)) == (Leg.L1, Leg.L1)


def test_empty_tabs_returns_empty() -> None:
    assert legs_for_tabs(()) == ()


def test_zero_or_negative_tab_raises() -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        legs_for_tabs((0,))
    with pytest.raises(ValueError, match="must be >= 1"):
        legs_for_tabs((-1,))
    with pytest.raises(ValueError, match="must be >= 1"):
        legs_for_tabs((1, 0))
