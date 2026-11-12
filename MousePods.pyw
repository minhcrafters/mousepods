# SPDX-License-Identifier: Apache-2.0
"""Windows launcher: double-click (pythonw, no console). Starts the controller in the background
if it is not running, then opens the MousePods window (Edge / Chrome app window, else the browser)."""
import os
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import airpods_mouse_controller as m  # noqa: E402

m.open_ui(8737, m.DEFAULT_CONFIG_PATH)
