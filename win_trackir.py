# SPDX-License-Identifier: Apache-2.0
"""TrackIR / FreeTrack for native Windows games, with MousePods' own DLLs (native/).

On Linux the Games page installs the DLLs into each Proton prefix. On Windows a game looks the DLLs
up in the user's registry, as with a real TrackIR or opentrack:
  HKCU\\Software\\NaturalPoint\\NATURALPOINT\\NPClient Location   Path = <folder with NPClient(64).dll>\\
  HKCU\\Software\\Freetrack\\FreeTrackClient                      Path = <folder with freetrackclient(64).dll>\\
  HKCU\\Software\\MousePods                                       PoseFile = <the pose file below>
The DLLs map the 64-byte pose file (layout in game_bridge.py) and report a centred pose when it
stops changing, so a game never freezes on the last head angle when MousePods quits.

The values that were there before (TrackIR software, opentrack, ...) are saved under
HKCU\\Software\\MousePods\\Previous and put back when the output is switched off, unless another
tracker has taken the key over in the meantime.
"""
import logging
import mmap
import os
import struct
import threading

import paths
from game_bridge import MAX_DT, SIZE, VERSION
from game_profiles import ProfileFilter, effective_profile, normalize_baseline, normalize_profile

log = logging.getLogger(__name__)

KEY_NPCLIENT = r"Software\NaturalPoint\NATURALPOINT\NPClient Location"
KEY_FREETRACK = r"Software\Freetrack\FreeTrackClient"
KEY_MOUSEPODS = r"Software\MousePods"
KEY_PREVIOUS = KEY_MOUSEPODS + r"\Previous"
DLLS = ("NPClient.dll", "NPClient64.dll", "freetrackclient.dll", "freetrackclient64.dll")
ABSENT = "\x00absent"          # saved marker: the key had no Path before us
TRACKER_KEYS = (("npclient", KEY_NPCLIENT), ("freetrack", KEY_FREETRACK))


def pose_file_path():
    return os.path.join(paths.runtime_dir(), "trackir-pose")


def ansi_safe(path):
    """The DLLs (and many games) read these paths with the ANSI API. A path the ANSI code page
    cannot spell (non-ASCII user name, say) is replaced by its 8.3 short form, which is ASCII."""
    try:
        path.encode("mbcs", "strict")
        return path
    except UnicodeEncodeError:
        pass
    except LookupError:          # not Windows (tests): ASCII is the portable test
        if path.isascii():
            return path
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetShortPathNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        k32.GetShortPathNameW.restype = ctypes.c_uint32
        buf = ctypes.create_unicode_buffer(1024)
        if k32.GetShortPathNameW(path, buf, 1024):
            return buf.value
    except (OSError, AttributeError):
        pass
    log.warning("path not readable by the TrackIR DLLs (no short name): %s", path)
    return path


def _dir_value(d):
    return d.rstrip("\\/") + "\\"


class Registry:
    """The few winreg calls used here, behind an interface the tests replace with a dict."""

    def __init__(self):
        import winreg
        self.w = winreg

    def get(self, key, name):
        try:
            with self.w.OpenKey(self.w.HKEY_CURRENT_USER, key) as k:
                return self.w.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def set(self, key, name, value):
        with self.w.CreateKey(self.w.HKEY_CURRENT_USER, key) as k:
            self.w.SetValueEx(k, name, 0, self.w.REG_SZ, value)

    def delete(self, key, name):
        try:
            with self.w.OpenKey(self.w.HKEY_CURRENT_USER, key, 0, self.w.KEY_SET_VALUE) as k:
                self.w.DeleteValue(k, name)
        except OSError:
            pass


def register(reg, dll_dir, pose_file):
    """Point the TrackIR and FreeTrack keys at our DLLs, saving what was there (once)."""
    ours = _dir_value(dll_dir)
    for tag, key in TRACKER_KEYS:
        current = reg.get(key, "Path")
        if current != ours and reg.get(KEY_PREVIOUS, tag) is None:
            reg.set(KEY_PREVIOUS, tag, current if current is not None else ABSENT)
        reg.set(key, "Path", ours)
    reg.set(KEY_MOUSEPODS, "PoseFile", pose_file)


def unregister(reg, dll_dir):
    """Put back the saved values, only where the key still points at us."""
    ours = _dir_value(dll_dir)
    for tag, key in TRACKER_KEYS:
        prev = reg.get(KEY_PREVIOUS, tag)
        if reg.get(key, "Path") == ours:
            if prev is None or prev == ABSENT:
                reg.delete(key, "Path")
            else:
                reg.set(key, "Path", prev)
        reg.delete(KEY_PREVIOUS, tag)


def foreign_tracker(reg, dll_dir):
    """Path of another tracker registered now (None if none or ours)."""
    ours = _dir_value(dll_dir)
    for _, key in TRACKER_KEYS:
        p = reg.get(key, "Path")
        if p and p != ours:
            return p
    return None


class WinPoseFile:
    """The 64-byte pose file, mapped. Windows variant of game_bridge.PoseFile (no uid / fchmod)."""

    def __init__(self, path):
        self.path = path
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            if os.fstat(fd).st_size != SIZE:
                os.ftruncate(fd, SIZE)
            self.mm = mmap.mmap(fd, SIZE)
        finally:
            os.close(fd)
        self.seq = struct.unpack_from("<I", self.mm, 0)[0]
        struct.pack_into("<I", self.mm, 4, VERSION)

    def write(self, pose6, t):
        struct.pack_into("<6dd", self.mm, 8, *pose6, t)
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        struct.pack_into("<I", self.mm, 0, self.seq)

    def close(self):
        self.mm.close()   # the file stays: a running game keeps it mapped (see native/README.md)


class TrackIRBridge:
    """Same interface as game_bridge.GameBridge (active / configure / tick / close), one output for
    every game. cfg: config "trackir" = {"enabled": bool, "profile": {...}}."""

    def __init__(self, dll_dir, reg=None, pose_path=None, pose_factory=WinPoseFile):
        self.dll_dir = dll_dir
        self.reg = reg or Registry()
        self.pose_path = pose_path or pose_file_path()
        self.pose_factory = pose_factory
        self.pf = None
        self.flt = None
        self.profile = None
        self.last_now = None
        self.error = None
        self.lock = threading.Lock()

    @property
    def active(self):
        return self.pf is not None

    def missing_dlls(self):
        return [n for n in DLLS if not os.path.isfile(os.path.join(self.dll_dir, n))]

    def configure(self, cfg, baseline=None):
        with self.lock:
            profile = effective_profile(normalize_profile((cfg or {}).get("profile")), normalize_baseline(baseline))
            if self.pf is None:
                missing = self.missing_dlls()
                if missing:
                    self.error = "missing_dll"
                    log.error("TrackIR output: DLLs missing in %s (%s); build them with native/build.sh",
                              self.dll_dir, ", ".join(missing))
                    return
                try:
                    self.pf = self.pose_factory(self.pose_path)
                    self.dll_dir = ansi_safe(self.dll_dir)
                    register(self.reg, self.dll_dir, ansi_safe(self.pose_path))
                except OSError as e:
                    self.error = str(e)
                    log.error("TrackIR output unavailable: %s", e)
                    if self.pf:
                        self.pf.close()
                        self.pf = None
                    return
                self.error = None
                log.info("TrackIR / FreeTrack: games load %s", self.dll_dir)
            if profile != self.profile:
                self.profile, self.flt = profile, ProfileFilter(profile)

    def tick(self, now, pose6):
        with self.lock:
            if self.pf is None:
                return
            dt = 0.0 if self.last_now is None else min(max(now - self.last_now, 0.0), MAX_DT)
            self.last_now = now
            self.pf.write(self.flt.apply(pose6, dt), now)

    def close(self):
        with self.lock:
            if self.pf is None:
                return
            try:
                unregister(self.reg, self.dll_dir)
            except OSError as e:
                log.warning("TrackIR registry not restored: %s", e)
            self.pf.close()
            self.pf = None

    def status(self):
        return {"active": self.active, "error": self.error, "dll_dir": self.dll_dir,
                "foreign": foreign_tracker(self.reg, self.dll_dir) if not self.active else None}
