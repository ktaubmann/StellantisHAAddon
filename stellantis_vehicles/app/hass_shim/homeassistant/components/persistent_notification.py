"""Notifications are logged and kept in memory for the ingress UI."""
import logging
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)


def async_create(hass, message: str, title: str | None = None, notification_id: str | None = None) -> None:
    _LOGGER.warning("NOTIFICATION %s: %s", title or "", message)
    if notification_id is not None:
        # Like HA: a notification with the same id replaces the previous one.
        hass.notifications[:] = [n for n in hass.notifications if n.get("id") != notification_id]
    hass.notifications.append({
        "id": notification_id,
        "title": title,
        "message": message,
        "created": datetime.now(timezone.utc).isoformat(),
    })


def async_dismiss(hass, notification_id: str) -> None:
    hass.notifications[:] = [n for n in hass.notifications if n.get("id") != notification_id]
