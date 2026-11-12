# SPDX-License-Identifier: Apache-2.0
"""Windows port: structure layouts, parsing, the MagicAAP channel socket, SendInput pointer,
command channel, TrackIR registry handling, autostart, and the AirPods Pro (1st gen) handling.

Everything here runs on any OS: Win32 calls are replaced by fakes. On a real Windows machine the
layout tests also check the sizes Windows itself expects.
Run:  python -m unittest tests.test_windows -v
"""
import ctypes
import os
import select
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import airpods_mouse_controller as m  # noqa: E402
import ctrl_channel  # noqa: E402
import win_bt  # noqa: E402
import win_input  # noqa: E402
import win_trackir  # noqa: E402

PTR64 = struct.calcsize("P") == 8
PRO1_ID = r"BTHENUM\{0000110B-0000-1000-8000-00805F9B34FB}_VID&0001004C_PID&200E\7&2A4E2C0F&0&F0B3EC123456_C00000000"
MAGICAAP_PATH = (r"\\?\bthenum#{74ec2172-0bad-4d01-8f77-997b2be0722a}_vid&0001004c_pid&200e"
                 r"#7&1b6e2e2f&0&70aed5c8ec3d_c00000000#{9eec98bb-3c54-45d4-a843-7900c4635e08}")


@unittest.skipUnless(PTR64, "layouts checked for 64-bit Python (what Windows users run)")
class LayoutTests(unittest.TestCase):
    """Sizes and offsets from the Windows SDK headers (x64)."""

    def test_setupapi(self):
        self.assertEqual(ctypes.sizeof(win_bt.GUID), 16)
        self.assertEqual(ctypes.sizeof(win_bt.SP_DEVINFO_DATA), 32)
        self.assertEqual(ctypes.sizeof(win_bt.SP_DEVICE_INTERFACE_DATA), 32)
        self.assertEqual(win_bt.DETAIL_CBSIZE, 8)

    def test_bluetooth_api(self):
        self.assertEqual(ctypes.sizeof(win_bt.BLUETOOTH_DEVICE_SEARCH_PARAMS), 40)
        # WCHAR is 2 bytes on Windows (560 there); ctypes.c_wchar is 4 bytes on Linux
        name = 248 * ctypes.sizeof(ctypes.c_wchar)
        self.assertEqual(ctypes.sizeof(win_bt.BLUETOOTH_DEVICE_INFO), (64 + name + 7) // 8 * 8)
        if ctypes.sizeof(ctypes.c_wchar) == 2:
            self.assertEqual(ctypes.sizeof(win_bt.BLUETOOTH_DEVICE_INFO), 560)
        self.assertEqual(win_bt.BLUETOOTH_DEVICE_INFO.Address.offset, 8)
        self.assertEqual(win_bt.BLUETOOTH_DEVICE_INFO.fConnected.offset, 20)
        self.assertEqual(win_bt.BLUETOOTH_DEVICE_INFO.szName.offset, 64)

    def test_overlapped(self):
        self.assertEqual(ctypes.sizeof(win_bt.OVERLAPPED), 32)
        self.assertEqual(win_bt.OVERLAPPED.hEvent.offset, 24)

    def test_sendinput(self):
        self.assertEqual(ctypes.sizeof(win_input.INPUT), 40)
        self.assertEqual(win_input.INPUT.u.offset, 8)
        self.assertTrue(win_input.input_size_ok())

    def test_msg(self):
        self.assertEqual(ctypes.sizeof(win_input.MSG), 48)
        self.assertEqual(win_input.MSG.wParam.offset, 16)


class ParsingTests(unittest.TestCase):
    def test_address_from_u64(self):
        self.assertEqual(win_bt.addr_from_u64(0xF0B3EC123456), "F0:B3:EC:12:34:56")

    def test_address_hex_roundtrip(self):
        self.assertEqual(win_bt.addr_to_hex("F0:B3:EC:12:34:56"), "f0b3ec123456")
        self.assertEqual(win_bt.addr_from_hex("f0b3ec123456"), "F0:B3:EC:12:34:56")
        with self.assertRaises(ValueError):
            win_bt.addr_to_hex("nope")

    def test_pro1_instance_id(self):
        self.assertEqual(win_bt.apple_pid_in_id(PRO1_ID), "200e")
        self.assertEqual(win_bt.addr_in_path(PRO1_ID), "F0:B3:EC:12:34:56")

    def test_magicaap_interface_path(self):
        self.assertEqual(win_bt.addr_in_path(MAGICAAP_PATH), "70:AE:D5:C8:EC:3D")
        self.assertEqual(win_bt.apple_pid_in_id(MAGICAAP_PATH), "200e")

    def test_dev_node_without_profile(self):
        # BTHENUM\Dev_<addr> nodes carry no VID/PID: ignored, the profile nodes give the model
        ids = [r"BTHENUM\Dev_F0B3EC123456\8&1&BluetoothDevice_F0B3EC123456", PRO1_ID,
               r"BTHENUM\{0000110B-0000-1000-8000-00805F9B34FB}_VID&000205AC_PID&0256\7&1&0&001122334455_C00000000"]
        self.assertEqual(win_bt.pids_from_instance_ids(ids), {"F0:B3:EC:12:34:56": "200e"})

    def test_not_apple(self):
        self.assertIsNone(win_bt.apple_pid_in_id(r"BTHENUM\{...}_VID&0001000A_PID&0001\7&0&001122334455_C00000000"))


class DeviceListTests(unittest.TestCase):
    def test_listing_shape_matches_bluez(self):
        with mock.patch.object(win_bt, "bthenum_instance_ids", return_value=[PRO1_ID]), \
                mock.patch.object(win_bt, "magicaap_paths", return_value={}), \
                mock.patch.object(win_bt, "paired_classic_devices",
                                  return_value=[("F0:B3:EC:12:34:56", "AirPods Pro", True),
                                                ("00:11:22:33:44:55", "Keyboard", True)]):
            self.assertEqual(win_bt.apple_audio_devices(), [("F0:B3:EC:12:34:56", "AirPods Pro", "200e", True)])

    def test_magicaap_interface_counts_as_connected(self):
        with mock.patch.object(win_bt, "bthenum_instance_ids", return_value=[PRO1_ID]), \
                mock.patch.object(win_bt, "magicaap_paths", return_value={"F0:B3:EC:12:34:56": "p"}), \
                mock.patch.object(win_bt, "paired_classic_devices",
                                  return_value=[("F0:B3:EC:12:34:56", "AirPods Pro", False)]):
            self.assertTrue(win_bt.info("f0:b3:ec:12:34:56")["connected"])


class DriverDetectionTests(unittest.TestCase):
    def test_driver_installed_uses_registered_interfaces(self):
        with mock.patch.object(win_bt, "magicaap_paths", return_value={}) as mp:
            self.assertFalse(win_bt.driver_installed())          # empty set: never bound
        mp.assert_called_once_with(present=False)
        with mock.patch.object(win_bt, "magicaap_paths", return_value={"F0:B3:EC:12:34:56": "p"}):
            self.assertTrue(win_bt.driver_installed())

    def test_open_channel_without_driver(self):
        with mock.patch.object(win_bt, "magicaap_paths", return_value={}), \
                mock.patch.object(win_bt, "driver_installed", return_value=False):
            with self.assertRaises(win_bt.DriverMissing):
                win_bt.open_channel("F0:B3:EC:12:34:56", timeout=0)


class FakeDriver:
    def __init__(self, packets):
        self.packets = list(packets)
        self.written = []
        self.closed = False

    def read(self, buf, stop):
        while not stop():
            if self.packets:
                p = self.packets.pop(0)
                if p is None:
                    return 0
                ctypes.memmove(buf, p, len(p))
                return len(p)
            time.sleep(0.01)
        return None

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


class ChannelSocketTests(unittest.TestCase):
    def recv(self, s, timeout=2.0):
        r, _, _ = select.select([s], [], [], timeout)
        self.assertTrue(r, "no datagram")
        return s.recv(8192)

    def test_packets_keep_boundaries_and_order(self):
        pk = [m.PKT_HANDSHAKE, b"\x04\x00\x04\x00\x06\x00\x00\x00", bytes(range(256)) * 10]
        s = win_bt.ChannelSocket(FakeDriver(pk))
        try:
            self.assertEqual([self.recv(s) for _ in pk], pk)
        finally:
            s.close()

    def test_send_goes_to_the_driver(self):
        d = FakeDriver([])
        s = win_bt.ChannelSocket(d)
        s.send(m.PKT_FEATURES)
        s.close()
        self.assertEqual(d.written, [m.PKT_FEATURES])
        self.assertTrue(d.closed)

    def test_driver_eof_reads_as_closed(self):
        s = win_bt.ChannelSocket(FakeDriver([b"\x01", None]))
        try:
            self.assertEqual(self.recv(s), b"\x01")
            self.assertEqual(self.recv(s), b"")
        finally:
            s.close()

    def test_shutdown_ends_the_channel(self):
        # the UI's "reconnect" calls sock.shutdown(2): the main loop must see b"" and reconnect
        s = win_bt.ChannelSocket(FakeDriver([]))
        try:
            s.shutdown(2)
            self.assertEqual(self.recv(s), b"")
        finally:
            s.close()


class PointerTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.p = win_input.Pointer(send=lambda inputs: self.sent.extend(inputs))

    def test_relative_keeps_fractions(self):
        self.p.move_rel(0.6, 0)
        self.assertEqual(self.sent, [])
        self.p.move_rel(0.6, -1.5)
        (i,) = self.sent
        self.assertEqual((i.u.mi.dx, i.u.mi.dy, i.u.mi.dwFlags), (1, -1, win_input.MOUSEEVENTF_MOVE))
        self.assertEqual(i.u.mi.dwExtraInfo, win_input.EXTRA_INFO)

    def test_absolute_covers_virtual_desktop(self):
        self.p.move_abs(1.2, 0.5)
        self.p.move_abs(1.0, 0.5)          # same position: not sent again
        (i,) = self.sent
        self.assertEqual((i.u.mi.dx, i.u.mi.dy), (65535, 32768))
        f = i.u.mi.dwFlags
        self.assertTrue(f & win_input.MOUSEEVENTF_ABSOLUTE and f & win_input.MOUSEEVENTF_VIRTUALDESK)

    def test_click_is_down_then_up(self):
        self.p.click("right")
        self.assertEqual([i.u.mi.dwFlags for i in self.sent],
                         [win_input.MOUSEEVENTF_RIGHTDOWN, win_input.MOUSEEVENTF_RIGHTUP])

    def test_scroll_direction_and_steps(self):
        self.p.scroll(0.5)
        self.p.scroll(0.7)
        self.p.scroll(-2)
        # same rounding as the uinput Pointer: 1.2 -> 1 (0.2 kept), then 0.2 - 2 = -1.8 -> -1
        self.assertEqual([ctypes.c_int32(i.u.mi.mouseData).value for i in self.sent], [120, -120])


class CtrlChannelWindowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(ctrl_channel, "IS_WINDOWS", True),
                        mock.patch.object(ctrl_channel.paths, "runtime_dir", return_value=self.tmp.name)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_roundtrip(self):
        srv = ctrl_channel.Server()
        try:
            self.assertTrue(ctrl_channel.send("recenter"))
            select.select([srv], [], [], 2)
            self.assertEqual(srv.read(), "recenter")
        finally:
            srv.close()
        self.assertFalse(os.path.exists(ctrl_channel.info_path()))
        self.assertFalse(ctrl_channel.send("quit"))

    def test_wrong_token_is_ignored(self):
        import socket
        srv = ctrl_channel.Server()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(b"0" * 32 + b"\nquit", srv.sock.getsockname())
            s.close()
            select.select([srv], [], [], 2)
            self.assertEqual(srv.read(), "")
        finally:
            srv.close()


class FakeReg:
    def __init__(self, values=None):
        self.v = dict(values or {})

    def get(self, key, name):
        return self.v.get((key, name))

    def set(self, key, name, value):
        self.v[(key, name)] = value

    def delete(self, key, name):
        self.v.pop((key, name), None)


class TrackIRRegistryTests(unittest.TestCase):
    DLL = r"C:\MousePods\native\out"
    OURS = DLL + "\\"

    def test_register_and_restore_previous_tracker(self):
        reg = FakeReg({(win_trackir.KEY_NPCLIENT, "Path"): r"C:\Program Files\opentrack\\"})
        win_trackir.register(reg, self.DLL, r"C:\pose")
        win_trackir.register(reg, self.DLL, r"C:\pose")      # twice: the saved value stays the original
        self.assertEqual(reg.get(win_trackir.KEY_NPCLIENT, "Path"), self.OURS)
        self.assertEqual(reg.get(win_trackir.KEY_FREETRACK, "Path"), self.OURS)
        self.assertEqual(reg.get(win_trackir.KEY_MOUSEPODS, "PoseFile"), r"C:\pose")
        win_trackir.unregister(reg, self.DLL)
        self.assertEqual(reg.get(win_trackir.KEY_NPCLIENT, "Path"), r"C:\Program Files\opentrack\\")
        self.assertIsNone(reg.get(win_trackir.KEY_FREETRACK, "Path"))      # was absent: removed again
        self.assertIsNone(reg.get(win_trackir.KEY_PREVIOUS, "npclient"))

    def test_key_taken_over_later_is_left_alone(self):
        reg = FakeReg()
        win_trackir.register(reg, self.DLL, "p")
        reg.set(win_trackir.KEY_NPCLIENT, "Path", r"C:\TrackIR\\")
        win_trackir.unregister(reg, self.DLL)
        self.assertEqual(reg.get(win_trackir.KEY_NPCLIENT, "Path"), r"C:\TrackIR\\")
        self.assertEqual(win_trackir.foreign_tracker(reg, self.DLL), r"C:\TrackIR\\")

    def test_bridge_lifecycle(self):
        with tempfile.TemporaryDirectory() as d:
            for n in win_trackir.DLLS:
                open(os.path.join(d, n), "wb").close()
            pose = os.path.join(d, "pose")
            reg = FakeReg()
            b = win_trackir.TrackIRBridge(d, reg=reg, pose_path=pose)
            b.configure({"enabled": True})
            self.assertTrue(b.active)
            self.assertEqual(reg.get(win_trackir.KEY_NPCLIENT, "Path"), d + "\\")
            b.tick(1.0, (0, 0, 0, 10.0, 5.0, 0.0))
            with open(pose, "rb") as f:
                data = f.read()
            self.assertEqual(len(data), 64)
            seq, version = struct.unpack_from("<II", data)
            self.assertEqual((seq, version), (1, 1))
            self.assertEqual(struct.unpack_from("<d", data, 56)[0], 1.0)
            b.close()
            self.assertFalse(b.active)
            self.assertIsNone(reg.get(win_trackir.KEY_NPCLIENT, "Path"))

    def test_missing_dlls_reported(self):
        with tempfile.TemporaryDirectory() as d:
            b = win_trackir.TrackIRBridge(d, reg=FakeReg(), pose_path=os.path.join(d, "pose"))
            b.configure({"enabled": True})
            self.assertFalse(b.active)
            self.assertEqual(b.status()["error"], "missing_dll")


class FakeWinreg(types.SimpleNamespace):
    def __init__(self):
        super().__init__(HKEY_CURRENT_USER=1, KEY_SET_VALUE=2, REG_SZ=1, store={})

    class _K:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, key, *a):
        return self._K()

    CreateKey = OpenKey

    def SetValueEx(self, k, name, _, typ, value):
        self.store[name] = value

    def QueryValueEx(self, k, name):
        if name not in self.store:
            raise FileNotFoundError(name)
        return self.store[name], 1

    def DeleteValue(self, k, name):
        if name not in self.store:
            raise FileNotFoundError(name)
        del self.store[name]


class AutostartWindowsTests(unittest.TestCase):
    def test_run_key(self):
        from desktop import autostart
        w = FakeWinreg()
        cmd = [r"C:\Py\pythonw.exe", r"C:\My Apps\mousepods\airpods_mouse_controller.py"]
        autostart._win_set(True, cmd, winreg=w)
        self.assertEqual(w.store["MousePods"],
                         r'C:\Py\pythonw.exe "C:\My Apps\mousepods\airpods_mouse_controller.py"')
        self.assertTrue(autostart._win_enabled(winreg=w))
        autostart._win_set(False, cmd, winreg=w)
        autostart._win_set(False, cmd, winreg=w)          # already off: no error
        self.assertFalse(autostart._win_enabled(winreg=w))


class ControllerWindowsTests(unittest.TestCase):
    def test_connect_uses_magicaap(self):
        class Reached(Exception):
            pass
        c = m.AAPClient("F0:B3:EC:12:34:56", auto_connect=True)
        with mock.patch.object(m, "IS_WINDOWS", True), \
                mock.patch.object(m.win_bt, "info", return_value={"connected": True, "pid": "200e", "name": "x"}), \
                mock.patch.object(m.win_bt, "open_channel", side_effect=Reached) as oc, \
                mock.patch.object(m.l2cap, "open_seqpacket", side_effect=AssertionError("Linux path")):
            with self.assertRaises(Reached):
                c.connect()
        oc.assert_called_once_with("F0:B3:EC:12:34:56", 8)
        self.assertEqual(c.pid, "200e")

    def test_auto_without_driver_says_so(self):
        c = m.AAPClient("auto")
        with mock.patch.object(m, "IS_WINDOWS", True), \
                mock.patch.object(m.win_bt, "driver_installed", return_value=False):
            with self.assertRaises(m.win_bt.DriverMissing) as cm:
                c._pick()
        self.assertEqual(str(m.connect_failure_status(cm.exception)), m.tr("srv.status.magicaap_missing"))

    def test_bt_connect_does_not_shell_out(self):
        with mock.patch.object(m, "IS_WINDOWS", True), \
                mock.patch.object(m.subprocess, "run", side_effect=AssertionError("bluetoothctl")), \
                mock.patch.object(m.win_bt, "info", return_value=None):
            self.assertFalse(m.bt_connect("AA:BB:CC:DD:EE:FF", 5))
            m.bt_disconnect("AA:BB:CC:DD:EE:FF")

    def test_games_page_on_windows(self):
        import games_service
        with mock.patch.object(games_service.sys, "platform", "win32"), \
                mock.patch.object(games_service.steam_games, "find_steam_roots",
                                  side_effect=AssertionError("no Steam scan on Windows")):
            p = games_service.list_payload(os.devnull)
            self.assertEqual((p["platform"], p["games"]), ("windows", []))
            self.assertIsNone(games_service.find("244210"))


class Pro1Tests(unittest.TestCase):
    """AirPods Pro (1st gen), product ID 0x200E."""

    def client(self, pid):
        c = m.AAPClient("F0:B3:EC:12:34:56")
        c.pid = pid
        c.sent = []
        c.send = c.sent.append
        return c

    def test_model_name(self):
        import mousepods_web
        self.assertIn("200e", mousepods_web.MODELS)
        self.assertEqual(m.i18n.tr("model.200e", lang="en"), "AirPods Pro (1st generation)")

    def test_pro1_is_h1_but_not_remotehid_only(self):
        self.assertIn(m.PID_PRO1, m.H1_MODELS)
        self.assertNotIn(m.PID_PRO1, m.RHID_ONLY_MODELS)

    def test_pro1_starts_both_protocols(self):
        c = self.client(m.PID_PRO1)
        with mock.patch.object(m.time, "sleep"):
            c.start_tracking()
        wx = [p for p in c.sent if p[:10] == m.WX_PREFIX]
        rhid = [p for p in c.sent if p[:10] == m.RHID_PREFIX]
        self.assertEqual(len(wx), 3)                                   # ACTIVITY on, DEVMOTION6, ACTIVITY off
        self.assertIn(bytes([0x08, m.SVC_DEVMOTION6]), wx[1])
        self.assertTrue(rhid, "RemoteHID HostConnect must be tried too")

    def test_airpods3_still_remotehid_only(self):
        c = self.client(m.PID_AIRPODS3)
        c.start_tracking()
        self.assertTrue(c.sent)
        self.assertTrue(all(p[:10] == m.RHID_PREFIX for p in c.sent))

    def test_pro1_remotehid_motion_is_used(self):
        # if the Pro 1 announce devmotion6 over RemoteHID (like the AirPods 3), it is started
        c = self.client(m.PID_PRO1)
        plist = b"<?xml version><dict><key>AccessoryService</key><string>devmotion6</string></dict>"
        pkt = m.rhid_frame(1, [m.rhid_sub(5, m.RHID_CONNECT, plist)])
        c.process(pkt)
        self.assertEqual(c.rhid_motion, 5)
        report = bytes([1]) + bytes(58)
        blocks = c.process(m.rhid_frame(2, [m.rhid_sub(5, m.RHID_INPUT, report)]))
        self.assertEqual(blocks, [bytes(58)])


@unittest.skipUnless(sys.platform == "win32", "real Win32 calls (CI windows-latest job)")
class RealWindowsTests(unittest.TestCase):
    """No AirPods or Bluetooth radio needed: the calls must run and return empty results."""

    def test_bluetooth_and_setupapi_calls(self):
        self.assertIsInstance(win_bt.paired_classic_devices(), list)
        self.assertIsInstance(win_bt.bthenum_instance_ids(), list)
        self.assertIsInstance(win_bt.magicaap_paths(present=False), dict)
        self.assertIsInstance(win_bt.apple_audio_devices(), list)
        self.assertIsInstance(win_bt.driver_installed(), bool)

    def test_ui_language(self):
        import i18n
        lang = i18n.windows_ui_lang()
        self.assertTrue(lang is None or "-" in lang or len(lang) == 2, lang)

    def test_command_channel(self):
        srv = ctrl_channel.Server()
        try:
            self.assertTrue(ctrl_channel.send("pause"))
            select.select([srv], [], [], 2)
            self.assertEqual(srv.read(), "pause")
        finally:
            srv.close()

    def test_hotkeys_thread(self):
        hk = win_input.Hotkeys(lambda cmd: None)
        hk.start()
        hk.stop()
        hk.join(2)
        self.assertFalse(hk.is_alive())

    def test_sendinput_signature(self):
        p = win_input.Pointer()
        p.move_rel(0, 0)          # nothing to send; constructing loads user32 with argtypes
        p.close()

    def test_registry_and_pose_file(self):
        import winreg
        key = r"Software\MousePodsTest"
        reg = win_trackir.Registry()
        try:
            reg.set(key, "Path", "C:\\x\\")
            self.assertEqual(reg.get(key, "Path"), "C:\\x\\")
            reg.delete(key, "Path")
            self.assertIsNone(reg.get(key, "Path"))
        finally:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass
        with tempfile.TemporaryDirectory() as d:
            pf = win_trackir.WinPoseFile(os.path.join(d, "pose"))
            pf.write((0.0,) * 6, 1.0)
            pf.close()
            self.assertTrue(os.path.isdir(win_trackir.ansi_safe(d)))   # short name if needed, same folder


if __name__ == "__main__":
    unittest.main()
