"""Celery group vs single for series episode packs."""

from unittest.mock import MagicMock, patch

from worker.tasks import _apply_celery_signatures


def test_apply_single_signature():
    sig = MagicMock()
    result = MagicMock()
    sig.apply_async.return_value = result
    assert _apply_celery_signatures([sig]) is result
    sig.apply_async.assert_called_once()


def test_apply_multiple_uses_group():
    sig_a = MagicMock()
    sig_b = MagicMock()
    group_result = MagicMock()
    with patch("celery.group", return_value=MagicMock(apply_async=MagicMock(return_value=group_result))) as grp:
        out = _apply_celery_signatures([sig_a, sig_b])
    grp.assert_called_once()
    assert out is group_result
