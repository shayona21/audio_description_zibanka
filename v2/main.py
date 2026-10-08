import argparse
from pathlib import Path

from dotenv import load_dotenv

from .excel_parser import SUPPORTED_FPS, parse_excel
from .pipeline import run_pipeline


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description="AD version 2: English Excel to episode/character WAV tracks")
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--output", type=Path, default=Path("v2/runtime/AD_v2_output.zip"))
    parser.add_argument("--fps", type=int, choices=SUPPORTED_FPS, default=25)
    parser.add_argument("--sheet", help="Worksheet name; defaults to the first worksheet")
    parser.add_argument("--validate-only", action="store_true", help="Validate without calling ElevenLabs")
    args = parser.parse_args()
    try:
        if args.validate_only:
            rows = parse_excel(args.workbook, fps=args.fps, sheet_name=args.sheet)
            print(f"Valid: {len(rows)} rows, {len({row.group for row in rows})} episode/character tracks")
        else:
            run_pipeline(args.workbook, args.output, fps=args.fps, sheet_name=args.sheet)
    except Exception as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
