"""
Read photo metadata from the EXIF that Cuddeback cameras write into every JPEG.

Standard EXIF fields used:
    DateTimeOriginal   capture time to the second, camera-local
                       (falls back to DateTime)
    Model / Software   e.g. "LL-3A" / "LL-3A FW 3.4.0"

Cuddeback also packs its own settings into the EXIF UserComment as
comma-separated KEY=VALUE pairs, e.g.

    MR=C.1,AD=3/20/2025,...,BP=50%,BS=EXT,...,TR=MOTION,ID=C57,LO=013,
    MA=151778739346,SM=ADV,...,CD=3MIN

Of those, these are kept:
    ID   camera name as set on the unit (same as the info bar, e.g. "C57")
    LO   image number (same as the info bar, e.g. "013")
    MA   hardware ID: stays with the physical unit even when it's renamed
    BP   battery percentage, e.g. "50%"
    BS   battery source, e.g. "EXT"
    TR   what triggered the photo, e.g. "MOTION"
The full comment is stored too, so other keys can be used later without
reprocessing.

Photos only have this metadata if they're the original files from the SD
card. A photo that's been re-saved or exported (by a photo editor, a chat
app, etc.) usually loses it; those get no date and can be dated by hand in
the Library viewer.

Usage (check one photo):
    python cuddeback_exif.py path/to/photo.jpg
"""

import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

EXIF_IFD = 0x8769
TAG_DATETIME = 306
TAG_MODEL = 272
TAG_SOFTWARE = 305
TAG_DATETIME_ORIGINAL = 36867
TAG_USER_COMMENT = 37510

FIELDS = ("captured_at", "camera_id", "image_seq", "hardware_id", "battery",
          "battery_source", "trigger", "camera_model", "firmware", "exif_comment")


def _text(value):
    """EXIF strings may arrive as str or as bytes (UserComment can carry an
    8-byte charset prefix like b'ASCII\\0\\0\\0')."""
    if value is None:
        return None
    if isinstance(value, bytes):
        for prefix in (b"ASCII\0\0\0", b"UNICODE\0", b"\0" * 8):
            if value.startswith(prefix):
                value = value[len(prefix):]
                break
        value = value.decode("utf-8", errors="replace")
    value = str(value).strip("\0 \t\r\n")
    return value or None


def parse_user_comment(comment):
    """'KEY=VALUE,KEY=VALUE,...' -> dict. Unparseable pieces are skipped."""
    out = {}
    for part in (comment or "").split(","):
        key, sep, val = part.partition("=")
        if sep and key.strip():
            out[key.strip().upper()] = val.strip()
    return out


def _parse_exif_datetime(raw):
    try:
        return datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S")
    except (AttributeError, ValueError):
        return None


def read_photo_metadata(path):
    """All metadata for one photo as a dict of FIELDS. Never raises: anything
    missing or unreadable is None."""
    result = dict.fromkeys(FIELDS)
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            try:
                sub = exif.get_ifd(EXIF_IFD)
            except Exception:
                sub = {}
    except Exception:
        return result

    dt = (_parse_exif_datetime(_text(sub.get(TAG_DATETIME_ORIGINAL)))
          or _parse_exif_datetime(_text(exif.get(TAG_DATETIME))))
    if dt:
        result["captured_at"] = dt.strftime("%Y-%m-%dT%H:%M:%S")

    result["camera_model"] = _text(exif.get(TAG_MODEL))
    result["firmware"] = _text(exif.get(TAG_SOFTWARE))

    comment = _text(sub.get(TAG_USER_COMMENT))
    result["exif_comment"] = comment
    kv = parse_user_comment(comment)
    result["camera_id"] = kv.get("ID") or None
    result["image_seq"] = kv.get("LO") or None
    result["hardware_id"] = kv.get("MA") or None
    result["battery"] = kv.get("BP") or None
    result["battery_source"] = kv.get("BS") or None
    result["trigger"] = kv.get("TR") or None
    return result


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    for k, v in read_photo_metadata(Path(sys.argv[1])).items():
        print(f"{k:15} {v}")
