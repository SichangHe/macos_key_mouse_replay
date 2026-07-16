#!/usr/bin/env python3
"""
Simplify q/f mouse-click segments in macro JSON files.

Rules implemented:
- A valid segment starts with a single-event key_press for q or f.
- It ends with a single-event key_release for the same key.
- Between them, only mouse_move blocks and exactly one mouse_click block are allowed.
- Any intervening key_press, unrelated block type, mismatched release, missing click,
  or incomplete segment leaves that candidate unchanged.
- Movement before a click within a valid segment is collapsed to its final event.
- A bridge is a gap made entirely of mouse_move blocks between two consecutive
  valid segments. The whole bridge is collapsed to its final movement event.
- To match recorder timing, post-click movement before the preceding key release
  is absorbed into that bridge when the bridge contains movement events. This is
  why a tiny post-click move can disappear in favor of the bridge's later point.
- Post-click movement that is not followed by such a bridge is collapsed in place.
- All event delays inside valid segments and valid bridges are changed to 0.1.
- Movement before the first valid segment, after the last valid segment, or in a
  non-bridge gap is never modified.

By default, output is written beside the input as:
    INPUT_STEM_simplified.json

Use --in-place to replace the input atomically, or -o/--output for another path.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

JSONValue = Any
Block = dict[str, JSONValue]


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    click: int
    key: str


@dataclass
class Stats:
    valid_segments: int = 0
    bridges: int = 0
    mouse_move_blocks_removed: int = 0
    mouse_move_events_removed: int = 0
    delays_changed: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Simplify mouse movement inside valid q/f click segments and "
            "movement-only bridges between consecutive valid segments."
        )
    )
    parser.add_argument("input", type=Path, help="Input macro JSON file")
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("-o", "--output", type=Path, help="Output JSON file")
    destination.add_argument(
        "--in-place",
        action="store_true",
        help="Atomically replace the input file",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Delay assigned to transformed events (default: 0.1)",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation level (default: 2)",
    )
    return parser.parse_args()


def event_list(block: Block) -> list[JSONValue] | None:
    events = block.get("events")
    return events if isinstance(events, list) else None


def single_key(block: Block, expected_type: str) -> str | None:
    """Return the key only for a strict, single-event key block."""
    if block.get("type") != expected_type:
        return None

    events = event_list(block)
    if events is None or len(events) != 1:
        return None

    event = events[0]
    if not isinstance(event, dict):
        return None

    key = event.get("key")
    return key if isinstance(key, str) else None


def structurally_valid_action_block(block: Block) -> bool:
    """Mouse action blocks must at least contain a list-valued events field."""
    return (
        block.get("type") in {"mouse_move", "mouse_click"}
        and event_list(block) is not None
    )


def find_valid_segments(blocks: list[Block]) -> list[Segment]:
    segments: list[Segment] = []
    i = 0

    while i < len(blocks):
        press_key = single_key(blocks[i], "key_press")
        if press_key not in {"q", "f"}:
            i += 1
            continue

        click_blocks = 0
        click_index: int | None = None
        j = i + 1
        found: Segment | None = None

        while j < len(blocks):
            block = blocks[j]
            block_type = block.get("type")

            # Any new key press invalidates this candidate immediately.
            if block_type == "key_press":
                break

            # The first release encountered must be the matching release.
            if block_type == "key_release":
                release_key = single_key(block, "key_release")
                if (
                    release_key == press_key
                    and click_blocks == 1
                    and click_index is not None
                ):
                    found = Segment(i, j, click_index, press_key)
                break

            if block_type == "mouse_click":
                if not structurally_valid_action_block(block):
                    break
                click_blocks += 1
                click_index = j
                if click_blocks > 1:
                    break
            elif block_type == "mouse_move":
                if not structurally_valid_action_block(block):
                    break
            else:
                # The segment grammar allows only mouse_move and one mouse_click.
                break

            j += 1

        if found is not None:
            segments.append(found)
            i = found.end + 1
        else:
            # Advance only one block so a later q/f press can still start a segment.
            i += 1

    return segments


def set_delays(block: Block, delay: float, stats: Stats) -> Block:
    result = copy.deepcopy(block)
    events = event_list(result)
    if events is None:
        return result

    for event in events:
        if isinstance(event, dict):
            if event.get("delay") != delay:
                stats.delays_changed += 1
            event["delay"] = delay

    return result


def collapse_mouse_run(run: list[Block], delay: float, stats: Stats) -> list[Block]:
    """
    Collapse one contiguous mouse_move run to its final event.

    If every block in the run has an empty events list, leave the run's block
    count intact but still apply the requested delay where possible. This is
    conservative because no "final event" exists to retain.
    """
    if not run:
        return []

    total_events = sum(len(event_list(block) or []) for block in run)

    last_block_with_event: Block | None = None
    last_event: JSONValue | None = None
    for block in run:
        events = event_list(block) or []
        if events:
            last_block_with_event = block
            last_event = events[-1]

    if last_block_with_event is None:
        return [set_delays(block, delay, stats) for block in run]

    collapsed = copy.deepcopy(last_block_with_event)
    collapsed["events"] = [copy.deepcopy(last_event)]
    collapsed = set_delays(collapsed, delay, stats)

    stats.mouse_move_blocks_removed += len(run) - 1
    stats.mouse_move_events_removed += max(0, total_events - 1)
    return [collapsed]


def transform_segment(
    blocks: list[Block],
    segment: Segment,
    delay: float,
    stats: Stats,
    absorb_post_click: bool,
) -> list[Block]:
    """
    Transform one validated segment.

    When absorb_post_click is true, mouse movement after the click and before the
    release is omitted here because it is collapsed together with the following
    movement-only bridge.
    """
    transformed: list[Block] = []
    i = segment.start

    while i <= segment.end:
        block = blocks[i]

        if block.get("type") != "mouse_move":
            transformed.append(set_delays(block, delay, stats))
            i += 1
            continue

        run_start = i
        while i <= segment.end and blocks[i].get("type") == "mouse_move":
            i += 1

        # In a valid segment, the click splits inbound movement from outbound
        # movement. Only the outbound run may be absorbed into the next bridge.
        if absorb_post_click and run_start > segment.click:
            continue

        transformed.extend(collapse_mouse_run(blocks[run_start:i], delay, stats))

    return transformed


def is_bridge(blocks: Iterable[Block]) -> bool:
    gap = list(blocks)
    return bool(gap) and all(
        block.get("type") == "mouse_move" and event_list(block) is not None
        for block in gap
    )


def simplify_macro(blocks: list[Block], delay: float) -> tuple[list[Block], Stats]:
    stats = Stats()
    segments = find_valid_segments(blocks)
    stats.valid_segments = len(segments)

    # Each replacement range is keyed by its inclusive start index and stores
    # (inclusive end index, replacement blocks).
    replacements: dict[int, tuple[int, list[Block]]] = {}
    absorb_post_click_for: set[int] = set()

    # A bridge exists only when every block between consecutive valid segments
    # is a mouse_move block. A different block type protects the whole gap.
    #
    # To match the supplied example, a post-click movement tail inside the
    # preceding segment is collapsed together with the bridge whenever the gap
    # itself contains at least one actual movement event. The retained event is
    # therefore the chronologically final point from the gap and remains after
    # the preceding key_release.
    for previous, following in zip(segments, segments[1:]):
        bridge_start = previous.end + 1
        bridge_end = following.start - 1

        if bridge_start > bridge_end:
            continue

        gap = blocks[bridge_start : bridge_end + 1]
        if not is_bridge(gap):
            continue

        gap_has_event = any(event_list(block) for block in gap)
        if not gap_has_event:
            # There is no final bridge event to retain safely.
            continue

        post_click_tail = blocks[previous.click + 1 : previous.end]
        combined_movement = post_click_tail + gap

        replacements[bridge_start] = (
            bridge_end,
            collapse_mouse_run(combined_movement, delay, stats),
        )
        absorb_post_click_for.add(previous.start)
        stats.bridges += 1

    for segment in segments:
        replacements[segment.start] = (
            segment.end,
            transform_segment(
                blocks,
                segment,
                delay,
                stats,
                absorb_post_click=segment.start in absorb_post_click_for,
            ),
        )

    result: list[Block] = []
    i = 0
    while i < len(blocks):
        replacement = replacements.get(i)
        if replacement is None:
            result.append(copy.deepcopy(blocks[i]))
            i += 1
            continue

        end, replacement_blocks = replacement
        result.extend(replacement_blocks)
        i = end + 1

    return result, stats


def load_macro(path: Path) -> list[Block]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise SystemExit(f"Input file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: "
            f"{exc.msg}"
        ) from exc
    except OSError as exc:
        raise SystemExit(f"Could not read {path}: {exc}") from exc

    if not isinstance(data, list):
        raise SystemExit("The macro JSON root must be an array.")

    for index, block in enumerate(data):
        if not isinstance(block, dict):
            raise SystemExit(
                f"Top-level item {index} is not a JSON object; no file was written."
            )

    return data


def write_json(path: Path, data: list[Block], indent: int, atomic: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    if not atomic:
        try:
            with path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=indent, ensure_ascii=False)
                handle.write("\n")
        except OSError as exc:
            raise SystemExit(f"Could not write {path}: {exc}") from exc
        return

    # Atomic replacement keeps the original intact if writing fails midway.
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(data, handle, indent=indent, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_name, path)
    except OSError as exc:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise SystemExit(f"Could not replace {path}: {exc}") from exc


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}_simplified.json")


def main() -> None:
    args = parse_args()

    if args.delay < 0:
        raise SystemExit("--delay must be zero or greater.")
    if args.indent < 0:
        raise SystemExit("--indent must be zero or greater.")

    input_path = args.input.expanduser().resolve()
    output_path = (
        input_path
        if args.in_place
        else (
            args.output.expanduser().resolve()
            if args.output
            else default_output_path(input_path)
        )
    )

    blocks = load_macro(input_path)
    simplified, stats = simplify_macro(blocks, args.delay)
    write_json(output_path, simplified, args.indent, atomic=args.in_place)

    print(f"Wrote: {output_path}")
    print(f"Valid q/f segments: {stats.valid_segments}")
    print(f"Movement-only bridges: {stats.bridges}")
    print(f"Mouse-move blocks removed: {stats.mouse_move_blocks_removed}")
    print(f"Mouse-move events removed: {stats.mouse_move_events_removed}")
    print(f"Delays changed: {stats.delays_changed}")


if __name__ == "__main__":
    main()
