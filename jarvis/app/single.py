"""One JARVIS at a time, and the second one shows the first.

Double-clicking the shortcut while a copy sits hidden in the tray started
another one. Nothing complained, because nothing was watching -- four copies
were found running at once. Each had its own wake-word listener on the same
microphone, its own scheduler and its own call watcher, so saying the wake
word woke whichever heard it first: a different window from the one in front
of you, which looks exactly like the app opening a new window. It also made
the wake word erratic, four processes being a poor way to share one
microphone.

**The lock is a socket, not a file.** A lock file has to be cleaned up, and a
copy that crashes leaves one behind that blocks the next launch for no reason.
A bound port is released by the operating system however the process ends, so
a crash cannot lock anybody out.

**SO_REUSEADDR is deliberately not set.** The whole point is that the second
bind fails; that option exists to make it succeed.

The port accepts one word, `show`. It runs no commands and carries no data, so
a local process can ask for the window and nothing else.
"""
from __future__ import annotations

import socket
import threading

# Arbitrary, high, and unlikely to collide. Loopback only, so nothing beyond
# this machine can reach it at all.
HOST = "127.0.0.1"
PORT = 48219
MESSAGE = b"show"


def _tell_the_running_one() -> bool:
    """Ask the copy that already holds the port to show itself."""
    try:
        with socket.create_connection((HOST, PORT), timeout=1.5) as client:
            client.sendall(MESSAGE)
        return True
    except OSError:
        # It holds the port but will not answer -- still starting, or wedged.
        # Declining to launch anyway is right: two copies is the bug.
        return False


def claim(on_show=None):
    """Take the single-instance lock, or return None if another copy has it.

    Returns the listening socket on success. Keep a reference to it: closing
    it releases the lock. on_show is called on a worker thread whenever
    another copy is launched and asks for the window.
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind((HOST, PORT))
        holder.listen(4)
    except OSError:
        holder.close()
        _tell_the_running_one()
        return None

    def serve() -> None:
        while True:
            try:
                connection, _address = holder.accept()
            except OSError:
                return          # the socket closed; we are shutting down
            try:
                with connection:
                    connection.settimeout(1.0)
                    if connection.recv(16).strip() == MESSAGE and on_show:
                        on_show()
            except Exception:  # noqa: BLE001 - a bad caller must not end this
                continue

    threading.Thread(target=serve, daemon=True, name="single-instance").start()
    return holder
