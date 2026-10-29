# Dubbing Quality Check

Review an episode's dub against its picture: every character's track plays in sync with the video,
with mute, solo and volume per character. Independent app: it imports the `v2` modules (Excel
parser, pass 2 fit, timing rule, exporter helpers) and does not change V1.

**Built so far:** Block 1 (upload), Block 2 (dubbing with the AD version 2 engine), Block 3
(multitrack player), Block 4 (live script and subtitles), Block 5 (edit and regenerate a line),
Block 6 (export).

Flow: choose where the dub comes from and add the video, **Dub**, **Review** every character's
track in sync with the picture, then **Export**. The dub comes from one of two sources:

- **Generate from the script**: upload the Excel; every line is dubbed with ElevenLabs and fitted to
  its slot with FFmpeg, exactly as AD version 2 does.
- **Use AD Tool V2 audio**: pick a finished AD Tool V2 run on this server (or upload the ZIP it
  delivered) and the episode to review. No ElevenLabs request is made.

## Setup and launch (from the repository root)

Use Python 3.11 or 3.12, as for `v2`.

```sh
python3.12 -m venv quality_check/.venv
quality_check/.venv/bin/python -m pip install -r quality_check/requirements.txt
cp quality_check/.env.example quality_check/.env
quality_check/.venv/bin/python -m quality_check.app
```

Open http://127.0.0.1:5072. AD version 2 can keep running on 5071.

## What to upload

| Input | Limit | Notes |
| --- | --- | --- |
| Script `.xlsx` (Generate) | 20 MB | Same format as AD version 2. Exactly one `EP NO`. Checked the moment it is chosen. |
| AD Tool V2 run (Use AD Tool V2 audio) | 500 MB | A finished run listed from `v2/runtime`, or the ZIP AD Tool V2 delivered. Any number of episodes; pick one. |
| Video | 500 MB | `.mp4` (H.264) plays everywhere; `.mov` often does not. Must start at `00:00:00:00`. |

Settings (under "Change"): timecode frame rate and worksheet name. A run on this server brings its
own; for an uploaded ZIP they are a hint (the frame rate is found from the run's report).

FFmpeg must be installed on the server: Generate fits every line with it, and line speed uses it.

The ElevenLabs key is read from `quality_check/.env`, or from `v2/.env` if this app's own key is empty.

## How the dub is made (Block 2)

`dubbing.py` runs AD version 2's pipeline step for step, calling the `v2` modules directly:
`ElevenLabsClient` for each row (VOICE ID and audio tags from the Excel), pass 2 (each line fitted to
its slot with FFmpeg, 0.70x to 1.50x, `v2/audio_speed.py`), `place_dialogue` for collisions,
`build_track` per character. The character tracks are byte-identical to AD version 2's output for
the same script (a test checks this).

- **Every line is kept**: `clips/<excel row>.take.wav` is ElevenLabs' take as delivered and
  `clips/<excel row>.wav` is what plays (the take at its speed), so Block 5 can regenerate one line
  and rebuild only that character's track.
- **The video uploads while the episode is being dubbed**; the review opens when both are done.
- **Resumable.** If ElevenLabs fails on a line, the page shows the row and the provider's reason.
  *Retry from this line* reuses every clip already generated, so no line is paid for twice. A server
  restart mid-dub is reported the same way and resumes the same way.
- Requests are sequential and never retried automatically, as in AD version 2.
- Each session dubs the whole episode once, which uses ElevenLabs credits each time an episode is loaded.
  To review audio that is already dubbed, use **Use AD Tool V2 audio** instead.

## Use AD Tool V2 audio

AD Tool V2 writes `run.json` next to each run's ZIP in `v2/runtime/<job>/`; finished runs are listed
here, newest first (30 at most). If AD Tool V2 runs from a different folder than this app, set
`AD_V2_RUNTIME_DIR` to its `v2/runtime` folder. A run made elsewhere can be uploaded as its ZIP instead (kept 24
hours). After picking the run and the episode:

- The episode's clips are taken from the ZIP's `pass1/lines/` and `pass2/lines/` folders
  (`ep<episode>_<character>_row<row>.wav`, the names from `v2.exporter.line_name`) and its rows from
  the ZIP's `script.xlsx`. Pass 1 becomes each line's take and pass 2 is what plays. Every clip is
  checked first; a missing one is reported by Excel row before anything is stored.
- The character tracks are rebuilt from the pass 2 clips with V2's rule, so they are sample-identical
  to `pass2/tracks/` in the ZIP (a test checks this) and one line can later be changed on its own.
- Each line's fit speed comes from the ZIP's `processing_report.csv`. An uploaded ZIP's frame rate is
  found by matching the report's scripted durations against the workbook.
- ZIPs from an earlier AD Tool V2 (without `pass1/` and `pass2/` line clips) are refused with a
  message to run the script again.
- Editing a line's words still makes one ElevenLabs request for that line, fitted like the rest.
## Script panel and subtitles (Block 4)

- The **Script** tab lists every line: TCR IN, character, text (audio tags in amber), and badges
  for audio past TCR OUT (`+0.6 s`), lines pushed late by the previous line, and edited lines.
- The lines **being heard** are highlighted and the list follows playback (it pauses following for
  a few seconds when you scroll it yourself). The same lines appear as **subtitles** on the picture.
- Click a line to jump to it; the play button (or **P**) auditions just that line with a little
  picture before and after, then stops. **↑ / ↓** move between lines.

## Edit and regenerate (Block 5)

- **Edit** (pencil, double-click, or **Enter**) opens the line inline. Change the words and the
  [audio tags]; tag chips insert the tags this episode already uses plus common ElevenLabs ones.
  **Regenerate line** (or **Ctrl + Enter**) re-dubs that one line. Regenerating with the same words
  gives a fresh take, for a line that was not emoted well.
- The server makes **one** ElevenLabs request (same voice, model and settings as the dub), fits
  the new take to its slot, re-places the episode with V2's rule, and rebuilds **only the tracks that changed**: normally just
  that character's. If the change moves the episode's last audio, every track is rebuilt so they
  keep one length, as in V2.
- The page swaps the new track into the player without reloading, redraws the lanes, says which of
  that character's later lines moved, and plays the new take straight away.
- If ElevenLabs fails, the error shows in the editor and nothing changes (clip and track are
  restored). Old takes are not kept.
- Edits are saved with the session (`edits` in `session.json`, script wording kept as `original`)
  and survive a page reload; the uploaded Excel is never modified. Export (Block 6) writes them out.

## Line speed

Every line starts at its **fit speed**: pass 2's speed (spoken length / slot, 0.70x to 1.50x, 1.00x
within 0.10 s), from FFmpeg here or from AD Tool V2's report. Lines not at 1.00x show a green badge
such as `1.30x`.

Next to Video sound and Dub sound, **Line speed** sets the speed of the line selected in the script,
from 0.70x to 1.50x (pitch preserved, FFmpeg). Speeds are always relative to ElevenLabs' natural
delivery and applied to the line's take (pass 1), so changes never stack: 1.00x is the natural take
and the fit speed gives the pass 2 clip back exactly. The line keeps its start time; only that character's track is rebuilt and no
ElevenLabs request is made. A speed set in review shows a blue badge. Regenerating a line gives a
new take at its own fit speed.

Beside the slider, **Suggested speed** shows the selected line's fit speed, computed on the server
from the take's spoken length. It is shown in amber when even the 0.70x or 1.50x limit does not make
the line fit. It never changes the audio; the reviewer decides whether to use the slider.

## Export (Block 6)

**Export episode** (top right of the review) downloads `ep<ep>_qc.zip`, with AD version 2's names
(lowercase, spaces as underscores):

| File | Contents |
| --- | --- |
| `ep<ep>_<character>.wav` | One per character, exactly as heard in the review (edits and line speeds included) |
| `ep<ep>_timeline.csv` | AD version 2's combined timeline, recomputed for the reviewed audio |
| `processing_report.csv` | AD version 2's report columns: `pass1_` columns are each line's take (ElevenLabs as delivered), `fit_speed` and `pass2_` columns the audio as reviewed |
| `<script>_qc.xlsx` | The workbook (uploaded, or the run's `script.xlsx`) with edited lines written into TARGET DIALOGUE. Changed cells are highlighted and carry a note with the script wording (and any speed set in review). Nothing else in the workbook changes. |
| `qc_changes.csv` | One row per line changed in review: Excel row, character, TCR IN, script wording, reviewed wording, line speed set in review (empty if only the words changed) |

The session stays open after exporting, so the reviewer can keep working and export again.

## Picture frame

The video sits in a 16:9 frame sized from the available height; the script panel gets the rest of
the width. Landscape, square and phone (9:16) videos all play inside the frame without cropping.

## How playback stays in sync (Block 3)

- **The video is the master clock.** Play, pause, seek and buffering all belong to the `<video>`;
  the dub tracks follow it through Web Audio (one `AudioBufferSourceNode` per character, a gain
  per character for mute/solo/volume, and a master gain).
- **Output latency is compensated.** Audio is scheduled ahead by the device's output latency
  (from `AudioContext.getOutputTimestamp`), so what you hear matches the frame on screen, even
  on Bluetooth headphones.
- **Drift is corrected.** Every animation frame compares the audio being heard with the video's
  media clock (`requestVideoFrameCallback`). If the smoothed difference exceeds one frame, the
  sources restart at the right offset behind a 10 ms fade. The transport shows the live offset.
- Tracks are decoded at their own 24 kHz rate, so a 2-minute, 7-character episode needs about
  90 MB of browser memory.

### Shortcuts

| Key | Action |
| --- | --- |
| Space | Play or pause |
| ← / → | Back or forward one frame |
| Shift + ← / → | Back or forward one second |
| ↑ / ↓ | Previous or next line |
| P | Play the selected line |
| Enter | Edit the selected line (Ctrl + Enter regenerates, Esc cancels) |
| 1–9 | Mute that character |
| Shift + 1–9 | Solo that character |
| V | Video's own sound on or off (off by default) |
| S | Subtitles on or off |

Click or drag anywhere on the lanes to scrub. Double-click a volume slider to reset it.

## Sessions

Each episode gets `quality_check/runtime/<session id>/` holding the script, clips, tracks, peaks and
video. Uploaded AD Tool V2 ZIPs are kept in `quality_check/runtime/uploads/`. Reloading the page reopens the session where it was (dubbing or review). *New episode* and
*Cancel* delete the session (and stop dubbing before its next ElevenLabs request); anything older
than 24 hours (sessions and uploaded ZIPs) is removed automatically. Quality Check only reads
`v2/runtime`; it never changes or removes AD Tool V2's runs.

## Tests

The tests use a fake voice instead of ElevenLabs (no credits used) and compare the result with the
real `v2` pipeline. Like `v2`, they need Python 3.11 or 3.12 for `pydub`.

```sh
quality_check/.venv/bin/python -m unittest discover -s quality_check/tests -t . -v
```

## Two-pass speed test (`tools/two_pass_test.py`)

An experiment, separate from the web app: how well does changing a line's speed make it fit its slot?
Pass 1 dubs every line exactly as AD version 2 does; the script measures the spoken part of each clip
(silence before and after the voice is not counted) and computes speed needed = spoken length /
(TCR OUT - TCR IN). Every line more than 0.10 s off then gets pass 2:

- **FFmpeg (default):** `atempo` on the same pass 1 take, pitch preserved, held between 0.7x and 1.5x.
  No ElevenLabs request, so it is free and keeps the performance. This is AD version 2's pass 2
  (the tool calls `v2/audio_speed.py`), now built into both apps.
- **ElevenLabs (`--compare`, optional):** the line dubbed again with ElevenLabs' `speed` setting, held
  between 0.7x and 1.2x (ElevenLabs' range), in extra columns next to the FFmpeg result. Uses credits.

Each line is measured on its own at its TCR IN. Needs FFmpeg installed.

```sh
python -m quality_check.tools.two_pass_test EP01_script.xlsx
python -m quality_check.tools.two_pass_test EP01_script.xlsx --compare
```

Options: `--rows 2-10,14` to test only some rows, `--out NAME.xlsx`, `--tolerance 0.10`, `--fps`, `--sheet`.
Output goes to `quality_check/tools_outputs/` (ignored by git): `two_pass_results_<date>_<time>.xlsx`
(Summary and Lines sheets; calculated columns are Excel formulas) and a matching `_audio/` folder with every
pass 1 and pass 2 clip for listening. Each run gets its own timestamp, so earlier runs are kept. A request ElevenLabs refuses
is noted on its row and the run continues.
