# Dubbing Quality Check

Review an episode's dub against its picture: every character's track plays in sync with the video,
with mute, solo and volume per character. Independent app: it imports the `v2` modules (Excel
parser, timing rule, exporter helpers) and does not change `v2` or V1.

**Built so far:** Block 1 (upload), Block 2 (dubbing with the AD version 2 engine), Block 3
(multitrack player), Block 4 (live script and subtitles), Block 5 (edit and regenerate a line),
Block 6 (export).

Flow: **Upload** the Excel and the video, **Dub** (ElevenLabs, exactly as AD version 2),
**Review** every character's track in sync with the picture, then **Export** (Block 6).

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
| Script `.xlsx` | 20 MB | Same format as AD version 2. Exactly one `EP NO`. Checked the moment it is chosen. |
| Video | 500 MB | `.mp4` (H.264) plays everywhere; `.mov` often does not. Must start at `00:00:00:00`. |

Settings (under "Change"): timecode frame rate, AD version 2's speed dial (1.00x to 1.20x, FFmpeg
needed above 1.00x) and worksheet name.

The ElevenLabs key is read from `quality_check/.env`, or from `v2/.env` if this app's own key is empty.

## How the dub is made (Block 2)

`dubbing.py` runs AD version 2's pipeline step for step, calling the `v2` modules directly:
`ElevenLabsClient` for each row (VOICE ID and audio tags from the Excel), `adjust_audio_speed`,
`place_dialogue` for collisions, `build_track` per character. The character tracks are
byte-identical to AD version 2's output for the same script (a test checks this).

- **Every line is kept** as `clips/<excel row>.wav`, so Block 5 can regenerate one line and rebuild
  only that character's track.
- **The video uploads while the episode is being dubbed**; the review opens when both are done.
- **Resumable.** If ElevenLabs fails on a line, the page shows the row and the provider's reason.
  *Retry from this line* reuses every clip already generated, so no line is paid for twice. A server
  restart mid-dub is reported the same way and resumes the same way.
- Requests are sequential and never retried automatically, as in AD version 2.
- Each session dubs the whole episode once, which uses ElevenLabs credits each time an episode is loaded.

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
- The server makes **one** ElevenLabs request (same voice, model, settings and speed as the dub),
  re-places the episode with V2's rule, and rebuilds **only the tracks that changed**: normally just
  that character's. If the change moves the episode's last audio, every track is rebuilt so they
  keep one length, as in V2.
- The page swaps the new track into the player without reloading, redraws the lanes, says which of
  that character's later lines moved, and plays the new take straight away.
- If ElevenLabs fails, the error shows in the editor and nothing changes (clip and track are
  restored). Old takes are not kept.
- Edits are saved with the session (`edits` in `session.json`, script wording kept as `original`)
  and survive a page reload; the uploaded Excel is never modified. Export (Block 6) writes them out.

## Line speed

Next to Video sound and Dub sound, **Line speed** speeds up the line selected in the script from
1.00x to 1.20x (pitch preserved, FFmpeg needed, as with AD version 2's speed dial). The line keeps
its start time and simply ends sooner; only that character's track is rebuilt and no ElevenLabs
request is made. Speed is always applied to the line's take as dubbed (`clips/<row>.take.wav`), so
changes never stack and 1.00x restores the take exactly. Sped-up lines show a badge such as `1.08x`.
Regenerating a line gives a new take at 1.00x.

Beside the slider, **Suggested speed** shows, for the selected line only, the speed at which its take
would fit between TCR IN and TCR OUT: take length divided by slot length, rounded up to 0.01x, and
1.00x when the line already fits. Above 1.20x it is shown in amber. It is a calculation only and never
changes the audio; the reviewer decides whether to use the slider.

## Export (Block 6)

**Export episode** (top right of the review) downloads `Episode_<ep>_QC.zip`, in AD version 2's layout:

| File | Contents |
| --- | --- |
| `Episode_<ep>_<character>.wav` | One per character, exactly as heard in the review (edits and line speeds included) |
| `Episode_<ep>_timeline.csv` | AD version 2's combined timeline, recomputed for the reviewed audio |
| `processing_report.csv` | AD version 2's report columns, recomputed for the reviewed audio |
| `<script>_QC.xlsx` | The uploaded workbook with edited lines written into TARGET DIALOGUE. Changed cells are highlighted and carry a note with the script wording (and any line speed). Nothing else in the workbook changes. |
| `qc_changes.csv` | One row per line changed in review: Excel row, character, TCR IN, script wording, reviewed wording, line speed |

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
video. Reloading the page reopens the session where it was (dubbing or review). *New episode* and
*Cancel* delete the session (and stop dubbing before its next ElevenLabs request); anything older
than 24 hours is removed automatically.

## Tests

The tests use a fake voice instead of ElevenLabs (no credits used) and compare the result with the
real `v2` pipeline. Like `v2`, they need Python 3.11 or 3.12 for `pydub`.

```sh
quality_check/.venv/bin/python -m unittest discover -s quality_check/tests -t . -v
```
