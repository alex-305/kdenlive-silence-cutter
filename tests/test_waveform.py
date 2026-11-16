import math
import os
import shutil
import tempfile
import unittest
import wave
from array import array
from pathlib import Path
from unittest.mock import patch

from silence_cutter.waveform import analyze_with_waveform


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
class WaveformTests(unittest.TestCase):
    def test_extracts_stereo_envelope_and_silence_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            media = folder / "tone.wav"
            rate = 16000
            samples = array("h")
            for index in range(rate * 2):
                time = index / rate
                samples.append(round(12000 * math.sin(2 * math.pi * 440 * time))
                               if time < 0.4 or time >= 1.2 else 0)
            with wave.open(str(media), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(samples.tobytes())

            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(folder / "cache")}):
                waveform, silences = analyze_with_waveform(
                    media, noise_db=-40, min_duration=0.5)
                self.assertAlmostEqual(waveform.duration, 2.0, places=2)
                self.assertEqual(len(waveform.levels[0]) // 4, waveform.bucket_count)
                self.assertGreater(max(waveform.levels[0]), 7000)
                self.assertTrue(any(0.35 <= item.start <= 0.45 and
                                    1.15 <= item.end <= 1.25 for item in silences))
                cached, _ = analyze_with_waveform(media, noise_db=-40, min_duration=0.5)
                self.assertEqual(cached.bucket_count, waveform.bucket_count)
                self.assertTrue(list((folder / "cache").rglob("*.pcmpeaks")))


if __name__ == "__main__":
    unittest.main()
