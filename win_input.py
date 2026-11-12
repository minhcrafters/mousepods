# SPDX-License-Identifier: Apache-2.0
"""Windows mouse output (SendInput) and global hotkeys (RegisterHotKey) for MousePods.

Pointer has the same methods as the uinput Pointer in airpods_mouse_controller:
  * move_rel: relative motion, like a real mouse. Windows applies its pointer speed and, with
    "Enhance pointer precision", its acceleration on top (libinput does the same with uinput on Linux).
  * move_abs: absolute position on the whole virtual desktop (every monitor), 0..1 on each axis.
  * click / scroll.

Events are tagged in dwExtraInfo so they can be told apart from a real mouse. Windows does not let
a normal process inject input into an elevated (administrator) window: run MousePods elevated too
if that is needed.
"""
import ctypes
import logging
import struct
import threading

log = logging.getLogger(__name__)

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000
MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x4000, 0x8000
WHEEL_DELTA = 120
EXTRA_INFO = 0x4D504F44            # "MPOD": marks our injected events
BUTTONS = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
           "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
           "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}
ABS_MAX = 65535


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


def mouse_input(dx=0, dy=0, data=0, flags=0):
    i = INPUT()
    i.type = INPUT_MOUSE
    i.u.mi = MOUSEINPUT(int(dx), int(dy), ctypes.c_uint32(int(data) & 0xFFFFFFFF).value, flags, 0, EXTRA_INFO)
    return i


def abs_coords(fx, fy):
    """0..1 fractions -> SendInput's normalised absolute coordinates (0..65535)."""
    return (int(round(min(max(fx, 0.0), 1.0) * ABS_MAX)), int(round(min(max(fy, 0.0), 1.0) * ABS_MAX)))


class Pointer:
    """Drop-in replacement for the uinput Pointer. `send` is injectable for tests."""

    def __init__(self, send=None):
        if send is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SendInput.argtypes = [ctypes.c_uint32, ctypes.POINTER(INPUT), ctypes.c_int32]
            user32.SendInput.restype = ctypes.c_uint32

            def send(inputs):
                arr = (INPUT * len(inputs))(*inputs)
                n = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
                if n != len(inputs):
                    # blocked by UIPI (elevated window in front) or a secure desktop (UAC, lock screen)
                    log.debug("SendInput injected %d of %d events (error %d)", n, len(inputs),
                              ctypes.get_last_error())
        self._send = send
        self._fx = self._fy = 0.0
        self._wheel = 0.0
        self._last_abs = None

    def move_abs(self, fx, fy):
        ix, iy = abs_coords(fx, fy)
        if (ix, iy) == self._last_abs:
            return
        self._last_abs = (ix, iy)
        self._send([mouse_input(ix, iy, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)])

    def move_rel(self, dx, dy):
        self._fx += dx
        self._fy += dy
        ix, iy = int(self._fx), int(self._fy)
        self._fx -= ix
        self._fy -= iy
        if ix or iy:
            self._send([mouse_input(ix, iy, 0, MOUSEEVENTF_MOVE)])
            self._last_abs = None

    def click(self, button="left"):
        down, up = BUTTONS[button]
        self._send([mouse_input(flags=down), mouse_input(flags=up)])

    def scroll(self, lines):
        """lines > 0 scrolls up (away from the user), as REL_WHEEL on Linux."""
        self._wheel += lines
        n = int(self._wheel)
        if n:
            self._wheel -= n
            self._send([mouse_input(data=n * WHEEL_DELTA, flags=MOUSEEVENTF_WHEEL)])

    def close(self):
        pass


# ------------------------------------------------------------------------------------ hotkeys
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
# Same shortcuts as the GNOME ones (--install-hotkeys on Linux)
HOTKEYS = [("recenter", MOD_CONTROL | MOD_ALT, ord("C")),
           ("toggle", MOD_CONTROL | MOD_ALT, ord("P")),
           ("scroll", MOD_CONTROL | MOD_ALT, ord("S")),
           ("mode", MOD_CONTROL | MOD_ALT, ord("M"))]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32), ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t), ("time", ctypes.c_uint32), ("pt", POINT),
                ("lPrivate", ctypes.c_uint32)]


class Hotkeys(threading.Thread):
    """Registers Ctrl+Alt+C/P/S/M for the whole session and calls on_command(name) when pressed.
    RegisterHotKey binds to the calling thread, so registration and the message loop both run here."""

    def __init__(self, on_command, keys=HOTKEYS):
        super().__init__(name="hotkeys", daemon=True)
        self.on_command = on_command
        self.keys = keys
        self.registered = []
        self.failed = []
        self._tid = None
        self._ready = threading.Event()

    def run(self):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32")
        user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint32]
        user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int32]
        user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
        user32.GetMessageW.restype = ctypes.c_int32
        self._tid = k32.GetCurrentThreadId()
        for i, (cmd, mods, vk) in enumerate(self.keys, start=1):
            if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                self.registered.append(i)
            else:
                self.failed.append(cmd)      # another app already owns this shortcut
        if self.failed:
            log.warning("Hotkeys already taken by another app: %s", ", ".join(self.failed))
        self._ready.set()
        msg = MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and 1 <= msg.wParam <= len(self.keys):
                    try:
                        self.on_command(self.keys[msg.wParam - 1][0])
                    except Exception as e:  # noqa: BLE001  (a failing command must not end the loop)
                        log.warning("Hotkey command failed: %s", e)
        finally:
            for i in self.registered:
                user32.UnregisterHotKey(None, i)

    def stop(self):
        self._ready.wait(1.0)
        if self._tid:
            user32 = ctypes.WinDLL("user32")
            user32.PostThreadMessageW.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t]
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


def input_size_ok():
    """sizeof(INPUT) as Windows expects it (40 on 64-bit, 28 on 32-bit)."""
    return ctypes.sizeof(INPUT) == (40 if struct.calcsize("P") == 8 else 28)
