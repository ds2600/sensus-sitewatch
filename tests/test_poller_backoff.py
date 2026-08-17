"""Poller failure-backoff circuit breaker (poller.py's
_apply_failure_backoff): after poller_failure_threshold consecutive sweeps
where every device is unreachable, the poller backs off to
poller_backoff_minutes and sends one Google Chat alert on the transition;
it recovers (and alerts once more) the moment any later sweep sees a
reachable device again.
"""
from unittest.mock import patch

from sitewatch.extensions import db
from sitewatch.models import Setting
from sitewatch.poller import poll_all_devices
from tests.factories import make_site, make_device


def _all_unreachable_device():
    site = make_site()
    return make_device(site, hostname="dead-dev [sim:unreachable]")


def test_backoff_triggers_after_threshold_and_sends_one_alert(app):
    with app.app_context():
        Setting.set("poller_failure_threshold", "2")
        _all_unreachable_device()
        db.session.commit()

        with patch("sitewatch.poller.send_poller_backoff_alert") as mock_backoff:
            poll_all_devices()
            assert Setting.get("poller_backed_off", "0") == "0"
            assert Setting.get_int("poller_consecutive_failures", 0) == 1
            mock_backoff.assert_not_called()

            poll_all_devices()
            assert Setting.get("poller_backed_off", "0") == "1"
            assert Setting.get_int("poller_consecutive_failures", 0) == 2
            mock_backoff.assert_called_once_with(2)

            # A third fully-failed sweep must not re-fire the alert.
            poll_all_devices()
            assert Setting.get("poller_backed_off", "0") == "1"
            mock_backoff.assert_called_once()


def test_recovery_resets_state_and_sends_one_alert(app):
    with app.app_context():
        Setting.set("poller_failure_threshold", "1")
        device = _all_unreachable_device()
        db.session.commit()

        with patch("sitewatch.poller.send_poller_backoff_alert"):
            poll_all_devices()
        assert Setting.get("poller_backed_off", "0") == "1"

        device.hostname = "recovered-dev"  # drop the [sim:unreachable] tag
        db.session.commit()

        with patch("sitewatch.poller.send_poller_recovered_alert") as mock_recovered:
            poll_all_devices()

        assert Setting.get("poller_backed_off", "0") == "0"
        assert Setting.get_int("poller_consecutive_failures", 0) == 0
        mock_recovered.assert_called_once()


def test_empty_device_list_does_not_count_as_failure(app):
    with app.app_context():
        Setting.set("poller_failure_threshold", "1")
        db.session.commit()

        with patch("sitewatch.poller.send_poller_backoff_alert") as mock_backoff:
            poll_all_devices()

        assert Setting.get("poller_backed_off", "0") == "0"
        assert Setting.get_int("poller_consecutive_failures", 0) == 0
        mock_backoff.assert_not_called()


def test_cancelled_sweep_does_not_count_as_failure(app):
    with app.app_context():
        Setting.set("poller_failure_threshold", "1")
        _all_unreachable_device()
        db.session.commit()

        with patch("sitewatch.poller.job_log.cancel_requested", return_value=True), \
             patch("sitewatch.poller.send_poller_backoff_alert") as mock_backoff:
            poll_all_devices()

        assert Setting.get("poller_backed_off", "0") == "0"
        assert Setting.get_int("poller_consecutive_failures", 0) == 0
        mock_backoff.assert_not_called()


def test_get_poller_status_reports_backoff_fields(app):
    with app.app_context():
        Setting.set("poller_backed_off", "1")
        Setting.set("poller_consecutive_failures", "4")
        db.session.commit()

        from sitewatch.poller import get_poller_status
        status = get_poller_status()
        assert status["backed_off"] is True
        assert status["consecutive_failures"] == 4
