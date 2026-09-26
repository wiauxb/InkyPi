import logging

logger = logging.getLogger(__name__)

MAX_DISPLAY_DURATION_SECONDS = 24 * 60 * 60

def parse_display_duration(refresh_settings):
    """Parses the optional per-instance display duration from a refresh settings form payload.

    Returns a tuple (seconds_or_None, error_or_None). None seconds means "use the global
    plugin cycle interval". The form sends displayMode ("default" | "custom"), displayDuration
    and displayUnit ("minute" | "hour").
    """
    if refresh_settings.get("displayMode", "default") != "custom":
        return None, None
    value = refresh_settings.get("displayDuration")
    unit = refresh_settings.get("displayUnit")
    if not unit or unit not in ["minute", "hour"]:
        return None, "Display duration unit is required"
    if value is None or not str(value).strip().isnumeric():
        return None, "Display duration is required"
    seconds = calculate_seconds(int(value), unit)
    if seconds <= 0 or seconds > MAX_DISPLAY_DURATION_SECONDS:
        return None, "Display duration must be between 1 minute and 24 hours"
    return seconds, None

def calculate_seconds(interval, unit):
    seconds = 5 * 60 # default to five minutes
    if unit == "minute":
        seconds = interval * 60
    elif unit == "hour":
        seconds = interval * 60 * 60
    elif unit == "day":
        seconds = interval * 60 * 60 * 24
    else:
        logger.warning(f"Unrecognized unit: {unit}, defaulting to 5 minutes")
    return seconds