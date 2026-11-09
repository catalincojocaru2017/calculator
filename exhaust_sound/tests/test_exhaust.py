import unittest

import numpy as np

from exhaust_sound.engine_synth import EngineSynth, Profile
from exhaust_sound.obd import Elm327, SimulatedSource, parse_rpm, parse_throttle

SR = 44100
# virtual range == real range, so f0 = rpm/60*cyl/2 exactly
TEST_PROFILE = Profile("test", 4, 800, 6000, 1.0, 0.0, 0.3, 0.0, 0.0)


def settle(synth, rpm, blocks=200, thr=None):
    for i in range(blocks):
        synth.set_input(rpm, thr, now=i * 0.01)
        out = synth.render(512)
    return out


class ParseTests(unittest.TestCase):
    def test_rpm(self):
        self.assertEqual(parse_rpm("41 0C 1A F8 \r\r>"), 1726.0)
        self.assertEqual(parse_rpm("410C0C80"), 800.0)

    def test_rpm_garbage(self):
        for bad in ("NO DATA", "UNABLE TO CONNECT", "?", "SEARCHING..."):
            self.assertIsNone(parse_rpm(bad))

    def test_throttle(self):
        self.assertAlmostEqual(parse_throttle("41 11 FF"), 100.0)
        self.assertIsNone(parse_throttle("NO DATA"))


class FakeElm:
    """Emulates an ELM327 over a transport; supports_throttle toggles PID 0111."""

    def __init__(self, supports_throttle=True, supports_suffix=True):
        self.out, self.sent = b"", []
        self.thr, self.suffix = supports_throttle, supports_suffix

    def write(self, data):
        cmd = data.decode().strip()
        self.sent.append(cmd)
        if cmd.startswith("AT"):
            r = "OK"
        elif cmd == "0100":
            r = "SEARCHING...\r41 00 BE 3F A8 13"
        elif cmd.endswith("1") and len(cmd) == 5 and not self.suffix:
            r = "?"
        elif cmd.startswith("010C"):
            r = "41 0C 1A F8"
        elif cmd.startswith("0111"):
            r = "41 11 80" if self.thr else "NO DATA"
        else:
            r = "?"
        self.out += (r + "\r\r>").encode()

    def read(self):
        o, self.out = self.out, b""
        return o

    def close(self):
        pass


class ElmTests(unittest.TestCase):
    def test_connect_and_read(self):
        e = Elm327(FakeElm())
        e.connect()
        rpm, _ = e.read()
        self.assertEqual(rpm, 1726.0)

    def test_throttle_detected_every_4th_read(self):
        e = Elm327(FakeElm(supports_throttle=True))
        e.connect()
        thr = [e.read()[1] for _ in range(4)]
        self.assertIsNone(thr[0])
        self.assertAlmostEqual(thr[3], 128 * 100 / 255, places=3)

    def test_no_throttle_support(self):
        e = Elm327(FakeElm(supports_throttle=False))
        e.connect()
        self.assertTrue(all(e.read()[1] is None for _ in range(8)))

    def test_only_mode_01_and_at_commands_are_sent(self):
        f = FakeElm()
        e = Elm327(f)
        e.connect()
        for _ in range(5):
            e.read()
        for c in f.sent:
            self.assertTrue(c.startswith(("AT", "01")), c)


class SynthTests(unittest.TestCase):
    def test_silent_when_engine_off(self):
        s = EngineSynth(TEST_PROFILE, sample_rate=SR, seed=1)
        out = settle(s, 0.0, blocks=20)
        self.assertLess(float(np.abs(out).max()), 1e-4)

    def test_fundamental_tracks_rpm(self):
        for rpm in (1500, 3000, 4500):
            s = EngineSynth(TEST_PROFILE, sample_rate=SR, seed=1)
            blocks = []
            for i in range(300):
                s.set_input(rpm, 50, now=i * 0.01)
                blocks.append(s.render(512))
            x = np.concatenate(blocks[-60:])
            spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
            f0 = rpm / 60 * 2
            freqs = np.fft.rfftfreq(len(x), 1 / SR)
            band = (freqs > f0 - 3) & (freqs < f0 + 3)
            self.assertGreater(spec[band].max(), 0.3 * spec.max(), f"rpm={rpm}")

    def test_louder_with_throttle(self):
        quiet = settle(EngineSynth(TEST_PROFILE, sample_rate=SR, seed=1), 3000, thr=0)
        loud = settle(EngineSynth(TEST_PROFILE, sample_rate=SR, seed=1), 3000, thr=100)
        self.assertGreater(float(np.abs(loud).mean()), float(np.abs(quiet).mean()))

    def test_output_bounded_and_finite(self):
        s = EngineSynth(TEST_PROFILE, sample_rate=SR, seed=1)
        sim = SimulatedSource()
        for i in range(2000):
            rpm, thr = sim.at(i * 512 / SR)
            s.set_input(rpm, thr, now=i * 512 / SR)
            out = s.render(512)
            self.assertTrue(np.isfinite(out).all())
            self.assertLessEqual(float(np.abs(out).max()), 1.0)


if __name__ == "__main__":
    unittest.main()
