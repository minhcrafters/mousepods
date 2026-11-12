# SPDX-License-Identifier: Apache-2.0
"""Local command channel of the controller (--send recenter|toggle|pause|resume|scroll|mode|quit).

Linux: a Unix datagram socket in XDG_RUNTIME_DIR, mode 0600 (as upstream).
Windows has no Unix datagram sockets in Python: a UDP socket on 127.0.0.1 with a random port.
Any local process can reach a loopback port, so every datagram carries a random token, and the
port + token are kept in a file in the user's own %LOCALAPPDATA%\\MousePods.
"""
import hmac
import json
import os
import secrets
import socket
import sys

import paths

IS_WINDOWS = sys.platform == "win32"


def sock_path():
    return os.path.join(paths.runtime_dir(), "airpods-mouse.sock")


def info_path():
    return os.path.join(paths.runtime_dir(), "ctrl.json")


class Server:
    """The controller's end. `sock` is selectable; read() returns the command or ""."""

    def __init__(self):
        if IS_WINDOWS:
            self.token = secrets.token_hex(16)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("127.0.0.1", 0))
            tmp = info_path() + ".tmp"
            with open(tmp, "w", encoding="ascii") as f:
                json.dump({"port": self.sock.getsockname()[1], "token": self.token, "pid": os.getpid()}, f)
            os.replace(tmp, info_path())
        else:
            self.token = None
            path = sock_path()
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.bind(path)
            os.chmod(path, 0o600)

    def fileno(self):
        return self.sock.fileno()

    def read(self):
        data = self.sock.recv(512)
        if self.token is None:
            return data.decode(errors="ignore")
        tok, _, cmd = data.partition(b"\n")
        if not hmac.compare_digest(tok, self.token.encode()):
            return ""
        return cmd.decode(errors="ignore")

    def close(self):
        self.sock.close()
        try:
            os.unlink(info_path() if IS_WINDOWS else sock_path())
        except OSError:
            pass


def send(cmd):
    """Send one command to the running controller. False if it is not running."""
    if IS_WINDOWS:
        try:
            with open(info_path(), encoding="ascii") as f:
                info = json.load(f)
        except (OSError, ValueError):
            return False
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(f"{info['token']}\n{cmd}".encode(), ("127.0.0.1", int(info["port"])))
        except (OSError, KeyError, ValueError):
            return False
        finally:
            s.close()
        return True
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(cmd.encode(), sock_path())
    except (FileNotFoundError, ConnectionRefusedError):
        return False
    finally:
        s.close()
    return True
