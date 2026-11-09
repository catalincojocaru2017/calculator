"""Fake exhaust sound: OBD2 RPM -> synthesized engine sound on any audio output.

Examples:
  python -m exhaust_sound.main --source sim --profile ferrari_v8
  python -m exhaust_sound.main --source serial --port /dev/rfcomm0 --device "soundbar"
  python -m exhaust_sound.main --source sim --wav demo.wav --duration 16   # no audio device needed
"""
from __future__ import annotations

import argparse
import math
import threading
import time
import wave

import numpy as np

from .engine_synth import PROFILES, EngineSynth
from .obd import Elm327, SerialTransport, SimulatedSource, TcpTransport

SAMPLE_RATE = 44100
BLOCK = 512


def render_wav(path: str, synth: EngineSynth, sim: SimulatedSource, seconds: float) -> None:
    chunks, t = [], 0.0
    for _ in range(int(seconds * SAMPLE_RATE / BLOCK)):
        rpm, thr = sim.at(t)
        if int(t * 10) != int((t - BLOCK / SAMPLE_RATE) * 10):  # ~10 Hz OBD updates
            synth.set_input(rpm, thr, now=t)
        chunks.append(synth.render(BLOCK))
        t += BLOCK / SAMPLE_RATE
    pcm = (np.clip(np.concatenate(chunks), -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    print(f"wrote {path} ({seconds:.0f}s)")


def obd_loop(source, synth: EngineSynth, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            rpm, thr = source.read()
        except Exception as exc:  # adapter hiccup: keep last RPM, try again
            print("OBD read error:", exc)
            time.sleep(0.5)
            continue
        if not math.isnan(rpm):
            synth.set_input(rpm, thr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--source", choices=["sim", "serial", "tcp"], default="sim")
    ap.add_argument("--port", default="/dev/rfcomm0", help="serial port for Bluetooth Classic ELM327")
    ap.add_argument("--host", default="192.168.0.10")
    ap.add_argument("--tcp-port", type=int, default=35000)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="ferrari_v8")
    ap.add_argument("--real-idle", type=float, default=800)
    ap.add_argument("--real-redline", type=float, default=6000)
    ap.add_argument("--device", help="audio output device (name substring or index)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--volume", type=float, default=1.0)
    ap.add_argument("--wav", help="render the simulated drive cycle to this WAV and exit")
    ap.add_argument("--duration", type=float, default=SimulatedSource.LENGTH)
    args = ap.parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    synth = EngineSynth(PROFILES[args.profile], args.real_idle, args.real_redline, SAMPLE_RATE)

    if args.wav:
        render_wav(args.wav, synth, SimulatedSource(), args.duration)
        return

    if args.source == "sim":
        source = SimulatedSource()
    else:
        transport = (SerialTransport(args.port) if args.source == "serial"
                     else TcpTransport(args.host, args.tcp_port))
        source = Elm327(transport)
        print("Connecting to ELM327 (ignition ON)...")
        source.connect()
        print("Connected.")

    import sounddevice as sd

    def callback(outdata, frames, _time, _status):
        outdata[:, 0] = synth.render(frames) * args.volume

    stop = threading.Event()
    threading.Thread(target=obd_loop, args=(source, synth, stop), daemon=True).start()
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    try:
        with sd.OutputStream(samplerate=SAMPLE_RATE, blocksize=BLOCK, channels=1,
                             dtype="float32", device=device, callback=callback,
                             latency="low"):
            print(f"Playing '{args.profile}'. Ctrl+C to stop.")
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        source.close()


if __name__ == "__main__":
    main()
