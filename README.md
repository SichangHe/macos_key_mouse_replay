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

## Implementation note

Initially, mouse clicks did not work because they were recorded as
`mouse_click (pressed=True)`, `mouse_move (the microscopic jitter)`,
`mouse_click (pressed=False)`.
Switching to recording a single `mouse_click` event solved this.

Users are still suggested to hard press and click multiple times just to
be sure.