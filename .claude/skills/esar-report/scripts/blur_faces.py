#!/usr/bin/env python3
"""Blur faces in a traffic photo or video before it is uploaded as e-SAR evidence.

    blur_faces.py INPUT [-o OUT_DIR] [--box X,Y,W,H ...] [--min-score 0.4] [--mute]

Writes to OUT_DIR (default ./esar-out):
    <stem>_blurred.jpg|mp4   the file to upload (all metadata stripped)
    <stem>_check.jpg         NOT for upload: outlines and a pixel grid, to check the blur by eye
and prints a JSON summary: faces found, metadata read from the original, warnings.

Number plates are never blurred: the police need them to identify the vehicle.
Face detection is automatic and can miss faces (profile, tiny, behind glass, in
mirrors or on screens). Always check the result by eye and add --box for misses.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import ExifTags, Image, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
DEFAULT_MODEL = Path(__file__).resolve().parent.parent / "models" / "face_detection_yunet_2023mar.onnx"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp"}
FACE_PAD = 0.4  # fraction of the detected box added on every side (hair, ears, detector error)
CHECK_FRAMES = 8


def parse_box(text):
    try:
        x, y, w, h = (int(float(v)) for v in text.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("expected X,Y,W,H in pixels, e.g. 120,80,60,60")
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("width and height must be positive")
    return (x, y, w, h)


def load_detector(model_path, min_score):
    if not model_path.is_file():
        sys.exit(f"face model not found: {model_path}")
    if hashlib.sha256(model_path.read_bytes()).hexdigest() != MODEL_SHA256:
        sys.exit(f"face model {model_path} does not match the pinned sha256; refusing to load it")
    return cv2.FaceDetectorYN.create(str(model_path), "", (320, 320), min_score, 0.3, 5000)


def iou(a, b):
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    iw, ih = min(ax1, bx1) - max(ax0, bx0), min(ay1, by1) - max(ay0, by0)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def scales_for(longest, video):
    if video:
        return [1.0]
    scales = [1.0]
    if longest > 1200:
        scales.insert(0, 0.5)  # large close-up faces
    if longest <= 2200:
        scales.append(2.0)  # small faces behind windscreens
    return scales


def detect_faces(detector, img, scales, min_score):
    """Return [(x, y, w, h), ...] in the coordinates of img, merged across scales."""
    boxes, scores = [], []
    for s in scales:
        if s == 1.0:
            view = img
        else:
            interp = cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA
            view = cv2.resize(img, None, fx=s, fy=s, interpolation=interp)
        detector.setInputSize((view.shape[1], view.shape[0]))
        _, faces = detector.detect(view)
        for f in faces if faces is not None else []:
            boxes.append([float(v) / s for v in f[:4]])
            scores.append(float(f[14]))
    if not boxes:
        return []
    keep = cv2.dnn.NMSBoxes(boxes, scores, min_score, 0.3)
    return [tuple(boxes[int(i)]) for i in np.array(keep).flatten()]


def blur_box(img, box, pad, ellipse):
    """Mosaic + gaussian blur of a box, in place. Manual boxes (ellipse=False) are fully covered."""
    h, w = img.shape[:2]
    x, y, bw, bh = box
    x0, y0 = max(0, int(x - pad * bw)), max(0, int(y - pad * bh))
    x1, y1 = min(w, int(x + bw + pad * bw)), min(h, int(y + bh + pad * bh))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return
    roi = img[y0:y1, x0:x1]
    rh, rw = roi.shape[:2]
    small = cv2.resize(roi, (max(2, rw // 10), max(2, rh // 10)), interpolation=cv2.INTER_AREA)
    mosaic = cv2.resize(small, (rw, rh), interpolation=cv2.INTER_LINEAR)
    k = max(3, (min(rw, rh) // 3) | 1)
    soft = cv2.GaussianBlur(mosaic, (k, k), 0)
    if not ellipse:
        img[y0:y1, x0:x1] = soft
        return
    mask = np.zeros((rh, rw), np.float32)
    cv2.ellipse(mask, (rw // 2, rh // 2), (rw // 2, rh // 2), 0, 0, 360, 1.0, -1)
    mask = cv2.GaussianBlur(mask, (k, k), 0)[..., None]
    img[y0:y1, x0:x1] = (soft * mask + roi * (1 - mask)).astype(np.uint8)


def put_label(img, text, org, scale):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_AA)


def draw_check(img, faces, manual):
    """Copy of img with a labelled pixel grid, green outlines for auto and magenta for manual blurs."""
    h, w = img.shape[:2]
    step = max(50, int(round(max(w, h) / 10 / 50)) * 50)
    scale = max(w, h) / 1500
    grid = img.copy()
    for gx in range(step, w, step):
        cv2.line(grid, (gx, 0), (gx, h), (255, 255, 255), 1)
    for gy in range(step, h, step):
        cv2.line(grid, (0, gy), (w, gy), (255, 255, 255), 1)
    out = cv2.addWeighted(grid, 0.3, img, 0.7, 0)
    for gx in range(step, w, step):
        put_label(out, str(gx), (gx + 3, int(18 * max(scale, 0.6) + 2)), 0.5 * max(scale, 0.6))
    for gy in range(step, h, step):
        put_label(out, str(gy), (3, gy - 4), 0.5 * max(scale, 0.6))
    for x, y, bw, bh in faces:
        x0, y0 = int(x - FACE_PAD * bw), int(y - FACE_PAD * bh)
        x1, y1 = int(x + bw * (1 + FACE_PAD)), int(y + bh * (1 + FACE_PAD))
        cv2.rectangle(out, (x0, y0), (x1, y1), (0, 200, 0), max(1, int(scale)))
    for x, y, bw, bh in manual:
        cv2.rectangle(out, (x, y), (x + bw, y + bh), (255, 0, 255), max(1, int(scale)))
    return out


def dms_to_deg(values, ref):
    deg, minutes, seconds = (float(v) for v in values)
    result = deg + minutes / 60 + seconds / 3600
    return -result if str(ref).upper() in ("S", "W") else result


def read_exif(im):
    """Capture time and GPS from the original, if the file still has them."""
    meta = {}
    exif = im.getexif()
    taken = exif.get_ifd(ExifTags.IFD.Exif).get(36867) or exif.get(306)
    if taken:
        # Camera clock, local time, no timezone.
        meta["captured_at"] = str(taken).replace(":", "-", 2).replace(" ", "T")
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    if gps.get(2) and gps.get(4):
        lat = dms_to_deg(gps[2], gps.get(1, "N"))
        lon = dms_to_deg(gps[4], gps.get(3, "E"))
        meta["gps"] = {"lat": round(lat, 6), "lon": round(lon, 6),
                       "map": f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lon:.6f}#map=18/{lat:.6f}/{lon:.6f}"}
    return meta


def probe_video_meta(src):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    cmd = [ffprobe, "-v", "error", "-show_entries", "format_tags=creation_time,location", "-of", "json", str(src)]
    try:
        tags = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout)["format"]["tags"]
    except (subprocess.SubprocessError, KeyError, ValueError):
        return {}
    meta = {}
    if "creation_time" in tags:
        meta["captured_at"] = tags["creation_time"]  # usually UTC
    if "location" in tags:
        meta["location_tag"] = tags["location"]
    return meta


def process_image(src, out_dir, detector, args):
    with Image.open(src) as im:
        meta = read_exif(im)
        rgb = ImageOps.exif_transpose(im).convert("RGB")
    bgr = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    faces = detect_faces(detector, bgr, scales_for(max(h, w), False), args.min_score)
    out = bgr.copy()
    for box in faces:
        blur_box(out, box, FACE_PAD, True)
    for box in args.box:
        blur_box(out, box, 0, False)
    blurred, check = out_dir / f"{src.stem}_blurred.jpg", out_dir / f"{src.stem}_check.jpg"
    cv2.imwrite(str(blurred), out, [cv2.IMWRITE_JPEG_QUALITY, 92])  # cv2 writes no EXIF
    cv2.imwrite(str(check), draw_check(out, faces, args.box), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return {"kind": "image", "output": str(blurred), "check": str(check), "size": [w, h],
            "faces_auto": len(faces), "metadata": meta, "warnings": []}


def encode_with_audio(silent, src, dst, mute):
    """Re-encode to H.264 (+ original audio) and drop every metadata tag. Returns warnings."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        shutil.move(silent, dst)
        return ["ffmpeg not found: the output has no audio and uses the mp4v codec, which the portal may not accept"]
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(silent)]
    if mute:
        cmd += ["-an"]
    else:
        cmd += ["-i", str(src), "-map", "0:v:0", "-map", "1:a:0?", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-map_metadata", "-1", "-movflags", "+faststart", str(dst)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"ffmpeg failed: {result.stderr.strip()}")
    return []


def process_video(src, out_dir, detector, args):
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        sys.exit(f"cannot open video: {src}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    sample_every = max(1, total // CHECK_FRAMES) if total > 0 else 90
    hold = max(1, round(fps / 3))  # keep a blur this many frames after the detector loses the face
    scales = scales_for(max(w, h), True)
    tracks, samples, detections = [], [], 0
    blurred, check = out_dir / f"{src.stem}_blurred.mp4", out_dir / f"{src.stem}_check.jpg"
    with tempfile.TemporaryDirectory() as tmp:
        silent = Path(tmp) / "silent.mp4"
        writer = cv2.VideoWriter(str(silent), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            found = detect_faces(detector, frame, scales, args.min_score)
            detections += len(found)
            tracks = [[b, ttl - 1] for b, ttl in tracks if ttl > 1 and not any(iou(b, d) > 0.2 for d in found)]
            tracks += [[d, hold] for d in found]
            for box, _ in tracks:
                blur_box(frame, box, FACE_PAD, True)
            for box in args.box:
                blur_box(frame, box, 0, False)
            if idx % sample_every == 0 and len(samples) < CHECK_FRAMES:
                samples.append(draw_check(frame, [t[0] for t in tracks], args.box))
            writer.write(frame)
            idx += 1
        writer.release()
        cap.release()
        warnings = encode_with_audio(silent, src, blurred, args.mute)
    if samples:
        thumbs = [cv2.resize(s, (640, int(640 * h / w))) for s in samples]
        thumbs += [np.zeros_like(thumbs[0])] * (-len(thumbs) % 2)
        rows = [np.hstack(thumbs[i:i + 2]) for i in range(0, len(thumbs), 2)]
        cv2.imwrite(str(check), np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 85])
    size_mb = round(blurred.stat().st_size / 1e6, 1)
    warnings.append("--box blurs the same area in every frame; a moving face the detector misses stays visible")
    warnings.append(f"check image shows {len(samples)} sampled frames, not the whole video")
    if size_mb > 100:
        warnings.append(f"output is {size_mb} MB; reports mention a 100 MB limit, the upload form shows the real one")
    return {"kind": "video", "output": str(blurred), "check": str(check) if samples else None,
            "size": [w, h], "frames": idx, "fps": round(fps, 2), "size_mb": size_mb,
            "face_detections": detections, "metadata": probe_video_meta(src), "warnings": warnings}


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--out-dir", type=Path, default=Path("esar-out"))
    parser.add_argument("--box", type=parse_box, action="append", default=[], metavar="X,Y,W,H",
                        help="extra area to blur, in pixels of the oriented image (repeatable)")
    parser.add_argument("--min-score", type=float, default=0.4,
                        help="detector confidence threshold; lower finds more faces and more false hits")
    parser.add_argument("--mute", action="store_true", help="video: drop the audio track")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    args = parser.parse_args(argv)
    if not args.input.is_file():
        sys.exit(f"input not found: {args.input}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    detector = load_detector(args.model, args.min_score)
    process = process_video if args.input.suffix.lower() in VIDEO_EXT else process_image
    summary = {"input": str(args.input), "boxes_manual": len(args.box), **process(args.input, args.out_dir, detector, args)}
    summary["warnings"].insert(0, "automatic detection can miss faces: check the _check.jpg image by eye")
    if not summary.get("faces_auto", summary.get("face_detections")):
        summary["warnings"].append("no face detected: confirm by eye that there really are none")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))
