# SPDX-License-Identifier: Apache-2.0
"""Windows Bluetooth for MousePods: paired AirPods, their model, and the AAP channel.

Windows does not let applications open an L2CAP channel to a classic Bluetooth device (Winsock
AF_BTH only does RFCOMM). The AirPods talk AAP on L2CAP PSM 0x1001, so on Windows the channel is
opened by the **MagicAAP** profile driver (https://magicpods.app/magicaap/). For every connected
AirPods it exposes a device interface ({9eec98bb-3c54-45d4-a843-7900c4635e08}); one ReadFile
returns one AAP packet, one WriteFile sends one. The same interface is used by
StarNumber12046/airpods-head-track.

The controller's loops are written around a socket (select + recv). `open_channel` therefore
returns a loopback UDP socket: a reader thread copies every packet read from the driver into it
(UDP keeps packet boundaries), and send() writes straight to the driver. An empty datagram means
"channel closed", exactly like recv() returning b"" on the Linux L2CAP socket.

Everything goes through ctypes (no pywin32 / winrt dependency). The pure helpers (address and
instance-ID parsing, structure layouts) are importable and tested on any platform.
"""
import ctypes
import logging
import re
import socket
import struct
import sys
import threading

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
PTR64 = struct.calcsize("P") == 8

# ------------------------------------------------------------------------------------ constants
MAGICAAP_GUID = (0x9EEC98BB, 0x3C54, 0x45D4, bytes([0xA8, 0x43, 0x79, 0x00, 0xC4, 0x63, 0x5E, 0x08]))
MAGICAAP_URL = "https://magicpods.app/magicaap/"

DIGCF_PRESENT, DIGCF_ALLCLASSES, DIGCF_DEVICEINTERFACE = 0x2, 0x4, 0x10
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
OPEN_EXISTING, FILE_FLAG_OVERLAPPED = 3, 0x40000000
ERROR_IO_PENDING, ERROR_OPERATION_ABORTED = 997, 995
ERROR_INSUFFICIENT_BUFFER, ERROR_NO_MORE_ITEMS = 122, 259
ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED = 32, 5
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 0x102
INVALID_HANDLE_VALUE = (1 << (64 if PTR64 else 32)) - 1

READ_BUFFER = 65536      # one AAP packet per read; the AirPods 3 RemoteHID frames reach ~2.5 KB


# ------------------------------------------------------------------------------------ structures
class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, spec):
        d1, d2, d3, d4 = spec
        return cls(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("ClassGuid", GUID), ("DevInst", ctypes.c_uint32),
                ("Reserved", ctypes.c_size_t)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("InterfaceClassGuid", GUID), ("Flags", ctypes.c_uint32),
                ("Reserved", ctypes.c_size_t)]


# sizeof(SP_DEVICE_INTERFACE_DETAIL_DATA_W): DWORD + WCHAR[1], padded to the struct alignment
DETAIL_CBSIZE = 8 if PTR64 else 6


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint16) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour",
                                               "wMinute", "wSecond", "wMilliseconds")]


class BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("fReturnAuthenticated", ctypes.c_int32),
                ("fReturnRemembered", ctypes.c_int32), ("fReturnUnknown", ctypes.c_int32),
                ("fReturnConnected", ctypes.c_int32), ("fIssueInquiry", ctypes.c_int32),
                ("cTimeoutMultiplier", ctypes.c_ubyte), ("hRadio", ctypes.c_void_p)]


class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("Address", ctypes.c_uint64), ("ulClassofDevice", ctypes.c_uint32),
                ("fConnected", ctypes.c_int32), ("fRemembered", ctypes.c_int32),
                ("fAuthenticated", ctypes.c_int32), ("stLastSeen", SYSTEMTIME), ("stLastUsed", SYSTEMTIME),
                ("szName", ctypes.c_wchar * 248)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32), ("hEvent", ctypes.c_void_p)]


# ------------------------------------------------------------------------------------ pure helpers
_ADDR = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
_APPLE_ID = re.compile(r"VID&000[12]004C_PID&([0-9A-F]{4})", re.I)
_ADDR_IN_ID = re.compile(r"[&\\_]([0-9A-F]{12})_C[0-9A-F]*(?:[#\\]|$)", re.I)


def addr_from_u64(v):
    """BLUETOOTH_ADDRESS (ullLong, most significant byte first in the usual notation) -> AA:BB:..."""
    return ":".join("%02X" % ((v >> (8 * i)) & 0xFF) for i in range(5, -1, -1))


def addr_to_hex(addr):
    """'AA:BB:CC:DD:EE:FF' -> 'aabbccddeeff' (how addresses appear in device paths)."""
    if not _ADDR.match(addr or ""):
        raise ValueError(f"invalid Bluetooth address {addr!r}")
    return addr.replace(":", "").replace("-", "").lower()


def addr_from_hex(h):
    h = h.upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def addr_in_path(path):
    """Bluetooth address embedded in a BTHENUM instance ID or device-interface path, or None.
    e.g. BTHENUM\\{0000110b-...}_VID&0001004C_PID&200E\\7&2a4e2c0f&0&F0B3EC123456_C00000000"""
    m = _ADDR_IN_ID.search(path or "")
    return addr_from_hex(m.group(1)) if m else None


def apple_pid_in_id(instance_id):
    """Apple product ID (lowercase hex, as BlueZ's modalias) from a BTHENUM instance ID, or None."""
    m = _APPLE_ID.search(instance_id or "")
    return m.group(1).lower() if m else None


def pids_from_instance_ids(ids):
    """{address: pid} for every Apple device among BTHENUM instance IDs."""
    out = {}
    for iid in ids:
        pid, addr = apple_pid_in_id(iid), addr_in_path(iid)
        if pid and addr:
            out.setdefault(addr, pid)
    return out


# ------------------------------------------------------------------------------------ Win32 access
class DriverMissing(OSError):
    """MagicAAP is not installed (or not loaded): no device interface class at all."""

    def __init__(self):
        super().__init__(2, f"MagicAAP driver not found: install it from {MAGICAAP_URL} "
                            "(Windows needs it to open the AirPods' Bluetooth channel)")


class ChannelBusy(OSError):
    def __init__(self):
        super().__init__(ERROR_SHARING_VIOLATION,
                         "the AirPods channel is in use by another app (MagicPods?): close it and retry")


def _dlls():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sapi = ctypes.WinDLL("setupapi", use_last_error=True)
    vp, u32, i32 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, u32, u32, vp, u32, u32, vp]
    k32.CreateFileW.restype = vp
    k32.ReadFile.argtypes = [vp, vp, u32, ctypes.POINTER(u32), ctypes.POINTER(OVERLAPPED)]
    k32.WriteFile.argtypes = [vp, vp, u32, ctypes.POINTER(u32), ctypes.POINTER(OVERLAPPED)]
    k32.ReadFile.restype = k32.WriteFile.restype = i32
    k32.GetOverlappedResult.argtypes = [vp, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(u32), i32]
    k32.GetOverlappedResult.restype = i32
    k32.CreateEventW.argtypes = [vp, i32, i32, vp]
    k32.CreateEventW.restype = vp
    k32.WaitForSingleObject.argtypes = [vp, u32]
    k32.WaitForSingleObject.restype = u32
    k32.CancelIoEx.argtypes = [vp, ctypes.POINTER(OVERLAPPED)]
    k32.CancelIoEx.restype = i32
    k32.ResetEvent.argtypes = [vp]
    k32.CloseHandle.argtypes = [vp]
    k32.CloseHandle.restype = i32
    sapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), ctypes.c_wchar_p, vp, u32]
    sapi.SetupDiGetClassDevsW.restype = vp
    sapi.SetupDiEnumDeviceInterfaces.argtypes = [vp, vp, ctypes.POINTER(GUID), u32,
                                                 ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    sapi.SetupDiEnumDeviceInterfaces.restype = i32
    sapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [vp, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), vp, u32,
                                                      ctypes.POINTER(u32), vp]
    sapi.SetupDiGetDeviceInterfaceDetailW.restype = i32
    sapi.SetupDiEnumDeviceInfo.argtypes = [vp, u32, ctypes.POINTER(SP_DEVINFO_DATA)]
    sapi.SetupDiEnumDeviceInfo.restype = i32
    sapi.SetupDiGetDeviceInstanceIdW.argtypes = [vp, ctypes.POINTER(SP_DEVINFO_DATA), ctypes.c_wchar_p, u32,
                                                 ctypes.POINTER(u32)]
    sapi.SetupDiGetDeviceInstanceIdW.restype = i32
    sapi.SetupDiDestroyDeviceInfoList.argtypes = [vp]
    return k32, sapi


_DLLS = None


def dlls():
    global _DLLS
    if _DLLS is None:
        _DLLS = _dlls()
    return _DLLS


def _err():
    e = ctypes.get_last_error()
    return OSError(e, ctypes.FormatError(e).strip() if hasattr(ctypes, "FormatError") else f"error {e}")


def _bthapi():
    for name in ("BluetoothApis", "bthprops.cpl"):
        try:
            lib = ctypes.WinDLL(name, use_last_error=True)
            break
        except OSError:
            continue
    else:
        raise OSError(2, "Windows Bluetooth API not available")
    vp = ctypes.c_void_p
    lib.BluetoothFindFirstDevice.argtypes = [ctypes.POINTER(BLUETOOTH_DEVICE_SEARCH_PARAMS),
                                             ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
    lib.BluetoothFindFirstDevice.restype = vp
    lib.BluetoothFindNextDevice.argtypes = [vp, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)]
    lib.BluetoothFindNextDevice.restype = ctypes.c_int32
    lib.BluetoothFindDeviceClose.argtypes = [vp]
    return lib


def paired_classic_devices():
    """[(address, name, connected)] of the paired (remembered/authenticated) classic devices."""
    lib = _bthapi()
    p = BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.sizeof(BLUETOOTH_DEVICE_SEARCH_PARAMS), 1, 1, 0, 1, 0, 0, None)
    info = BLUETOOTH_DEVICE_INFO()
    info.dwSize = ctypes.sizeof(BLUETOOTH_DEVICE_INFO)
    h = lib.BluetoothFindFirstDevice(ctypes.byref(p), ctypes.byref(info))
    out = []
    if not h:
        return out       # ERROR_NO_MORE_ITEMS, or no radio
    try:
        while True:
            out.append((addr_from_u64(info.Address), info.szName, bool(info.fConnected)))
            info = BLUETOOTH_DEVICE_INFO()
            info.dwSize = ctypes.sizeof(BLUETOOTH_DEVICE_INFO)
            if not lib.BluetoothFindNextDevice(h, ctypes.byref(info)):
                break
    finally:
        lib.BluetoothFindDeviceClose(h)
    return out


def bthenum_instance_ids():
    """Instance IDs of every Bluetooth-enumerated device node (present or not)."""
    _, sapi = dlls()
    h = sapi.SetupDiGetClassDevsW(None, "BTHENUM", None, DIGCF_ALLCLASSES)
    if not h or h == INVALID_HANDLE_VALUE:
        return []
    ids = []
    try:
        i = 0
        while True:
            d = SP_DEVINFO_DATA()
            d.cbSize = ctypes.sizeof(SP_DEVINFO_DATA)
            if not sapi.SetupDiEnumDeviceInfo(h, i, ctypes.byref(d)):
                break
            buf = ctypes.create_unicode_buffer(512)
            need = ctypes.c_uint32(0)
            if sapi.SetupDiGetDeviceInstanceIdW(h, ctypes.byref(d), buf, 512, ctypes.byref(need)):
                ids.append(buf.value)
            i += 1
    finally:
        sapi.SetupDiDestroyDeviceInfoList(h)
    return ids


def magicaap_paths(present=True):
    """{address or '': device path} of the MagicAAP interfaces present now (= AirPods connected
    with the driver bound). present=False also lists interfaces of devices not connected now:
    they exist once the driver has been bound to the AirPods at least once."""
    _, sapi = dlls()
    guid = GUID.of(MAGICAAP_GUID)
    flags = DIGCF_DEVICEINTERFACE | (DIGCF_PRESENT if present else 0)
    h = sapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, flags)
    if not h or h == INVALID_HANDLE_VALUE:
        raise _err()
    out = {}
    try:
        i = 0
        while True:
            ifd = SP_DEVICE_INTERFACE_DATA()
            ifd.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not sapi.SetupDiEnumDeviceInterfaces(h, None, ctypes.byref(guid), i, ctypes.byref(ifd)):
                break
            i += 1
            need = ctypes.c_uint32(0)
            sapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            if need.value < DETAIL_CBSIZE:
                continue
            buf = ctypes.create_string_buffer(need.value + 2)
            struct.pack_into("<I", buf, 0, DETAIL_CBSIZE)
            if not sapi.SetupDiGetDeviceInterfaceDetailW(h, ctypes.byref(ifd), buf, need.value, None, None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            out[addr_in_path(path) or ""] = path
    finally:
        sapi.SetupDiDestroyDeviceInfoList(h)
    return out


def driver_installed():
    """True once MagicAAP has been bound to some AirPods (its interface was registered).
    SetupAPI returns an empty set, not an error, for an interface class nobody registered."""
    try:
        return bool(magicaap_paths(present=False))
    except OSError:
        return False


# ------------------------------------------------------------------------------------ device listing
def apple_audio_devices():
    """Same shape as the BlueZ listing: [(address, name, pid, connected)] for paired Apple devices.
    Connected means "the AAP channel can be opened now": MagicAAP has an interface for it, or
    Windows reports the device connected."""
    pids = pids_from_instance_ids(bthenum_instance_ids())
    try:
        aap = magicaap_paths()
    except OSError:
        aap = {}
    out = []
    for addr, name, connected in paired_classic_devices():
        pid = pids.get(addr)
        if pid:
            out.append((addr, name, pid, connected or addr in aap))
    return out


def info(addr):
    for a, name, pid, connected in apple_audio_devices():
        if a == addr.upper():
            return {"connected": connected, "pid": pid, "name": name}
    return None


# ------------------------------------------------------------------------------------ AAP channel
class DriverHandle:
    """Overlapped read/write on a MagicAAP device handle."""

    def __init__(self, path):
        k32, _ = dlls()
        self.k32 = k32
        h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                            OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if not h or h == INVALID_HANDLE_VALUE:
            e = ctypes.get_last_error()
            if e in (ERROR_SHARING_VIOLATION, ERROR_ACCESS_DENIED):
                raise ChannelBusy()
            raise _err()
        self.h = h
        self.wlock = threading.Lock()
        self.closed = False

    def _io(self, fn, buf, n, timeout_ms, stop=None):
        """Run one overlapped ReadFile/WriteFile; returns the byte count, None on timeout (I/O
        cancelled). The buffer and OVERLAPPED stay alive until the I/O has really completed."""
        k32 = self.k32
        ev = k32.CreateEventW(None, 1, 0, None)
        if not ev:
            raise _err()
        ov = OVERLAPPED()
        ov.hEvent = ev
        done = ctypes.c_uint32(0)
        try:
            if fn(self.h, buf, n, None, ctypes.byref(ov)):
                if not k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(done), 1):
                    raise _err()
                return done.value
            if ctypes.get_last_error() != ERROR_IO_PENDING:
                raise _err()
            waited = 0
            while True:
                step = min(200, timeout_ms - waited) if timeout_ms is not None else 200
                r = k32.WaitForSingleObject(ev, max(step, 0))
                if r == WAIT_OBJECT_0:
                    break
                waited += step
                failed = r != WAIT_TIMEOUT
                if failed or (stop and stop()) or (timeout_ms is not None and waited >= timeout_ms):
                    k32.CancelIoEx(self.h, ctypes.byref(ov))
                    k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(done), 1)  # wait for it
                    if failed:
                        raise _err()
                    return None
            if not k32.GetOverlappedResult(self.h, ctypes.byref(ov), ctypes.byref(done), 1):
                e = ctypes.get_last_error()
                if e == ERROR_OPERATION_ABORTED:
                    return None
                raise _err()
            return done.value
        finally:
            k32.CloseHandle(ev)

    def read(self, buf, stop):
        return self._io(self.k32.ReadFile, buf, len(buf), None, stop)

    def write(self, data, timeout_ms=2000):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        with self.wlock:
            n = self._io(self.k32.WriteFile, buf, len(data), timeout_ms)
        if n is None:
            raise TimeoutError("AAP write timed out")
        return n

    def close(self):
        if not self.closed:
            self.closed = True
            self.k32.CancelIoEx(self.h, None)
            self.k32.CloseHandle(self.h)


class ChannelSocket(socket.socket):
    """Loopback UDP socket fed with the packets read from the driver; send() writes to the driver.
    Usable with select(); recv() returning b"" means the channel is closed."""

    def __init__(self, driver):
        super().__init__(socket.AF_INET, socket.SOCK_DGRAM)
        self._driver = driver
        self._peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.bind(("127.0.0.1", 0))
        self._peer.bind(("127.0.0.1", 0))
        self.connect(self._peer.getsockname())      # only the peer's datagrams are accepted
        self._peer.connect(self.getsockname())
        for s in (self, self._peer):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
            except OSError:
                pass
        self._stop = threading.Event()
        self._eof_sent = False
        self._eof_lock = threading.Lock()
        self._reader = threading.Thread(target=self._pump, name="magicaap-read", daemon=True)
        self._reader.start()

    def _signal_eof(self):
        with self._eof_lock:
            if self._eof_sent:
                return
            self._eof_sent = True
        try:
            self._peer.send(b"")
        except OSError:
            pass

    def _pump(self):
        buf = ctypes.create_string_buffer(READ_BUFFER)
        try:
            while not self._stop.is_set():
                n = self._driver.read(buf, self._stop.is_set)
                if n is None:
                    continue           # stop requested (checked by the loop)
                if n == 0:
                    break              # driver reports the channel closed
                self._peer.send(buf.raw[:n])
        except OSError as e:
            if not self._stop.is_set():
                log.warning("MagicAAP read failed: %s", e)
        finally:
            self._signal_eof()

    # --- the socket API the controller uses
    def send(self, data, flags=0):
        return self._driver.write(data)

    def sendall(self, data, flags=0):
        self._driver.write(data)

    def _finish(self):
        """Stop the reader (it cancels its pending read within 200 ms), then close the handle."""
        self._stop.set()
        if self._reader.is_alive() and threading.current_thread() is not self._reader:
            self._reader.join(timeout=1.0)
        self._driver.close()

    def shutdown(self, how=socket.SHUT_RDWR):
        """Used by the UI's "reconnect": end the channel so the controller's loop reconnects."""
        self._finish()
        self._signal_eof()

    def close(self):
        self._finish()
        try:
            self._peer.close()
        finally:
            super().close()


def open_channel(addr, timeout=8):
    """Open the AAP channel to `addr` through MagicAAP. The driver connects L2CAP when the
    AirPods are connected to Windows; `timeout` is how long to wait for its interface to appear."""
    import time
    want = addr.upper()
    t_end = time.monotonic() + timeout
    while True:
        paths = magicaap_paths()
        path = paths.get(want)
        if path is None and len(paths) == 1 and "" in paths:
            path = paths[""]          # address not in the path (other driver build): the only one
        if path:
            break
        if time.monotonic() >= t_end:
            if not driver_installed():
                raise DriverMissing()
            raise OSError(112, f"no MagicAAP channel for {want}: are the AirPods connected to Windows "
                               "(Settings > Bluetooth & devices) with the MagicAAP driver bound?")
        time.sleep(0.5)
    return ChannelSocket(DriverHandle(path))
