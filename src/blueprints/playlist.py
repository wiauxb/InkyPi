from flask import Blueprint, request, jsonify, current_app, render_template
from utils.time_utils import calculate_seconds, parse_display_duration
from model import PluginInstance, Playlist
import json
from datetime import datetime, timedelta
import os
import logging
from utils.app_utils import resolve_path, handle_request_files, parse_form


logger = logging.getLogger(__name__)
playlist_bp = Blueprint("playlist", __name__)

EVENT_TARGET_PREFIX = "event:"

def _parse_event_payload(data):
    """Validates the JSON body of the event routes. Returns (fields, error)."""
    name = (data.get("name") or "").strip()
    start_time, end_time = data.get("start_time"), data.get("end_time")
    date = (data.get("date") or "").strip() or None
    days = data.get("days")
    if not name:
        return None, "Event name is required"
    if not start_time or not end_time:
        return None, "Start time and End time are required"
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return None, "Date must be YYYY-MM-DD"
        days = None
    elif days is not None:
        if not isinstance(days, list) or any(not isinstance(d, int) or d < 0 or d > 6 for d in days):
            return None, "Days must be a list of weekday numbers 0-6"
        days = days or None
    if Playlist(name, start_time, end_time).get_time_range_minutes() <= 0:
        return None, "End time must be after start time"
    return {"name": name, "start_time": start_time, "end_time": end_time, "date": date, "days": days}, None

@playlist_bp.route('/move_plugin_instance', methods=['POST'])
def move_plugin_instance():
    """Reorders an item, or moves it to another playlist or event (drag and drop)."""
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    data = request.get_json() or {}
    source, destination = data.get("source"), data.get("destination")
    plugin_id, instance_name = data.get("plugin_id"), data.get("plugin_instance")
    index, mode = data.get("index"), data.get("mode", "move")
    if not all([source, destination, plugin_id, instance_name]):
        return jsonify({"error": "Missing required fields"}), 400
    if index is not None and (not isinstance(index, int) or index < 0):
        return jsonify({"error": "Index must be a non-negative integer"}), 400

    global_interval = device_config.get_config("plugin_cycle_interval_seconds", default=3600)
    error, displaced = playlist_manager.move_plugin(source, plugin_id, instance_name, destination, index,
                                                    global_interval, mode=mode)
    if error:
        return jsonify({"error": error}), 400
    if displaced is not None:
        from blueprints.plugin import _delete_plugin_instance_images
        _delete_plugin_instance_images(device_config, displaced)

    device_config.write_config()
    # the item on screen may have moved, or the active event's screen changed
    current_app.config['REFRESH_TASK'].signal_config_change()
    return jsonify({"success": True, "message": f"Moved '{instance_name}' to '{destination}'."})

@playlist_bp.route('/create_event', methods=['POST'])
def create_event():
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    fields, error = _parse_event_payload(request.get_json() or {})
    if error:
        return jsonify({"error": error}), 400
    if playlist_manager.get_playlist_or_event(fields["name"]):
        return jsonify({"error": f"A playlist or event named '{fields['name']}' already exists"}), 400

    playlist_manager.add_event(**fields)
    device_config.write_config()
    current_app.config['REFRESH_TASK'].signal_config_change()
    return jsonify({"success": True, "message": "Created new Event!"})

@playlist_bp.route('/update_event/<string:event_name>', methods=['PUT'])
def update_event(event_name):
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    if not playlist_manager.get_event(event_name):
        return jsonify({"error": f"Event '{event_name}' does not exist"}), 400
    fields, error = _parse_event_payload(request.get_json() or {})
    if error:
        return jsonify({"error": error}), 400
    new_name = fields.pop("name")
    if new_name != event_name and playlist_manager.get_playlist_or_event(new_name):
        return jsonify({"error": f"A playlist or event named '{new_name}' already exists"}), 400

    playlist_manager.update_event(event_name, new_name, **fields)
    device_config.write_config()
    current_app.config['REFRESH_TASK'].signal_config_change()
    return jsonify({"success": True, "message": f"Updated event '{new_name}'!"})

@playlist_bp.route('/delete_event/<string:event_name>', methods=['DELETE'])
def delete_event(event_name):
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    event = playlist_manager.get_event(event_name)
    if not event:
        return jsonify({"error": f"Event '{event_name}' does not exist"}), 400

    if event.plugin:
        from blueprints.plugin import _delete_plugin_instance_images
        _delete_plugin_instance_images(device_config, event.plugin)

    playlist_manager.delete_event(event_name)
    device_config.write_config()
    current_app.config['REFRESH_TASK'].signal_config_change()
    return jsonify({"success": True, "message": f"Deleted event '{event_name}'!"})

@playlist_bp.route('/add_plugin', methods=['POST'])
def add_plugin():
    device_config = current_app.config['DEVICE_CONFIG']
    refresh_task = current_app.config['REFRESH_TASK']
    playlist_manager = device_config.get_playlist_manager()

    try:
        plugin_settings = parse_form(request.form)
        refresh_settings = json.loads(plugin_settings.pop("refresh_settings"))
        plugin_id = plugin_settings.pop("plugin_id")

        playlist = refresh_settings.get('playlist')
        instance_name = refresh_settings.get('instance_name')
        if not playlist:
            return jsonify({"error": "Playlist name is required"}), 400
        if not instance_name or not instance_name.strip():
            return jsonify({"error": "Instance name is required"}), 400
        if not all(char.isalpha() or char.isspace() or char.isnumeric() for char in instance_name):
            return jsonify({"error": "Instance name can only contain alphanumeric characters and spaces"}), 400
        refresh_type = refresh_settings.get('refreshType')
        if not refresh_type or refresh_type not in ["interval", "scheduled"]:
            return jsonify({"error": "Refresh type is required"}), 400

        existing = playlist_manager.find_plugin(plugin_id, instance_name)
        if existing:
            return jsonify({"error": f"Plugin instance '{instance_name}' already exists"}), 400

        # targets are playlist names, or "event:<name>" for an event's single screen
        target_event = None
        if playlist.startswith(EVENT_TARGET_PREFIX):
            target_event = playlist_manager.get_event(playlist[len(EVENT_TARGET_PREFIX):])
            if not target_event:
                return jsonify({"error": f"Event '{playlist[len(EVENT_TARGET_PREFIX):]}' not found"}), 400

        if refresh_type == "interval":
            unit, interval = refresh_settings.get('unit'), refresh_settings.get("interval")
            if not unit or unit not in ["minute", "hour", "day"]:
                return jsonify({"error": "Refresh interval unit is required"}), 400
            if not interval:
                return jsonify({"error": "Refresh interval is required"}), 400
            refresh_interval_seconds = calculate_seconds(int(interval), unit)
            refresh_config = {"interval": refresh_interval_seconds}
        else:
            refresh_time = refresh_settings.get('refreshTime')
            if not refresh_settings.get('refreshTime'):
                return jsonify({"error": "Refresh time is required"}), 400
            refresh_config = {"scheduled": refresh_time}

        display_duration, duration_error = parse_display_duration(refresh_settings)
        if duration_error:
            return jsonify({"error": duration_error}), 400

        if target_event is None:
            target_playlist = playlist_manager.get_playlist(playlist)
            if not target_playlist:
                return jsonify({"error": f"Playlist '{playlist}' not found"}), 400
            global_interval = device_config.get_config("plugin_cycle_interval_seconds", default=3600)
            candidate = PluginInstance("candidate", "candidate", {}, refresh_config, display_duration=display_duration)
            window_error = target_playlist.validate_durations(global_interval, extra_plugin=candidate)
            if window_error:
                return jsonify({"error": window_error}), 400

        plugin_settings.update(handle_request_files(request.files))
        plugin_dict = {
            "plugin_id": plugin_id,
            "refresh": refresh_config,
            "plugin_settings": plugin_settings,
            "name": instance_name,
            # an event's screen is shown for its whole window, so it carries no display duration
            "display_duration": None if target_event else display_duration
        }
        if target_event is not None:
            if target_event.plugin:
                from blueprints.plugin import _delete_plugin_instance_images
                _delete_plugin_instance_images(device_config, target_event.plugin)
            result = target_event.add_plugin(plugin_dict)
        else:
            result = playlist_manager.add_plugin_to_playlist(playlist, plugin_dict)
        if not result:
            return jsonify({"error": "Failed to add to playlist"}), 500

        device_config.write_config()
        # the new screen may belong to the active event, or change the next boundary
        refresh_task.signal_config_change()
    except Exception as e:
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500
    return jsonify({"success": True, "message": "Scheduled refresh configured."})

@playlist_bp.route('/playlist')
def playlists():
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()
    refresh_info = device_config.get_refresh_info()
    plugins_list = device_config.get_plugins()

    global_interval = device_config.get_config("plugin_cycle_interval_seconds", default=3600)
    # per playlist: seconds needed for one pass through its items, and the window it has to do it in
    playlist_usage = {
        p.name: {"items_seconds": p.total_item_duration(global_interval), "window_seconds": p.get_time_range_minutes() * 60}
        for p in playlist_manager.playlists
    }

    now = datetime.now()
    events = []
    for event in playlist_manager.events:
        event_dict = event.to_dict()
        event_dict["expired"] = event.is_expired(now)
        events.append(event_dict)

    return render_template(
        'playlist.html',
        playlist_config=playlist_manager.to_dict(),
        refresh_info=refresh_info.to_dict(),
        plugins={p["id"]: p for p in plugins_list},
        plugin_cycle_interval_seconds=global_interval,
        playlist_usage=playlist_usage,
        events=events,
        weekday_names=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    )

@playlist_bp.route('/create_playlist', methods=['POST'])
def create_playlist():
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    data = request.json
    playlist_name = data.get("playlist_name")
    start_time = data.get("start_time")
    end_time = data.get("end_time")

    if not playlist_name or not playlist_name.strip():
        return jsonify({"error": "Playlist name is required"}), 400
    if not start_time or not end_time:
        return jsonify({"error": "Start time and End time are required"}), 400

    try:
        playlist = playlist_manager.get_playlist(playlist_name)
        if playlist:
            return jsonify({"error": f"Playlist with name '{playlist_name}' already exists"}), 400

        result = playlist_manager.add_playlist(playlist_name, start_time, end_time)
        if not result:
            return jsonify({"error": "Failed to create playlist"}), 500

        # save changes to device config file
        device_config.write_config()

    except Exception as e:
        logger.exception("EXCEPTION CAUGHT: " + str(e))
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

    return jsonify({"success": True, "message": "Created new Playlist!"})


@playlist_bp.route('/update_playlist/<string:playlist_name>', methods=['PUT'])
def update_playlist(playlist_name):
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    data = request.get_json()

    new_name = data.get("new_name")
    start_time = data.get("start_time")
    end_time = data.get("end_time")
    if not new_name or not start_time or not end_time:
        return jsonify({"success": False, "error": "Missing required fields"}), 400

    playlist = playlist_manager.get_playlist(playlist_name)
    if not playlist:
        return jsonify({"error": f"Playlist '{playlist_name}' does not exist"}), 400

    # the new window must still fit one pass through the items
    global_interval = device_config.get_config("plugin_cycle_interval_seconds", default=3600)
    candidate = Playlist(new_name, start_time, end_time, [p.to_dict() for p in playlist.plugins])
    window_error = candidate.validate_durations(global_interval)
    if window_error:
        return jsonify({"error": window_error}), 400

    result = playlist_manager.update_playlist(playlist_name, new_name, start_time, end_time)
    if not result:
        return jsonify({"error": "Failed to update playlist"}), 500
    device_config.write_config()

    return jsonify({"success": True, "message": f"Updated playlist '{playlist_name}'!"})

@playlist_bp.route('/delete_playlist/<string:playlist_name>', methods=['DELETE'])
def delete_playlist(playlist_name):
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    if not playlist_name:
        return jsonify({"error": f"Playlist name is required"}), 400

    playlist = playlist_manager.get_playlist(playlist_name)
    if not playlist:
        return jsonify({"error": f"Playlist '{playlist_name}' does not exist"}), 400

    # Delete all images associated with plugin instances in this playlist
    from blueprints.plugin import _delete_plugin_instance_images
    for plugin_instance in playlist.plugins:
        _delete_plugin_instance_images(device_config, plugin_instance)

    playlist_manager.delete_playlist(playlist_name)
    device_config.write_config()

    return jsonify({"success": True, "message": f"Deleted playlist '{playlist_name}'!"})

@playlist_bp.app_template_filter('format_duration')
def format_duration(seconds):
    """Formats a number of seconds as a short human string, e.g. '5 min', '1 h', '1 h 30 min'."""
    seconds = int(seconds or 0)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours} h {minutes} min"
    if hours:
        return f"{hours} h"
    return f"{minutes} min"

@playlist_bp.app_template_filter('format_duration_short')
def format_duration_short(seconds):
    """Compact duration for badges: '5m', '1h', '1h30'."""
    seconds = int(seconds or 0)
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h{minutes:02d}"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"

@playlist_bp.app_template_filter('format_relative_time')
def format_relative_time(iso_date_string):
    # Parse the input ISO date string
    dt = datetime.fromisoformat(iso_date_string)

    # Get the timezone from the parsed datetime
    if dt.tzinfo is None:
        raise ValueError("Input datetime doesn't have a timezone.")

    # Get the current time in the same timezone as the input datetime
    now = datetime.now(dt.tzinfo)
    delta = now - dt

    # Compute time difference
    diff_seconds = delta.total_seconds()
    diff_minutes = diff_seconds / 60

    # Define formatting
    time_format = "%I:%M %p"  # Example: 04:30 PM
    month_day_format = "%b %d at " + time_format  # Example: Feb 12 at 04:30 PM

    # Determine relative time string
    if diff_seconds < 120:
        return "just now"
    elif diff_minutes < 60:
        return f"{int(diff_minutes)} minutes ago"
    elif dt.date() == now.date():
        return "today at " + dt.strftime(time_format).lstrip("0")
    elif dt.date() == (now.date() - timedelta(days=1)):
        return "yesterday at " + dt.strftime(time_format).lstrip("0")
    else:
        return dt.strftime(month_day_format).replace(" 0", " ")  # Removes leading zero in day
