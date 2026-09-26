from datetime import datetime, timedelta, timezone

import pytest

from model import Playlist, PlaylistManager, RefreshInfo
from refresh_task import RefreshTask, PLAYLIST_REFRESH_TYPE, MANUAL_REFRESH_TYPE

GLOBAL_INTERVAL = 3600
NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)


class FakeDeviceConfig:
    def __init__(self, interval=GLOBAL_INTERVAL):
        self.config = {"plugin_cycle_interval_seconds": interval}

    def get_config(self, key=None, default={}):
        return self.config.get(key, default)


def make_task(interval=GLOBAL_INTERVAL):
    return RefreshTask(FakeDeviceConfig(interval), display_manager=None)


def make_manager(clock_duration=3300, weather_duration=None):
    playlist = Playlist("Default", "00:00", "24:00", [
        {"plugin_id": "clock", "name": "Clock", "plugin_settings": {}, "refresh": {"interval": 60},
         "display_duration": clock_duration},
        {"plugin_id": "weather", "name": "Weather", "plugin_settings": {}, "refresh": {"interval": 3600},
         "display_duration": weather_duration},
    ])
    return PlaylistManager([playlist])


def playlist_info(instance_name, plugin_id, slot_started_ago_seconds):
    started = NOW - timedelta(seconds=slot_started_ago_seconds)
    return RefreshInfo(PLAYLIST_REFRESH_TYPE, plugin_id, started.isoformat(), "hash",
                       playlist="Default", plugin_instance=instance_name, slot_start_time=started.isoformat())


class TestCurrentSlot:

    def test_instance_with_override_uses_its_duration(self):
        inst, slot, elapsed = make_task()._current_slot(make_manager(), playlist_info("Clock", "clock", 600), NOW)
        assert inst.name == "Clock"
        assert slot == 3300
        assert elapsed == 600

    def test_instance_without_override_uses_global(self):
        inst, slot, elapsed = make_task()._current_slot(make_manager(), playlist_info("Weather", "weather", 60), NOW)
        assert inst.name == "Weather"
        assert slot == GLOBAL_INTERVAL

    def test_manual_update_falls_back_to_global(self):
        info = RefreshInfo(MANUAL_REFRESH_TYPE, "clock", NOW.isoformat(), "hash")
        assert make_task()._current_slot(make_manager(), info, NOW) == (None, GLOBAL_INTERVAL, None)

    def test_unknown_instance_falls_back_to_global(self):
        info = playlist_info("Deleted", "clock", 10)
        assert make_task()._current_slot(make_manager(), info, NOW) == (None, GLOBAL_INTERVAL, None)

    def test_missing_refresh_time_has_no_elapsed(self):
        info = RefreshInfo(PLAYLIST_REFRESH_TYPE, "clock", None, None, playlist="Default", plugin_instance="Clock")
        inst, slot, elapsed = make_task()._current_slot(make_manager(), info, NOW)
        assert inst.name == "Clock"
        assert elapsed is None

    def test_legacy_info_without_slot_start_uses_refresh_time(self):
        started = NOW - timedelta(seconds=120)
        info = RefreshInfo(PLAYLIST_REFRESH_TYPE, "clock", started.isoformat(), "hash",
                           playlist="Default", plugin_instance="Clock")
        _, _, elapsed = make_task()._current_slot(make_manager(), info, NOW)
        assert elapsed == 120


class TestComputeSleepTime:

    def test_no_previous_refresh_sleeps_full_global(self):
        info = RefreshInfo(None, None, None, None)
        assert make_task()._compute_sleep_time(make_manager(), info, NOW) == GLOBAL_INTERVAL

    def test_sleeps_for_remaining_slot(self):
        assert make_task()._compute_sleep_time(make_manager(), playlist_info("Clock", "clock", 600), NOW) == 2700

    def test_overdue_slot_sleeps_full_slot(self):
        assert make_task()._compute_sleep_time(make_manager(), playlist_info("Clock", "clock", 4000), NOW) == 3300


class TestDetermineNextPlugin:

    def test_not_due_returns_nothing(self):
        assert make_task()._determine_next_plugin(make_manager(), playlist_info("Clock", "clock", 1800), NOW) == (None, None)

    def test_default_duration_item_not_due_before_global(self):
        assert make_task()._determine_next_plugin(make_manager(), playlist_info("Weather", "weather", 360), NOW) == (None, None)

    def test_item_override_beats_global(self):
        manager = make_manager(weather_duration=300)
        manager.playlists[0].current_plugin_index = 1
        playlist, plugin = make_task()._determine_next_plugin(manager, playlist_info("Weather", "weather", 360), NOW)
        assert playlist.name == "Default"
        assert plugin.name == "Clock"

    def test_expired_slot_advances(self):
        manager = make_manager()
        manager.playlists[0].current_plugin_index = 0
        _, plugin = make_task()._determine_next_plugin(manager, playlist_info("Clock", "clock", 3300), NOW)
        assert plugin.name == "Weather"

    def test_manual_update_advances_immediately(self):
        manager = make_manager()
        info = RefreshInfo(MANUAL_REFRESH_TYPE, "clock", NOW.isoformat(), "hash")
        _, plugin = make_task()._determine_next_plugin(manager, info, NOW)
        assert plugin is not None

    def test_first_run_advances(self):
        _, plugin = make_task()._determine_next_plugin(make_manager(), RefreshInfo(None, None, None, None), NOW)
        assert plugin.name == "Clock"

    def test_inactive_playlist_returns_nothing(self):
        manager = PlaylistManager([Playlist("Night", "22:00", "23:00", [
            {"plugin_id": "clock", "name": "Clock", "plugin_settings": {}, "refresh": {"interval": 60}}])])
        assert make_task()._determine_next_plugin(manager, RefreshInfo(None, None, None, None), NOW) == (None, None)
