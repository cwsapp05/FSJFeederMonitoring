"""Shared data rules for training species classifiers from Camelot labels.

Everything here reads the Camelot CSV and the photo folder. Nothing writes to the app's database.
"""
import collections
import csv
import datetime as dt
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / "runs" / "species_training"
MODELS_DIR = ROOT / "models"

SPECIES = "Species Common Name"          # The "(paths file)" columns match the Camelot export; "Species"/"Jay ID" don't.
QUANTITY = "Sighting Quantity (paths file)"
STATION = "Trap Station Name"
TAKEN = "Date/Time"
FILENAME = "Media Filename"

# Labels that say nothing about what is in the photo, so they can't be used as either class.
UNUSABLE = {"", "Unknown Sp"}
BLOCK = dt.timedelta(hours=1)
SPLIT_SEED = "fsj-v1"
SPLIT_FRACTIONS = (("train", 0.70), ("val", 0.15), ("test", 0.15))


def read_photos(csv_path):
    """One record per photo, with every Camelot sighting on it."""
    photos = {}
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            name = r[FILENAME].strip()
            p = photos.setdefault(name, {
                "stem": name,
                "station": r[STATION].strip(),
                "taken": dt.datetime.strptime(r[TAKEN], "%Y-%m-%d %H:%M:%S"),
                "species": collections.Counter(),
            })
            species = r[SPECIES].strip()
            if species:
                try:
                    quantity = int(r[QUANTITY])
                except ValueError:
                    quantity = 1
                p["species"][species] += quantity
    return photos


def assign_blocks(photos):
    """One-hour blocks per station, anchored on the block's first photo rather than a rolling window.

    The first photo opens a block and everything under an hour after it joins. The first photo after
    that opens the next. Sets photo["block"] and photo["gap_before_block"] (seconds since the previous
    photo at the station, on the photo that opened the block).
    """
    by_station = collections.defaultdict(list)
    for p in photos.values():
        by_station[p["station"]].append(p)
    for station, items in by_station.items():
        items.sort(key=lambda p: (p["taken"], p["stem"]))
        anchor, index, previous = None, 0, None
        for p in items:
            if anchor is None or p["taken"] - anchor >= BLOCK:
                anchor, index = p["taken"], index + 1
                p["opens_block_gap"] = (p["taken"] - previous).total_seconds() if previous else None
            p["block"] = f"{station}#{index:05d}"
            previous = p["taken"]


def assign_splits(photos):
    """Reproducible random split of whole blocks, balanced by station and month.

    Blocks are ordered by hash inside each (station, month) stratum and dealt out in the 70/15/15
    proportions, so each split sees the same mix and a block can never land in two splits.
    """
    blocks = collections.defaultdict(list)
    for p in photos.values():
        blocks[p["block"]].append(p)
    strata = collections.defaultdict(list)
    for key, members in blocks.items():
        first = min(m["taken"] for m in members)
        strata[(members[0]["station"], first.strftime("%Y-%m"))].append(key)
    pattern = _deal_pattern()
    for stratum, keys in strata.items():
        keys.sort(key=lambda k: hashlib.sha256(f"{SPLIT_SEED}:{k}".encode()).hexdigest())
        offset = int(hashlib.sha256(f"{SPLIT_SEED}:{stratum}".encode()).hexdigest(), 16) % len(pattern)
        for i, key in enumerate(keys):
            split = pattern[(i + offset) % len(pattern)]
            for m in blocks[key]:
                m["split"] = split


def _deal_pattern():
    """20 positions that hand out the splits evenly in the configured proportions."""
    counts = {name: round(frac * 20) for name, frac in SPLIT_FRACTIONS}
    pattern = []
    used = collections.Counter()
    for i in range(20):
        name = max(counts, key=lambda n: counts[n] * (i + 1) / 20 - used[n])
        used[name] += 1
        pattern.append(name)
    return pattern


def load_dataset(csv_path):
    photos = read_photos(csv_path)
    assign_blocks(photos)
    assign_splits(photos)
    return photos


def usable_labels(photo):
    """The species on a photo, or None when any sighting is unknown or the photo has no label at all."""
    names = set(photo["species"])
    if not names or names & UNUSABLE:
        return None
    return names


def stem_of(path):
    return os.path.splitext(os.path.basename(path))[0]
