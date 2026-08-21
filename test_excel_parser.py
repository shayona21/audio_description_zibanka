import csv
import tempfile
import unittest
from datetime import time
from pathlib import Path

from openpyxl import Workbook

from excel_parser import detect_file_type, parse_file


class ExcelParserTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_csv_and_xlsx_produce_the_same_pipeline_rows(self):
        source_rows = [
            ["Start timecode", "End timecode", "Hindi dialogue"],
            ["00:00:01:02", "00:00:02:02", "पहली पंक्ति"],
            ["00:00:06:05", "00:00:06:19", "दूसरी पंक्ति"],
        ]

        csv_path = self.directory / "script.CSV"
        with csv_path.open("w", encoding="utf-8", newline="") as file_handle:
            csv.writer(file_handle).writerows(source_rows)

        excel_path = self.directory / "script.xlsx"
        workbook = Workbook()
        worksheet = workbook.active
        for row in source_rows:
            worksheet.append(row)
        workbook.save(excel_path)

        expected = [
            {
                "row_number": 1,
                "start_ms": 1080,
                "end_ms": 2080,
                "duration_ms": 1000,
                "text": "पहली पंक्ति",
            },
            {
                "row_number": 2,
                "start_ms": 6200,
                "end_ms": 6760,
                "duration_ms": 560,
                "text": "दूसरी पंक्ति",
            },
        ]

        self.assertEqual(parse_file(csv_path, progress_callback=lambda _: None), expected)
        self.assertEqual(parse_file(excel_path, progress_callback=lambda _: None), expected)

    def test_excel_native_time_cells_are_supported(self):
        excel_path = self.directory / "native-times.xlsx"
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.append([time(0, 0, 1, 80_000), time(0, 0, 2, 80_000), "विवरण"])
        workbook.save(excel_path)

        rows = parse_file(excel_path, progress_callback=lambda _: None)

        self.assertEqual(rows[0]["start_ms"], 1080)
        self.assertEqual(rows[0]["end_ms"], 2080)
        self.assertEqual(rows[0]["duration_ms"], 1000)

    def test_file_type_detection_is_case_insensitive(self):
        self.assertEqual(detect_file_type("script.CSV"), "csv")
        self.assertEqual(detect_file_type("script.XLSX"), "excel")
        self.assertEqual(detect_file_type("script.xls"), "excel")

    def test_empty_file_has_a_clear_validation_error(self):
        csv_path = self.directory / "empty.csv"
        csv_path.write_text("", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "No valid audio-description rows"):
            parse_file(csv_path, progress_callback=lambda _: None)


if __name__ == "__main__":
    unittest.main()
