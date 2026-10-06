import io
import unittest
from unittest.mock import patch

from pydub import AudioSegment
from pydub.generators import Sine

from audio_overlap import repair_overlap
from audio_speed import adjust_audio_speed


def tone(duration_ms):
    clip = Sine(440, sample_rate=24000).to_audio_segment(duration=duration_ms)
    return clip.export(io.BytesIO(), format="wav").getvalue()


class OverlapRepairTests(unittest.TestCase):
    def test_no_overlap_and_last_row_bypass_repair(self):
        clip = tone(3000)
        for window in (None, 3000, 4000):
            with self.subTest(window=window), patch("audio_overlap.adjust_audio_speed") as adjust:
                result = repair_overlap(clip, window)
                self.assertIs(result.audio, clip)
                self.assertFalse(result.repaired)
                self.assertIsNone(result.reason)
                adjust.assert_not_called()

    def test_exact_ten_percent_and_larger_overlaps_require_manual_fix(self):
        clip = tone(3000)
        for window in (2700, 2699, 1000, 0, -1):
            with self.subTest(window=window), patch("audio_overlap.adjust_audio_speed") as adjust:
                result = repair_overlap(clip, window)
                self.assertIs(result.audio, clip)
                self.assertFalse(result.repaired)
                self.assertIn("at least 10%", result.reason)
                adjust.assert_not_called()

    def test_real_repairs_fit_preserve_pitch_and_audio_format(self):
        for duration, window in ((3000, 2800), (3000, 2701), (2001, 2000)):
            with self.subTest(duration=duration, window=window):
                result = repair_overlap(tone(duration), window)
                self.assertTrue(result.repaired, result.reason)
                self.assertLessEqual(result.duration_ms, window)
                self.assertLess(result.speed, 1.20)
                clip = AudioSegment.from_wav(io.BytesIO(result.audio))
                self.assertEqual((clip.frame_rate, clip.channels, clip.sample_width), (24000, 1, 2))
                samples = clip[300:1300].get_array_of_samples()
                crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
                self.assertAlmostEqual(crossings, 440, delta=2)

    def test_repair_is_additional_to_the_speed_dial(self):
        dial_output = adjust_audio_speed(tone(3600), 1.20)
        result = repair_overlap(dial_output, 2800)
        self.assertTrue(result.repaired, result.reason)
        self.assertLessEqual(result.duration_ms, 2800)
        self.assertGreater(result.speed * 1.20, 1.20)
        self.assertLess(result.speed, 1.20)

    def test_residual_overlap_is_measured_and_retried(self):
        clip = tone(3000)
        with patch("audio_overlap.adjust_audio_speed", side_effect=[tone(2802), tone(2800)]) as adjust:
            result = repair_overlap(clip, 2800)
        self.assertTrue(result.repaired)
        self.assertEqual(result.duration_ms, 2800)
        self.assertAlmostEqual(adjust.call_args_list[0].args[1], 3000 / 2800)
        self.assertGreater(adjust.call_args_list[1].args[1], 3000 / 2800)
        self.assertTrue(all(call.args[0] is clip for call in adjust.call_args_list))

    def test_residual_overlap_cannot_push_speed_to_or_over_cap(self):
        clip = tone(3000)
        with patch("audio_overlap.adjust_audio_speed", return_value=tone(4000)) as adjust:
            result = repair_overlap(clip, 2800)
        self.assertIs(result.audio, clip)
        self.assertFalse(result.repaired)
        self.assertIn("not below 1.20x", result.reason)
        self.assertEqual(adjust.call_count, 1)

    def test_unresolved_residual_returns_original_after_bounded_retries(self):
        clip = tone(3000)
        with patch("audio_overlap.adjust_audio_speed", return_value=tone(2801)) as adjust:
            result = repair_overlap(clip, 2800)
        self.assertIs(result.audio, clip)
        self.assertFalse(result.repaired)
        self.assertIn("still overlaps", result.reason)
        self.assertEqual(adjust.call_count, 4)

    def test_missing_ffmpeg_or_failed_repair_retains_entire_clip(self):
        clip = tone(3000)
        with patch("audio_speed.shutil.which", return_value=None):
            result = repair_overlap(clip, 2800)
        self.assertIs(result.audio, clip)
        self.assertIn("FFmpeg", result.reason)
        with patch("audio_overlap.adjust_audio_speed", side_effect=RuntimeError("timed out")):
            result = repair_overlap(clip, 2800)
        self.assertIs(result.audio, clip)
        self.assertIn("timed out", result.reason)


if __name__ == "__main__":
    unittest.main()
