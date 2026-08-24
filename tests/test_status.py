from stoat_discord_bridge.status import HealthState, HealthTracker


def test_unconnected_target_is_failing():
    tracker = HealthTracker({"discord": "Discord"})
    assert tracker.snapshot()["discord"] == HealthState.FAILING


def test_connected_with_no_history_is_healthy():
    tracker = HealthTracker({"discord": "Discord"})
    tracker.mark_connected("discord")
    assert tracker.snapshot()["discord"] == HealthState.HEALTHY


def test_disconnect_after_connect_goes_back_to_failing():
    tracker = HealthTracker({"discord": "Discord"})
    tracker.mark_connected("discord")
    tracker.mark_disconnected("discord")
    assert tracker.snapshot()["discord"] == HealthState.FAILING


def test_one_recent_failure_degrades_but_doesnt_fail():
    tracker = HealthTracker({"discord": "Discord"})
    tracker.mark_connected("discord")
    tracker.record_error("discord")
    assert tracker.snapshot()["discord"] == HealthState.DEGRADED


def test_five_recent_failures_marks_failing_even_while_connected():
    tracker = HealthTracker({"discord": "Discord"})
    tracker.mark_connected("discord")
    for _ in range(5):
        tracker.record_error("discord")
    assert tracker.snapshot()["discord"] == HealthState.FAILING


def test_only_the_most_recent_window_counts():
    tracker = HealthTracker({"discord": "Discord"})
    tracker.mark_connected("discord")
    for _ in range(4):
        tracker.record_error("discord")
    # 20 successes push all 4 failures out of the 20-entry window
    for _ in range(20):
        tracker.record_success("discord")
    assert tracker.snapshot()["discord"] == HealthState.HEALTHY


def test_render_includes_every_configured_connector():
    tracker = HealthTracker({"discord": "Discord", "irc": "IRC"})
    tracker.mark_connected("discord")
    rendered = tracker.render()
    assert "Discord" in rendered
    assert "IRC" in rendered
