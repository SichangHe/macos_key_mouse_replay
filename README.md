# Mouse&keyboard recorder&playback for macOS

First do:

```sh
uv run macro_recorder.py
```

Then press <kbd>F1</kbd> to start/end recording, and <kbd>F2</kbd> to
play back the last recording in a loop. Press <kbd>Esc</kbd> to stop playback.

Each recording is saved in `macro_YYYYMMDD_HHMMSS.json` at PWD.
Rename them as needed for future playback. Choose the playback to use with:

```sh
uv run macro_recorder.py RECORDING_NAME.json
```

To source another JSON 3 times:

```json
[
  {
    "type": "source",
    "file": "another_macro.json",
    "count": 3
  }
]
```

## Implementation note

Initially, mouse clicks did not work because they were recorded as
`mouse_click (pressed=True)`, `mouse_move (the microscopic jitter)`,
`mouse_click (pressed=False)`.
Switching to recording a single `mouse_click` event solved this.

Users are still suggested to hard press and click multiple times just to
be sure.

### Delay reduction

Simulating a click immediately after teleporting the mouse cursor often fails.
macOS requires a brief coordinate hover sequence to
register window focus before it processes a click.
Keeping at least two to three natural `mouse_move`
events immediately preceding a click satisfies this OS-level focus hit-test.

While reducing delays makes macros run faster, reducing delays below 3 to
5 milliseconds between cursor movement and click event execution leads to
dropped inputs.
Keeping a minimum delay threshold of 5ms for preserved movements and
clicks ensures stable input registration while
maintaining instantaneous execution speeds.

(?) For key-plus-click combinations,
releasing the key before the click sequence concludes may break click
mechanics.
The trigger key should be held down, the cursor moved, the click executed, and
only then should the key release event be simulated.