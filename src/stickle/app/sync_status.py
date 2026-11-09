"""How sync stands, in words."""

from PySide6.QtCore import QObject

from stickle.sync.outcome import Stopped


class SyncWords(QObject):
    def stopped(self, why: Stopped, cloud: str) -> str:
        """Sync stopped until the user does something, and why."""
        match why:
            case Stopped.SIGN_IN:
                return self.tr("Sync stopped — check your server password")
            case Stopped.NO_ACCESS:
                return self.tr(
                    "This account can't write to that folder. Check its permissions on the server."
                )
            case Stopped.BIG_CHANGE:
                return self.tr("Sync stopped — check a big change from another computer")
            case Stopped.VAULT_GONE:
                return self.tr("Sync stopped — the Stickle folder is gone from %1").replace(
                    "%1", cloud
                )
            case Stopped.VAULT_CHANGED:
                return self.tr("Sync stopped — the Stickle folder was made anew")
            case Stopped.CERTIFICATE:
                return self.tr("Sync stopped — the server's certificate changed")
            case Stopped.NEWER:
                return self.tr("Sync stopped — update Stickle to carry on")

    def full(self, cloud: str) -> str:
        return self.tr("Uploads stopped — your %1 is full").replace("%1", cloud)
