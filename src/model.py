import os
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

END_OF_DAY = "24:00"

def time_in_window(start_time, end_time, current_time):
    """True when the 'HH:MM' current_time falls in [start_time, end_time), handling windows that wrap midnight."""
    if start_time <= end_time:
        # Non-wrapping window (EG: 09:00-15:00)
        return start_time <= current_time < end_time
    # Wrapping window across midnight (EG: 21:00-03:00)
    return current_time >= start_time or current_time < end_time

def window_minutes(start_time, end_time):
    """Length of a 'HH:MM' window in minutes; '24:00' means midnight at the end of the day."""
    start = datetime.strptime(start_time, "%H:%M")
    if end_time != END_OF_DAY:
        end = datetime.strptime(end_time, "%H:%M")
    else:
        end = datetime.strptime("00:00", "%H:%M") + timedelta(days=1)
    # If the window wraps past midnight (EG: 21:00 -> 03:00), treat end as next day
    if end < start:
        end += timedelta(days=1)
    return int((end - start).total_seconds() // 60)

def next_occurrence(time_str, current_dt):
    """The next datetime strictly after current_dt at which the 'HH:MM' wall-clock time occurs."""
    if time_str == END_OF_DAY:
        candidate = current_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        parsed = datetime.strptime(time_str, "%H:%M").time()
        candidate = current_dt.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
        if candidate <= current_dt:
            candidate += timedelta(days=1)
    return candidate

class RefreshInfo:
    """Keeps track of refresh metadata.

    Attributes:
        refresh_time (str): ISO-formatted time string of the refresh.
        image_hash (int): SHA-256 hash of the image.
        refresh_type (str): Refresh type ['Manual Update', 'Playlist'].
        plugin_id (str): Plugin id of the refresh.
        playlist (str): Playlist name if refresh_type is 'Playlist'.
        plugin_instance (str): Plugin instance name if refresh_type is 'Playlist'.
    """

    def __init__(self, refresh_type, plugin_id, refresh_time, image_hash, playlist=None, plugin_instance=None,
                 slot_start_time=None):
        """Initialize RefreshInfo instance."""
        self.refresh_time = refresh_time
        self.image_hash = image_hash
        self.refresh_type = refresh_type
        self.plugin_id = plugin_id
        self.playlist = playlist
        self.plugin_instance = plugin_instance
        # When the currently displayed playlist item took the screen. Unlike refresh_time it is not
        # advanced by in-place regenerations of the same item, so it anchors the item's display duration.
        self.slot_start_time = slot_start_time

    def get_refresh_datetime(self):
        """Returns the refresh time as a datetime object or None if not set."""
        latest_refresh = None
        if self.refresh_time:
            latest_refresh = datetime.fromisoformat(self.refresh_time)
        return latest_refresh

    def get_slot_start_datetime(self):
        """Returns when the current item took the screen, falling back to the refresh time for older configs."""
        if self.slot_start_time:
            return datetime.fromisoformat(self.slot_start_time)
        return self.get_refresh_datetime()

    def to_dict(self):
        refresh_dict = {
            "refresh_time": self.refresh_time,
            "image_hash": self.image_hash,
            "refresh_type": self.refresh_type,
            "plugin_id": self.plugin_id,
        }
        if self.playlist:
            refresh_dict["playlist"] = self.playlist
        if self.plugin_instance:
            refresh_dict["plugin_instance"] = self.plugin_instance
        if self.slot_start_time:
            refresh_dict["slot_start_time"] = self.slot_start_time
        return refresh_dict

    @classmethod
    def from_dict(cls, data):
        return cls(
            refresh_time=data.get("refresh_time"),
            image_hash=data.get("image_hash"),
            refresh_type=data.get("refresh_type"),
            plugin_id=data.get("plugin_id"),
            playlist=data.get("playlist"),
            plugin_instance=data.get("plugin_instance"),
            slot_start_time=data.get("slot_start_time")
        )

class PlaylistManager:
    """A class managing multiple time-based playlists.

    Attributes:
        playlists (list): A list of Playlist instances managed by the manager.
        events (list): A list of Event instances; an active event overrides the playlists.
        active_playlist (str): Name of the currently active playlist or event.
    """
    DEFAULT_PLAYLIST_START = "00:00"
    DEFAULT_PLAYLIST_END = "24:00"

    def __init__(self, playlists=[], active_playlist=None, events=None):
        """Initialize PlaylistManager with a list of playlists."""
        self.playlists = playlists
        self.events = events if events is not None else []
        self.active_playlist = active_playlist

    def get_playlist_names(self):
        """Returns a list of all playlist names."""
        return [p.name for p in self.playlists]

    def get_event_names(self):
        """Returns a list of all event names."""
        return [e.name for e in self.events]

    # ---- events -------------------------------------------------------------------------------

    def get_event(self, name):
        """Returns the event with the given name, or None."""
        return next((e for e in self.events if e.name == name), None)

    def get_playlist_or_event(self, name):
        """Returns the playlist with the given name, else the event with that name, else None."""
        return self.get_playlist(name) or self.get_event(name)

    def add_event(self, name, start_time, end_time, date=None, days=None):
        """Creates an event. Names must not collide with other events or playlists."""
        if self.get_playlist_or_event(name):
            return False
        self.events.append(Event(name, start_time, end_time, date=date, days=days))
        return True

    def update_event(self, old_name, new_name, start_time, end_time, date=None, days=None):
        event = self.get_event(old_name)
        if not event:
            return False
        if new_name != old_name and self.get_playlist_or_event(new_name):
            return False
        event.name = new_name
        event.start_time = start_time
        event.end_time = end_time
        event.date = date or None
        event.days = sorted(set(days)) if days else None
        return True

    def delete_event(self, name):
        self.events = [e for e in self.events if e.name != name]

    def determine_active_event(self, current_dt):
        """The event that should own the screen now, or None. Dated events beat weekly ones, then shorter windows."""
        active = [e for e in self.events if e.plugin and e.is_active(current_dt)]
        if not active:
            return None
        active.sort(key=lambda e: e.get_priority())
        return active[0]

    def seconds_until_next_boundary(self, current_dt):
        """Seconds until the next playlist or event window starts or ends, or None when there is none."""
        deltas = []
        for playlist in self.playlists:
            for time_str in (playlist.start_time, playlist.end_time):
                deltas.append((next_occurrence(time_str, current_dt) - current_dt).total_seconds())
        for event in self.events:
            if not event.plugin:
                continue
            for time_str in (event.start_time, event.end_time):
                occurrence = next_occurrence(time_str, current_dt)
                # a window ending at 24:00 belongs to the day before the occurrence
                window_day = (occurrence - timedelta(minutes=1)).date() if time_str == END_OF_DAY else occurrence.date()
                if event.applies_on(window_day):
                    deltas.append((occurrence - current_dt).total_seconds())
        return min(deltas) if deltas else None

    def add_default_playlist(self):
        """Add a default playlist to the manager, called when no playlists exist."""
        return self.playlists.append(
            Playlist("Default", PlaylistManager.DEFAULT_PLAYLIST_START, PlaylistManager.DEFAULT_PLAYLIST_END, []))

    def find_plugin(self, plugin_id, instance):
        """Searches playlists and events to find a plugin with the given ID and instance."""
        owner = self.find_plugin_owner(plugin_id, instance)
        return owner.find_plugin(plugin_id, instance) if owner else None

    def find_plugin_owner(self, plugin_id, instance):
        """Returns the playlist or event containing the given plugin instance, or None."""
        for owner in list(self.playlists) + list(self.events):
            if owner.find_plugin(plugin_id, instance):
                return owner
        return None

    def validate_all_durations(self, global_seconds):
        """Returns the first playlist error for the given global cycle interval, or None when every playlist fits."""
        for playlist in self.playlists:
            error = playlist.validate_durations(global_seconds)
            if error:
                return error
        return None

    def determine_active_playlist(self, current_datetime):
        """Determine the active playlist based on the current time."""
        current_time = current_datetime.strftime("%H:%M")  # Get current time in "HH:MM" format

        # get active playlists that have plugins
        active_playlists = [p for p in self.playlists if p.is_active(current_time)]
        if not active_playlists:
            return None

        # Sort playlists by priority
        active_playlists.sort(key=lambda p: p.get_priority())
        playlist = active_playlists[0]

        return playlist

    def get_playlist(self, playlist_name):
        """Returns the playlist with the specified name."""
        return next((p for p in self.playlists if p.name == playlist_name), None)

    def add_plugin_to_playlist(self, playlist_name, plugin_data):
        """Adds a plugin to a playlist by the specified name. Returns true if successfully added,
        False if playlist doesn't exist"""
        playlist = self.get_playlist(playlist_name)
        if playlist:
            if playlist.add_plugin(plugin_data):
                return True
        else:
            logger.warning(f"Playlist '{playlist_name}' not found.")
        return False

    def add_playlist(self, name, start_time=None, end_time=None):
        """Creates and adds a new playlist with the given start and end times."""
        if not start_time:
            start_time = PlaylistManager.DEFAULT_PLAYLIST_START
        if not end_time:
            end_time = PlaylistManager.DEFAULT_PLAYLIST_END
        self.playlists.append(Playlist(name, start_time, end_time))
        return True

    def update_playlist(self, old_name, new_name, start_time, end_time):
        """Updates an existing playlist's name, start time, and end time."""
        playlist = self.get_playlist(old_name)
        if playlist:
            playlist.name = new_name
            playlist.start_time = start_time
            playlist.end_time = end_time
            return True
        logger.warning(f"Playlist '{old_name}' not found.")
        return False

    def delete_playlist(self, name):
        """Deletes the playlist with the specified name."""
        self.playlists = [p for p in self.playlists if p.name != name]

    def to_dict(self):
        return {
            "playlists": [p.to_dict() for p in self.playlists],
            "events": [e.to_dict() for e in self.events],
            "active_playlist": self.active_playlist
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            playlists=[Playlist.from_dict(p) for p in data.get("playlists", [])],
            events=[Event.from_dict(e) for e in data.get("events", [])],
            active_playlist=data.get("active_playlist")
        )

    @staticmethod
    def should_refresh(latest_refresh, interval_seconds, current_time):
        """Determines whether a refresh should occur on the interval and latest refresh time."""
        if not latest_refresh:
            return True  # No previous refresh, so it's time to refresh

        return (current_time - latest_refresh) >= timedelta(seconds=interval_seconds)

class Playlist:
    """Represents a playlist with a time interval.

    Attributes:
        name (str): Name of the playlist.
        start_time (str): Playlist start time in 'HH:MM'.
        end_time (str): Playlist end time in 'HH:MM'.
        plugins (list): A list of PluginInstance objects within the playlist.
        current_plugin_index (int): Index of the currently active plugin in the playlist.
    """

    def __init__(self, name, start_time, end_time, plugins=None, current_plugin_index=None):
        self.name = name
        self.start_time = start_time
        self.end_time = end_time
        self.plugins = [PluginInstance.from_dict(p) for p in (plugins or [])]
        self.current_plugin_index = current_plugin_index

    def is_active(self, current_time):
        """Check if the playlist is active at the given 'HH:MM' time."""
        return time_in_window(self.start_time, self.end_time, current_time)

    def add_plugin(self, plugin_data):
        """Add a new plugin instance to the playlist."""
        if self.find_plugin(plugin_data["plugin_id"], plugin_data["name"]):
            logger.warning(f"Plugin '{plugin_data['plugin_id']}' with instance '{plugin_data['name']}' already exists.")
            return False
        self.plugins.append(PluginInstance.from_dict(plugin_data))
        return True

    def update_plugin(self, plugin_id, instance_name, updated_data):
        """Updates an existing plugin instance in the playlist."""
        plugin = self.find_plugin(plugin_id, instance_name)
        if plugin:
            plugin.update(updated_data)
            return True
        logger.warning(f"Plugin '{plugin_id}' with name '{instance_name}' not found.")
        return False

    def delete_plugin(self, plugin_id, name):
        """Remove a specific plugin instance from the playlist."""
        initial_count = len(self.plugins)
        self.plugins = [p for p in self.plugins if not (p.plugin_id == plugin_id and p.name == name)]
        
        if len(self.plugins) == initial_count:
            logger.warning(f"Plugin '{plugin_id}' with instance '{name}' not found.")
            return False
        return True

    def find_plugin(self, plugin_id, name):
        """Find a plugin instance by its plugin_id and name."""
        return next((p for p in self.plugins if p.plugin_id == plugin_id and p.name == name), None)

    def get_next_plugin(self):
        """Returns the next plugin instance in the playlist and update the current_plugin_index."""
        if self.current_plugin_index is None:
            self.current_plugin_index = 0
        else:
            self.current_plugin_index = (self.current_plugin_index + 1) % len(self.plugins)

        return self.plugins[self.current_plugin_index]

    def set_current_plugin(self, plugin_instance):
        """Moves the rotation cursor to the given instance so the rotation continues from it."""
        for index, plugin in enumerate(self.plugins):
            if plugin.plugin_id == plugin_instance.plugin_id and plugin.name == plugin_instance.name:
                self.current_plugin_index = index
                return True
        return False

    def total_item_duration(self, global_seconds):
        """Seconds needed to show every item once, using the global cycle interval for items without an override."""
        return sum(p.get_display_duration(global_seconds) for p in self.plugins)

    def validate_durations(self, global_seconds, extra_plugin=None):
        """Checks that one pass through the items fits in the playlist's time window.

        Returns an error message, or None when the items fit. `extra_plugin` lets callers validate an item
        before it is added.
        """
        total = self.total_item_duration(global_seconds)
        if extra_plugin is not None:
            total += extra_plugin.get_display_duration(global_seconds)
        window = self.get_time_range_minutes() * 60
        if total > window:
            return (f"Playlist '{self.name}' runs {self.start_time}-{self.end_time} ({window // 60} min) but its items "
                    f"need {total // 60} min to show once. Shorten some display durations or widen the playlist.")
        return None

    def get_priority(self):
        """Determine priority of a playlist, based on the time range"""
        return self.get_time_range_minutes()

    def get_time_range_minutes(self):
        """Calculate the time difference in minutes between start_time and end_time."""
        return window_minutes(self.start_time, self.end_time)

    def to_dict(self):
        return {
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "plugins": [p.to_dict() for p in self.plugins],
            "current_plugin_index": self.current_plugin_index
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            name=data["name"],
            start_time=data["start_time"],
            end_time=data["end_time"],
            plugins=data["plugins"],
            current_plugin_index=data.get("current_plugin_index", None)
        )

class Event:
    """One screen shown in a fixed time window on a specific date or on given weekdays.

    An active event always takes precedence over playlists. It holds at most one plugin instance,
    which is shown for the whole window and refreshed according to its own refresh rule.

    Attributes:
        name (str): Unique event name.
        start_time (str): Window start in 'HH:MM'.
        end_time (str): Window end in 'HH:MM' (may be '24:00').
        date (str): 'YYYY-MM-DD' for a one-off event, or None.
        days (list): Weekday numbers (0=Monday .. 6=Sunday) the event repeats on, or None for every day.
        plugin (PluginInstance): The screen to show, or None while the event is empty.
    """

    def __init__(self, name, start_time, end_time, date=None, days=None, plugin=None):
        self.name = name
        self.start_time = start_time
        self.end_time = end_time
        self.date = date or None
        self.days = sorted(set(days)) if days else None
        self.plugin = PluginInstance.from_dict(plugin) if plugin else None

    @property
    def plugins(self):
        """The event's plugin instances as a list, for code that treats it like a playlist."""
        return [self.plugin] if self.plugin else []

    def get_date(self):
        return datetime.strptime(self.date, "%Y-%m-%d").date() if self.date else None

    def applies_on(self, day):
        """True when the event may run on the given date (ignoring the time window)."""
        if self.date:
            return day == self.get_date()
        if self.days is not None:
            return day.weekday() in self.days
        return True

    def is_active(self, current_dt):
        """True when the event should own the screen at the given datetime."""
        if not self.applies_on(current_dt.date()):
            return False
        return time_in_window(self.start_time, self.end_time, current_dt.strftime("%H:%M"))

    def is_expired(self, current_dt):
        """True for a one-off event whose date has passed."""
        return self.date is not None and self.get_date() < current_dt.date()

    def get_priority(self):
        """Lower sorts first: dated events beat weekly ones, then shorter windows beat longer ones."""
        return (0 if self.date else 1, window_minutes(self.start_time, self.end_time))

    def find_plugin(self, plugin_id, name):
        if self.plugin and self.plugin.plugin_id == plugin_id and self.plugin.name == name:
            return self.plugin
        return None

    def add_plugin(self, plugin_data):
        """Sets the event's single plugin instance, replacing any previous one."""
        self.plugin = PluginInstance.from_dict(plugin_data)
        return True

    def delete_plugin(self, plugin_id, name):
        if self.find_plugin(plugin_id, name):
            self.plugin = None
            return True
        return False

    def get_next_plugin(self):
        return self.plugin

    def to_dict(self):
        return {
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "date": self.date,
            "days": self.days,
            "plugin": self.plugin.to_dict() if self.plugin else None,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            name=data["name"],
            start_time=data["start_time"],
            end_time=data["end_time"],
            date=data.get("date"),
            days=data.get("days"),
            plugin=data.get("plugin"),
        )

class PluginInstance:
    """Represents an individual plugin instance within a playlist.

    Attributes:
        plugin_id (str): Plugin id for this instance.
        name (str): Name of the plugin instance.
        settings (dict): Settings associated with the plugin.
        refresh (dict): Refresh settings, such as interval and scheduled time.
        latest_refresh (str): ISO-formatted string representing the last refresh time.
        display_duration (int): Seconds this instance stays on screen before the playlist advances,
            or None to use the device-wide plugin cycle interval.
    """

    def __init__(self, plugin_id, name, settings, refresh, latest_refresh_time=None, display_duration=None):
        self.plugin_id = plugin_id
        self.name = name
        self.settings = settings
        self.refresh = refresh
        self.latest_refresh_time = latest_refresh_time
        self.display_duration = display_duration

    def get_display_duration(self, fallback_seconds):
        """Returns how long this instance stays on screen, using the fallback when no override is set."""
        return self.display_duration if self.display_duration else fallback_seconds

    def update(self, updated_data):
        """Update attributes of the class with the dictionary values."""
        for key, value in updated_data.items():
            setattr(self, key, value)

    def should_refresh(self, current_time):
        """Checks whether the plugin should be refreshed based on its refresh settings and the current time."""
        latest_refresh_dt = self.get_latest_refresh_dt()
        if not latest_refresh_dt:
            return True

        # Check for interval-based refresh
        if "interval" in self.refresh:
            interval = self.refresh.get("interval")
            if interval and (current_time - latest_refresh_dt) >= timedelta(seconds=interval):
                return True

        # Check for scheduled refresh (HH:MM format)
        if "scheduled" in self.refresh:
            scheduled_time_str = self.refresh.get("scheduled")
            latest_refresh_str = latest_refresh_dt.strftime("%H:%M")

            # If the latest refresh is before the scheduled time today
            if latest_refresh_str < scheduled_time_str:
                return True
        
        if "scheduled" in self.refresh:
            scheduled_time_str = self.refresh.get("scheduled")
            scheduled_time = datetime.strptime(scheduled_time_str, "%H:%M").time()
            
            latest_refresh_date = latest_refresh_dt.date()
            current_date = current_time.date()

            # Determine if a refresh is needed based on scheduled time and last refresh
            if (latest_refresh_date < current_date and current_time.time() >= scheduled_time) or \
            (latest_refresh_date == current_date and latest_refresh_dt.time() < scheduled_time <= current_time.time()):
                return True

        return False

    def seconds_until_next_refresh(self, current_time):
        """Seconds until this instance's refresh rule next fires, or None when it is due now.

        Mirrors the rules in should_refresh: an interval counts from the latest refresh; a scheduled
        HH:MM fires at its next occurrence today or tomorrow.
        """
        latest_refresh_dt = self.get_latest_refresh_dt()
        if not latest_refresh_dt:
            return None

        if "interval" in self.refresh:
            interval = self.refresh.get("interval")
            if interval:
                remaining = (latest_refresh_dt + timedelta(seconds=interval) - current_time).total_seconds()
                return max(remaining, 0)

        if "scheduled" in self.refresh:
            scheduled_time = datetime.strptime(self.refresh.get("scheduled"), "%H:%M").time()
            next_fire = current_time.replace(hour=scheduled_time.hour, minute=scheduled_time.minute,
                                             second=0, microsecond=0)
            if next_fire <= current_time:
                next_fire += timedelta(days=1)
            return (next_fire - current_time).total_seconds()

        return None

    def get_image_path(self):
        """Formats the image path for this plugin instance."""
        return f"{self.plugin_id}_{self.name.replace(' ', '_')}.png"

    def get_latest_refresh_dt(self):
        """Returns the latest refresh time as a datetime object, or None if not set."""
        latest_refresh = None
        if self.latest_refresh_time:
            latest_refresh = datetime.fromisoformat(self.latest_refresh_time)
        return latest_refresh
    
    def to_dict(self):
        return {
            "plugin_id": self.plugin_id,
            "name": self.name,
            "plugin_settings": self.settings,
            "refresh": self.refresh,
            "latest_refresh_time": self.latest_refresh_time,
            "display_duration": self.display_duration,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            plugin_id=data["plugin_id"],
            name=data["name"],
            settings=data["plugin_settings"],
            refresh=data["refresh"],
            latest_refresh_time=data.get("latest_refresh_time"),
            display_duration=data.get("display_duration"),
        )