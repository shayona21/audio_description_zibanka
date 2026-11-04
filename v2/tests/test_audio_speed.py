"""Measurements, the normal / rewrite / pass 2 rules and the success target (standard library only)."""

import io
import math
import struct
import unittest
from unittest.mock import patch
import wave

from v2.audio_speed import (MAX_SPEED, MIN_SPEED, Measurement, apply_pass2, format_sig, measure,
                            pass2_speed, sig, success_rate)


def clip(speech_ms, tail_ms=300, rate=24000):
    """A trimmed take: a tone from the first sample, then trailing silence."""
    tone = [round(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(speech_ms * rate // 1000)]
    frames = struct.pack(f"<{len(tone)}h", *tone) + b"\0\0" * (tail_ms * rate // 1000)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return output.getvalue()


class MeasurementTests(unittest.TestCase):
    def test_audio_duration_is_the_spoken_part_without_trailing_silence(self):
        self.assertEqual(measure(clip(2500), 2000).audio_duration_ms, 2500)

    def test_overrun_is_negative_when_too_short(self):
        m = Measurement(tcr_duration_ms=10000, audio_duration_ms=5000)
        self.assertEqual(m.overrun_ms, -5000)
        self.assertEqual(m.percent_overrun, -50.0)
        self.assertEqual(m.desired_speed, 0.5)

    def test_desired_speed_keeps_eight_significant_figures_without_limits(self):
        self.assertEqual(Measurement(2100, 3050).desired_speed, 1.452381)     # 1.45238095... -> 8 s.f.
        self.assertEqual(format_sig(Measurement(2100, 3050).desired_speed), "1.4523810")
        self.assertEqual(Measurement(1000, 4000).desired_speed, 4.0)          # no upper limit
        self.assertEqual(format_sig(4.0), "4.0000000")
        self.assertEqual(format_sig(-2.0), "-2.0000000")
        self.assertEqual(format_sig(93.181818181), "93.181818")
        self.assertEqual(sig(2 / 3), 0.66666667)

    def test_normal_rows_are_within_two_percent_either_way(self):
        self.assertTrue(Measurement(1000, 1020).is_normal)
        self.assertTrue(Measurement(1000, 980).is_normal)
        self.assertFalse(Measurement(1000, 1021).is_normal)
        self.assertFalse(Measurement(1000, 979).is_normal)

    def test_rewrite_only_outside_the_universal_limits(self):
        self.assertFalse(Measurement(1000, 1200).needs_rewrite)   # exactly 1.2: pass 2 can fit it
        self.assertFalse(Measurement(1000, 800).needs_rewrite)    # exactly 0.8
        self.assertTrue(Measurement(1000, 1201).needs_rewrite)
        self.assertTrue(Measurement(1000, 799).needs_rewrite)
        self.assertTrue(Measurement(1000, 0).needs_rewrite)       # silent take
        self.assertEqual((MIN_SPEED, MAX_SPEED), (0.8, 1.2))

    def test_pass2_speed(self):
        self.assertEqual(pass2_speed(Measurement(1000, 1015)), 1.0)                       # normal
        self.assertEqual(pass2_speed(Measurement(1000, 1150)), 1.15)                      # sped up
        self.assertEqual(pass2_speed(Measurement(1000, 850)), 0.85)                       # slowed down
        self.assertEqual(pass2_speed(Measurement(1000, 1500)), MAX_SPEED)                 # held at the limit
        self.assertEqual(pass2_speed(Measurement(1000, 500)), MIN_SPEED)
        self.assertEqual(pass2_speed(Measurement(1000, 1500), manual_review=True), 1.0)   # flagged: left at 1.0x

    def test_apply_pass2_leaves_normal_and_flagged_rows_untouched(self):
        take = clip(1010)
        with patch("v2.audio_speed.shutil.which", return_value=None):   # no FFmpeg needed at 1.0x
            self.assertEqual(apply_pass2(take, measure(take, 1000)), (take, 1.0))
            self.assertEqual(apply_pass2(take, measure(take, 500), manual_review=True), (take, 1.0))

    def test_apply_pass2_fits_the_line_with_ffmpeg(self):
        take = clip(2300)
        fitted, speed = apply_pass2(take, measure(take, 2000))
        self.assertEqual(speed, 1.15)
        self.assertTrue(measure(fitted, 2000).within_target)

    def test_within_target_counts_short_lines_and_up_to_two_percent_long(self):
        self.assertTrue(Measurement(1000, 600).within_target)
        self.assertTrue(Measurement(1000, 1020).within_target)
        self.assertFalse(Measurement(1000, 1021).within_target)

    def test_success_rate(self):
        rows = [Measurement(1000, 1000)] * 9 + [Measurement(1000, 1100)]
        self.assertEqual(success_rate(rows), (90.0, True))
        rows = [Measurement(1000, 1000)] * 8 + [Measurement(1000, 1100)] * 3
        self.assertEqual(success_rate(rows), (72.727273, False))


if __name__ == "__main__":
    unittest.main()
