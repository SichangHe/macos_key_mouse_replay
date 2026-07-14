# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pynput",
# ]
# ///

# --- CRITICAL MONKEYPATCH FOR PYTHON 3.14 + PYOBJC ARM64 BUG ---
try:
    import pynput._util.darwin as pynput_darwin

    class HIServicesWrapper:
        def __getattr__(self, name):
            if name in ("AXIsProcessTrusted", "AXIsProcessTrustedWithOptions"):
                return lambda *args, **kwargs: True
            return getattr(pynput_darwin.HIServices, name)

    pynput_darwin.HIServices = HIServicesWrapper()
except Exception:
    pass
# ---------------------------------------------------------------

import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from threading import Thread

from pynput import keyboard, mouse
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController

# Global state
recording = False
playing = False
recorded_events = []
start_time = None
pressed_keys = set()
active_macro_file = None

# Controllers
mouse_ctrl = MouseController()
keyboard_ctrl = KeyboardController()


def send_notification(title, message):
    """Sends a native macOS notification via osascript."""
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    applescript = f'display notification "{safe_message}" with title "{safe_title}"'
    subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)


def get_virtual_keycode(key):
    if hasattr(key, "value") and hasattr(key.value, "vk"):
        return key.value.vk
    if hasattr(key, "vk"):
        return key.vk
    return None


def resolve_macro_file():
    global active_macro_file
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if not target.endswith(".json"):
            target += ".json"
        active_macro_file = os.path.abspath(target)
        if os.path.exists(active_macro_file):
            load_macro_from_file(active_macro_file)
        else:
            send_notification(
                "Macro Engine",
                f"Ready to record to: {os.path.basename(active_macro_file)}",
            )
        return

    macro_files = glob.glob(os.path.join(os.getcwd(), "macro_*.json"))
    if macro_files:
        latest_file = max(macro_files, key=os.path.getmtime)
        active_macro_file = latest_file
        load_macro_from_file(latest_file)
    else:
        active_macro_file = None


def save_macro_to_file():
    global active_macro_file
    if active_macro_file is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        active_macro_file = os.path.join(os.getcwd(), f"macro_{timestamp}.json")

    try:
        with open(active_macro_file, "w") as f:
            json.dump(recorded_events, f, indent=2)
        send_notification(
            "Macro Engine", f"Saved: {os.path.basename(active_macro_file)}"
        )
    except Exception as e:
        send_notification("Macro Engine Error", f"Write failure: {str(e)}")


def load_macro_from_file(filepath):
    global recorded_events
    try:
        with open(filepath, "r") as f:
            recorded_events = json.load(f)
        send_notification(
            "Macro Engine",
            f"Loaded: {os.path.basename(filepath)} ({len(recorded_events)} events)",
        )
    except Exception:
        send_notification("Macro Engine Error", "Failed to resolve file schema.")


def on_click(x, y, button, pressed):
    if not recording:
        return
    # Only record on initial PRESS to capture the exact coordinates of the intent
    # This prevents split down/up sequences from getting ruined by micro-movement jitters
    if pressed:
        recorded_events.append(
            {
                "type": "mouse_click",
                "time": time.time() - start_time,
                "x": x,
                "y": y,
                "button": button.name,
            }
        )


def on_move(x, y):
    if not recording:
        return
    recorded_events.append(
        {"type": "mouse_move", "time": time.time() - start_time, "x": x, "y": y}
    )


def on_press(key):
    global recording, playing, start_time
    if key in [keyboard.Key.f1, keyboard.Key.f2, keyboard.Key.esc]:
        if key == keyboard.Key.f1:
            toggle_recording()
        elif key == keyboard.Key.f2:
            if not recording and not playing:
                Thread(target=play_macro, daemon=True).start()
        elif key == keyboard.Key.esc:
            if playing:
                stop_playback()
        return

    if recording:
        if key in pressed_keys:
            return
        pressed_keys.add(key)
        vk = get_virtual_keycode(key)
        if vk is not None:
            recorded_events.append(
                {"type": "key_press", "time": time.time() - start_time, "vk": vk}
            )


def on_release(key):
    global recording
    if key in pressed_keys:
        pressed_keys.remove(key)

    if recording:
        if key in [keyboard.Key.f1, keyboard.Key.f2, keyboard.Key.esc]:
            return
        vk = get_virtual_keycode(key)
        if vk is not None:
            recorded_events.append(
                {"type": "key_release", "time": time.time() - start_time, "vk": vk}
            )


def toggle_recording():
    global recording, start_time, recorded_events, active_macro_file
    if not recording:
        send_notification("Macro Engine", "🔴 Recording started... Press F1 to stop.")
        if len(sys.argv) == 1:
            active_macro_file = None
        recorded_events = []
        start_time = time.time()
        recording = True
    else:
        recording = False
        save_macro_to_file()


def stop_playback():
    global playing
    playing = False
    send_notification("Macro Engine", "🛑 Playback aborted.")


def play_macro():
    global playing
    if not recorded_events:
        send_notification("Macro Engine", "⚠️ No macro data loaded! Press F1.")
        return

    playing = True
    send_notification(
        "Macro Engine",
        f"▶️ Looping: {os.path.basename(active_macro_file) if active_macro_file else 'Unsaved Macro'}",
    )

    while playing:
        start_play = time.time()
        for event in recorded_events:
            if not playing:
                break

            sleep_time = (start_play + event["time"]) - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)

            try:
                if event["type"] == "mouse_move":
                    mouse_ctrl.position = (event["x"], event["y"])

                elif event["type"] == "mouse_click":
                    mouse_ctrl.position = (event["x"], event["y"])
                    # Let the system register mouse movement coordinate shift before clicking
                    time.sleep(0.01)

                    btn_name = event["button"]
                    button = (
                        Button[btn_name]
                        if btn_name in Button.__members__
                        else Button.left
                    )

                    # Fire an atomic, native OS-level mouse click
                    mouse_ctrl.click(button, 1)

                elif event["type"] == "key_press":
                    keyboard_ctrl.press(KeyCode.from_vk(event["vk"]))
                    # Give the keypress a 10ms hold time so applications register the down-state
                    time.sleep(0.01)

                elif event["type"] == "key_release":
                    keyboard_ctrl.release(KeyCode.from_vk(event["vk"]))
                    time.sleep(0.01)
            except Exception:
                pass

        # Space out playback iterations
        time.sleep(0.5)

    playing = False


# Setup active files
resolve_macro_file()

# Start global OS listening threads
keyboard_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
mouse_listener = mouse.Listener(on_click=on_click, on_move=on_move)
keyboard_listener.start()
mouse_listener.start()

if not active_macro_file:
    send_notification("Macro Engine", "Active. Press F1 to record.")

keyboard_listener.join()
mouse_listener.join()
