# excel_parser.py
# Reads the Audio Description CSV file and converts each row into
# a dictionary with start_ms, end_ms, and hindi_text

import csv

# Frame rate for Indian TV content
FRAME_RATE = 25

def timecode_to_ms(timecode):
    """
    Convert a timecode string HH:MM:SS:FF to milliseconds.

    Example:
        "00:01:04:09" at 25fps
        = (0 * 3600 + 1 * 60 + 4 + 9/25) * 1000
        = 64360 ms
    """

    parts = timecode.strip().split(":")
    hours   = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2])
    frames  = int(parts[3])

    total_seconds = (hours * 3600) + (minutes * 60) + seconds + (frames / FRAME_RATE)
    total_ms = int(total_seconds * 1000)

    return total_ms


def parse_csv(file_path, progress_callback=print):
    """
    Read the AD CSV file and return a list of rows.

    Each row is a dictionary:
    {
        "row_number":  1,
        "start_ms":    1040,
        "end_ms":      2040,
        "duration_ms": 1000,
        "text":        "एम एक्स ओरिजिनल का लोगो।"
    }

    Skips any row that has no Hindi text.
    """

    rows = []
    row_number = 0

    with open(file_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)

        for row in reader:

            # Skip rows that don't have exactly 3 columns
            if len(row) < 3:
                continue

            start_tc = row[0].strip()
            end_tc   = row[1].strip()
            text     = row[2].strip()

            # Skip empty rows or rows with no text
            if not start_tc or not end_tc or not text:
                continue

            row_number += 1

            # Convert timecodes to milliseconds
            start_ms = timecode_to_ms(start_tc)
            end_ms   = timecode_to_ms(end_tc)

            rows.append({
                "row_number":  row_number,
                "start_ms":    start_ms,
                "end_ms":      end_ms,
                "duration_ms": end_ms - start_ms,
                "text":        text
            })

    progress_callback(f"Parsed {len(rows)} AD rows from {file_path}")
    return rows


# ── Quick test ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    rows = parse_csv("raktanchal_s3_ep7_ad.csv")

    # Print first 5 rows to verify
    for row in rows[:5]:
        print(f"Row {row['row_number']}: {row['start_ms']}ms → {row['end_ms']}ms | {row['text'][:40]}")
