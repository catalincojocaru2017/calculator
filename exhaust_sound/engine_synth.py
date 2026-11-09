"""Procedural engine-sound synthesizer driven by RPM (and optional throttle).

No audio samples needed: the sound is built from harmonics of the engine's
firing frequency plus filtered noise, saturated for aggression.

Firing frequency of a 4-stroke engine:  f0 = rpm / 60 * cylinders / 2
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

import numpy as np

TWO_PI = 2.0 * math.pi
N_HARMONICS = 16


@dataclass(frozen=True)
class Profile:
    name: str
    cylinders: int
    virtual_idle: float  # RPM the "fake" engine idles at
    virtual_redline: float  # RPM the "fake" engine reaches at the real redline
    slope: float  # harmonic roll-off; higher = darker/smoother
    half_order: float  # 0.5-order content: uneven-firing "burble" (cross-plane V8)
    drive: float  # saturation amount; higher = more aggressive
    noise: float  # intake/exhaust rasp level
    crackle: float  # 0..1 pops on deceleration


PROFILES = {
    "ferrari_v8": Profile("ferrari_v8", 8, 1000, 8700, 0.85, 0.30, 2.2, 0.07, 0.8),
    "lambo_v10": Profile("lambo_v10", 10, 1100, 8500, 0.75, 0.0, 2.8, 0.08, 0.6),
    "bmw_i6": Profile("bmw_i6", 6, 800, 7000, 1.25, 0.0, 1.6, 0.05, 0.4),
    "muscle_v8": Profile("muscle_v8", 8, 700, 6200, 1.35, 0.80, 2.0, 0.06, 0.9),
}


class EngineSynth:
    def __init__(
        self,
        profile: Profile,
        real_idle: float = 800.0,
        real_redline: float = 6000.0,
        sample_rate: int = 44100,
        rpm_tau: float = 0.12,
        seed: int | None = None,
    ) -> None:
        self.profile = profile
        self.real_idle = real_idle
        self.real_redline = real_redline
        self.sr = sample_rate
        self.rpm_tau = rpm_tau
        self._rng = np.random.default_rng(seed)
        self._lock = threading.Lock()

        # inputs (written by OBD thread)
        self._target_rpm = 0.0
        self._throttle: float | None = None
        self._last_in_t: float | None = None
        self._rpm_rate = 0.0  # RPM/s estimated from consecutive inputs

        # state (audio thread)
        self._rpm = 0.0
        self._phase = 0.0
        self._load = 0.0
        self._gain = 0.0
        self._w = np.zeros(N_HARMONICS)
        self._k = np.arange(1, N_HARMONICS + 1, dtype=np.float64)
        self._norm = 1.0 / float(np.sum(self._k**-profile.slope))
        self._noise_tail = np.zeros(7)

    # ------------------------------------------------------------------ input
    def set_input(
        self, rpm: float, throttle: float | None = None, now: float | None = None
    ) -> None:
        """Feed a new OBD reading. throttle is 0..100 (%) or None if unsupported."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._last_in_t is not None and now > self._last_in_t:
                inst = (rpm - self._target_rpm) / (now - self._last_in_t)
                self._rpm_rate = 0.6 * self._rpm_rate + 0.4 * inst
            self._last_in_t = now
            self._target_rpm = max(0.0, float(rpm))
            self._throttle = None if throttle is None else float(throttle)

    # ----------------------------------------------------------------- helpers
    def _virtual_rpm(self, rpm: np.ndarray) -> np.ndarray:
        p = self.profile
        t = (rpm - self.real_idle) / (self.real_redline - self.real_idle)
        return np.maximum(300.0, p.virtual_idle + t * (p.virtual_redline - p.virtual_idle))

    def _estimate_load(self, rpm: float, rate: float, throttle: float | None) -> float:
        tn = min(1.0, max(0.0, (rpm - self.real_idle) / (self.real_redline - self.real_idle)))
        base = 0.15 + 0.25 * tn
        if throttle is not None:
            est = throttle / 100.0
        else:
            est = max(0.0, rate) / 1500.0  # rising RPM ~ foot down
        return float(min(1.0, max(base, est)))

    # ------------------------------------------------------------------ render
    def render(self, n: int) -> np.ndarray:
        p, sr = self.profile, self.sr
        with self._lock:
            target, throttle, rate = self._target_rpm, self._throttle, self._rpm_rate

        # smooth RPM (one-pole) and ramp linearly across the block
        alpha = 1.0 - math.exp(-n / sr / self.rpm_tau)
        new_rpm = self._rpm + (target - self._rpm) * alpha
        rpm_ramp = np.linspace(self._rpm, new_rpm, n, endpoint=False)
        self._rpm = new_rpm

        # load follows quickly up, slowly down
        load_t = self._estimate_load(new_rpm, rate, throttle)
        a = 0.35 if load_t > self._load else 0.08
        self._load += (load_t - self._load) * a
        load = self._load

        v_rpm = self._virtual_rpm(rpm_ramp)
        f0 = v_rpm / 60.0 * p.cylinders / 2.0

        # phase accumulator, wrapped at 4*pi so the half-order stays continuous
        phase = self._phase + TWO_PI * np.cumsum(f0 / sr)
        self._phase = float(phase[-1] % (2.0 * TWO_PI))

        # per-block harmonic weights (brighter with load/rpm), crossfaded across block
        tn = min(1.0, max(0.0, (new_rpm - self.real_idle) / (self.real_redline - self.real_idle)))
        f_mean = float(f0.mean())
        fc = 600.0 + 5000.0 * load * (0.4 + 0.6 * tn)
        w_new = self._k**-p.slope / (1.0 + (self._k * f_mean / fc) ** 2)
        w_new = np.where(self._k * f_mean < 0.45 * sr, w_new, 0.0) * self._norm
        ramp = np.linspace(0.0, 1.0, n, endpoint=False)
        w_ramp = self._w[:, None] + (w_new - self._w)[:, None] * ramp[None, :]
        self._w = w_new
        harm = (np.sin(np.outer(self._k, phase)) * w_ramp).sum(axis=0)
        if p.half_order > 0.0:
            harm += p.half_order * (np.sin(0.5 * phase) * 0.6 + np.sin(1.5 * phase) * 0.3)

        # filtered noise (6-tap moving average = cheap low-pass), carried across blocks
        white = self._rng.standard_normal(n)
        buf = np.concatenate([self._noise_tail, white])
        self._noise_tail = buf[-7:]
        noise = np.convolve(buf, np.ones(7) / 7.0, mode="valid")[:n] * 2.5
        sig = harm * 0.8 + noise * p.noise * (0.2 + 0.8 * load)

        # deceleration crackle: falling RPM, foot off
        if (
            p.crackle > 0
            and load < 0.3
            and rate < -800.0
            and float(v_rpm.mean()) > p.virtual_idle * 1.5
            and self._rng.random() < p.crackle * n / sr * 25.0
        ):
            length = min(n, 500)
            pos = int(self._rng.integers(0, max(1, n - length + 1)))
            env = np.exp(-np.arange(length) / 90.0)
            sig[pos : pos + length] += self._rng.standard_normal(length) * env * 2.0

        # master: saturation + gain (silent when engine is off)
        gain_t = 0.0 if new_rpm < 100.0 else 0.35 + 0.65 * load
        g = np.linspace(self._gain, gain_t, n, endpoint=False)
        self._gain = gain_t
        out = np.tanh(p.drive * sig) * g * 0.8
        return out.astype(np.float32)
