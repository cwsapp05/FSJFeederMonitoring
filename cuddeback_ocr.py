"""
Read the burned-in info bar on Cuddeback trail camera photos.

The bar runs along the bottom of every photo and looks like:

    [moon] [weather]   1/27/2026   12:28 PM   C37 - 015        [Cuddeback logo]

The moon-phase and weather icons on the left and the logo on the right confuse
Tesseract (it reads the moon as "4" and glues it onto the date), so only the
middle band of the bar, where the text sits, is cropped. The crop is defined
as fractions of the image size so it works at any resolution the camera is set
to.

Fields returned:
    date        "YYYY-MM-DD"
    time        "HH:MM" (24-hour)
    captured_at "YYYY-MM-DDTHH:MM:00" — naive local camera time
    camera_id   e.g. "C37" (the camera's own name — see the CamSwitch sheet of
                the Log for how camera names map to positions over time)
    image_seq   e.g. "015" (the camera's per-trigger image counter)
    ocr_raw     the raw OCR string, kept for debugging misreads

Any field that can't be read is None. EXIF DateTimeOriginal, when the file has
one, is preferred over the OCR'd date/time (see read_capture_time).

Usage (manual check against one photo):
    python cuddeback_ocr.py path/to/photo.jpg
"""

import re
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

try:
    import pytesseract
except ImportError:  # OCR becomes a no-op; EXIF still works
    pytesseract = None

# Fractions of the image (left, top, right, bottom) containing the bar's text.
# Measured on a 2000x1540 Cuddeback photo: the bar is the bottom ~3% and the
# text sits roughly between 28% and 72% of the width.
BAR_TEXT_BOX = (0.27, 0.968, 0.73, 1.0)

OCR_CONFIG = "--psm 7 -c tessedit_char_whitelist=0123456789/:APMC- "

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AP])\s*M", re.IGNORECASE)
CAMERA_RE = re.compile(r"(C+\d+)\s*-\s*(\d+)")


def _crop_bar(img):
    w, h = img.size
    l, t, r, b = BAR_TEXT_BOX
    return img.crop((int(w * l), int(h * t), int(w * r), int(h * b)))


def _prepare(crop):
    """Grayscale, upscale 2x, threshold to white-on-black, then invert so
    Tesseract sees dark text on a light background."""
    g = crop.convert("L")
    g = g.resize((g.width * 2, g.height * 2))
    g = ImageOps.autocontrast(g)
    g = g.point(lambda p: 255 if p > 140 else 0)
    return ImageOps.invert(g)


def parse_bar_text(text):
    """Pull date/time/camera fields out of the raw OCR string. Works whether
    or not Tesseract kept the spaces between fields."""
    out = {"date": None, "time": None, "camera_id": None, "image_seq": None}
    compact = (text or "").replace(" ", "").upper()

    m = DATE_RE.search(compact)
    if m:
        month, day, year = map(int, m.groups())
        try:
            out["date"] = datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass
        rest = compact[m.end():]
    else:
        rest = compact
    # "/" only belongs in the date; anywhere after it it's a misread stroke
    # (e.g. "C76" read as "C7/6").
    rest = rest.replace("/", "")

    m = TIME_RE.search(rest)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if 1 <= hour <= 12 and 0 <= minute <= 59:
            if ampm == "P" and hour != 12:
                hour += 12
            if ampm == "A" and hour == 12:
                hour = 0
            out["time"] = f"{hour:02d}:{minute:02d}"
        rest = rest[m.end():]

    # "PMC37-015" — the camera name starts right after AM/PM once spaces are gone.
    m = CAMERA_RE.search(rest)
    if m:
        out["camera_id"] = m.group(1)
        out["image_seq"] = m.group(2)
    return out


def read_exif_datetime(img):
    """EXIF DateTimeOriginal (or DateTime) as a datetime, or None."""
    try:
        exif = img.getexif()
    except Exception:
        return None
    raw = None
    try:
        raw = exif.get_ifd(0x8769).get(36867)  # DateTimeOriginal
    except Exception:
        pass
    raw = raw or exif.get(306)  # DateTime
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def read_photo_metadata(path):
    """All metadata for one photo. Never raises — failures leave fields None."""
    result = {
        "date": None, "time": None, "captured_at": None,
        "camera_id": None, "image_seq": None, "ocr_raw": None,
        "time_source": None,
    }
    try:
        with Image.open(path) as img:
            img.load()
            exif_dt = read_exif_datetime(img)
            if pytesseract is not None:
                try:
                    raw = pytesseract.image_to_string(_prepare(_crop_bar(img)), config=OCR_CONFIG)
                    result["ocr_raw"] = raw.strip()
                    result.update(parse_bar_text(raw))
                except Exception as e:  # tesseract binary missing, etc.
                    result["ocr_raw"] = f"OCR failed: {e}"
    except Exception as e:
        result["ocr_raw"] = f"Could not open image: {e}"
        return result

    if exif_dt:
        result["date"] = exif_dt.strftime("%Y-%m-%d")
        result["time"] = exif_dt.strftime("%H:%M")
        result["time_source"] = "exif"
    elif result["date"] and result["time"]:
        result["time_source"] = "ocr"

    if result["date"] and result["time"]:
        result["captured_at"] = f"{result['date']}T{result['time']}:00"
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    for k, v in read_photo_metadata(Path(sys.argv[1])).items():
        print(f"{k:12} {v}")
