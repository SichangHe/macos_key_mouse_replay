# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

import json
import os
import sys
from datetime import datetime

# Ultra-fast yet reliable timing thresholds
MAX_PRESERVED_MOVES = 3  # Keep up to 3 moves before the click for focus
FAST_MOVE_DELAY = 0.005  # 5ms: The sweet spot for macOS focus registration
FAST_CLICK_DELAY = 0.005  # 5ms: Wait briefly after moving before clicking
CONSECUTIVE_GAP_DELAY = 0.005  # 5ms: Gap between consecutive actions


def clean_macro(input_filepath):
    if not os.path.exists(input_filepath):
        print(f"Error: File '{input_filepath}' not found.")
        return

    with open(input_filepath, "r") as f:
        events = json.load(f)

    n = len(events)
    windows = []
    i = 0

    # Pass 1: Map out exact trigger-to-click windows
    while i < n:
        ev = events[i]
        is_trigger = ev.get("type") == "key_press" and ev.get("key") in ("q", "f")
        if is_trigger:
            trigger_key = ev.get("key")
            click_idx = -1
            release_idx = -1

            # 1. Scan forward to find the associated click
            for j in range(i + 1, n):
                if events[j].get("type") == "mouse_click":
                    click_idx = j
                    break
                elif events[j].get("type") == "key_press" and events[j].get("key") in (
                    "q",
                    "f",
                ):
                    break

            # 2. Scan forward from the click to find the matching key_release
            if click_idx != -1:
                for k in range(click_idx + 1, n):
                    if (
                        events[k].get("type") == "key_release"
                        and events[k].get("key") == trigger_key
                    ):
                        release_idx = k
                        break
                    elif events[k].get("type") == "key_press" and events[k].get(
                        "key"
                    ) in ("q", "f"):
                        break

            if click_idx != -1 and release_idx != -1:
                # Track preceding mouse movements
                preceding_moves = []
                for m in range(click_idx - 1, i, -1):
                    if events[m].get("type") == "mouse_move":
                        preceding_moves.insert(0, m)
                        if len(preceding_moves) == MAX_PRESERVED_MOVES:
                            break

                windows.append(
                    {
                        "start_idx": i,
                        "click_idx": click_idx,
                        "release_idx": release_idx,
                        "preceding_moves": preceding_moves,
                        "trigger_key": trigger_key,
                    }
                )
                i = release_idx + 1
                continue
        i += 1

    # Identify consecutive windows (only movements/releases of q/f between them)
    consecutive_starts = set()
    consecutive_travel_indices = (
        set()
    )  # Indices of travel moves between consecutive windows

    for w_idx in range(len(windows) - 1):
        w_curr = windows[w_idx]
        w_next = windows[w_idx + 1]

        is_consecutive = True
        temp_indices = set()
        for idx in range(w_curr["release_idx"] + 1, w_next["start_idx"]):
            ev = events[idx]
            if ev.get("type") in ("mouse_click", "key_press"):
                is_consecutive = False
                break
            if ev.get("type") == "key_release" and ev.get("key") not in ("q", "f"):
                is_consecutive = False
                break
            temp_indices.add(idx)

        if is_consecutive:
            consecutive_starts.add(w_next["start_idx"])
            consecutive_travel_indices.update(temp_indices)

    # Build the strict "Delay Incineration" set (delays here are wiped and never accumulated)
    discard_delay_indices = set()

    for w in windows:
        keep_indices = {w["start_idx"], w["click_idx"], w["release_idx"]}
        for m_idx in w["preceding_moves"]:
            keep_indices.add(m_idx)

        # Discard all non-preserved events inside active windows
        for idx in range(w["start_idx"] + 1, w["release_idx"]):
            if idx not in keep_indices:
                discard_delay_indices.add(idx)

    # Discard dead-time delays between consecutive window chains
    discard_delay_indices.update(consecutive_travel_indices)

    # Pass 2: Rebuild the macro
    cleaned_events = []
    processed_indices = set()

    for idx, ev in enumerate(events):
        if idx in processed_indices:
            continue

        # If this is a travel move between consecutive actions, DROP it completely
        if idx in consecutive_travel_indices:
            continue

        window = next((w for w in windows if w["start_idx"] == idx), None)

        if window:
            start_ev = events[window["start_idx"]]
            click_ev = events[window["click_idx"]]
            release_ev = events[window["release_idx"]]

            # Apply microscopic delays to Consecutive trigger sequences
            if idx in consecutive_starts:
                start_ev["delay"] = CONSECUTIVE_GAP_DELAY
            else:
                start_ev["delay"] = min(start_ev.get("delay", 0.005), 0.005)

            start_ev["is_shielded"] = True

            # 1. Trigger Key Press
            cleaned_events.append(start_ev)

            # 2. Preserved preceding mouse moves (Forced to ultra-fast 5ms)
            for m_idx in window["preceding_moves"]:
                move_ev = events[m_idx]
                move_ev["delay"] = FAST_MOVE_DELAY
                move_ev["is_shielded"] = True
                cleaned_events.append(move_ev)

            # 3. Mouse Click (Forced to 5ms)
            click_ev["delay"] = FAST_CLICK_DELAY
            click_ev["is_shielded"] = True
            cleaned_events.append(click_ev)

            # 4. Key Release (Forced to 5ms)
            release_ev["delay"] = FAST_MOVE_DELAY
            release_ev["is_shielded"] = True
            cleaned_events.append(release_ev)

            # Mark all indices inside the window as processed
            for k in range(window["start_idx"], window["release_idx"] + 1):
                processed_indices.add(k)
        else:
            if ev.get("type") == "key_release" and ev.get("key") in ("q", "f"):
                continue
            cleaned_events.append(ev)

    # Pass 3: Resolve relative delays, permanently destroying targeted dead time
    final_events = []
    accumulated_delay = 0.0

    for idx, orig_event in enumerate(events):
        matched_clean_event = next(
            (e for e in cleaned_events if id(e) == id(orig_event)), None
        )

        if matched_clean_event is not None:
            # If the event is shielded inside a trigger sequence, do NOT pass forward accumulated delays
            if matched_clean_event.get("is_shielded"):
                matched_clean_event.pop("is_shielded", None)
                final_events.append(matched_clean_event)
                accumulated_delay = 0.0  # Reset accumulator (dead time is destroyed)
            else:
                matched_clean_event["delay"] = round(
                    matched_clean_event["delay"] + accumulated_delay, 4
                )
                final_events.append(matched_clean_event)
                accumulated_delay = 0.0
        else:
            # Drop the event; accumulate its delay only if it is not a discarded delay
            if idx not in discard_delay_indices:
                accumulated_delay += orig_event.get("delay", 0.0)

    # Save output
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_filename = f"cleaned_{timestamp}_{os.path.basename(input_filepath)}"
    output_path = os.path.join(os.path.dirname(input_filepath), output_filename)

    with open(output_path, "w") as f:
        json.dump(final_events, f, indent=2)

    print(f"Successfully cleaned: {os.path.basename(input_filepath)}")
    print(f"Reduced event footprint: {len(events)} -> {len(final_events)} events.")
    print(f"Saved optimized profile as: {output_filename}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run clean_macro.py <macro_file_name.json>")
        sys.exit(1)

    clean_macro(sys.argv[1])
