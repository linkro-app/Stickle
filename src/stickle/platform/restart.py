"""Starting Stickle again, as a process of its own, once this one has let go."""

import subprocess
import sys

from stickle.platform.autostart import launch_command


def start_again() -> None:
    """Start a new Stickle, the same way it is started from the app list.

    Call it only after the instance lock is released, or the new one would
    just ask this one to show its window. It is not this process's child:
    it goes on when this one ends.
    """
    command = launch_command()
    if sys.platform == "win32":
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
