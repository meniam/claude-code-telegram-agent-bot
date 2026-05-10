"""PlanRouter: arm / is_armed / disarm — pure per-chat state."""

import logging
from unittest.mock import MagicMock

from src.ui.plan_router import PlanRouter


def _make_router() -> PlanRouter:
    return PlanRouter(
        agent=MagicMock(),
        gate=MagicMock(),
        tr=MagicMock(),
        glog=logging.getLogger("test.plan_router"),
        bot_name="test",
    )


def test_not_armed_by_default() -> None:
    r = _make_router()
    assert r.is_armed(123) is False


def test_arm_then_is_armed_true() -> None:
    r = _make_router()
    r.arm(123, logging.getLogger("test"))
    assert r.is_armed(123) is True


def test_disarm_clears_state() -> None:
    r = _make_router()
    r.arm(123, logging.getLogger("test"))
    r.disarm(123)
    assert r.is_armed(123) is False


def test_disarm_unknown_chat_no_error() -> None:
    r = _make_router()
    # Must not raise.
    r.disarm(999)


def test_multiple_chats_isolated() -> None:
    r = _make_router()
    r.arm(1, logging.getLogger("test"))
    r.arm(2, logging.getLogger("test"))
    r.disarm(1)
    assert r.is_armed(1) is False
    assert r.is_armed(2) is True
