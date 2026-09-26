import pytest
from datetime import datetime, timezone

from src.model import Playlist, PluginInstance, RefreshInfo


def _instance(**overrides):
    data = {"plugin_id": "clock", "name": "Clock", "plugin_settings": {}, "refresh": {"interval": 60}}
    data.update(overrides)
    return PluginInstance.from_dict(data)


class TestPluginInstance:

    def test_round_trip_keeps_display_duration(self):
        instance = _instance(display_duration=300)
        assert instance.to_dict()["display_duration"] == 300
        assert PluginInstance.from_dict(instance.to_dict()).display_duration == 300

    def test_legacy_dict_without_display_duration_loads(self):
        instance = _instance()
        assert instance.display_duration is None
        assert instance.to_dict()["display_duration"] is None

    @pytest.mark.parametrize("value,expected", [(None, 3600), (0, 3600), (300, 300)])
    def test_get_display_duration_falls_back_to_global(self, value, expected):
        assert _instance(display_duration=value).get_display_duration(3600) == expected

    NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)

    def test_next_refresh_is_none_when_never_refreshed(self):
        assert _instance().seconds_until_next_refresh(self.NOW) is None

    def test_next_refresh_counts_down_interval(self):
        instance = _instance(latest_refresh_time="2026-09-26T09:59:30+00:00")
        assert instance.seconds_until_next_refresh(self.NOW) == 30

    def test_next_refresh_overdue_interval_is_zero(self):
        instance = _instance(latest_refresh_time="2026-09-26T09:00:00+00:00")
        assert instance.seconds_until_next_refresh(self.NOW) == 0

    @pytest.mark.parametrize("scheduled,expected", [("10:30", 1800), ("10:00", 86400), ("09:00", 82800)])
    def test_next_refresh_scheduled_time(self, scheduled, expected):
        instance = _instance(refresh={"scheduled": scheduled}, latest_refresh_time="2026-09-26T09:30:00+00:00")
        assert instance.seconds_until_next_refresh(self.NOW) == expected


class TestRefreshInfo:

    def test_slot_start_defaults_to_refresh_time(self):
        info = RefreshInfo.from_dict({"refresh_type": "Playlist", "plugin_id": "clock",
                                      "refresh_time": "2026-09-26T10:00:00+00:00", "image_hash": None})
        assert info.slot_start_time is None
        assert info.get_slot_start_datetime() == datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
        assert "slot_start_time" not in info.to_dict()

    def test_slot_start_round_trip(self):
        info = RefreshInfo.from_dict({"refresh_type": "Playlist", "plugin_id": "clock",
                                      "refresh_time": "2026-09-26T10:30:00+00:00", "image_hash": None,
                                      "slot_start_time": "2026-09-26T10:00:00+00:00"})
        assert info.get_slot_start_datetime() == datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
        assert info.to_dict()["slot_start_time"] == "2026-09-26T10:00:00+00:00"


class TestPlaylist:

    def test_set_current_plugin_moves_cursor(self):
        playlist = Playlist("P", "00:00", "24:00", [
            {"plugin_id": "clock", "name": "A", "plugin_settings": {}, "refresh": {"interval": 60}},
            {"plugin_id": "clock", "name": "B", "plugin_settings": {}, "refresh": {"interval": 60}},
            {"plugin_id": "clock", "name": "C", "plugin_settings": {}, "refresh": {"interval": 60}},
        ])
        assert playlist.set_current_plugin(playlist.plugins[1]) is True
        assert playlist.get_next_plugin().name == "C"
        assert playlist.set_current_plugin(_instance(name="missing")) is False

    def _window_playlist(self, start, end, durations):
        return Playlist("Morning", start, end, [
            {"plugin_id": "p", "name": f"Item{i}", "plugin_settings": {}, "refresh": {"interval": 60},
             "display_duration": d}
            for i, d in enumerate(durations)])

    def test_total_item_duration_uses_global_for_unset_items(self):
        playlist = self._window_playlist("09:00", "10:00", [600, None, 300])
        assert playlist.total_item_duration(1200) == 2100

    def test_items_that_fit_pass_validation(self):
        playlist = self._window_playlist("09:00", "10:00", [1800, 1800])
        assert playlist.validate_durations(3600) is None

    def test_items_that_overflow_fail_validation(self):
        playlist = self._window_playlist("09:00", "10:00", [1800, 1801])
        error = playlist.validate_durations(3600)
        assert "Morning" in error and "60 min" in error

    def test_global_interval_counts_for_unset_items(self):
        playlist = self._window_playlist("09:00", "10:00", [None, None])
        assert playlist.validate_durations(1800) is None
        assert playlist.validate_durations(1801) is not None

    def test_extra_plugin_is_included_before_adding(self):
        playlist = self._window_playlist("09:00", "10:00", [3000])
        assert playlist.validate_durations(3600, extra_plugin=_instance(display_duration=600)) is None
        assert playlist.validate_durations(3600, extra_plugin=_instance(display_duration=601)) is not None

    def test_wrapping_window_is_measured_correctly(self):
        playlist = self._window_playlist("23:00", "01:00", [3600, 3600])
        assert playlist.validate_durations(3600) is None

    def test_manager_validates_every_playlist(self):
        from src.model import PlaylistManager
        manager = PlaylistManager([self._window_playlist("09:00", "10:00", [None]),
                                   self._window_playlist("00:00", "24:00", [None, None])])
        assert manager.validate_all_durations(3600) is None
        assert "Morning" in manager.validate_all_durations(3601)

    @pytest.mark.parametrize(
        "start,end,current,expected,priority",
        [
            # --- Non-wrapping cases 09:00 <-> 15:00 ---
            ("09:00", "15:00", "08:59", False, 360),  # just before start
            ("09:00", "15:00", "09:00", True, 360),   # exactly at start
            ("09:00", "15:00", "12:00", True, 360),   # during
            ("09:00", "15:00", "14:59", True, 360),   # just before end
            ("09:00", "15:00", "15:00", False, 360),  # exactly at end
            ("09:00", "15:00", "23:00", False, 360),  # way after
    
            # --- Wrapping cases (crossing midnight) 21:00 <-> 03:00 ---
            ("21:00", "03:00", "20:59", False, 360),  # just before start
            ("21:00", "03:00", "21:00", True, 360),   # exactly at start
            ("21:00", "03:00", "23:59", True, 360),   # before midnight
            ("21:00", "03:00", "00:00", True, 360),   # after midnight, inside
            ("21:00", "03:00", "02:59", True, 360),   # just before end
            ("21:00", "03:00", "03:00", False, 360),  # exactly at end
            ("21:00", "03:00", "11:00", False, 360),  # way after
    
            # --- Equal start and end 12:00 <-> 12:00 ---
            ("12:00", "12:00", "11:59", False, 0),
            ("12:00", "12:00", "12:00", False, 0),
            ("12:00", "12:00", "12:01", False, 0),
    
            # --- Midnight boundaries 18:00 <-> 00:00 ---
            ("18:00", "00:00", "17:59", False, 360),  # before start
            ("18:00", "00:00", "23:59", True, 360),   # before end
            ("18:00", "00:00", "00:00", False, 360),  # exactly at end
    
            # --- Midnight boundaries 00:00 <-> 06:00 ---
            ("00:00", "06:00", "00:00", True, 360),   # start at midnight
            ("00:00", "06:00", "05:59", True, 360),   # before end
            ("00:00", "06:00", "06:00", False, 360),  # exactly at end

            # --- All day 00:00 <-> 24:00 ---
            ("00:00", "24:00", "00:00", True, 1440),   # exactly at start
            ("00:00", "24:00", "10:00", True, 1440),   # during
            ("00:00", "24:00", "24:00", False, 1440),  # exactly at end
        ]
    )
    def test_is_active_and_priority(self, start, end, current, expected, priority):
        playlist = Playlist("Test Playlist", start, end)
        assert playlist.is_active(current) == expected
        assert playlist.get_priority() == priority
        