# MousePods on Windows

MousePods runs on Windows 10 and 11 (64-bit Python 3.10 or newer). It has the same controller, web
app, calibration and head tracking as on Linux. Only the parts that touch the operating system are
different:

| | Linux | Windows |
|---|---|---|
| AirPods data channel (L2CAP PSM `0x1001`) | kernel socket | **MagicAAP driver** (see below) |
| Mouse | `/dev/uinput` | `SendInput` |
| Shortcuts Ctrl+Alt+C / P / S / M | GNOME custom keybindings | built in, active while MousePods runs |
| Native games (TrackIR / FreeTrack) | DLLs installed per Proton prefix (Games page) | DLLs registered once for your user (Simulators page) |
| opentrack | UDP | UDP (same) |
| Start at login | XDG autostart / Flatpak portal | `HKCU\…\CurrentVersion\Run` |
| Virtual joystick, Sony / Pixel headsets, Garmin | yes | not yet |

## Why a driver is needed

AirPods expose their motion sensors over AAP, Apple's accessory protocol, on L2CAP channel `0x1001`.
Windows lets applications open RFCOMM channels to classic Bluetooth devices, but **not L2CAP channels**.
No user-mode API can reach the AirPods' sensors.

The **MagicAAP** profile driver (by the author of MagicPods) opens that channel inside the Bluetooth
stack and gives applications a device to read and write AAP packets. MousePods uses it the same way
[airpods-head-track](https://github.com/StarNumber12046/airpods-head-track) does. MagicAAP is not part
of MousePods, and its official build is **not signed**: Windows only loads it in **test mode**.

Test mode has costs you should weigh first:
- a "Test Mode" watermark on the desktop;
- some anti-cheat systems (Vanguard, FACEIT, some EAC games) refuse to run with it on;
- it lowers a protection against malicious drivers, so do not use it on a work or managed PC.

## Install

1. **Pair the AirPods** in *Settings > Bluetooth & devices* and check they play audio.
2. **Install MagicAAP** following https://magicpods.app/magicaap/. That usually means turning on test
   mode (`bcdedit /set testsigning on` from an administrator terminal, then reboot) and installing the
   driver. After that, with the AirPods connected, a MagicAAP device appears under *Bluetooth* in
   Device Manager.
3. **Install Python** from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH").
4. **Get MousePods** and run the installer script from the repository folder:
   ```powershell
   git clone https://github.com/starman-tech/mousepods
   cd mousepods
   powershell -ExecutionPolicy Bypass -File packaging\windows\install.ps1
   ```
   It creates `.venv`, installs the dependencies and adds **MousePods** to the Start menu.
5. Open **MousePods** from the Start menu (or double-click `MousePods.pyw`). It opens in an Edge or
   Chrome app window and starts the controller in the background.

From a terminal: `.venv\Scripts\python airpods_mouse_controller.py --ui`, `--diag`, `--send recenter`
and the other options work as on Linux.

Close **MagicPods** (or any other app using MagicAAP) while MousePods runs: only one app can hold the
AirPods channel. The status line says so if it is taken.

## Games

On the **Simulators** page, switch on **Act as a TrackIR**. MousePods then points the TrackIR
(`NPClient`) and FreeTrack registry keys of your user at its own DLLs, which read the head pose from
MousePods. Start the game afterwards: games look for TrackIR when they start. When you switch it off,
the previous tracker's keys (TrackIR software, opentrack) come back.

The DLLs are in `native\out\`. They are built on Linux (`native/build.sh`) or downloaded from the
`native-dlls` artifact of the CI build. The page tells you if they are missing.

opentrack works as well: Input **UDP over network** on the opentrack side, and *Send to opentrack* in
MousePods, as on Linux.

## Known limits

- **AirPods 3 may stay silent.** They only announce their RemoteHID sensors when the L2CAP MTU is large
  (2582, iOS's value; DISCOVERIES §6). On Linux MousePods sets it. On Windows the driver chooses the
  MTU, and we do not know MagicAAP's value. If the log says "aucun capteur annoncé" with AirPods 3, this
  is the likely reason. AirPods Pro 2 use SensorDataWX, which works with a normal MTU.
- **Connecting** is left to Windows: it connects paired AirPods when they leave the case. MousePods
  cannot ask Windows to connect or disconnect them, so the "full Bluetooth reconnect" recovery used on
  Linux only reopens the AAP channel here.
- **Elevated windows** (run as administrator) do not accept input from a normal process. Run MousePods
  elevated too if you need to control them.
- Windows' *Enhance pointer precision* adds its acceleration on top of MousePods' own (mouse mode).
  Turn it off in *Mouse settings* if the cursor feels too fast at high speeds.

## AirPods Pro (1st generation)

Product ID `0x200E`, H1 chip. What is known:
- They send motion as **SensorDataWX** frames (RTBuddy `0x00100000`), like the AirPods Pro 2, not
  RemoteHID like the AirPods 3. So the MTU limit above does not apply to them. Source: head-tracking
  data captured on AirPods Pro 1 in [librepods-org/librepods#719](https://github.com/librepods-org/librepods/issues/719).
- The start request reported working on them is LibrePods' one: the **ACTIVITY** service (14) at
  40 ms, left running. The AirPods Pro 2 use **DEVMOTION6** (16) instead, which the first version of
  this port asked for: the AirPods Pro 1 then connected (ear detection worked) but sent no motion.

MousePods now starts the AirPods Pro 1 with ACTIVITY at the configured rate. If nothing arrives within
4 s it tries ACTIVITY at 40 ms, then DEVMOTION6, and around again until one answers. The log says
which one worked (`Motion data: SensorDataWX service …`) and prints the first SensorDataWX packets it
could not decode, so a failing pair can still be diagnosed from a log.

If you have a pair, run `python airpods_mouse_controller.py --diag -v` and open a
[device report](https://github.com/starman-tech/mousepods/issues/new?template=device_report.yml) with
the log.
