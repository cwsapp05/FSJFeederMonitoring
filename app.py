"""
FSJ Feeder Monitoring — Flask backend.

Adapted from ArbCameraTrapCV (github.com/cwsapp05/ArbCameraTrapCV) for still
photos from Cuddeback feeder cameras. Three tabs:

  Upload    pick a folder of photos + the feeder position they came from;
            they're chunk-uploaded to this server, queued, and run through
            MegaDetector + SpeciesNet. Each photo's date/time/camera name is
            read from EXIF or, failing that, from the Cuddeback info bar via
            OCR (cuddeback_ocr.py). The field Log workbook is uploaded here too.
  Library   photos grouped by species, with correction, count, notes,
            band ID and review flag.
  Timeline  Log events (log_import.py) and photos laid out on a scrollable
            per-position timeline.

Single-user, local app: there is no sign-in, and it listens on 127.0.0.1
(this computer only) by default.

Storage is SQLite (runs/fsj.db, see db.py) plus the uploaded photos and
thumbnails under runs/. Photos are served from where they were uploaded and
never copied again.

Run (development):  python app.py
Run (production):   gunicorn -c gunicorn.conf.py wsgi:app   (Linux/macOS)
                    python serve_windows.py                 (Windows)

IMPORTANT: this app must run as a SINGLE process. The background job worker
starts at import time, and a second process would start a second worker and
run two SpeciesNet jobs at once. Concurrency comes from threads. See
gunicorn.conf.py.
"""

import collections
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageOps

import cuddeback_ocr
import db
import log_import

app = Flask(__name__)

BASE_DIR = Path(__file__).parent.resolve()
RUNS_DIR = BASE_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR = RUNS_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
THUMBNAILS_DIR = RUNS_DIR / "thumbnails"
THUMBNAILS_DIR.mkdir(exist_ok=True)
JOBS_DIR = RUNS_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
LOG_DIR = RUNS_DIR / "log"
LOG_DIR.mkdir(exist_ok=True)
UPLOAD_BATCHES_FILE = RUNS_DIR / "upload_batches.json"

db.init(RUNS_DIR / "fsj.db")

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
CHUNK_SIZE = 8 * 1024 * 1024
MAX_CHUNK_BYTES = CHUNK_SIZE * 2
THUMBNAIL_MAX_EDGE = 480

# Default number of feeder positions when no Log has been uploaded yet.
DEFAULT_POSITIONS = int(os.environ.get("NUM_POSITIONS", "25"))

# SpeciesNet geofencing — restricts predictions to species known to occur
# there. Set SPECIESNET_COUNTRY="" to disable.
SPECIESNET_COUNTRY = os.environ.get("SPECIESNET_COUNTRY", "USA")
SPECIESNET_ADMIN1 = os.environ.get("SPECIESNET_ADMIN1", "FL")

# Optional site coordinates for the Dawn/Day/Dusk/Night label. Left blank,
# no diel period is computed.
SITE_LAT = os.environ.get("SITE_LAT")
SITE_LON = os.environ.get("SITE_LON")
SITE_TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")


app.config.update(
    MAX_CONTENT_LENGTH=64 * 1024 * 1024,  # per request; photos arrive in 8MB chunks
)


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# ===========================================================================
# Positions
# ===========================================================================

def get_kv(key, default=None):
    row = db.query_one("SELECT value FROM kv WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else default


def set_kv(conn, key, value):
    conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))


def known_positions():
    """Positions from the uploaded Log, else 1..NUM_POSITIONS, plus any
    position an image already uses."""
    positions = set(get_kv("log_positions") or range(1, DEFAULT_POSITIONS + 1))
    for r in db.query("SELECT DISTINCT position FROM images WHERE position IS NOT NULL"):
        positions.add(r["position"])
    return sorted(positions)


@app.route("/api/positions")
def list_positions():
    return jsonify(known_positions())


# ===========================================================================
# Chunked folder upload (from ArbCameraTrapCV, trimmed: no disk-space
# preflight, no Empty Trash)
# ===========================================================================

upload_batches_lock = threading.Lock()
upload_batches = json.loads(UPLOAD_BATCHES_FILE.read_text()) if UPLOAD_BATCHES_FILE.exists() else {}


def save_upload_batches():
    with upload_batches_lock:
        data = json.dumps(upload_batches)
    UPLOAD_BATCHES_FILE.write_text(data)


def _sanitize_relpath(raw):
    """A browser-supplied file name, reduced to a safe single path component.
    Photos from one folder upload are stored flat in the batch directory."""
    name = Path(str(raw or "").replace("\\", "/")).name
    if not name or name in (".", "..") or name.startswith("."):
        raise ValueError("Invalid file name")
    if Path(name).suffix.lower() not in PHOTO_EXTENSIONS:
        raise ValueError("Not a photo")
    return name


@app.route("/api/uploads", methods=["POST"])
def init_upload_batch():
    """Body: {"files": [{"path", "size"}], "resume_batch_id"?}. Pre-sizes each
    destination file so chunks can be written at their offsets in any order.
    Returns {"batch_id", "received": {name: [chunk indexes already on disk]}}."""
    data = request.get_json(force=True)
    files = data.get("files") or []
    if not files:
        return jsonify({"error": "No photos to upload"}), 400

    cleaned = []
    for f in files:
        try:
            rel = _sanitize_relpath(f.get("path"))
            size = int(f.get("size"))
            if size < 0:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"error": f"Invalid file entry: {f.get('path')!r}"}), 400
        cleaned.append((rel, size))

    resume_id = data.get("resume_batch_id")
    with upload_batches_lock:
        resumable = bool(resume_id and upload_batches.get(resume_id, {}).get("status") == "uploading")
        batch_id = resume_id if resumable else uuid.uuid4().hex[:16]
        if not resumable:
            upload_batches[batch_id] = {"status": "uploading", "created_at": now_iso(), "files": {}}
        manifest = upload_batches[batch_id]
        batch_dir = UPLOADS_DIR / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)
        received = {}
        for rel, size in cleaned:
            existing = manifest["files"].get(rel)
            if existing and existing["size"] == size:
                received[rel] = existing["received_chunks"]
                continue
            with open(batch_dir / rel, "wb") as fh:
                fh.truncate(size)
            manifest["files"][rel] = {"size": size, "expected_chunks": max(1, -(-size // CHUNK_SIZE)),
                                      "received_chunks": []}
            received[rel] = []
    save_upload_batches()
    return jsonify({"batch_id": batch_id, "received": received, "chunk_size": CHUNK_SIZE})


@app.route("/api/uploads/<batch_id>/chunk", methods=["POST"])
def upload_chunk(batch_id):
    with upload_batches_lock:
        manifest = upload_batches.get(batch_id)
        if not manifest or manifest["status"] != "uploading":
            return jsonify({"error": "Unknown or already-submitted upload"}), 404
        try:
            rel = _sanitize_relpath(request.form.get("path"))
            chunk_index = int(request.form.get("chunk_index"))
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid chunk metadata"}), 400
        entry = manifest["files"].get(rel)
        if entry is None or not (0 <= chunk_index < entry["expected_chunks"]):
            return jsonify({"error": "Unexpected chunk"}), 400

    chunk = request.files.get("chunk")
    if chunk is None:
        return jsonify({"error": "Missing chunk data"}), 400
    payload = chunk.read(MAX_CHUNK_BYTES + 1)
    if len(payload) > MAX_CHUNK_BYTES:
        return jsonify({"error": "Chunk too large"}), 400
    try:
        with open(UPLOADS_DIR / batch_id / rel, "r+b") as fh:
            fh.seek(chunk_index * CHUNK_SIZE)
            fh.write(payload)
    except OSError as e:
        if e.errno == errno.ENOSPC:
            return jsonify({"error": "The server ran out of disk space.", "code": "insufficient_storage"}), 507
        raise

    with upload_batches_lock:
        if chunk_index not in entry["received_chunks"]:
            entry["received_chunks"].append(chunk_index)
        manifest["updated_at"] = now_iso()
    save_upload_batches()
    return jsonify({"ok": True})


@app.route("/api/uploads/<batch_id>", methods=["DELETE"])
def delete_upload_batch(batch_id):
    with upload_batches_lock:
        manifest = upload_batches.get(batch_id)
        if manifest is None:
            return jsonify({"deleted": batch_id})
        if manifest["status"] != "uploading":
            return jsonify({"error": "Already submitted — cancel the job instead"}), 400
        del upload_batches[batch_id]
    save_upload_batches()
    shutil.rmtree(UPLOADS_DIR / batch_id, ignore_errors=True)
    return jsonify({"deleted": batch_id})


# ===========================================================================
# Job queue (single worker thread, one SpeciesNet run at a time)
# ===========================================================================

job_queue = collections.deque()
queue_cv = threading.Condition()
running_processes = {}
running_processes_lock = threading.Lock()


def _job(job_id):
    return db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))


def _set_job(job_id, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


@app.route("/api/run", methods=["POST"])
def run_job():
    data = request.get_json(force=True)
    batch_id = (data.get("batch_id") or "").strip()
    display_name = (data.get("folder_name") or "").strip()
    try:
        position = int(data.get("position"))
    except (TypeError, ValueError):
        return jsonify({"error": "Choose the position these photos came from"}), 400

    with upload_batches_lock:
        manifest = upload_batches.get(batch_id)
        if manifest is None:
            return jsonify({"error": "Unknown upload"}), 400
        if manifest["status"] != "uploading":
            return jsonify({"error": f"This upload is already {manifest['status']}"}), 400
        incomplete = sum(1 for f in manifest["files"].values()
                         if len(f["received_chunks"]) < f["expected_chunks"])
        file_count = len(manifest["files"])
    if incomplete:
        return jsonify({"error": f"{incomplete} file(s) not fully uploaded yet"}), 400

    folder = UPLOADS_DIR / batch_id
    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    output_json = job_dir / "predictions.json"
    log_file = job_dir / "log.txt"

    cmd = [sys.executable, "-m", "megadetector.detection.run_md_and_speciesnet",
           str(folder), str(output_json), "--skip_video"]
    if SPECIESNET_COUNTRY:
        cmd += ["--country", SPECIESNET_COUNTRY]
        if SPECIESNET_ADMIN1:
            cmd += ["--admin1_region", SPECIESNET_ADMIN1]

    seq = (db.query_one("SELECT COALESCE(MAX(seq), 0) AS s FROM jobs")["s"] or 0) + 1
    db.execute(
        """INSERT INTO jobs(id, seq, batch_id, display_name, position, status, created_at, folder,
                            output_json, log_file, cmd, file_count, created_by)
           VALUES(?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)""",
        (job_id, seq, batch_id, display_name or batch_id, position, now_iso(), str(folder),
         str(output_json), str(log_file), json.dumps(cmd), file_count, None))

    with upload_batches_lock:
        upload_batches[batch_id]["status"] = "processing"
    save_upload_batches()

    with queue_cv:
        job_queue.append(job_id)
        queue_cv.notify()
    return jsonify({"job_id": job_id})


def worker_loop():
    while True:
        with queue_cv:
            while not job_queue:
                queue_cv.wait()
            job_id = job_queue.popleft()
        try:
            _execute_job(job_id)
        except Exception as e:  # never let one bad job kill the worker
            _set_job(job_id, status="error", finished_at=now_iso())
            try:
                with open(_job(job_id)["log_file"], "a") as log:
                    log.write(f"\nUnexpected error: {e}\n")
            except Exception:
                pass


def _execute_job(job_id):
    job = _job(job_id)
    if not job or job["status"] != "queued":
        return
    _set_job(job_id, status="running", started_at=now_iso())
    cmd = json.loads(job["cmd"])
    log_path = Path(job["log_file"])

    try:
        with open(log_path, "w") as log:
            log.write("Running: " + " ".join(cmd) + "\n\n")
            log.flush()
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
            with running_processes_lock:
                running_processes[job_id] = proc
            returncode = proc.wait()
    except Exception as e:
        returncode = -1
        with open(log_path, "a") as log:
            log.write(f"\nCould not run SpeciesNet: {e}\n")
    finally:
        with running_processes_lock:
            running_processes.pop(job_id, None)

    if _job(job_id)["status"] == "cancelling":
        _set_job(job_id, status="cancelled", finished_at=now_iso())
        _cleanup_batch(job)
        return
    if returncode != 0:
        _set_job(job_id, status="error", finished_at=now_iso())
        return

    _set_job(job_id, status="syncing")
    with open(log_path, "a") as log:
        sync_images_from_job(job_id, log)
    _set_job(job_id, status="done", finished_at=now_iso())


def _cleanup_batch(job):
    batch_id = job.get("batch_id")
    if not batch_id:
        return
    with upload_batches_lock:
        upload_batches.pop(batch_id, None)
    save_upload_batches()
    shutil.rmtree(UPLOADS_DIR / batch_id, ignore_errors=True)


def _terminate_then_kill(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


@app.route("/api/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    job = _job(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    if job["status"] == "queued":
        with queue_cv:
            try:
                job_queue.remove(job_id)
            except ValueError:
                pass
        _set_job(job_id, status="cancelled", finished_at=now_iso())
        _cleanup_batch(job)
    elif job["status"] == "running":
        _set_job(job_id, status="cancelling")
        with running_processes_lock:
            proc = running_processes.get(job_id)
        if proc:
            threading.Thread(target=_terminate_then_kill, args=(proc,), daemon=True).start()
    else:
        return jsonify({"error": f"Job is {job['status']} and can't be cancelled"}), 400
    return jsonify({"ok": True})


def _log_tail(path, max_bytes=6000):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


@app.route("/api/queue")
def get_queue():
    jobs = db.query("SELECT * FROM jobs ORDER BY seq DESC LIMIT 50")
    out = []
    for j in jobs:
        item = {k: j[k] for k in ("id", "display_name", "position", "status", "created_at",
                                  "started_at", "finished_at", "file_count", "created_by")}
        if j["status"] in ("running", "syncing", "cancelling"):
            item["log_tail"] = _log_tail(j["log_file"])
        out.append(item)
    return jsonify(out)


@app.route("/api/jobs/<job_id>/log")
def job_log(job_id):
    job = _job(job_id)
    if not job:
        abort(404)
    return jsonify({"log": _log_tail(job["log_file"], 200_000)})


def _restore_queue_on_startup():
    """Queued jobs survive restarts. A job that was mid-run when the server
    stopped is re-queued from the start (its SpeciesNet run was lost)."""
    db.execute("UPDATE jobs SET status = 'queued' WHERE status IN ('running', 'syncing')")
    db.execute("UPDATE jobs SET status = 'cancelled' WHERE status = 'cancelling'")
    for j in db.query("SELECT id FROM jobs WHERE status = 'queued' ORDER BY seq"):
        job_queue.append(j["id"])


# ===========================================================================
# Turning predictions.json into image records
# ===========================================================================

def image_id_for(job_id, filename):
    return hashlib.sha1(f"{job_id}:{filename}".encode()).hexdigest()[:16]


def compute_diel_period(captured_at):
    if not (captured_at and SITE_LAT and SITE_LON):
        return None
    try:
        from zoneinfo import ZoneInfo
        from astral import LocationInfo
        from astral.sun import sun
        tz = ZoneInfo(SITE_TIMEZONE)
        dt = datetime.fromisoformat(captured_at).replace(tzinfo=tz)
        loc = LocationInfo(latitude=float(SITE_LAT), longitude=float(SITE_LON), timezone=SITE_TIMEZONE)
        s = sun(loc.observer, date=dt.date(), tzinfo=tz)
        if s["dawn"] <= dt < s["sunrise"]:
            return "Dawn"
        if s["sunrise"] <= dt < s["sunset"]:
            return "Day"
        if s["sunset"] <= dt < s["dusk"]:
            return "Dusk"
        return "Night"
    except Exception:
        return None


def make_thumbnail(src, image_id):
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMBNAIL_MAX_EDGE, THUMBNAIL_MAX_EDGE))
            im.convert("RGB").save(THUMBNAILS_DIR / f"{image_id}.jpg", "JPEG", quality=80)
        return True
    except Exception:
        return False


def _best_species(dets, class_cats):
    """Highest-confidence non-blank classification across this photo's
    detections, plus how many detections share that label (the count)."""
    best = None  # (label, cls_conf, det_conf)
    labels = []
    for d in dets:
        if not d.get("classifications"):
            continue
        cls_idx, cls_conf = d["classifications"][0]
        label = class_cats.get(cls_idx, cls_idx)
        if label == "blank":
            continue
        labels.append(label)
        if best is None or cls_conf > best[1]:
            best = (label, cls_conf, d.get("conf", 0))
    if best is None:
        return None, None, None, 0
    return best[0], best[1], best[2], labels.count(best[0])


def sync_images_from_job(job_id, log):
    job = _job(job_id)
    output_json = Path(job["output_json"])
    if not output_json.exists():
        log.write("\nNo predictions.json was produced.\n")
        return
    data = json.loads(output_json.read_text())
    class_cats = data.get("classification_categories", {})

    with db.transaction() as conn:
        conn.executemany("INSERT OR IGNORE INTO species(label) VALUES(?)",
                         [(label,) for label in class_cats.values()])

    folder = Path(job["folder"])
    entries = data.get("images", [])
    log.write(f"\nReading metadata and making thumbnails for {len(entries)} photos...\n")
    log.flush()

    for i, img in enumerate(entries, 1):
        filename = img.get("file", "")
        path = folder / filename
        image_id = image_id_for(job_id, filename)
        ai_species, ai_conf, ai_det_conf, ai_count = _best_species(img.get("detections") or [], class_cats)
        meta = cuddeback_ocr.read_photo_metadata(path)
        has_thumb = make_thumbnail(path, image_id)

        existing = db.query_one("SELECT * FROM images WHERE id = ?", (image_id,))
        if existing and existing["metadata_edited"]:
            captured_at, time_source = existing["captured_at"], existing["time_source"]
        else:
            captured_at, time_source = meta["captured_at"], meta["time_source"]
        corrected = existing["corrected_species"] if existing else None
        display = corrected or ai_species or "blank"

        with db.transaction() as conn:
            conn.execute(
                """INSERT INTO images(id, job_id, folder, filename, position, camera_id, image_seq,
                       captured_at, time_source, diel_period, ai_species, ai_conf, ai_det_conf,
                       display_species, count, ocr_raw, has_thumbnail, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       camera_id = excluded.camera_id, image_seq = excluded.image_seq,
                       captured_at = excluded.captured_at, time_source = excluded.time_source,
                       diel_period = excluded.diel_period, ai_species = excluded.ai_species,
                       ai_conf = excluded.ai_conf, ai_det_conf = excluded.ai_det_conf,
                       display_species = excluded.display_species, ocr_raw = excluded.ocr_raw,
                       has_thumbnail = excluded.has_thumbnail""",
                (image_id, job_id, str(folder), filename, job["position"], meta["camera_id"],
                 meta["image_seq"], captured_at, time_source, compute_diel_period(captured_at),
                 ai_species, round(ai_conf, 3) if ai_conf is not None else None,
                 round(ai_det_conf, 3) if ai_det_conf is not None else None,
                 display, ai_count, meta["ocr_raw"], int(has_thumb), now_iso()))
        if i % 100 == 0 or i == len(entries):
            log.write(f"  {i}/{len(entries)}\n")
            log.flush()
    log.write("Done.\n")


# ===========================================================================
# Library
# ===========================================================================

IMAGE_PUBLIC_FIELDS = (
    "id", "job_id", "filename", "position", "camera_id", "image_seq", "captured_at", "time_source",
    "diel_period", "ai_species", "ai_conf", "ai_det_conf", "corrected_species", "corrected_by",
    "corrected_at", "display_species", "count", "notes", "band_id", "marked_for_review",
    "metadata_edited", "ocr_raw", "has_thumbnail",
)


def public_image(r):
    out = {k: r.get(k) for k in IMAGE_PUBLIC_FIELDS}
    out["marked_for_review"] = bool(out["marked_for_review"])
    out["metadata_edited"] = bool(out["metadata_edited"])
    out["has_thumbnail"] = bool(out["has_thumbnail"])
    return out


@app.route("/api/species")
def species_groups():
    """Species present in the library with counts, review counts and a cover
    image (the most recent photo of that species)."""
    rows = db.query(
        """SELECT display_species AS label, COUNT(*) AS count,
                  SUM(marked_for_review) AS review_count,
                  (SELECT id FROM images i2 WHERE i2.display_species = images.display_species
                     ORDER BY (i2.captured_at IS NULL), i2.captured_at DESC LIMIT 1) AS cover_id
           FROM images GROUP BY display_species ORDER BY count DESC""")
    return jsonify(rows)


@app.route("/api/species/all")
def species_taxonomy():
    labels = {r["label"] for r in db.query("SELECT label FROM species")}
    labels |= {r["display_species"] for r in db.query("SELECT DISTINCT display_species FROM images")}
    labels.add("blank")
    return jsonify(sorted(labels, key=str.lower))


@app.route("/api/images")
def list_images():
    """Query params: species, position, review=1, start/end (YYYY-MM-DD),
    sort=newest|oldest, offset, limit."""
    where, params = [], []
    if request.args.get("species"):
        where.append("display_species = ?"); params.append(request.args["species"])
    if request.args.get("position"):
        where.append("position = ?"); params.append(int(request.args["position"]))
    if request.args.get("review") in ("1", "true"):
        where.append("marked_for_review = 1")
    if request.args.get("start"):
        where.append("captured_at >= ?"); params.append(request.args["start"])
    if request.args.get("end"):
        where.append("captured_at < ?"); params.append(_next_day(request.args["end"]))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order = "ASC" if request.args.get("sort") == "oldest" else "DESC"
    limit = min(int(request.args.get("limit", 120)), 1000)
    offset = int(request.args.get("offset", 0))

    total = db.query_one(f"SELECT COUNT(*) AS n FROM images {clause}", params)["n"]
    rows = db.query(
        f"""SELECT * FROM images {clause}
            ORDER BY (captured_at IS NULL), captured_at {order}, filename {order}
            LIMIT ? OFFSET ?""", (*params, limit, offset))
    return jsonify({"total": total, "items": [public_image(r) for r in rows]})


def _next_day(d):
    return (datetime.strptime(d[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _get_image_or_404(image_id):
    r = db.query_one("SELECT * FROM images WHERE id = ?", (image_id,))
    if not r:
        abort(404)
    return r


@app.route("/api/images/<image_id>")
def get_image(image_id):
    return jsonify(public_image(_get_image_or_404(image_id)))


@app.route("/api/images/<image_id>/correct", methods=["POST"])
def correct_species(image_id):
    """{"species": "<label>"} sets a correction; "" clears it (back to AI)."""
    r = _get_image_or_404(image_id)
    species = (request.get_json(force=True).get("species") or "").strip()
    if species:
        db.execute("INSERT OR IGNORE INTO species(label) VALUES(?)", (species,))
        db.execute(
            """UPDATE images SET corrected_species = ?, corrected_by = ?, corrected_at = ?,
                   display_species = ?, marked_for_review = 0 WHERE id = ?""",
            (species, None, now_iso(), species, image_id))
    else:
        db.execute(
            """UPDATE images SET corrected_species = NULL, corrected_by = NULL, corrected_at = NULL,
                   display_species = ? WHERE id = ?""",
            (r["ai_species"] or "blank", image_id))
    return jsonify(public_image(_get_image_or_404(image_id)))


@app.route("/api/images/<image_id>/update", methods=["POST"])
def update_image(image_id):
    """Any of: count, notes, band_id, marked_for_review, position,
    captured_at ("YYYY-MM-DDTHH:MM")."""
    _get_image_or_404(image_id)
    data = request.get_json(force=True)
    sets, params = [], []
    if "count" in data:
        try:
            sets.append("count = ?"); params.append(max(0, int(data["count"])))
        except (TypeError, ValueError):
            return jsonify({"error": "Count must be a whole number"}), 400
    for key in ("notes", "band_id"):
        if key in data:
            sets.append(f"{key} = ?"); params.append(str(data[key] or "")[:5000])
    if "marked_for_review" in data:
        sets.append("marked_for_review = ?"); params.append(int(bool(data["marked_for_review"])))
    if "position" in data:
        try:
            sets.append("position = ?"); params.append(int(data["position"]))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid position"}), 400
    if "captured_at" in data:
        raw = (data["captured_at"] or "").strip()
        if raw:
            try:
                captured = datetime.fromisoformat(raw).strftime("%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return jsonify({"error": "Date/time must look like 2026-01-27T12:28"}), 400
        else:
            captured = None
        sets += ["captured_at = ?", "time_source = ?", "diel_period = ?", "metadata_edited = 1"]
        params += [captured, "manual" if captured else None, compute_diel_period(captured)]
    if not sets:
        return jsonify({"error": "Nothing to update"}), 400
    db.execute(f"UPDATE images SET {', '.join(sets)} WHERE id = ?", (*params, image_id))
    return jsonify(public_image(_get_image_or_404(image_id)))


@app.route("/api/images/<image_id>/delete", methods=["POST"])
def delete_image(image_id):
    """Removes the library record and its thumbnail. The uploaded photo file
    itself stays on disk (same as ArbCameraTrapCV's single-entry delete)."""
    _get_image_or_404(image_id)
    db.execute("DELETE FROM images WHERE id = ?", (image_id,))
    (THUMBNAILS_DIR / f"{image_id}.jpg").unlink(missing_ok=True)
    return jsonify({"deleted": image_id})


@app.route("/media/<image_id>")
def serve_media(image_id):
    r = _get_image_or_404(image_id)
    folder = Path(r["folder"]).resolve()
    if UPLOADS_DIR.resolve() not in folder.parents and folder != UPLOADS_DIR.resolve():
        abort(404)
    if not (folder / r["filename"]).is_file():
        abort(404)
    return send_from_directory(str(folder), r["filename"], max_age=86400)


@app.route("/thumb/<image_id>")
def serve_thumbnail(image_id):
    name = f"{image_id}.jpg"
    if not (THUMBNAILS_DIR / name).is_file():
        return serve_media(image_id)
    return send_from_directory(str(THUMBNAILS_DIR), name, max_age=86400)


# ===========================================================================
# Field Log + Timeline
# ===========================================================================

@app.route("/api/log", methods=["GET"])
def log_info():
    return jsonify({
        "info": get_kv("log_info"),
        "kinds": log_import.EVENT_KINDS,
    })


@app.route("/api/log", methods=["POST"])
def upload_log():
    """Replaces the stored Log with a newly uploaded workbook. Excel stays the
    source of truth: every upload is a full replacement, nothing is merged."""
    f = request.files.get("file")
    if f is None or not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "Choose the Log .xlsx file"}), 400
    tmp = LOG_DIR / f"incoming-{uuid.uuid4().hex[:8]}.xlsx"
    f.save(tmp)
    try:
        result = log_import.import_log(tmp)
    except ValueError as e:
        tmp.unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 400

    info = {**result["summary"], "filename": f.filename, "uploaded_at": now_iso()}
    with db.transaction() as conn:
        conn.execute("DELETE FROM log_events")
        conn.executemany(
            'INSERT INTO log_events(kind, start, "end", position, equipment, title, detail) VALUES(?, ?, ?, ?, ?, ?, ?)',
            [(e["kind"], e["start"], e["end"], e["position"], e["equipment"], e["title"], e["detail"])
             for e in result["events"]])
        set_kv(conn, "log_info", info)
        set_kv(conn, "log_positions", result["positions"])
    tmp.replace(LOG_DIR / "latest.xlsx")
    return jsonify(info)


@app.route("/api/timeline/range")
def timeline_range():
    """Overall date span to draw: the Log's dates plus every photo's."""
    log = db.query_one('SELECT MIN(start) AS a, MAX(COALESCE("end", start)) AS b FROM log_events')
    img = db.query_one("SELECT MIN(substr(captured_at, 1, 10)) AS a, MAX(substr(captured_at, 1, 10)) AS b "
                       "FROM images WHERE captured_at IS NOT NULL")
    starts = [d for d in (log["a"], img["a"]) if d]
    ends = [d for d in (log["b"], img["b"]) if d]
    today = datetime.now().strftime("%Y-%m-%d")
    return jsonify({
        "start": min(starts) if starts else today,
        "end": max(ends) if ends else today,
        "positions": known_positions(),
        "undated_images": db.query_one("SELECT COUNT(*) AS n FROM images WHERE captured_at IS NULL")["n"],
    })


@app.route("/api/timeline/events")
def timeline_events():
    return jsonify(db.query('SELECT id, kind, start, "end", position, equipment, title, detail '
                            'FROM log_events ORDER BY start'))


@app.route("/api/timeline/daily")
def timeline_daily():
    """Photo counts per day, position and species — what the zoomed-out
    timeline draws instead of individual thumbnails."""
    where, params = ["captured_at IS NOT NULL"], []
    if request.args.get("hide_blank") in ("1", "true"):
        where.append("display_species != 'blank'")
    return jsonify(db.query(
        f"""SELECT substr(captured_at, 1, 10) AS date, position, display_species AS species,
                   COUNT(*) AS n
            FROM images WHERE {' AND '.join(where)}
            GROUP BY date, position, species""", params))


@app.route("/api/timeline/images")
def timeline_images():
    """Individual photos in [start, end] for the zoomed-in timeline."""
    start, end = request.args.get("start"), request.args.get("end")
    if not start or not end:
        return jsonify({"error": "start and end are required"}), 400
    where = ["captured_at >= ?", "captured_at < ?"]
    params = [start, _next_day(end)]
    if request.args.get("hide_blank") in ("1", "true"):
        where.append("display_species != 'blank'")
    limit = min(int(request.args.get("limit", 3000)), 10000)
    rows = db.query(
        f"""SELECT id, position, captured_at, display_species, count, marked_for_review, camera_id
            FROM images WHERE {' AND '.join(where)} ORDER BY captured_at LIMIT ?""", (*params, limit))
    return jsonify(rows)


# ===========================================================================

@app.template_global()
def asset_version(filename):
    try:
        return int((BASE_DIR / "static" / filename).stat().st_mtime)
    except OSError:
        return 0


@app.route("/")
def index():
    return render_template("index.html")


_restore_queue_on_startup()
worker_thread = threading.Thread(target=worker_loop, daemon=True)
worker_thread.start()


if __name__ == "__main__":
    # use_reloader=False: the reloader would import this module twice and
    # start two job workers. See the module docstring.
    app.run(host=os.environ.get("HOST", "127.0.0.1"),
            port=int(os.environ.get("PORT", "5000")),
            use_reloader=False, threaded=True)
