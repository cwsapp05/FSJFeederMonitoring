"""Apply the scrub-jay model to the workspace's photos using saved features, without rerunning the pipeline.

    .venv\\Scripts\\python training\\relabel_library.py

Run it with the app's environment, after extract.py. It backs up the database first and can be rerun:
photos that already have a score are left alone, so stopping partway loses nothing.

- Photos already in the Library keep SpeciesNet's answer, and a score at or above the threshold makes them
  florida scrub-jay, as the pipeline would. Corrections stay on display.
- Photos not in the Library are added without a SpeciesNet answer. Those under the threshold show as
  "not classified" rather than "blank", because nothing has said they're empty. They count as unprocessed,
  so Process on the Workspace tab still runs SpeciesNet on them later.
"""
import argparse
import collections
import concurrent.futures
import datetime as dt
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(HERE), str(ROOT)]
DB_PATH = ROOT / "runs" / "fsj.db"
COMMIT_EVERY = 200


def back_up():
    """Before anything else, including the column migration that importing the app runs."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = DB_PATH.with_name(f"fsj-before-relabel-{stamp}.db")
    source = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    if source.execute("SELECT 1 FROM jobs WHERE status IN ('queued', 'running', 'cancelling') LIMIT 1").fetchone():
        raise SystemExit("A job is queued or running. Let it finish, or cancel it, first.")
    copy = sqlite3.connect(target)
    source.backup(copy)
    source_rows = source.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    copy_rows = copy.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    check = copy.execute("PRAGMA integrity_check").fetchone()[0]
    source.close()
    copy.close()
    if check != "ok" or copy_rows != source_rows:
        raise SystemExit(f"Backup failed its check ({check}, {copy_rows} of {source_rows} rows). Nothing was changed.")
    print(f"Backed up {copy_rows:,} photos to {target}", flush=True)


def saved_scores():
    """{stem: {"score", "count"}} from the saved features, one part at a time to keep memory down."""
    from common import WORK_DIR
    from features import FRAME
    from score_photos import load_heads, photo_result, score

    model, box_head, frame_head = load_heads(ROOT / "models" / "florida_scrub_jay.pt", "cpu")
    results = {}
    parts = sorted((WORK_DIR / "features").glob("part-*.npz"))
    if not parts:
        raise SystemExit("No saved features. Run extract.py first.")
    for path in parts:
        with np.load(path) as z:
            stems, category, conf, feature = z["stem"], z["category"], z["conf"], z["feature"]
        box = score(box_head, feature, "cpu")
        frame = score(frame_head, feature, "cpu")
        rows = collections.defaultdict(list)
        for i, stem in enumerate(stems.tolist()):
            rows[stem].append(i)
        for stem, idx in rows.items():
            idx = np.array(idx)
            kept = idx[(category[idx] != FRAME) & (conf[idx] >= model["floor"])]
            frame_row = idx[category[idx] == FRAME][0]
            results[stem.lower()] = photo_result(model, float(frame[frame_row]), box[kept], category[kept] == 1, conf[kept])
    print(f"Scored {len(results):,} photos from {len(parts)} saved files", flush=True)
    return model, results


def label_existing(app, db, model, results, sightings):
    changed = called = 0
    rows = db.query("SELECT * FROM images WHERE fsj_score IS NULL")
    with db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO species(label) VALUES(?)", (app.SPECIES_MODEL_LABEL,))
        for r in rows:
            result = results.get(r["stem"])
            if not result:
                continue
            changed += 1
            ai_species, ai_is_group, ai_conf, count = r["ai_species"], r["ai_is_group"], r["ai_conf"], r["count"]
            possible = 0
            if result["score"] >= model["threshold"]:
                called += 1
                ai_species, ai_is_group, ai_conf, count = app.SPECIES_MODEL_LABEL, 0, round(result["score"], 3), result["count"]
                possible = int(result["score"] < model["review_below"])
            camelot = app.camelot_fields(r["filename"], ai_species, ai_is_group, sightings)
            conn.execute(
                """UPDATE images SET speciesnet_species = ?, fsj_score = ?, fsj_possible = ?, ai_species = ?, ai_is_group = ?,
                       ai_conf = ?, count = ?, camelot_agreement = ?, display_species = ? WHERE id = ?""",
                (r["ai_species"], round(result["score"], 4), possible, ai_species, ai_is_group, ai_conf, count,
                 camelot["camelot_agreement"], r["corrected_species"] or ai_species or "blank", r["id"]))
    print(f"Library photos scored: {changed:,}. Now florida scrub-jay: {called:,}.", flush=True)


def prepare_new(app, folder, filename):
    """The slow part, reading the photo: its metadata and thumbnail."""
    image_id = app.image_id_for(filename)
    thumb = app.THUMBNAILS_DIR / f"{image_id}.jpg"
    has_thumb = thumb.exists() or app.make_thumbnail(folder / filename, image_id)
    return filename, image_id, app.cuddeback_exif.read_photo_metadata(folder / filename), has_thumb


def add_new(app, db, model, results, positions, sightings, folder, limit):
    known = {r["filename"] for r in db.query("SELECT filename FROM images")}
    known |= {r["filename"] for r in db.query("SELECT filename FROM dismissed_photos")}
    todo = [p for p in app.scan_photos(folder) if p not in known and app.photo_stem(p) in results][:limit]
    print(f"Adding {len(todo):,} photos to the Library", flush=True)

    exif_cols = ("camera_id", "image_seq", "hardware_id", "battery", "battery_source",
                 "trigger", "camera_model", "firmware", "exif_comment")
    cols = ("id", "folder", "filename", "stem", "position", *exif_cols, "captured_at", "time_source", "diel_period",
            "ai_species", "ai_conf", "speciesnet_species", "fsj_score", "fsj_possible", "camelot_species",
            "camelot_agreement", "band_id", "display_species", "count", "has_thumbnail", "created_at")
    insert = f"INSERT INTO images({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})"
    buffer, added, called, undated = [], 0, 0, 0

    def save():
        with db.transaction() as conn:
            conn.executemany(insert, buffer)
        buffer.clear()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for filename, image_id, meta, has_thumb in pool.map(lambda p: prepare_new(app, folder, p), todo):
            result = results[app.photo_stem(filename)]
            is_jay = result["score"] >= model["threshold"]
            ai_species = app.SPECIES_MODEL_LABEL if is_jay else app.NOT_CLASSIFIED
            camelot = app.camelot_fields(filename, ai_species, 0, sightings)
            captured_at = meta["captured_at"]
            undated += not captured_at
            called += is_jay
            buffer.append((
                image_id, str(folder), filename, app.photo_stem(filename), app.position_for(filename, positions),
                *(meta[c] for c in exif_cols), captured_at, "exif" if captured_at else None, app.compute_diel_period(captured_at),
                ai_species, round(result["score"], 3) if is_jay else None, app.NOT_CLASSIFIED,
                round(result["score"], 4), int(is_jay and result["score"] < model["review_below"]),
                camelot["camelot_species"], camelot["camelot_agreement"], camelot["jay_ids"],
                ai_species, result["count"] if is_jay else 0, int(has_thumb), app.now_iso()))
            added += 1
            if len(buffer) >= COMMIT_EVERY:
                save()
            if added % 2000 == 0:
                print(f"  {added:,} of {len(todo):,}", flush=True)
    if buffer:
        save()
    print(f"Added {added:,} photos. Called florida scrub-jay: {called:,}. Not classified: {added - called:,}. "
          f"No capture time: {undated:,}.", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="add at most this many new photos (for trying it out)")
    args = ap.parse_args()

    back_up()
    import app
    import db

    workspace = app.get_kv("workspace")
    if not workspace:
        raise SystemExit("No workspace is set.")
    folder = Path(workspace["folder"])
    if not folder.is_dir():
        raise SystemExit(f"Folder not found: {folder}")
    positions, sightings = app.read_camelot_csv(workspace["positions_csv"])
    model, results = saved_scores()
    label_existing(app, db, model, results, sightings)
    add_new(app, db, model, results, positions, sightings, folder, args.limit)
    left = db.query_one("SELECT COUNT(*) AS n FROM images WHERE fsj_score IS NULL")["n"]
    print(f"Library photos with no score: {left:,}", flush=True)
    os._exit(0)  # importing the app started its job worker thread


if __name__ == "__main__":
    main()
