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


def format_sig_figs(val, sig_figs=5):
    """Formats a float to at most `sig_figs` significant figures."""
    if not isinstance(val, float):
        return val
    if val == 0.0:
        return 0.0
    try:
        return float(f"{val:.{sig_figs}g}")
    except ValueError:
        return val


def clean_floats(obj):
    """Recursively traverses Lists and Dicts, reducing all floats to at most 5 sig figs."""
    if isinstance(obj, float):
        return format_sig_figs(obj)
    elif isinstance(obj, dict):
        return {k: clean_floats(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_floats(x) for x in obj]
    return obj


def get_event_delay():
    """Calculates the relative delay since the last action, keeping 5 sig figs."""
    global last_event_time
    now = time.time()
    if last_event_time is None:
        delay = 0.0
    else:
        delay = now - last_event_time
    last_event_time = now
    return format_sig_figs(delay)


def append_collapsed_event(event_type, event_details):
    """Appends an event to recorded_events, grouping adjacent items of the same type."""
    global recorded_events
    if recorded_events and recorded_events[-1]["type"] == event_type:
        recorded_events[-1]["events"].append(event_details)
    else:
        recorded_events.append({"type": event_type, "events": [event_details]})


def normalize_macro_schema(blocks):
    """Normalize clean, legacy, and accidentally double-wrapped macro schemas."""
    if not isinstance(blocks, list):
        return []

    def unwrap_event_items(items):
        """Flatten wrappers such as {"events": [{...actual event...}]} recursively."""
        if not isinstance(items, list):
            return []

        flattened = []
        for item in items:
            if not isinstance(item, dict):
                continue

            # A prior migration may have wrapped an event one or more extra times.
            if set(item.keys()) == {"events"} and isinstance(item["events"], list):
                flattened.extend(unwrap_event_items(item["events"]))
            else:
                flattened.append(dict(item))
        return flattened

    def unwrap_source_fields(block):
        """Recover source.file/source.count from the old broken events wrapper."""
        source_block = dict(block)

        if source_block.get("file") or source_block.get("path"):
            source_block.pop("events", None)
            return source_block

        wrapped = unwrap_event_items(source_block.get("events", []))
        if wrapped:
            payload = wrapped[0]
            filename = payload.get("file") or payload.get("path")
            if filename:
                source_block.pop("events", None)
                if "file" in payload:
                    source_block["file"] = payload["file"]
                else:
                    source_block["path"] = payload["path"]
                source_block["count"] = payload.get(
                    "count", source_block.get("count", 1)
                )
        return source_block

    normalized = []

    for block in blocks:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")
        if not block_type:
            continue

        if block_type == "source":
            normalized.append(unwrap_source_fields(block))
            continue

        if block_type == "repeat":
            repeat_block = dict(block)
            repeat_block["events"] = normalize_macro_schema(block.get("events", []))
            normalized.append(repeat_block)
            continue

        if isinstance(block.get("events"), list):
            grouped_block = dict(block)
            grouped_block["events"] = unwrap_event_items(block["events"])

            if (
                normalized
                and normalized[-1].get("type") == block_type
                and isinstance(normalized[-1].get("events"), list)
                and normalized[-1].get("type") not in ("source", "repeat")
            ):
                normalized[-1]["events"].extend(grouped_block["events"])
            else:
                normalized.append(grouped_block)
            continue

        # Legacy flat standard event: move its details into an events array.
        details = {k: v for k, v in block.items() if k != "type"}
        if (
            normalized
            and normalized[-1].get("type") == block_type
            and isinstance(normalized[-1].get("events"), list)
            and normalized[-1].get("type") not in ("source", "repeat")
        ):
            normalized[-1]["events"].append(details)
        else:
            normalized.append({"type": block_type, "events": [details]})

    return normalized


def custom_json_dumps(obj, indent_size=2):
    """Formats JSON so that 'events' items stay on one line, formatting floats to 5 sig figs."""

    def format_val(v, current_indent):
        next_indent = current_indent + " " * indent_size

        if isinstance(v, float):
            return json.dumps(format_sig_figs(v))

        elif isinstance(v, list):
            if not v:
                return "[]"
            # See if this is the "events" array containing dictionaries to keep on one line
            is_events_list = any(isinstance(item, dict) for item in v)
            if is_events_list:
                parts = []
                for item in v:
                    # Render dict on a single line with space-separated properties
                    dict_str = ", ".join(
                        f'"{dk}": {json.dumps(dv)}' for dk, dv in item.items()
                    )
                    parts.append(f"{next_indent}{{ {dict_str} }}")
                return "[\n" + ",\n".join(parts) + f"\n{current_indent}]"
            else:
                parts = [format_val(item, next_indent) for item in v]
                return "[\n" + ",\n".join(parts) + f"\n{current_indent}]"

        elif isinstance(v, dict):
            if not v:
                return "{}"
            # Keep standard formatting with multi-line layout for top-level structures
            parts = []
            for dk, dv in v.items():
                formatted_dv = format_val(dv, next_indent)
                parts.append(f'{next_indent}"{dk}": {formatted_dv}')
            return "{\n" + ",\n".join(parts) + f"\n{current_indent}" + "}"

        else:
            return json.dumps(v)

    return format_val(obj, "")


def resolve_playback_source():
    """Determines what file we read from, processing schemas and migrating old files in-place."""
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
    """Always writes recording to a brand-new timestamped file using single-line event formatting."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timestamped_filename = os.path.join(os.getcwd(), f"macro_{timestamp}.json")

    try:
        cleaned_events = clean_floats(recorded_events)
        formatted_json = custom_json_dumps(cleaned_events)
        with open(timestamped_filename, "w") as f:
            f.write(formatted_json)
        send_notification(
            "Macro Engine", f"Saved: {os.path.basename(timestamped_filename)}"
        )
    except Exception as e:
        send_notification("Macro Engine Error", f"Write failure: {str(e)}")


def load_macro_from_file(filepath):
    """Loads macro events from a JSON file, formatting and migrating in-place if needed."""
    global recorded_events
    try:
        with open(filepath, "r") as f:
            data = json.load(f)

        # Clean up all float representations (including coordinates & delays) recursively
        data = clean_floats(data)

        # Normalize every block, not just the first one. This preserves source/repeat
        # control fields while still migrating legacy flat input events.
        normalized_data = normalize_macro_schema(data)
        migrated = normalized_data != data
        data = normalized_data

        # Overwrite the file in-place with clean formatting (migrated or already grouped)
        try:
            formatted_json = custom_json_dumps(data)
            with open(filepath, "w") as f:
                f.write(formatted_json)

            if migrated:
                send_notification(
                    "Macro Engine", "Migrated and formatted legacy layout."
                )
            else:
                send_notification("Macro Engine", "Formatted macro file in-place.")
        except Exception as e:
            # Fallback output structure safety check
            send_notification(
                "Macro Engine Warning", f"Could not format file in-place: {str(e)}"
            )

        recorded_events = data
        send_notification(
            "Macro Engine",
            f"Loaded: {os.path.basename(filepath)} ({len(recorded_events)} event blocks)",
        )
    except Exception as e:
        send_notification("Macro Engine Error", f"Failed to parse file: {str(e)}")


def on_click(x, y, button, pressed):
    if not recording:
        return
    if pressed:
        append_collapsed_event(
            "mouse_click",
            {
                "delay": get_event_delay(),
                "x": format_sig_figs(float(x)),
                "y": format_sig_figs(float(y)),
                "button": button.name,
            },
        )


def on_move(x, y):
    if not recording:
        return
    append_collapsed_event(
        "mouse_move",
        {
            "delay": get_event_delay(),
            "x": format_sig_figs(float(x)),
            "y": format_sig_figs(float(y)),
        },
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

        append_collapsed_event(
            "key_press", {"delay": get_event_delay(), "key": key_to_repr(key)}
        )


def on_release(key):
    global recording
    if key in pressed_keys:
        pressed_keys.remove(key)

    if recording:
        if key in [keyboard.Key.f1, keyboard.Key.f2, keyboard.Key.esc]:
            return

        append_collapsed_event(
            "key_release", {"delay": get_event_delay(), "key": key_to_repr(key)}
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
        # Remove trailing F1 key releases from the ending group
        if recorded_events:
            last_group = recorded_events[-1]
            if last_group["type"] in ("key_release", "key_press"):
                last_group["events"] = [
                    ev for ev in last_group["events"] if ev.get("key") != "f1"
                ]
                if not last_group["events"]:
                    recorded_events.pop()

        save_macro_to_file()


def stop_playback():
    global playing
    playing = False
    send_notification("Macro Engine", "🛑 Playback aborted.")


def execute_block(block, base_dir=None, active_sources=None):
    """Recursively processes event blocks, supporting nested repeats and sourced files."""
    global playing
    if not playing:
        return

    if active_sources is None:
        active_sources = set()
    if base_dir is None:
        base_dir = os.getcwd()

    block_type = block.get("type")

    # Local block loops
    if block_type == "repeat":
        count = block.get("count", 1)
        sub_events = block.get("events", [])
        for _ in range(count):
            for sub_block in sub_events:
                if not playing:
                    return
                execute_block(sub_block, base_dir, active_sources)
        return

    # Sourced external macro loops
    if block_type == "source":
        filename = block.get("file") or block.get("path")
        if not filename:
            return

        # Resolve paths relative to the folder containing the currently running file
        filepath = os.path.abspath(os.path.join(base_dir, filename))
        count = block.get("count", 1)

        # Recursion and circular dependency guard
        if filepath in active_sources:
            send_notification(
                "Macro Engine Error", f"Circular dependency detected: {filename}"
            )
            return

        if not os.path.exists(filepath):
            send_notification("Macro Engine Error", f"Sourced file missing: {filename}")
            return

        try:
            with open(filepath, "r") as f:
                sourced_events = json.load(f)

            # Apply the same schema handling to nested files as to the main macro.
            sourced_events = normalize_macro_schema(clean_floats(sourced_events))
        except Exception as e:
            send_notification("Macro Engine Error", f"Read error {filename}: {str(e)}")
            return

        new_active = active_sources | {filepath}
        sourced_dir = os.path.dirname(filepath)

        for _ in range(count):
            for sub_block in sourced_events:
                if not playing:
                    return
                execute_block(sub_block, sourced_dir, new_active)
        return

    # Standard event collection blocks
    events = block.get("events", [])
    for event in events:
        if not playing:
            break

        if event.get("delay", 0) > 0:
            time.sleep(event["delay"])

        try:
            if block_type == "mouse_move":
                mouse_ctrl.position = (event["x"], event["y"])

            elif block_type == "mouse_click":
                mouse_ctrl.position = (event["x"], event["y"])
                time.sleep(0.01)

                btn_name = event["button"]
                button = (
                    Button[btn_name] if btn_name in Button.__members__ else Button.left
                )
                mouse_ctrl.click(button, 1)

            elif block_type == "key_press":
                key_obj = repr_to_key(event["key"])
                keyboard_ctrl.press(key_obj)
                time.sleep(0.01)

            elif block_type == "key_release":
                key_obj = repr_to_key(event["key"])
                keyboard_ctrl.release(key_obj)
                time.sleep(0.01)
        except Exception:
            pass


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

    # Establish the initial base directory of the active execution context
    base_dir = (
        os.path.dirname(playback_source_file) if playback_source_file else os.getcwd()
    )

    while playing:
        for block in recorded_events:
            if not playing:
                break
            execute_block(block, base_dir=base_dir)

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
