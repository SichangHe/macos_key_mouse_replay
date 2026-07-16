# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

import json
import os
import sys
from datetime import datetime
from typing import Any

# Timing targets
FINAL_MOVE_DELAY = 0.100  # 100 ms for the single remaining mouse move
FAST_CLICK_DELAY = 0.005
FAST_RELEASE_DELAY = 0.005
CONSECUTIVE_GAP_DELAY = 0.005
TRIGGER_KEYS = {"q", "f"}


def format_sig_figs(val: Any, sig_figs: int = 5) -> Any:
    """Format numeric values to at most ``sig_figs`` significant figures."""
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        return val
    val = float(val)
    if val == 0.0:
        return 0.0
    try:
        return float(f"{val:.{sig_figs}g}")
    except ValueError:
        return val


def custom_json_dumps(obj: Any, indent_size: int = 2) -> str:
    """Pretty-print JSON while keeping each event dictionary on one line."""

    def format_val(v: Any, current_indent: str) -> str:
        next_indent = current_indent + " " * indent_size

        if isinstance(v, float):
            return json.dumps(format_sig_figs(v))

        if isinstance(v, list):
            if not v:
                return "[]"
            if all(isinstance(item, dict) for item in v):
                parts = []
                for item in v:
                    dict_str = ", ".join(
                        f'"{key}": {json.dumps(format_sig_figs(value))}'
                        for key, value in item.items()
                    )
                    parts.append(f"{next_indent}{{ {dict_str} }}")
                return "[\n" + ",\n".join(parts) + f"\n{current_indent}]"

            parts = [format_val(item, next_indent) for item in v]
            return "[\n" + ",\n".join(parts) + f"\n{current_indent}]"

        if isinstance(v, dict):
            if not v:
                return "{}"
            parts = [
                f'{next_indent}"{key}": {format_val(value, next_indent)}'
                for key, value in v.items()
            ]
            return "{\n" + ",\n".join(parts) + f"\n{current_indent}}}"

        return json.dumps(v)

    return format_val(obj, "")


def is_single_key_block(block: dict[str, Any], block_type: str, key: str | None = None) -> bool:
    events = block.get("events", [])
    if block.get("type") != block_type or len(events) != 1:
        return False
    return key is None or events[0].get("key") == key


def copy_block(block: dict[str, Any]) -> dict[str, Any]:
    """Copy an untouched block while normalizing float precision."""
    result: dict[str, Any] = {}
    for key, value in block.items():
        if key == "events" and isinstance(value, list):
            result[key] = [
                {
                    event_key: format_sig_figs(event_value)
                    if isinstance(event_value, float)
                    else event_value
                    for event_key, event_value in event.items()
                }
                for event in value
            ]
        else:
            result[key] = format_sig_figs(value) if isinstance(value, float) else value
    return result


def last_mouse_move_event(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the final event from the final non-empty mouse_move block."""
    for block in reversed(blocks):
        if block.get("type") == "mouse_move" and block.get("events"):
            return block["events"][-1]
    return None


def clean_macro(input_filepath: str) -> str | None:
    if not os.path.exists(input_filepath):
        print(f"Error: File '{input_filepath}' not found.", file=sys.stderr)
        return None

    try:
        with open(input_filepath, "r", encoding="utf-8") as file:
            blocks = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Error reading '{input_filepath}': {exc}", file=sys.stderr)
        return None

    if not isinstance(blocks, list):
        print("Error: The macro JSON must contain a top-level array.", file=sys.stderr)
        return None

    cleaned: list[dict[str, Any]] = []
    pending_moves: list[dict[str, Any]] = []
    i = 0

    while i < len(blocks):
        block = blocks[i]

        # Hold contiguous mouse moves temporarily. If the next meaningful block starts
        # a q/f click sequence, these moves are absorbed into that sequence; otherwise
        # they are written back unchanged.
        if block.get("type") == "mouse_move":
            pending_moves.append(block)
            i += 1
            continue

        is_trigger = (
            is_single_key_block(block, "key_press")
            and block["events"][0].get("key") in TRIGGER_KEYS
        )

        if is_trigger:
            trigger_key = block["events"][0]["key"]
            click_idx: int | None = None
            release_idx: int | None = None

            # Find the first click before another key press.
            j = i + 1
            while j < len(blocks):
                current_type = blocks[j].get("type")
                if current_type == "mouse_click":
                    click_idx = j
                    break
                if current_type == "key_press":
                    break
                j += 1

            # Find the matching release before another key press.
            if click_idx is not None:
                j = click_idx + 1
                while j < len(blocks):
                    if is_single_key_block(blocks[j], "key_release", trigger_key):
                        release_idx = j
                        break
                    if blocks[j].get("type") == "key_press":
                        break
                    j += 1

            if click_idx is not None and release_idx is not None:
                # Include:
                #   1. contiguous moves immediately before q/f press, and
                #   2. every move inside press -> release, including post-click moves.
                # Then keep only the absolute final move and place it before the click.
                sequence_moves = pending_moves + [
                    candidate
                    for candidate in blocks[i + 1 : release_idx]
                    if candidate.get("type") == "mouse_move"
                ]
                pending_moves = []
                final_move = last_mouse_move_event(sequence_moves)

                press_delay = block["events"][0].get("delay", CONSECUTIVE_GAP_DELAY)
                cleaned.append(
                    {
                        "type": "key_press",
                        "events": [
                            {
                                "delay": format_sig_figs(press_delay),
                                "key": trigger_key,
                            }
                        ],
                    }
                )

                if final_move is not None and "x" in final_move and "y" in final_move:
                    cleaned.append(
                        {
                            "type": "mouse_move",
                            "events": [
                                {
                                    "delay": FINAL_MOVE_DELAY,
                                    "x": format_sig_figs(float(final_move["x"])),
                                    "y": format_sig_figs(float(final_move["y"])),
                                }
                            ],
                        }
                    )

                click_events = blocks[click_idx].get("events", [])
                if click_events:
                    click_event = click_events[-1]
                    cleaned.append(
                        {
                            "type": "mouse_click",
                            "events": [
                                {
                                    "delay": FAST_CLICK_DELAY,
                                    "x": format_sig_figs(float(click_event["x"])),
                                    "y": format_sig_figs(float(click_event["y"])),
                                    "button": click_event.get("button", "left"),
                                }
                            ],
                        }
                    )

                cleaned.append(
                    {
                        "type": "key_release",
                        "events": [{"delay": FAST_RELEASE_DELAY, "key": trigger_key}],
                    }
                )

                i = release_idx + 1
                continue

        # The current block did not begin a valid q/f click sequence, so preserve any
        # mouse moves that were waiting before it.
        cleaned.extend(copy_block(move) for move in pending_moves)
        pending_moves = []
        cleaned.append(copy_block(block))
        i += 1

    # Preserve trailing moves that were not associated with a q/f sequence.
    cleaned.extend(copy_block(move) for move in pending_moves)

    cleaned = [block for block in cleaned if block.get("events", [True])]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"cleaned_{timestamp}_{os.path.basename(input_filepath)}"
    output_path = os.path.join(os.getcwd(), output_filename)

    try:
        with open(output_path, "w", encoding="utf-8") as file:
            file.write(custom_json_dumps(cleaned))
            file.write("\n")
    except OSError as exc:
        print(f"Error writing '{output_path}': {exc}", file=sys.stderr)
        return None

    print(f"Successfully cleaned: {os.path.basename(input_filepath)}")
    print("All redundant q/f-sequence mouse movements stripped.")
    print("Kept exactly one final 100 ms mouse move per q/f click sequence.")
    print(f"Saved optimized profile as: {output_filename}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: uv run clean_macro_fixed.py <macro_file_name.json>", file=sys.stderr)
        sys.exit(1)

    sys.exit(0 if clean_macro(sys.argv[1]) else 1)
