# AD version 2

Independent English dubbing application. V1 files, settings and launch commands are unchanged. Run commands below from the repository root (`audio_description_tool`).

## Setup and launch

Use Python 3.11 or 3.12 (pydub requires the standard-library audioop module).

```sh
python3.12 -m venv v2/.venv
v2/.venv/bin/python -m pip install -r v2/requirements.txt
cp v2/.env.example v2/.env
```

Set `ELEVENLABS_API_KEY` and `GEMINI_API_KEY` in `v2/.env`, then run:

```sh
v2/.venv/bin/python -m v2.app
```

Open http://127.0.0.1:5071. The original application can continue on port 5070. V2 reads its API key and port from its own `.env` plus environment variables. Model selection is defined only by `MODEL_ID` in `v2/tts_client.py`; environment variables and constructor arguments cannot override it. Every request includes the `VOICE_SETTINGS` from that file: stability 0.5 and similarity boost 0.75. English dialogue is sent verbatim; no translation is performed. The API integration follows the [ElevenLabs create speech endpoint](https://elevenlabs.io/docs/api-reference/text-to-speech/convert).

**Every take starts on its first sound.** ElevenLabs puts a short, variable stretch of blank audio before the voice (measured 20–180 ms on Billionaire Bhakri episode 1), which made speech start after TCR IN and broke lipsync. Right after each ElevenLabs request, FFmpeg's `silenceremove` finds the first sound (the first 10 ms louder than −60 dBFS; a breath or click counts as sound) and everything before it is cut, on a whole-millisecond boundary, leaving the speech samples untouched. The amount is printed per row and recorded in `processing_report.csv` (`leading_trim_ms`). Trailing silence is kept. Quality Check trims regenerated lines the same way. `measure_leading_silence.py` measures the blank on any run ZIP; on a run made after this change it should report about 0 ms.

**Three passes fit every line to its slot** (TCR OUT − TCR IN). Limits are set once in `v2/audio_speed.py` (`MIN_SPEED`, `MAX_SPEED`, `NORMAL_TOLERANCE_PERCENT`, `SUCCESS_TARGET_PERCENT`).

- **Pass 1:** ElevenLabs generates every row, the leading silence is trimmed, and the take is measured. Its *audio duration* (the meter) runs from the first sound to the end of speech; trailing silence is not counted. `overrun_ms` = audio − slot (negative when the line is too short), `percent_overrun` = overrun ÷ slot × 100, `desired_speed` = audio ÷ slot with no limits. Speeds and percentages keep 8 significant figures. A row within ±2% is *normal* and is never changed. Pass 1 audio is not in the download.
- **Pass 1.5:** a row whose desired speed is above 1.2x or below 0.8x is rewritten by Gemini (`v2/rewrite.py`, model `MODEL_ID`) to last its slot at 1.0x, keeping its meaning, tone and character (the character's other lines in the episode are sent for context; `[tags]` are kept exactly), then generated again with ElevenLabs. Up to 2 attempts; the second is told how the first came out. The attempt closest to 1.0x is kept, stopping early once one is within 2%. If the best rewrite is still outside 0.8x–1.2x, or Gemini fails, or the line is tag-only, the row is kept at 1.0x and flagged `MANUAL_REVIEW`. More than 10% of rows flagged prints a warning: that points to a problem earlier in the pipeline. Set `GEMINI_API_KEY` in `v2/.env`.
- **Pass 2:** FFmpeg's `atempo` applies the desired speed, held between 0.8x and 1.2x, keeping the voice's pitch, to every row that is neither normal nor flagged. Each line is then measured again. The run prints its success rate: the share of rows at most 2% over their slot (shorter lines count as within target), against a target of 90%.

FFmpeg must be on PATH; a missing FFmpeg or Gemini key is reported before any speech is generated.

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

The ZIP contains:

| Path | Contents |
| --- | --- |
| `pass1_5/lines/ep<episode>_<character>_row<excel row>.wav` | Each dubbed row before the speed change, leading silence trimmed; rewritten rows use the rewritten take |
| `pass2/lines/ep<episode>_<character>_row<excel row>.wav` | The same rows after pass 2 (the audio delivered) |
| `pass2/tracks/ep<episode>_<character>.wav` | One full-length timeline per episode and character, built from the pass 2 lines |
| `ep<episode>_timeline.csv` | All characters of an episode in adjusted chronological order, with the dialogue actually spoken |
| `processing_report.csv` | One record per row, every pass side by side (columns below) |
| `script.xlsx` | A copy of the input workbook |

File names are lowercase with spaces as underscores: EP NO `1` and character `ONSCREEN TEXT` give `ep1_onscreen_text_row3.wav` and `ep1_onscreen_text.wav`. An EP NO that already starts with "EP" is not doubled (`EP01` gives `ep01`). Both passes are placed with the same timing rule below, so each pass's tracks show how that pass plays. The Dubbing Quality Check opens this ZIP directly (or lists finished runs from `v2/runtime/`, using each run's `run.json`). Filenames are sanitized and disambiguated so outputs cannot overwrite each other (names that only differ in case get `_2`). Audio is mono 24 kHz, 16-bit PCM.

Timing is calculated independently for each **episode + character** pair. Within each pair, rows are ordered by original TCR IN, with Excel row number breaking ties; input rows need not be sorted. Each character's first line retains its original start. Every later line starts at `max(original TCR IN, previous actual audio end)`, using only that character's preceding line in the same episode. When a clip reaches past the next scripted start, the next line follows immediately after it. Available silence absorbs overruns, allowing later lines to return to their original start times. The script's TCR OUT is used for duration reporting, not to schedule the next line. Different characters may speak simultaneously; their durations and delays never alter each other's placements. Episodes are adjusted independently. Speech is never truncated. The timeline CSVs, the report's status, track and adjusted TCRs describe the fitted (pass 2) audio; the report's `pass1_` columns describe pass 1.

For example, if A's first line finishes at 00:15, A's next 00:40 line stays at 00:40. If A's first line instead finishes at 00:45, the next line starts immediately at 00:45. B's 00:11–00:14 slot starts at 00:11 in both cases, even while A is speaking. A longer or shorter B clip has no effect on A's start times.

All tracks for an episode start at zero and have the same length: the latest original TCR OUT or actual audio end in that episode, whichever is later. This retains leading silence and all speech. The spreadsheet does not supply a separate full episode duration. Each combined timeline CSV has exactly six columns: **Dialogue, Original time start, Original time end, Adjusted time start, Adjusted time end, Delay**. Dialogue includes the speaker, e.g. `A: Hey, lets go on a walk`. Readable timestamps use `HH:MM:SS.mmm` (not frame-based input timecodes), and delay is shown in seconds, e.g. `5.000 s`. Adjusted end is the actual audio end.

`processing_report.csv` uses this exact column order:

```text
excel_row,episode,character,voice_id,start_TCR,end_TCR,fps,start_ms,end_ms,tcr_duration_ms,original_dialogue,final_dialogue,rewritten,rewrite_attempts,leading_trim_ms,pass1_audio_duration_ms,pass1_overrun_ms,pass1_percent_overrun,pass1_desired_speed,pass1_5_audio_duration_ms,pass1_5_overrun_ms,pass1_5_percent_overrun,pass1_5_desired_speed,pass2_speed,pass2_audio_duration_ms,pass2_overrun_ms,pass2_percent_overrun,within_target,status,note,track,adjusted_start_TCR,adjusted_end_TCR
```

- `start_TCR`/`end_TCR` are the script times as `HH:MM:SS:FF` at the run's frame rate (`fps`); `start_ms`/`end_ms` the same in milliseconds; `tcr_duration_ms` is the slot.
- `final_dialogue` is what is spoken in the delivered audio (the Gemini rewrite when `rewritten` is `yes`). `rewrite_attempts` counts Gemini requests.
- `pass1_*` describe the first ElevenLabs take, `pass1_5_*` the take kept after pass 1.5, `pass2_*` the delivered audio. `pass2_speed` is the FFmpeg speed applied (`1.0000000` for normal and flagged rows).
- `status` is `OK`, `TCR_OUT_OVERRUN` (pass 2 audio more than 2% over its slot) or `MANUAL_REVIEW`; `note` says why a row was flagged.
- `adjusted_start_TCR` and `adjusted_end_TCR` show the actual placement and the end of the pass 2 clip. TCR display rounds to the nearest frame (half-frame ties round up); audio placement stays in milliseconds.

Validation runs before speech generation. Requests are sequential and are not automatically retried: retrying after a provider failure can incur additional charges for already generated rows. A failed run produces no completed ZIP and discards temporary audio. API voice availability is checked by ElevenLabs during generation. No live API calls are needed for tests: ElevenLabs and Gemini are replaced by fakes.

## Command line

```sh
v2/.venv/bin/python -m v2.main script.xlsx --validate-only
v2/.venv/bin/python -m v2.main script.xlsx --output v2/runtime/session01.zip --fps 25
v2/.venv/bin/python -m v2.main script.xlsx --sheet "English dialogue" --validate-only
v2/.venv/bin/python -m unittest discover -s v2/tests -v
```

Existing output archives are not overwritten. Web jobs use separate UUID folders under `v2/runtime/`. Uploaded workbooks and completed ZIPs remain there until manually removed. Job status is held in memory: restarting the app loses browser status, but completed archives remain on disk. This is a local, single-process tool; a shared production deployment would need authentication and persistent job storage/workers. The renderer holds one character track in memory at a time, so long timelines need sufficient memory.
