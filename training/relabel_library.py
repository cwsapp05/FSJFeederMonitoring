"""Apply the species models to the workspace's photos using saved features, without rerunning the pipeline.

    .venv\\Scripts\\python training\\relabel_library.py

Run it with the app's environment, after extract.py. It backs up the database first and can be rerun:
photos that already have a score from every model are left alone, so stopping partway loses nothing.
After adding a model, rerun it and every photo is scored by the new one.

- Photos already in the Library keep SpeciesNet's answer, and the models' call replaces it as the pipeline
  would: the highest score at or above its threshold wins. Corrections stay on display.
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


def saved_scores(app):
    """{stem: {label: (score, count, threshold, review_below)}} for every model with a file, from the saved
    features, one part at a time to keep memory down."""
    from common import WORK_DIR
    from features import FRAME
    from score_photos import load_heads, photo_result, score

    models = [(m["label"], *load_heads(m["file"], "cpu")) for m in app.SPECIES_MODELS if m["file"].exists()]
    if not models:
        raise SystemExit("No species model files in models/.")
    results = collections.defaultdict(dict)
    parts = sorted((WORK_DIR / "features").glob("part-*.npz"))
    if not parts:
        raise SystemExit("No saved features. Run extract.py first.")
    for path in parts:
        with np.load(path) as z:
            stems, category, conf, feature = z["stem"], z["category"], z["conf"], z["feature"]
        rows = collections.defaultdict(list)
        for i, stem in enumerate(stems.tolist()):
            rows[stem].append(i)
        for label, model, box_head, frame_head in models:
            box = score(box_head, feature, "cpu")
            frame = score(frame_head, feature, "cpu")
            for stem, idx in rows.items():
                idx = np.array(idx)
                kept = idx[(category[idx] != FRAME) & (conf[idx] >= model["floor"])]
                frame_row = idx[category[idx] == FRAME][0]
                r = photo_result(model, float(frame[frame_row]), box[kept], category[kept] == 1, conf[kept])
                results[stem.lower()][label] = (r["score"], r["count"], model["threshold"], model["review_below"])
    print(f"Scored {len(results):,} photos with {len(models)} model(s) from {len(parts)} saved files", flush=True)
    return results


def score_columns(app, result):
    """The score columns' values, in SPECIES_SCORE_COLUMNS order. A model without a file stays NULL."""
    return [round(result[m["label"]][0], 4) if m["label"] in result else None for m in app.SPECIES_MODELS]


def label_existing(app, db, results, sightings):
    changed = 0
    called = collections.Counter()
    columns = app.SPECIES_SCORE_COLUMNS
    # Only the models with a file can fill a score in.
    missing = " OR ".join(f"{m['column']} IS NULL" for m in app.SPECIES_MODELS if m["file"].exists())
    rows = db.query(f"SELECT * FROM images WHERE {missing}")
    with db.transaction() as conn:
        conn.executemany("INSERT OR IGNORE INTO species(label) VALUES(?)", [(m["label"],) for m in app.SPECIES_MODELS])
        for r in rows:
            result = results.get(r["stem"])
            if not result:
                continue
            changed += 1
            # A photo some model has scored already has SpeciesNet's answer in speciesnet_species, and
            # ai_species may hold a model's call. Otherwise ai_species is still SpeciesNet's answer.
            scored_before = any(r[c] is not None for c in columns)
            speciesnet = r["speciesnet_species"] if scored_before else r["ai_species"]
            ai_species, ai_is_group, ai_conf, count = r["ai_species"], r["ai_is_group"], r["ai_conf"], r["count"]
            possible = 0
            call = app.species_model_call(result)
            if call:
                label, score, call_count, possible = call
                called[label] += 1
                if label != r["ai_species"] or not scored_before:
                    count = call_count
                ai_species, ai_is_group, ai_conf = label, 0, round(score, 3)
            camelot = app.camelot_fields(r["filename"], ai_species, ai_is_group, sightings)
            conn.execute(
                f"""UPDATE images SET speciesnet_species = ?, {", ".join(f"{c} = ?" for c in columns)}, fsj_possible = ?,
                       ai_species = ?, ai_is_group = ?, ai_conf = ?, count = ?, camelot_agreement = ?, display_species = ?
                    WHERE id = ?""",
                (speciesnet, *score_columns(app, result), possible, ai_species, ai_is_group, ai_conf, count,
                 camelot["camelot_agreement"], r["corrected_species"] or ai_species or "blank", r["id"]))
    print(f"Library photos scored: {changed:,}. Calls: {dict(called) or 'none'}.", flush=True)


def prepare_new(app, folder, filename):
    """The slow part, reading the photo: its metadata and thumbnail."""
    image_id = app.image_id_for(filename)
    thumb = app.THUMBNAILS_DIR / f"{image_id}.jpg"
    has_thumb = thumb.exists() or app.make_thumbnail(folder / filename, image_id)
    return filename, image_id, app.cuddeback_exif.read_photo_metadata(folder / filename), has_thumb


def add_new(app, db, results, positions, sightings, folder, limit):
    known = {r["filename"] for r in db.query("SELECT filename FROM images")}
    known |= {r["filename"] for r in db.query("SELECT filename FROM dismissed_photos")}
    todo = [p for p in app.scan_photos(folder) if p not in known and app.photo_stem(p) in results][:limit]
    print(f"Adding {len(todo):,} photos to the Library", flush=True)

    exif_cols = ("camera_id", "image_seq", "hardware_id", "battery", "battery_source",
                 "trigger", "camera_model", "firmware", "exif_comment")
    cols = ("id", "folder", "filename", "stem", "position", *exif_cols, "captured_at", "time_source", "diel_period",
            "ai_species", "ai_conf", "speciesnet_species", *app.SPECIES_SCORE_COLUMNS, "fsj_possible", "camelot_species",
            "camelot_agreement", "band_id", "display_species", "count", "has_thumbnail", "created_at")
    insert = f"INSERT INTO images({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})"
    buffer, added, undated = [], 0, 0
    called = collections.Counter()

    def save():
        with db.transaction() as conn:
            conn.executemany(insert, buffer)
        buffer.clear()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for filename, image_id, meta, has_thumb in pool.map(lambda p: prepare_new(app, folder, p), todo):
            result = results[app.photo_stem(filename)]
            call = app.species_model_call(result)
            ai_species = call[0] if call else app.NOT_CLASSIFIED
            camelot = app.camelot_fields(filename, ai_species, 0, sightings)
            captured_at = meta["captured_at"]
            undated += not captured_at
            if call:
                called[call[0]] += 1
            buffer.append((
                image_id, str(folder), filename, app.photo_stem(filename), app.position_for(filename, positions),
                *(meta[c] for c in exif_cols), captured_at, "exif" if captured_at else None, app.compute_diel_period(captured_at),
                ai_species, round(call[1], 3) if call else None, app.NOT_CLASSIFIED,
                *score_columns(app, result), call[3] if call else 0,
                camelot["camelot_species"], camelot["camelot_agreement"], camelot["jay_ids"],
                ai_species, call[2] if call else 0, int(has_thumb), app.now_iso()))
            added += 1
            if len(buffer) >= COMMIT_EVERY:
                save()
            if added % 2000 == 0:
                print(f"  {added:,} of {len(todo):,}", flush=True)
    if buffer:
        save()
    print(f"Added {added:,} photos. Calls: {dict(called) or 'none'}. Not classified: {added - sum(called.values()):,}. "
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
    results = saved_scores(app)
    label_existing(app, db, results, sightings)
    add_new(app, db, results, positions, sightings, folder, args.limit)
    missing = " OR ".join(f"{m['column']} IS NULL" for m in app.SPECIES_MODELS if m["file"].exists())
    left = db.query_one(f"SELECT COUNT(*) AS n FROM images WHERE {missing}")["n"]
    print(f"Library photos missing a model score (no saved features): {left:,}", flush=True)
    os._exit(0)  # importing the app started its job worker thread


if __name__ == "__main__":
    main()
