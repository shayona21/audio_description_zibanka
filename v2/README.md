# AD version 2

Independent English dubbing application. V1 files, settings and launch commands are unchanged. Run commands below from the repository root (`audio_description_tool`).

## Setup and launch

Use Python 3.11 or 3.12 (pydub requires the standard-library audioop module).

```sh
python3.12 -m venv v2/.venv
v2/.venv/bin/python -m pip install -r v2/requirements.txt
cp v2/.env.example v2/.env
```

Set `ELEVENLABS_API_KEY` in `v2/.env`, then run:

```sh
v2/.venv/bin/python -m v2.app
```

Open http://127.0.0.1:5071. The original application can continue on port 5070. V2 reads its API key and port from its own `.env` plus environment variables. Model selection is defined only by `MODEL_ID` in `v2/tts_client.py`; environment variables and constructor arguments cannot override it. Every request includes the `VOICE_SETTINGS` from that file: stability 0.5 and similarity boost 0.75. English dialogue is sent verbatim; no translation is performed. The API integration follows the [ElevenLabs create speech endpoint](https://elevenlabs.io/docs/api-reference/text-to-speech/convert).

The speed dial ranges from **1.00x to 1.20x**, in 0.01x steps, with 1.00x as the default. Faster settings use FFmpeg to shorten each generated clip while preserving pitch, before calculating each character's independent timeline. Install FFmpeg and ensure `ffmpeg` is on PATH to use speeds above 1.00x. Missing FFmpeg is reported before any speech generation. At 1.00x the generated audio passes through unchanged and FFmpeg is not needed. Faster speech can reduce or eliminate delays; no line starts before its original script time.

## Spreadsheet contract

Upload `.xlsx`, with headers in row 1. The first worksheet is used unless a worksheet name is supplied. Columns are matched by header name, ignoring case and repeated whitespace; column order does not matter.

| Header | Required value |
| --- | --- |
| EP NO | Episode identifier (store as text to preserve leading zeros) |
| SR NO | Ignored |
| TCR IN | Start time, e.g. `00:00:18:08` |
| TCR OUT | End time, strictly later than start |
| CHARACTERS | Character name |
| VOICE ID | ElevenLabs voice ID; rows with an empty or whitespace-only value are skipped |
| TARGET DIALOGUE | Text to synthesize |
| DUBBING STATUS | Ignored, including any completion status |

Rows with an empty or whitespace-only VOICE ID are skipped before validation and speech generation. Remaining dialogue rows must supply all six required fields. No implicit fill-down or merged-cell inheritance. Blank rows (across required fields) are skipped. If no rows remain, validation reports that no dialogue rows were found without calling ElevenLabs. Formulas in required fields are rejected: paste their values instead. Conflicting voice IDs within one episode/character pair are rejected. Episode and character grouping is case-sensitive after trimming surrounding whitespace. Ignored columns may be omitted.

Text timecodes are `HH:MM:SS:FF`, default 25 fps; 24/30/50/60 fps can also be selected. Native Excel time cells are accepted at their stored time precision. Drop-frame notation is not supported. Timelines start at zero: a `01:00:00:00` timecode includes an hour of leading silence.

## Output and timing

Tag-only dialogue cells such as `[Gasps]` are sent as `[Gasps] ...` to supply punctuation outside the tags for the API's empty-text check. Tags and spoken words are preserved; rows already containing dialogue or punctuation are unchanged. This is a compatibility workaround, not a guaranteed provider behavior: verify acceptance and listen to a sample with your selected voice before a full run. Delivery cues such as `[Conversational tone]` alone do not specify a vocal sound. Detailed provider errors remain visible if the request is rejected.

The ZIP contains `Episode_<episode>_<character>.wav` for each pair, one `Episode_<episode>_timeline.csv` combining all characters in adjusted chronological order for each episode (source Excel row breaks ties), plus `processing_report.csv`. Filenames are sanitized and disambiguated so outputs cannot overwrite each other. Audio is mono 24 kHz, 16-bit PCM.

Timing is calculated independently for each **episode + character** pair. Within each pair, rows are ordered by original TCR IN, with Excel row number breaking ties; input rows need not be sorted. Each character's first line retains its original start. Every later line starts at `max(original TCR IN, previous actual audio end)`, using only that character's preceding line in the same episode. When a clip reaches past the next scripted start, the next line follows immediately after it. Available silence absorbs overruns, allowing later lines to return to their original start times. The script's TCR OUT is used for duration reporting, not to schedule the next line. Different characters may speak simultaneously; their durations and delays never alter each other's placements. Episodes are adjusted independently. Speech is never truncated. Audio placements, timeline CSVs, and processing report durations all reflect the selected speed.

For example, if A's first line finishes at 00:15, A's next 00:40 line stays at 00:40. If A's first line instead finishes at 00:45, the next line starts immediately at 00:45. B's 00:11–00:14 slot starts at 00:11 in both cases, even while A is speaking. A longer or shorter B clip has no effect on A's start times.

All tracks for an episode start at zero and have the same length: the latest original TCR OUT or actual audio end in that episode, whichever is later. This retains leading silence and all speech. The spreadsheet does not supply a separate full episode duration. Each combined timeline CSV has exactly six columns: **Dialogue, Original time start, Original time end, Adjusted time start, Adjusted time end, Delay**. Dialogue includes the speaker, e.g. `A: Hey, lets go on a walk`. Readable timestamps use `HH:MM:SS.mmm` (not frame-based input timecodes), and delay is shown in seconds, e.g. `5.000 s`. Adjusted end is the actual audio end.

`processing_report.csv` uses this exact column order:

```text
excel_row,episode,character,voice_id,start_TCR,end_TCR,duration,actual_duration_ms,overrun_amnt_ms,status,track,adjusted_start_TCR,adjusted_end_TCR
```

- `excel_row` identifies the original Excel row; records retain source row order across episodes.
- `start_TCR` and `end_TCR` show the original script times as `HH:MM:SS:FF` at the selected frame rate.
- `duration` is the original scripted end minus start, **in milliseconds**.
- `actual_duration_ms` is the measured audio length **after speed adjustment**.
- `overrun_amnt_ms` is `max(0, actual_duration_ms − duration)`. It does not include delays caused by that character's earlier dialogue.
- `adjusted_start_TCR` and `adjusted_end_TCR` show the actual placement and speech end.
- TCR display rounds to the nearest frame (half-frame ties round up), including correct second/minute/hour rollover. Audio placement calculations remain in milliseconds, and the combined timeline preserves millisecond precision. Native Excel times that fall between frames are also rounded for TCR display only.
- `TCR_OUT_OVERRUN` in `status` means the row's audio exceeded its original scripted duration. It need not delay any later line if there is enough silence available. A row delayed by the same character's earlier dialogue can still be `OK` if its own duration fits. Overlaps between different characters are allowed. `track` names the corresponding WAV.

Validation runs before speech generation. Requests are sequential and are not automatically retried: retrying after a provider failure can incur additional charges for already generated rows. A failed run produces no completed ZIP and discards temporary audio. API voice availability is checked by ElevenLabs during generation. No live API calls are needed for tests.

## Command line

```sh
v2/.venv/bin/python -m v2.main script.xlsx --validate-only
v2/.venv/bin/python -m v2.main script.xlsx --output v2/runtime/session01.zip --fps 25
v2/.venv/bin/python -m v2.main script.xlsx --output v2/runtime/faster.zip --speed 1.20
v2/.venv/bin/python -m v2.main script.xlsx --sheet "English dialogue" --validate-only
v2/.venv/bin/python -m unittest discover -s v2/tests -v
```

Existing output archives are not overwritten. Web jobs use separate UUID folders under `v2/runtime/`. Uploaded workbooks and completed ZIPs remain there until manually removed. Job status is held in memory: restarting the app loses browser status, but completed archives remain on disk. This is a local, single-process tool; a shared production deployment would need authentication and persistent job storage/workers. The renderer holds one character track in memory at a time, so long timelines need sufficient memory.
