import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import ExifTags, Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import blur_faces  # noqa: E402


def sharpness(img):
    return cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()


def gradient_image(w=200, h=200):
    """Smooth background so JPEG re-encoding leaves it almost unchanged."""
    ramp = np.linspace(40, 200, w, dtype=np.uint8)
    return np.dstack([np.tile(ramp, (h, 1))] * 3)


def test_manual_box_is_blurred_and_the_rest_is_untouched(tmp_path):
    img = gradient_image()
    img[50:110, 50:110] = np.indices((60, 60)).sum(axis=0)[..., None] % 2 * 255  # checkerboard
    src = tmp_path / "scene.png"
    cv2.imwrite(str(src), img)

    summary = blur_faces.run([str(src), "-o", str(tmp_path / "out"), "--box", "50,50,60,60"])

    out = cv2.imread(summary["output"])
    assert sharpness(out[50:110, 50:110]) < sharpness(img[50:110, 50:110]) * 0.01
    outside = np.abs(out[130:200, 130:200].astype(int) - img[130:200, 130:200].astype(int))
    assert outside.max() <= 4
    assert summary["boxes_manual"] == 1
    assert Path(summary["check"]).is_file()


def test_output_is_upright_and_carries_no_metadata(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to display
    exif.get_ifd(ExifTags.IFD.Exif)[36867] = "2026:10:24 17:32:10"
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps.update({1: "N", 2: (44.0, 25.0, 36.0), 3: "E", 4: (26.0, 6.0, 0.0)})
    src = tmp_path / "phone.jpg"
    Image.fromarray(gradient_image(120, 80)).save(src, exif=exif)

    summary = blur_faces.run([str(src), "-o", str(tmp_path / "out")])

    with Image.open(summary["output"]) as out:
        assert out.size == (80, 120)  # width and height swapped by the orientation
        assert len(out.getexif()) == 0
    assert summary["metadata"]["captured_at"] == "2026-10-24T17:32:10"
    assert summary["metadata"]["gps"]["lat"] == pytest.approx(44.426667, abs=1e-5)
    assert summary["metadata"]["gps"]["lon"] == pytest.approx(26.1, abs=1e-5)


def test_no_face_gives_a_warning_not_a_failure(tmp_path):
    src = tmp_path / "empty.png"
    cv2.imwrite(str(src), gradient_image())

    summary = blur_faces.run([str(src), "-o", str(tmp_path / "out")])

    assert summary["faces_auto"] == 0
    assert any("no face detected" in w for w in summary["warnings"])


def test_tampered_model_is_refused(tmp_path):
    fake = tmp_path / "model.onnx"
    fake.write_bytes(b"not the pinned model")
    src = tmp_path / "scene.png"
    cv2.imwrite(str(src), gradient_image())

    with pytest.raises(SystemExit, match="pinned sha256"):
        blur_faces.run([str(src), "-o", str(tmp_path / "out"), "--model", str(fake)])


@pytest.fixture
def astronaut(tmp_path):
    data = pytest.importorskip("skimage.data")
    img = cv2.cvtColor(data.astronaut(), cv2.COLOR_RGB2BGR)  # public domain NASA photo
    path = tmp_path / "astronaut.png"
    cv2.imwrite(str(path), img)
    return path, img


FACE = (178, 63, 90, 113)  # x, y, w, h found by the detector on the sample photo


def test_detected_face_is_blurred(astronaut, tmp_path):
    src, img = astronaut

    summary = blur_faces.run([str(src), "-o", str(tmp_path / "out")])

    x, y, w, h = FACE
    out = cv2.imread(summary["output"])
    assert summary["faces_auto"] >= 1
    assert sharpness(out[y:y + h, x:x + w]) < sharpness(img[y:y + h, x:x + w]) * 0.05


def test_video_faces_are_blurred_in_every_frame(astronaut, tmp_path):
    src, img = astronaut
    video = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 10, (512, 512))
    for _ in range(12):
        writer.write(img)
    writer.release()

    summary = blur_faces.run([str(video), "-o", str(tmp_path / "out"), "--mute"])

    assert summary["kind"] == "video" and summary["frames"] == 12
    x, y, w, h = FACE
    cap = cv2.VideoCapture(summary["output"])
    frames = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames += 1
        assert sharpness(frame[y:y + h, x:x + w]) < sharpness(img[y:y + h, x:x + w]) * 0.1
    assert frames == 12
    assert Path(summary["check"]).is_file()
