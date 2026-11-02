"""Measure the blank audio at the start of each ElevenLabs take.

Reads the raw ElevenLabs lines (pass1/lines/*.wav) from one or more run ZIPs downloaded
from the dubbing tool, or any folder of .wav files, and reports how many milliseconds of
silence come before speech starts in each clip. Pass 2 lines are skipped on purpose:
they are time-stretched, which stretches the silence too.

Usage:
    Set INPUT_PATHS below, then run:
    python measure_leading_silence.py
    python measure_leading_silence.py --csv leading_silence.csv

"Blank" means near-digital silence: the measurement stops at the first 10 ms window
whose RMS level is above -60 dBFS, i.e. the first sound of any kind (a breath or
mouth click counts as sound, so it is never counted as blank). Change the cut-off
with --threshold if needed.

Standard library only, so it runs anywhere Python 3 does.
"""

import argparse
import csv
import io
import math
import statistics
import sys
import wave
import zipfile
from array import array
from pathlib import Path

WINDOW_MS = 10
DEFAULT_THRESHOLD = -60
INPUT_PATHS = [
    "/Users/shayonabasu/github/audio_description_tool/v2/silence_trim_test.zip",  # Replace with a run ZIP, WAV file, or WAV folder.
]
FRAME_MS_25FPS = 40  # one video frame at 25 fps: the tolerance for "constant"


def read_wav(data):
    """Return (mono samples as floats in -1..1, sample rate)."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"only 16-bit WAV is supported (got {8 * width}-bit)")
    samples = array("h", raw)
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:  # keep the first channel
        samples = samples[::channels]
    return [s / 32768.0 for s in samples], rate


def leading_silence_ms(samples, rate, threshold_dbfs):
    """Milliseconds before the first window louder than threshold_dbfs (None if never)."""
    window = max(1, rate * WINDOW_MS // 1000)
    limit = 10 ** (threshold_dbfs / 20)
    for start in range(0, len(samples), window):
        chunk = samples[start:start + window]
        rms = math.sqrt(sum(s * s for s in chunk) / len(chunk))
        if rms > limit:
            return round(start * 1000 / rate)
    return None


def iter_clips(paths):
    """Yield (source, clip name, wav bytes) for every raw ElevenLabs take found."""
    for path in map(Path, paths):
        if path.is_dir():
            for wav_path in sorted(path.rglob("*.wav")):
                if "pass2" in wav_path.parts or "tracks" in wav_path.parts:
                    continue
                yield str(path), str(wav_path.relative_to(path)), wav_path.read_bytes()
        elif path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                names = sorted(n for n in archive.namelist()
                               if n.startswith("pass1/lines/") and n.lower().endswith(".wav"))
                if not names:
                    print(f"WARNING: {path} has no pass1/lines/*.wav", file=sys.stderr)
                for name in names:
                    yield path.name, name, archive.read(name)
        elif path.suffix.lower() == ".wav":
            yield str(path.parent), path.name, path.read_bytes()
        else:
            print(f"WARNING: skipping {path} (not a .zip, .wav or folder)", file=sys.stderr)


def describe(values):
    if not values:
        return "no clips with detectable speech"
    spread = max(values) - min(values)
    line = (f"n={len(values)}  min={min(values)}  max={max(values)}  "
            f"mean={statistics.mean(values):.1f}  median={statistics.median(values):.1f}")
    if len(values) > 1:
        line += f"  stdev={statistics.stdev(values):.1f}"
    return line + f"  spread={spread}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="dBFS level below which audio counts as blank (default: %(default)s)")
    parser.add_argument("--csv", default="leading_silence.csv", help="per-clip output (default: %(default)s)")
    args = parser.parse_args(argv)

    rows, failures = [], 0
    for source, name, data in iter_clips(INPUT_PATHS):
        try:
            samples, rate = read_wav(data)
        except (wave.Error, ValueError, EOFError) as exc:
            print(f"WARNING: {source}:{name}: {exc}", file=sys.stderr)
            failures += 1
            continue
        row = {"source": source, "clip": Path(name).name,
               "clip_duration_ms": round(len(samples) * 1000 / rate), "sample_rate": rate}
        row["leading_blank_ms"] = leading_silence_ms(samples, rate, args.threshold)
        rows.append(row)

    if not rows:
        print("No clips measured.", file=sys.stderr)
        return 1

    with open(args.csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Measured {len(rows)} ElevenLabs clips ({failures} unreadable). Per-clip results: {args.csv}\n")
    values = [r["leading_blank_ms"] for r in rows if r["leading_blank_ms"] is not None]
    print(f"Leading blank audio in ms (below {args.threshold:g} dBFS, 10 ms windows):")
    print(f"  {describe(values)}")
    if values:
        spread = max(values) - min(values)
        print()
        if spread <= FRAME_MS_25FPS:
            print(f"VERDICT: roughly CONSTANT (spread {spread} ms <= one 25 fps frame). "
                  f"A fixed trim of ~{min(values)} ms would be safe for every clip.")
        else:
            print(f"VERDICT: DYNAMIC (spread {spread} ms > one 25 fps frame of {FRAME_MS_25FPS} ms). "
                  f"Trim should be detected per clip.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
