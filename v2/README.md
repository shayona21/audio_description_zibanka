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

Open http://127.0.0.1:5071. The original application can continue on port 5070. V2 reads its API key and port from its own `.env` plus environment variables. Model selection is defined only by `MODEL_ID` in `v2/tts_client.py`; environment variables and constructor arguments cannot override it. Every request includes the `VOICE_SETTINGS` from that file: stability 0.5 and similarity boost 0.75. English dialogue is sent verbatim; no translation is performed. The API integration follows the [ElevenLabs create speech endpoint](https://elevenlabs.io/docs/api-reference/text-to-speech/convert). No FFmpeg is needed for this WAV-only pipeline.

## Spreadsheet contract

Upload `.xlsx`, with headers in row 1. The first worksheet is used unless a worksheet name is supplied. Columns are matched by header name, ignoring case and repeated whitespace; column order does not matter.

| Header | Required value |
| --- | --- |
| EP NO | Episode identifier (store as text to preserve leading zeros) |
| SR NO | Ignored |
| TCR IN | Start time, e.g. `00:00:18:08` |
| TCR OUT | End time, strictly later than start |
| CHARACTERS | Character name |
| VOICE ID | ElevenLabs voice ID for this row |
| ENGLISH DIALOGUES | English text to synthesize |
| DUBBING STATUS | Ignored, including any completion status |

Every dialogue row must supply all six required fields. No implicit fill-down or merged-cell inheritance. Blank rows (across required fields) are skipped. Formulas in required fields are rejected: paste their values instead. Conflicting voice IDs within one episode/character pair are rejected. Episode and character grouping is case-sensitive after trimming surrounding whitespace. Ignored columns may be omitted.

Text timecodes are `HH:MM:SS:FF`, default 25 fps; 24/30/50/60 fps can also be selected. Native Excel time cells are accepted at their stored time precision. Drop-frame notation is not supported. Timelines start at zero: a `01:00:00:00` timecode includes an hour of leading silence.

## Output and timing

The ZIP contains `Episode_<episode>_<character>.wav` for each pair, plus `processing_report.csv`. Filenames are sanitized and disambiguated so characters cannot overwrite each other. Audio is mono 24 kHz, 16-bit PCM.

Each line starts at TCR IN with silence between lines. All tracks for an episode have the same length: the latest TCR OUT or actual speech end across that episode. The spreadsheet does not supply a separate full episode duration. Input rows need not be sorted. Speech is never truncated or accelerated. Overruns past TCR OUT and actual overlaps within a character's track are flagged in the report; simultaneous speech by different characters is allowed. Overlapping speech within a track is mixed and may need manual correction.

Validation runs before speech generation. Requests are sequential and are not automatically retried: retrying after a provider failure can incur additional charges for already generated rows. A failed run produces no completed ZIP and discards temporary audio. API voice availability is checked by ElevenLabs during generation. No live API calls are needed for tests.

## Command line

```sh
v2/.venv/bin/python -m v2.main script.xlsx --validate-only
v2/.venv/bin/python -m v2.main script.xlsx --output v2/runtime/session01.zip --fps 25
v2/.venv/bin/python -m v2.main script.xlsx --sheet "English dialogue" --validate-only
v2/.venv/bin/python -m unittest discover -s v2/tests -v
```

Existing output archives are not overwritten. Web jobs use separate UUID folders under `v2/runtime/`. Uploaded workbooks and completed ZIPs remain there until manually removed. Job status is held in memory: restarting the app loses browser status, but completed archives remain on disk. This is a local, single-process tool; a shared production deployment would need authentication and persistent job storage/workers. The renderer holds one character track in memory at a time, so long timelines need sufficient memory.
