"""File operations that survive Windows file locking.

On Windows a file that another process has open cannot be replaced or
deleted ("Zugriff verweigert" / "Access is denied", WinError 5 or 32). The
plotter reads progress.json, status.json and snapshots once per second, so a
writer will now and then hit the moment the reader has the file open. The
lock lasts milliseconds: retrying briefly is enough. On Linux the first
attempt always succeeds.
"""
import os
import time

ATTEMPTS = 40          # x DELAY = up to 2 s
DELAY = 0.05


def replace(src, dst, attempts=ATTEMPTS, delay=DELAY):
    """os.replace with retries while the destination is locked by a reader."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def try_unlink(path, attempts=5, delay=DELAY):
    """Delete if possible. Returns False if it stayed locked (try again later)."""
    for i in range(attempts):
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            time.sleep(delay)
    return False
