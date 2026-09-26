import threading
import time
import os
import logging
import psutil
import pytz
from datetime import datetime, timezone
from plugins.plugin_registry import get_plugin_instance
from utils.image_utils import compute_image_hash
from model import RefreshInfo, Event
from PIL import Image

logger = logging.getLogger(__name__)

PLAYLIST_REFRESH_TYPE = "Playlist"
MANUAL_REFRESH_TYPE = "Manual Update"
# Never wake more often than this; after a failed refresh back off further so a broken plugin cannot spin the loop.
MIN_SLEEP_SECONDS = 1
FAILURE_BACKOFF_SECONDS = 60

class RefreshTask:
    """Handles the logic for refreshing the display using a background thread."""

    def __init__(self, device_config, display_manager):
        self.device_config = device_config
        self.display_manager = display_manager

        self.thread = None
        self.lock = threading.Lock()
        self.condition = threading.Condition(self.lock)
        self.running = False
        self.manual_update_request = ()

        self.refresh_event = threading.Event()
        self.refresh_event.set()
        self.refresh_result = {}
        self.last_refresh_failed = False

    def start(self):
        """Starts the background thread for refreshing the display."""
        if not self.thread or not self.thread.is_alive():
            logger.info("Starting refresh task")
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.running = True
            self.thread.start()

    def stop(self):
        """Stops the refresh task by notifying the background thread to exit."""
        with self.condition:
            self.running = False
            self.condition.notify_all()  # Wake the thread to let it exit
        if self.thread:
            logger.info("Stopping refresh task")
            self.thread.join()

    def _run(self):
        """Background task that manages the periodic refresh of the display.

        This function runs in a loop, sleeping until the currently displayed playlist item's display duration
        expires (its own `display_duration`, or `plugin_cycle_interval_seconds` when unset), until that item's
        own refresh rule is due, or until manually triggered via `manual_update()`. Determines the next plugin
        to refresh based on active playlists and updates the display accordingly.

        Workflow:
        1. Waits until the current item's slot ends, its refresh rule fires, or a manual update / config change
           notifies the thread.
        2. Checks if a manual update has been requested:
        - If so, refreshes the specified plugin immediately.
        3. Otherwise, determines the next plugin to refresh based on the active playlist and generates an image.
           If the current item is not due to be replaced but its refresh rule is due, regenerates it in place.
        4. Compares the image hash with the last displayed image hash.
        - If the image has changed, updates the display.
        - If the image is the same, skips the refresh.
        5. Updates the refresh metadata in the device configuration.
        6. Repeats the process until `stop()` is called.

        Handles any exceptions that occur during the refresh process and ensures the refresh event is set 
        to indicate completion.

        Exceptions:
        - Captures and logs any unexpected errors during execution to prevent the thread from exiting.
        """
        while True:
            try:
                with self.condition:
                    sleep_time = self._compute_sleep_time(
                        self.device_config.get_playlist_manager(),
                        self.device_config.get_refresh_info(),
                        self._get_current_datetime())

                    # Wait for sleep_time or until notified
                    self.condition.wait(timeout=sleep_time)
                    self.refresh_result = {}
                    self.refresh_event.clear()

                    # Exit if `stop()` is called
                    if not self.running:
                        break

                    playlist_manager = self.device_config.get_playlist_manager()
                    latest_refresh = self.device_config.get_refresh_info()
                    current_dt = self._get_current_datetime()

                    refresh_action = None
                    # True when a different playlist item takes the screen, which starts a new display slot.
                    new_slot = False
                    if self.manual_update_request:
                        # handle immediate update request
                        logger.info("Manual update requested")
                        refresh_action = self.manual_update_request
                        self.manual_update_request = ()
                        new_slot = True
                    else:

                        if self.device_config.get_config("log_system_stats"):
                            self.log_system_stats()

                        # handle refresh based on playlists
                        logger.info(f"Running interval refresh check. | current_time: {current_dt.strftime('%Y-%m-%d %H:%M:%S')}")
                        playlist, plugin_instance = self._determine_next_plugin(playlist_manager, latest_refresh, current_dt)
                        if plugin_instance:
                            refresh_action = PlaylistRefresh(playlist, plugin_instance)
                            new_slot = True
                        else:
                            # keep the item on screen fresh according to its own refresh rule
                            owner, current_instance = self._determine_regeneration(playlist_manager, latest_refresh, current_dt)
                            if current_instance:
                                refresh_action = PlaylistRefresh(owner, current_instance)

                    self.last_refresh_failed = False
                    if refresh_action:
                        plugin_config = self.device_config.get_plugin(refresh_action.get_plugin_id())
                        if plugin_config is None:
                            logger.error(f"Plugin config not found for '{refresh_action.get_plugin_id()}'.")
                            continue
                        plugin = get_plugin_instance(plugin_config)
                        image = refresh_action.execute(plugin, self.device_config, current_dt)
                        image_hash = compute_image_hash(image)

                        refresh_info = refresh_action.get_refresh_info()
                        refresh_info.update({"refresh_time": current_dt.isoformat(), "image_hash": image_hash})
                        if new_slot:
                            refresh_info["slot_start_time"] = current_dt.isoformat()
                        elif latest_refresh.slot_start_time:
                            refresh_info["slot_start_time"] = latest_refresh.slot_start_time
                        # check if image is the same as current image
                        if image_hash != latest_refresh.image_hash:
                            logger.info(f"Updating display. | refresh_info: {refresh_info}")
                            self.display_manager.display_image(image, image_settings=plugin.config.get("image_settings", []))
                        else:
                            logger.info(f"Image already displayed, skipping refresh. | refresh_info: {refresh_info}")

                        # update latest refresh data in the device config
                        self.device_config.refresh_info = RefreshInfo(**refresh_info)
                        self.device_config.write_config()

            except Exception as e:
                logger.exception('Exception during refresh')
                self.refresh_result["exception"] = e  # Capture exception
                self.last_refresh_failed = True
            finally:
                self.refresh_event.set()

    def manual_update(self, refresh_action):
        """Manually triggers an update for the specified plugin id and plugin settings by notifying the background process."""
        if self.running:
            with self.condition:
                self.manual_update_request = refresh_action
                self.refresh_result = {}
                self.refresh_event.clear()

                self.condition.notify_all()  # Wake the thread to process manual update

            self.refresh_event.wait()
            if self.refresh_result.get("exception"):
                raise self.refresh_result.get("exception")
        else:
            logger.warning("Background refresh task is not running, unable to do a manual update")

    def signal_config_change(self):
        """Notify the background thread that config has changed (e.g., interval updated)."""
        if self.running:
            with self.condition:
                self.condition.notify_all()

    def _get_current_datetime(self):
        """Retrieves the current datetime based on the device's configured timezone."""
        tz_str = self.device_config.get_config("timezone", default="UTC")
        return datetime.now(pytz.timezone(tz_str))

    def _get_global_cycle_interval(self):
        """Returns the device-wide plugin cycle interval in seconds."""
        return self.device_config.get_config("plugin_cycle_interval_seconds", default=3600)

    def _current_slot(self, playlist_manager, latest_refresh_info, current_dt):
        """Describes the playlist item currently on screen.

        Returns (instance, slot_seconds, elapsed_seconds). `instance` is None when nothing from a playlist
        is on screen (first run, manual update, or the item was deleted); `slot_seconds` then falls back to
        the global cycle interval and `elapsed_seconds` is None.
        """
        global_interval = self._get_global_cycle_interval()
        if latest_refresh_info is None or latest_refresh_info.refresh_type != PLAYLIST_REFRESH_TYPE:
            return None, global_interval, None

        instance = playlist_manager.find_plugin(latest_refresh_info.plugin_id, latest_refresh_info.plugin_instance)
        if instance is None:
            return None, global_interval, None

        slot_start = latest_refresh_info.get_slot_start_datetime()
        elapsed = (current_dt - slot_start).total_seconds() if slot_start else None
        return instance, instance.get_display_duration(global_interval), elapsed

    def _compute_sleep_time(self, playlist_manager, latest_refresh_info, current_dt):
        """Seconds to sleep before the next scheduling check.

        The earliest of: the current item's slot ending, the current item's own refresh rule firing, and the
        next playlist or event window boundary (so switches happen on time).
        """
        instance, slot_seconds, elapsed = self._current_slot(playlist_manager, latest_refresh_info, current_dt)
        candidates = []

        if instance is not None and elapsed is not None:
            remaining_slot = slot_seconds - elapsed
            # When the slot is already over but nothing advanced (e.g. no active playlist), sleep a full slot
            # rather than spinning.
            candidates.append(remaining_slot if remaining_slot > 0 else slot_seconds)
            next_regeneration = instance.seconds_until_next_refresh(current_dt)
            if next_regeneration is not None:
                candidates.append(next_regeneration)
        else:
            candidates.append(slot_seconds)

        next_boundary = playlist_manager.seconds_until_next_boundary(current_dt)
        if next_boundary is not None:
            candidates.append(next_boundary)

        floor = FAILURE_BACKOFF_SECONDS if self.last_refresh_failed else MIN_SLEEP_SECONDS
        return max(min(candidates), floor)

    def _resolve_target(self, playlist_manager, current_dt):
        """What should own the screen now: an active event, else the active playlist with items, else None."""
        event = playlist_manager.determine_active_event(current_dt)
        if event:
            return event
        playlist = playlist_manager.determine_active_playlist(current_dt)
        if playlist and playlist.plugins:
            return playlist
        return None

    def _determine_regeneration(self, playlist_manager, latest_refresh_info, current_dt):
        """Returns (playlist, instance) when the item on screen should be regenerated in place, else (None, None)."""
        instance, _, _ = self._current_slot(playlist_manager, latest_refresh_info, current_dt)
        if instance is None or not instance.should_refresh(current_dt):
            return None, None
        owner = playlist_manager.find_plugin_owner(instance.plugin_id, instance.name)
        if owner is None:
            return None, None
        logger.info(f"Refreshing current plugin instance in place. | playlist: {owner.name} | plugin_instance: {instance.name}")
        return owner, instance

    def _determine_next_plugin(self, playlist_manager, latest_refresh_info, current_dt):
        """Determines the next plugin to show based on the active event or playlist, the current item's display
        duration, and the current time. Returns (owner, plugin) or (None, None) when nothing should change."""
        target = self._resolve_target(playlist_manager, current_dt)
        if target is None:
            playlist_manager.active_playlist = None
            logger.info(f"No active playlist or event determined.")
            return None, None

        playlist_manager.active_playlist = target.name
        instance, slot_seconds, elapsed = self._current_slot(playlist_manager, latest_refresh_info, current_dt)
        # the item on screen belongs to a playlist or event that is no longer the target: switch now
        switched = instance is not None and target.find_plugin(instance.plugin_id, instance.name) is None

        if isinstance(target, Event):
            if instance is not None and not switched:
                return None, None
            logger.info(f"Event took the screen. | event: {target.name} | plugin_instance: {target.plugin.name}")
            return target, target.plugin

        if instance is not None and not switched and elapsed is not None and elapsed < slot_seconds:
            logger.info(f"Not time to update display. | current_instance: {instance.name} | elapsed: {int(elapsed)}s | display_duration: {slot_seconds}s")
            return None, None

        plugin = target.get_next_plugin()
        logger.info(f"Determined next plugin. | active_playlist: {target.name} | plugin_instance: {plugin.name}")

        return target, plugin
    
    def log_system_stats(self):
        metrics = {
            'cpu_percent': psutil.cpu_percent(interval=1),
            'memory_percent': psutil.virtual_memory().percent,
            'disk_percent': psutil.disk_usage('/').percent,
            'load_avg_1_5_15': os.getloadavg(),
            'swap_percent': psutil.swap_memory().percent,
            'net_io': {
                'bytes_sent': psutil.net_io_counters().bytes_sent,
                'bytes_recv': psutil.net_io_counters().bytes_recv
            }
        }

        logger.info(f"System Stats: {metrics}")

class RefreshAction:
    """Base class for a refresh action. Subclasses should override the methods below."""
    
    def refresh(self, plugin, device_config, current_dt):
        """Perform a refresh operation and return the updated image."""
        raise NotImplementedError("Subclasses must implement the refresh method.")
    
    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        raise NotImplementedError("Subclasses must implement the get_refresh_info method.")
    
    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        raise NotImplementedError("Subclasses must implement the get_plugin_id method.")

class ManualRefresh(RefreshAction):
    """Performs a manual refresh based on a plugin's ID and its associated settings.
    
    Attributes:
        plugin_id (str): The ID of the plugin to refresh.
        plugin_settings (dict): The settings for the manual refresh.
    """

    def __init__(self, plugin_id: str, plugin_settings: dict):
        self.plugin_id = plugin_id
        self.plugin_settings = plugin_settings

    def execute(self, plugin, device_config, current_dt: datetime):
        """Performs a manual refresh using the stored plugin ID and settings."""
        return plugin.generate_image(self.plugin_settings, device_config)

    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        return {"refresh_type": MANUAL_REFRESH_TYPE, "plugin_id": self.plugin_id}

    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        return self.plugin_id

class PlaylistRefresh(RefreshAction):
    """Performs a refresh using a plugin instance within a playlist context.

    Attributes:
        playlist: The playlist object associated with the refresh.
        plugin_instance: The plugin instance to refresh.
    """

    def __init__(self, playlist, plugin_instance, force=False):
        self.playlist = playlist
        self.plugin_instance = plugin_instance
        self.force = force

    def get_refresh_info(self):
        """Return refresh metadata as a dictionary."""
        return {
            "refresh_type": PLAYLIST_REFRESH_TYPE,
            "playlist": self.playlist.name,
            "plugin_id": self.plugin_instance.plugin_id,
            "plugin_instance": self.plugin_instance.name
        }

    def get_plugin_id(self):
        """Return the plugin ID associated with this refresh."""
        return self.plugin_instance.plugin_id

    def execute(self, plugin, device_config, current_dt: datetime):
        """Performs a refresh for the specified plugin instance within its playlist context."""
        # Determine the file path for the plugin's image
        plugin_image_path = os.path.join(device_config.plugin_image_dir, self.plugin_instance.get_image_path())

        # Check if a refresh is needed based on the plugin instance's criteria
        if self.plugin_instance.should_refresh(current_dt) or self.force:
            logger.info(f"Refreshing plugin instance. | plugin_instance: '{self.plugin_instance.name}'") 
            # Generate a new image
            image = plugin.generate_image(self.plugin_instance.settings, device_config)
            image.save(plugin_image_path)
            self.plugin_instance.latest_refresh_time = current_dt.isoformat()
        else:
            logger.info(f"Not time to refresh plugin instance, using latest image. | plugin_instance: {self.plugin_instance.name}.")
            # Load the existing image from disk
            with Image.open(plugin_image_path) as img:
                image = img.copy()

        return image