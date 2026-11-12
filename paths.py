# SPDX-License-Identifier: Apache-2.0
"""Where MousePods writes its files.

From a source checkout everything stays next to the program (as upstream). Inside the Flatpak
the program directory is read-only, so the launcher sets MOUSEPODS_DATA_DIR to a writable place.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def in_flatpak():
    return bool(os.environ.get("FLATPAK_ID"))


def data_dir():
    return os.environ.get("MOUSEPODS_DATA_DIR") or HERE


def captures_dir(private=False):
    d = os.path.join(data_dir(), "captures", "private") if private else os.path.join(data_dir(), "captures")
    os.makedirs(d, exist_ok=True)
    return d


def is_windows():
    import sys
    return sys.platform == "win32"


def runtime_dir():
    """Per-user directory for sockets, tokens and pose files.
    Linux: XDG_RUNTIME_DIR (else /tmp, as upstream). Windows: %LOCALAPPDATA%\\MousePods, whose ACL
    already limits it to the user."""
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        d = os.path.join(base, "MousePods")
        os.makedirs(d, exist_ok=True)
        return d
    return os.environ.get("XDG_RUNTIME_DIR", "/tmp")
