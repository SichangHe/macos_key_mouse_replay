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
last_event_time = None
pressed_keys = set()
playback_source_file = None  # Holds the file we only read from

# Controllers
mouse_ctrl = MouseController()
keyboard_ctrl = KeyboardController()

# Custom Bidirectional Key Mapping for special keys
SPECIAL_KEY_MAP = {
    "cmd": Key.cmd,
    "shift": Key.shift,
    "caps_lock": Key.caps_lock,
    "alt": Key.alt,
    "ctrl": Key.ctrl,
    "space": Key.space,
    "enter": Key.enter,
    "tab": Key.tab,
    "backspace": Key.backspace,
    "esc": Key.esc,
    "f1": Key.f1,
    "f2": Key.f2,
    "up": Key.up,
    "down": Key.down,
    "left": Key.left,
    "right": Key.right,
}
REVERSE_SPECIAL_KEY_MAP = {v: k for k, v in SPECIAL_KEY_MAP.items()}


def send_notification(title, message):
    """Sends a native macOS notification via osascript."""
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    applescript = f'display notification "{safe_message}" with title "{safe_title}"'
    subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True)


def key_to_repr(key):
    """Translates a pynput Key or KeyCode object to a highly-readable string."""
    if key in REVERSE_SPECIAL_KEY_MAP:
        return REVERSE_SPECIAL_KEY_MAP[key]
    if isinstance(key, KeyCode):
        if key.char is not None:
            return key.char
        if hasattr(key, "vk") and key.vk is not None:
            return f"vk_{key.vk}"
    return str(key)


def repr_to_key(repr_str):
    """Translates a highly-readable string back into a pynput Key or KeyCode."""
    if repr_str in SPECIAL_KEY_MAP:
        return SPECIAL_KEY_MAP[repr_str]
    if repr_str.startswith("vk_"):
        try:
            return KeyCode.from_vk(int(repr_str.split("_")[1]))
        except ValueError:
            pass
    return KeyCode.from_char(repr_str)


def get_event_delay():
    """Calculates the relative delay since the last action."""
    global last_event_time
    now = time.time()
    if last_event_time is None:
        delay = 0.0
    else:
        delay = now - last_event_time
    last_event_time = now
    return round(delay, 4)


def resolve_playback_source():
    """Determines what file we read from for loop playback, without setting it for saves."""
    global playback_source_file
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if not target.endswith(".json"):
            target += ".json"
        playback_source_file = os.path.abspath(target)
        if os.path.exists(playback_source_file):
            load_macro_from_file(playback_source_file)
        else:
            send_notification(
                "Macro Engine",
                f"Playback target not found. F1 will save a new timestamped file.",
            )
        return

    macro_files = glob.glob(os.path.join(os.getcwd(), "macro_*.json"))
    if macro_files:
        latest_file = max(macro_files, key=os.path.getmtime)
        playback_source_file = latest_file
        load_macro_from_file(latest_file)
    else:
        playback_source_file = None


def save_macro_to_file():
    """Always writes recording to a brand-new timestamped file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_filename = os.path.join(os.getcwd(), f"macro_{timestamp}.json")

    try:
        with open(timestamped_filename, "w") as f:
            json.dump(recorded_events, f, indent=2)
        send_notification(
            "Macro Engine", f"Saved: {os.path.basename(timestamped_filename)}"
        )
    except Exception as e:
        send_notification("Macro Engine Error", f"Write failure: {str(e)}")


def load_macro_from_file(filepath):
    """Loads relative-timed macro events from a JSON file."""
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
    # Match working implementation: record on initial press for atomic playbacks
    if pressed:
        recorded_events.append(
            {
                "type": "mouse_click",
                "delay": get_event_delay(),
                "x": x,
                "y": y,
                "button": button.name,
            }
        )


def on_move(x, y):
    if not recording:
        return
    recorded_events.append(
        {"type": "mouse_move", "delay": get_event_delay(), "x": x, "y": y}
    )


def on_press(key):
    global recording, playing, last_event_time
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

        recorded_events.append(
            {"type": "key_press", "delay": get_event_delay(), "key": key_to_repr(key)}
        )


def on_release(key):
    global recording
    if key in pressed_keys:
        pressed_keys.remove(key)

    if recording:
        if key in [keyboard.Key.f1, keyboard.Key.f2, keyboard.Key.esc]:
            return

        recorded_events.append(
            {"type": "key_release", "delay": get_event_delay(), "key": key_to_repr(key)}
        )


def toggle_recording():
    global recording, last_event_time, recorded_events
    if not recording:
        send_notification("Macro Engine", "🔴 Recording started... Press F1 to stop.")
        recorded_events = []
        last_event_time = time.time()
        recording = True
    else:
        recording = False
        # Remove trailing F1 hotkey releases from the recorded file array
        while recorded_events and recorded_events[-1].get("key") == "f1":
            recorded_events.pop()
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
        f"▶️ Looping: {os.path.basename(playback_source_file) if playback_source_file else 'Unsaved Macro'}",
    )

    while playing:
        for event in recorded_events:
            if not playing:
                break

            if event["delay"] > 0:
                time.sleep(event["delay"])

            try:
                if event["type"] == "mouse_move":
                    mouse_ctrl.position = (event["x"], event["y"])

                elif event["type"] == "mouse_click":
                    mouse_ctrl.position = (event["x"], event["y"])
                    time.sleep(0.01)

                    btn_name = event["button"]
                    button = (
                        Button[btn_name]
                        if btn_name in Button.__members__
                        else Button.left
                    )

                    # Fire working atomic, native click
                    mouse_ctrl.click(button, 1)

                elif event["type"] == "key_press":
                    key_obj = repr_to_key(event["key"])
                    keyboard_ctrl.press(key_obj)
                    time.sleep(0.01)

                elif event["type"] == "key_release":
                    key_obj = repr_to_key(event["key"])
                    keyboard_ctrl.release(key_obj)
                    time.sleep(0.01)
            except Exception:
                pass

        time.sleep(0.5)

    playing = False


# Setup file target routing
resolve_playback_source()

# Start global OS listening threads
keyboard_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
mouse_listener = mouse.Listener(on_click=on_click, on_move=on_move)
keyboard_listener.start()
mouse_listener.start()

if not playback_source_file:
    send_notification("Macro Engine", "Active. Press F1 to record.")

keyboard_listener.join()
mouse_listener.join()
