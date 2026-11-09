"""Minimal read-only ELM327 client (RPM + optional throttle) and a drive-cycle simulator.

Only Mode 01 (live data) PIDs are ever sent. Nothing is written to the ECU.
  PID 010C  RPM       = (A*256 + B) / 4
  PID 0111  Throttle  = A * 100 / 255   (%)  -- not supported on every car
"""
from __future__ import annotations

import re
import socket
import time
from typing import Optional, Protocol, Tuple

Reading = Tuple[float, Optional[float]]  # (rpm, throttle %)

_RPM_RE = re.compile(r"410C([0-9A-F]{4})")
_THR_RE = re.compile(r"4111([0-9A-F]{2})")


def clean(resp: str) -> str:
    return re.sub(r"\s+", "", resp.upper().replace(">", ""))


def parse_rpm(resp: str) -> Optional[float]:
    m = _RPM_RE.search(clean(resp))
    return int(m.group(1), 16) / 4.0 if m else None


def parse_throttle(resp: str) -> Optional[float]:
    m = _THR_RE.search(clean(resp))
    return int(m.group(1), 16) * 100.0 / 255.0 if m else None


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...
    def read(self) -> bytes: ...
    def close(self) -> None: ...


class SerialTransport:
    """Bluetooth Classic paired as a serial port (Linux: /dev/rfcomm0, Windows: COMx)."""

    def __init__(self, port: str, baud: int = 38400) -> None:
        import serial  # lazy: only needed for real hardware
        self._s = serial.Serial(port, baud, timeout=0.05)

    def write(self, data: bytes) -> None:
        self._s.write(data)

    def read(self) -> bytes:
        return self._s.read(1024)

    def close(self) -> None:
        self._s.close()


class TcpTransport:
    """WiFi ELM327 adapters (typically 192.168.0.10:35000)."""

    def __init__(self, host: str = "192.168.0.10", port: int = 35000) -> None:
        self._s = socket.create_connection((host, port), timeout=5)
        self._s.settimeout(0.05)

    def write(self, data: bytes) -> None:
        self._s.sendall(data)

    def read(self) -> bytes:
        try:
            return self._s.recv(1024)
        except socket.timeout:
            return b""

    def close(self) -> None:
        self._s.close()


class Elm327:
    INIT = ["ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATAT1", "ATSP0"]

    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self._suffix = "1"        # "010C1": stop after the first reply -> faster
        self._has_throttle = False
        self._cycle = 0

    def _cmd(self, cmd: str, timeout: float = 2.0) -> str:
        self.t.write((cmd + "\r").encode())
        buf, end = b"", time.monotonic() + timeout
        while time.monotonic() < end:
            buf += self.t.read()
            if b">" in buf:
                break
        return buf.decode(errors="ignore")

    def connect(self) -> None:
        for c in self.INIT:
            self._cmd(c, timeout=3.0)
        # first real request triggers protocol search (can take several seconds)
        r = clean(self._cmd("0100", timeout=12.0))
        if "4100" not in r:
            raise ConnectionError(f"ECU did not answer 0100 (ignition on?): {r!r}")
        if self._cmd("010C1").strip().startswith("?"):
            self._suffix = ""
        if parse_rpm(self._cmd("010C" + self._suffix)) is None:
            raise ConnectionError("PID 010C (RPM) not supported/answered")
        self._has_throttle = parse_throttle(self._cmd("0111" + self._suffix)) is not None

    def read(self) -> Reading:
        rpm = parse_rpm(self._cmd("010C" + self._suffix, timeout=1.0))
        thr = None
        self._cycle += 1
        if self._has_throttle and self._cycle % 4 == 0:
            thr = parse_throttle(self._cmd("0111" + self._suffix, timeout=1.0))
        return (rpm if rpm is not None else float("nan")), thr

    def close(self) -> None:
        self.t.close()


class SimulatedSource:
    """Scripted drive cycle so the sound can be tuned without the car."""

    # (duration s, rpm_from, rpm_to, throttle %)
    CYCLE = [
        (3.0, 800, 800, 0),         # idle
        (4.0, 800, 6000, 80),       # pull to redline
        (1.0, 6000, 6000, 100),     # hold
        (1.5, 6000, 2500, 0),       # lift off -> crackle
        (2.0, 2500, 4500, 60),      # part-throttle
        (2.0, 4500, 800, 0),        # coast down
    ]
    LENGTH = sum(c[0] for c in CYCLE)

    def at(self, t: float) -> Reading:
        t %= self.LENGTH
        for dur, a, b, thr in self.CYCLE:
            if t < dur:
                return a + (b - a) * (t / dur), float(thr)
            t -= dur
        return 800.0, 0.0

    def read(self) -> Reading:
        time.sleep(0.1)  # ~10 Hz like a decent adapter on CAN
        return self.at(time.monotonic())

    def close(self) -> None:
        pass
