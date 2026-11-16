import unittest
from unittest.mock import patch

from silence_cutter.silence import Interval, detect_silence, parse_silencedetect


class SilenceParsingTests(unittest.TestCase):
    def test_parses_closed_intervals(self):
        text = "[silencedetect] silence_start: 1.25\n[silencedetect] silence_end: 2.5 | silence_duration: 1.25"
        self.assertEqual(parse_silencedetect(text), [Interval(1.25, 2.5)])

    def test_clamps_negative_and_unterminated_silence(self):
        self.assertEqual(parse_silencedetect("silence_start: -0.1", duration=3), [Interval(0, 3)])

    def test_ignores_empty_or_malformed_order(self):
        self.assertEqual(parse_silencedetect("silence_end: 2"), [])

    def test_detect_silence_reuses_known_duration(self):
        completed = type("Completed", (), {
            "returncode": 0,
            "stderr": "silence_start: 1.0\nsilence_end: 2.0 | silence_duration: 1.0",
        })()
        with patch("silence_cutter.silence.subprocess.run", return_value=completed), \
                patch("silence_cutter.silence.probe_duration") as probe:
            intervals = detect_silence("voice.wav", duration=3.0)

        self.assertEqual(intervals, [Interval(1.0, 2.0)])
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
