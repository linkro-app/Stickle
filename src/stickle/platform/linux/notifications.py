"""A desktop notification on Linux, through org.freedesktop.Notifications.

Every desktop has the service, GNOME too, which shows no tray icon to send one
from. It is sent from a thread of its own: a slow or missing service never
holds up the notes, and a notification that does not show changes nothing,
the note itself showing what it was for.
"""

import logging
import threading

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection

log = logging.getLogger(__name__)

NOTIFICATIONS = DBusAddress(
    "/org/freedesktop/Notifications",
    bus_name="org.freedesktop.Notifications",
    interface="org.freedesktop.Notifications",
)
APP_ICON = "co.linkro.stickle"
TIMEOUT_S = 5


def _send(title: str, text: str) -> None:
    message = new_method_call(
        NOTIFICATIONS,
        "Notify",
        "susssasa{sv}i",
        ("Stickle", 0, APP_ICON, title, text, [], {}, -1),
    )
    try:
        bus = open_dbus_connection(bus="SESSION")
    except (OSError, ValueError, KeyError) as error:  # no session bus
        log.warning("could not show a notification: %s", type(error).__name__)
        return
    try:
        serial = next(bus.outgoing_serial)
        bus.send(message, serial=serial)
        while True:  # the answer, past what else the bus says first
            answer = bus.receive(timeout=TIMEOUT_S)
            if answer.header.fields.get(HeaderFields.reply_serial) == serial:
                if answer.header.message_type == MessageType.error:
                    name = str(answer.header.fields.get(HeaderFields.error_name, ""))
                    log.warning("could not show a notification: %s", name)
                return
    except (OSError, TimeoutError) as error:
        log.warning("could not show a notification: %s", type(error).__name__)
    finally:
        bus.close()


def notify(title: str, text: str) -> None:
    threading.Thread(target=_send, args=(title, text), daemon=True).start()
