"""
FSJ Feeder Monitoring — Flask backend.

Adapted from ArbCameraTrapCV (github.com/cwsapp05/ArbCameraTrapCV) for still
photos from Cuddeback feeder cameras. Three tabs:

  Workspace choose the folder holding all the photos, plus the CSV that says
            which station each photo came from. Photos stay where they are and
            are run through MegaDetector + SpeciesNet in batches, so a long run
            can stop and pick up where it left off. Each photo's date/time,
            camera name and other Cuddeback fields are read from its EXIF
            (cuddeback_exif.py). The field Log workbook is uploaded here too.
  Library   photos grouped by species, with correction, count, notes,
            band ID and review flag.
  Timeline  Log events (log_import.py) and photos laid out on a scrollable
            per-position timeline.

Single-user, local app: there is no sign-in, and it listens on 127.0.0.1
(this computer only) by default.

Storage is SQLite (runs/fsj.db, see db.py) plus thumbnails under runs/. Photos
are read and served from the workspace folder, and are never copied or changed.

Run (development):  python app.py
Run (production):   gunicorn -c gunicorn.conf.py wsgi:app   (Linux/macOS)
                    python serve_windows.py                 (Windows)

IMPORTANT: this app must run as a SINGLE process. The background job worker
starts at import time, and a second process would start a second worker and
run two SpeciesNet jobs at once. Concurrency comes from threads. See
gunicorn.conf.py.
"""

import collections
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageOps

import cuddeback_exif
import db
import log_import

app = Flask(__name__)

BASE_DIR = Path(__file__).parent.resolve()
RUNS_DIR = BASE_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)
THUMBNAILS_DIR = RUNS_DIR / "thumbnails"
THUMBNAILS_DIR.mkdir(exist_ok=True)
JOBS_DIR = RUNS_DIR / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
LOG_DIR = RUNS_DIR / "log"
LOG_DIR.mkdir(exist_ok=True)

db.init(RUNS_DIR / "fsj.db")

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
THUMBNAIL_MAX_EDGE = 480

# Photos per pipeline run. Results are saved after each batch, so this is the
# most work an interruption can lose. Each batch also reloads the models
# (about 20 seconds on CPU).
PROCESS_BATCH_SIZE = int(os.environ.get("PROCESS_BATCH_SIZE", "500"))

# MegaDetector's own default output threshold, which the combined pipeline
# also uses.
DETECTOR_MODEL = "MDV5A"
DETECTOR_OUTPUT_THRESHOLD = "0.005"
# Runs after SpeciesNet, whose label list has no Florida Scrub-Jay. See training/.
SPECIES_MODEL = BASE_DIR / "models" / "florida_scrub_jay.pt"
SPECIES_MODEL_LABEL = "florida scrub-jay"
# Stands in for an answer on photos SpeciesNet hasn't seen (training/relabel_library.py
# adds these). Stored in speciesnet_species, and in ai_species unless the scrub-jay
# model called the photo. Unlike "blank", it doesn't claim the photo is empty.
NOT_CLASSIFIED = "not classified"

# Columns of the Camelot export that say which station each photo came from.
POSITION_FILE_COLUMN = "Media Filename"
POSITION_STATION_COLUMN = "Trap Station Name"
STATION_NUMBER_RE = re.compile(r"(\d+)\s*$")   # "WCP-13" -> position 13

# Camelot's names that differ from SpeciesNet's for the same thing, in lowercase.
CAMELOT_TO_AI = {"nothing nothing": "blank"}
# Labels that don't say what's in the photo, so there's nothing to compare.
CAMELOT_NOT_COMPARABLE = {"unknown sp"}

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
    MAX_CONTENT_LENGTH=64 * 1024 * 1024,  # the Log workbook is the only upload
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
# Workspace: the folder of photos the app works on, in place
#
# A photo's identity is its path relative to the workspace folder, so moving
# the folder and choosing its new location keeps every result and correction.
#
# The routes that act on local paths use request.get_json() without force=True.
# That rejects a plain cross-site form POST, so another website open in the
# browser can't point this app at a different folder.
# ===========================================================================

def scan_photos(folder):
    """Workspace-relative paths (forward slashes) of every photo under folder."""
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel_dir = Path(dirpath).relative_to(folder)
        found += [(rel_dir / name).as_posix() for name in filenames
                  if not name.startswith(".") and Path(name).suffix.lower() in PHOTO_EXTENSIONS]
    return sorted(found)


def photo_stem(filename):
    return Path(filename).stem.lower()


def _sighting_column(fieldnames, name):
    """A CSV merged from an export and a paths file has two copies of some
    columns. The plain Jay ID etc. are unreliable there: on a photo with several
    sightings they repeat one sighting and lose the others. The "(paths file)"
    copies have one correct row per Sighting ID and match the Camelot export."""
    merged = f"{name} (paths file)"
    return merged if merged in fieldnames else name


def read_camelot_csv(csv_path):
    """Returns (positions, sightings) from a Camelot export:
    positions  {stem: position}. A photo listed under two different stations is
               left out rather than guessed at.
    sightings  {stem: [{sighting_id, species, quantity, jay_id, life_stage}]}.
               Empty if the CSV has no label columns."""
    positions, conflicting, by_stem = {}, set(), {}
    with open(csv_path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        missing = {POSITION_FILE_COLUMN, POSITION_STATION_COLUMN} - set(fields)
        if missing:
            raise ValueError(f"That CSV has no {' or '.join(sorted(missing))} column")
        has_labels = "Species Common Name" in fields and "Sighting ID" in fields
        quantity_col = _sighting_column(fields, "Sighting Quantity")
        jay_col = _sighting_column(fields, "Jay ID")
        life_col = _sighting_column(fields, "Life stage")

        def cell(row, col):
            value = (row.get(col) or "").strip()
            return "" if value == "-" else value

        for row in reader:
            stem = photo_stem(row[POSITION_FILE_COLUMN] or "")
            if not stem:
                continue
            match = STATION_NUMBER_RE.search(row[POSITION_STATION_COLUMN] or "")
            if match:
                position = int(match.group(1))
                if positions.setdefault(stem, position) != position:
                    conflicting.add(stem)
            species = cell(row, "Species Common Name") if has_labels else ""
            sighting_id = cell(row, "Sighting ID") if has_labels else ""
            if species and sighting_id:
                quantity = cell(row, quantity_col)
                # Keyed by Sighting ID because a merged CSV repeats each one.
                by_stem.setdefault(stem, {})[sighting_id] = {
                    "sighting_id": sighting_id, "species": species,
                    "quantity": int(quantity) if quantity.isdigit() else None,
                    "jay_id": cell(row, jay_col), "life_stage": cell(row, life_col),
                }
    for stem in conflicting:
        del positions[stem]
    if not positions:
        raise ValueError("That CSV has no rows with both a file name and a station number")
    return positions, {stem: list(rows.values()) for stem, rows in by_stem.items()}


def position_for(filename, positions):
    return positions.get(photo_stem(filename))


def camelot_agreement(sightings, ai_species, ai_is_group):
    """How the AI's label compares with a photo's Camelot sightings: "agree",
    "group" (the AI only gave a broad group like "bird", which isn't checked
    against the species), "differ", or None if there's nothing to compare."""
    labels = {CAMELOT_TO_AI.get(s["species"].lower(), s["species"].lower()) for s in sightings}
    labels -= CAMELOT_NOT_COMPARABLE
    if not labels:
        return None
    if ai_species == NOT_CLASSIFIED:
        # Only the scrub-jay model has seen this photo, and it said no.
        return "differ" if SPECIES_MODEL_LABEL in labels else None
    ai = (ai_species or "blank").lower()
    if ai in labels or ("crow sp." in labels and ai.endswith(" crow")):
        return "agree"
    # A broad animal group can't be right about a photo that people marked as
    # empty or as a person.
    return "group" if ai_is_group and labels - {"blank", "human"} else "differ"


def camelot_fields(filename, ai_species, ai_is_group, sightings_by_stem):
    """What one photo's Camelot sightings add to its record."""
    sightings = sightings_by_stem.get(photo_stem(filename), [])
    return {
        "camelot_species": ", ".join(dict.fromkeys(s["species"] for s in sightings)) or None,
        "camelot_agreement": camelot_agreement(sightings, ai_species, ai_is_group),
        "jay_ids": ", ".join(dict.fromkeys(s["jay_id"] for s in sightings if s["jay_id"])),
    }


def pending_photos(folder):
    """Photos in the folder that SpeciesNet hasn't seen yet (and weren't deleted
    from the Library)."""
    known = {r["filename"] for r in db.query("SELECT filename FROM images WHERE speciesnet_species IS NOT ?", (NOT_CLASSIFIED,))}
    known |= {r["filename"] for r in db.query("SELECT filename FROM dismissed_photos")}
    return [p for p in scan_photos(folder) if p not in known]


def has_active_job():
    return db.query_one("SELECT 1 AS x FROM jobs WHERE status IN ('queued', 'running', 'cancelling') LIMIT 1") is not None


def workspace_state():
    state = {"folder": None, "active": has_active_job()}
    workspace = get_kv("workspace")
    if workspace:
        processed = db.query_one("SELECT COUNT(*) AS n FROM images WHERE speciesnet_species IS NOT ?", (NOT_CLASSIFIED,))["n"]
        dismissed = db.query_one("SELECT COUNT(*) AS n FROM dismissed_photos")["n"]
        state.update(workspace, processed=processed,
                     pending=max(0, workspace["photo_count"] - processed - dismissed))
    return state


@app.route("/api/workspace")
def get_workspace():
    return jsonify(workspace_state())


@app.route("/api/workspace", methods=["POST"])
def set_workspace():
    """Body: {"folder", "positions_csv"}. Also the way to pick up photos added
    to the folder, a moved folder, or a newer export."""
    if has_active_job():
        return jsonify({"error": "Wait for processing to finish, or cancel it, before changing the workspace"}), 409
    data = request.get_json()
    # Windows' "Copy as path" wraps the path in quotes.
    folder_raw = (data.get("folder") or "").strip().strip('"')
    csv_raw = (data.get("positions_csv") or "").strip().strip('"')
    if not folder_raw:
        return jsonify({"error": "Choose the photo folder"}), 400
    if not csv_raw:
        return jsonify({"error": "Choose the position CSV"}), 400
    folder = Path(folder_raw).expanduser().resolve()
    csv_path = Path(csv_raw).expanduser().resolve()
    if not folder.is_dir():
        return jsonify({"error": f"Folder not found: {folder}"}), 400
    if not csv_path.is_file():
        return jsonify({"error": f"CSV not found: {csv_path}"}), 400

    try:
        positions, sightings = read_camelot_csv(csv_path)
    except (OSError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    photos = scan_photos(folder)
    if not photos:
        return jsonify({"error": f"No photos found in {folder}"}), 400

    # Filenames are the photos' identity, so a different set of photos must not
    # be matched against results from this one.
    previous = get_kv("workspace")
    if previous and Path(previous["folder"]) != folder:
        on_disk = set(photos)
        missing = sum(1 for r in db.query("SELECT filename FROM images") if r["filename"] not in on_disk)
        if missing:
            return jsonify({"error": f"{missing:,} photos already in the Library aren't in that folder. "
                                     "Choose a folder that contains them."}), 400

    workspace = {
        "folder": str(folder),
        "positions_csv": str(csv_path),
        "photo_count": len(photos),
        "not_in_csv": sum(1 for p in photos if position_for(p, positions) is None),
        "with_labels": sum(1 for p in photos if photo_stem(p) in sightings),
        "set_at": now_iso(),
    }
    unassigned = db.query("SELECT id, filename FROM images WHERE position IS NULL")
    existing = db.query("SELECT id, filename, ai_species, ai_is_group FROM images")
    with db.transaction() as conn:
        conn.execute("UPDATE images SET folder = ?", (str(folder),))
        conn.executemany("UPDATE images SET position = ? WHERE id = ?",
                         [(position_for(r["filename"], positions), r["id"]) for r in unassigned])
        conn.execute("DELETE FROM camelot_sightings")
        conn.executemany(
            """INSERT INTO camelot_sightings(stem, sighting_id, species, quantity, jay_id, life_stage)
               VALUES(?, ?, ?, ?, ?, ?)""",
            [(stem, s["sighting_id"], s["species"], s["quantity"], s["jay_id"], s["life_stage"])
             for stem, rows in sightings.items() for s in rows])
        # Photos processed before this CSV. A Band ID someone typed is left alone.
        for r in existing:
            fields = camelot_fields(r["filename"], r["ai_species"], r["ai_is_group"], sightings)
            conn.execute(
                """UPDATE images SET stem = ?, camelot_species = ?, camelot_agreement = ?,
                       band_id = COALESCE(NULLIF(band_id, ''), ?) WHERE id = ?""",
                (photo_stem(r["filename"]), fields["camelot_species"], fields["camelot_agreement"],
                 fields["jay_ids"], r["id"]))
        set_kv(conn, "workspace", workspace)
    return jsonify(workspace_state())


# Runs as its own process: Tk needs its own main thread, which a web request
# thread can't provide.
_PICKER_SCRIPT = """
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)
if sys.argv[1] == "folder":
    path = filedialog.askdirectory(title="Choose the folder containing the photos")
else:
    path = filedialog.askopenfilename(title="Choose the position CSV",
                                      filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
sys.stdout.write(path or "")
"""


@app.route("/api/workspace/browse", methods=["POST"])
def browse_for_path():
    """Opens a native file dialog on this computer (the app only runs locally).
    Body: {"kind": "folder" | "csv"}. Returns {"path"}, null if cancelled."""
    kind = "folder" if request.get_json().get("kind") == "folder" else "csv"
    try:
        result = subprocess.run([sys.executable, "-c", _PICKER_SCRIPT, kind], capture_output=True,
                                encoding="utf-8", timeout=600, env={**os.environ, "PYTHONUTF8": "1"})
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode != 0:
        return jsonify({"error": "Couldn't open a file dialog. Paste the path into the box instead."}), 500
    return jsonify({"path": os.path.normpath(result.stdout) if result.stdout else None})


# ===========================================================================
# Job queue (single worker thread, one pipeline run at a time)
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


@app.route("/api/workspace/process", methods=["POST"])
def process_workspace():
    """Queues a job for every photo not in the Library yet, so this both starts
    a run and resumes a cancelled or failed one."""
    request.get_json()
    workspace = get_kv("workspace")
    if not workspace:
        return jsonify({"error": "Choose a workspace folder first"}), 400
    if has_active_job():
        return jsonify({"error": "Already processing"}), 409
    if not Path(workspace["folder"]).is_dir():
        return jsonify({"error": f"Folder not found: {workspace['folder']}"}), 400
    file_count = len(pending_photos(workspace["folder"]))
    if not file_count:
        return jsonify({"error": "Every photo in the workspace has already been processed"}), 400

    job_id = uuid.uuid4().hex[:12]
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    seq = (db.query_one("SELECT COALESCE(MAX(seq), 0) AS s FROM jobs")["s"] or 0) + 1
    db.execute(
        """INSERT INTO jobs(id, seq, display_name, status, created_at, folder, log_file, file_count, done_count)
           VALUES(?, ?, ?, 'queued', ?, ?, ?, ?, 0)""",
        (job_id, seq, Path(workspace["folder"]).name, now_iso(), workspace["folder"],
         str(job_dir / "log.txt"), file_count))

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
    # Keep the first start time when a job resumes after a restart.
    _set_job(job_id, status="running", started_at=job["started_at"] or now_iso())
    folder = job["folder"]

    with open(job["log_file"], "a", encoding="utf-8") as log:
        positions, sightings = read_camelot_csv(get_kv("workspace")["positions_csv"])
        # Worked out here rather than when the job was queued, so a job that was
        # interrupted by a restart continues with what's left.
        pending = pending_photos(folder)
        log.write(f"{len(pending):,} photos to process, in batches of {PROCESS_BATCH_SIZE}.\n")
        log.flush()
        for start in range(0, len(pending), PROCESS_BATCH_SIZE):
            if _is_cancelling(job_id):
                break
            batch = pending[start:start + PROCESS_BATCH_SIZE]
            log.write(f"\n=== Photos {start + 1:,}-{start + len(batch):,} of {len(pending):,} ===\n")
            log.flush()
            predictions = _run_pipeline(job_id, folder, batch, log)
            if _is_cancelling(job_id):
                break
            if predictions is None:
                log.write("\nStopped. Photos finished so far are saved; "
                          "press Process to retry the rest.\n")
                _set_job(job_id, status="error", finished_at=now_iso())
                return
            sync_batch(job_id, folder, predictions, positions, sightings, log)

        if _is_cancelling(job_id):
            log.write("\nCancelled. Photos finished so far are saved.\n")
            _set_job(job_id, status="cancelled", finished_at=now_iso())
            return
        left = len(pending_photos(folder))
        if left:
            log.write(f"\n{left:,} photo(s) couldn't be processed. Press Process to retry them.\n")
        log.write("Done.\n")
    _set_job(job_id, status="done", finished_at=now_iso())


def _is_cancelling(job_id):
    return _job(job_id)["status"] == "cancelling"


def _scores_file(predictions):
    return predictions.with_name(predictions.name.replace("predictions-", "scores-"))


def species_model_python():
    """The scrub-jay step needs PyTorch and uses a GPU if there is one, so it
    prefers the training environment (see README) over this app's environment."""
    override = os.environ.get("SPECIES_MODEL_PYTHON")
    if override:
        return override
    for candidate in (BASE_DIR / ".venv-train" / "Scripts" / "python.exe", BASE_DIR / ".venv-train" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _run_pipeline(job_id, folder, batch, log):
    """Runs MegaDetector, then SpeciesNet on its detections, then the scrub-jay
    model if there is one. The combined pipeline command can only take a whole
    folder, while the detector can take a list of files, which is what lets the
    photos stay in one folder. Returns the predictions file, or None if a step
    failed or was cancelled."""
    job_dir = Path(_job(job_id)["log_file"]).parent
    file_list = job_dir / "batch_files.json"
    detections = job_dir / "batch_detections.json"
    # Numbered by progress, so each batch's raw results are kept and a retried
    # batch overwrites its own failed attempt.
    predictions = job_dir / f"predictions-{_job(job_id)['done_count']:06d}.json"
    file_list.write_text(json.dumps([Path(folder, p).as_posix() for p in batch]), encoding="utf-8")

    detect = [sys.executable, "-m", "megadetector.detection.run_detector_batch", DETECTOR_MODEL,
              str(file_list), str(detections), "--threshold", DETECTOR_OUTPUT_THRESHOLD, "--quiet"]
    classify = [sys.executable, "-m", "megadetector.detection.run_md_and_speciesnet",
                folder, str(predictions), "--detections_file", str(detections), "--skip_video"]
    if SPECIESNET_COUNTRY:
        classify += ["--country", SPECIESNET_COUNTRY]
        if SPECIESNET_ADMIN1:
            classify += ["--admin1_region", SPECIESNET_ADMIN1]

    try:
        if not (_run_step(job_id, detect, log) and not _is_cancelling(job_id) and _run_step(job_id, classify, log)):
            return None
        if SPECIES_MODEL.exists() and not _is_cancelling(job_id):
            scores = _scores_file(predictions)
            scores.unlink(missing_ok=True)
            score = [species_model_python(), str(BASE_DIR / "training" / "score_photos.py"),
                     "--predictions", str(predictions), "--folder", folder,
                     "--model", str(SPECIES_MODEL), "--out", str(scores)]
            if not _run_step(job_id, score, log):
                return None
        return predictions
    finally:
        file_list.unlink(missing_ok=True)
        detections.unlink(missing_ok=True)


def _run_step(job_id, cmd, log):
    log.write("Running: " + " ".join(cmd) + "\n\n")
    log.flush()
    try:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except OSError as e:
        log.write(f"\nCould not start the pipeline: {e}\n")
        return False
    with running_processes_lock:
        running_processes[job_id] = proc
    try:
        # Covers a cancel that arrived just before the process was registered.
        if _is_cancelling(job_id):
            _terminate_then_kill(proc)
        return proc.wait() == 0
    finally:
        with running_processes_lock:
            running_processes.pop(job_id, None)


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
        item = {k: j[k] for k in ("id", "display_name", "status", "created_at", "started_at",
                                  "finished_at", "file_count", "done_count", "created_by")}
        if j["status"] in ("running", "cancelling"):
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
    stopped is re-queued, and continues with the photos not yet in the Library
    (only the batch in progress is lost)."""
    db.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
    db.execute("UPDATE jobs SET status = 'cancelled' WHERE status = 'cancelling'")
    for j in db.query("SELECT id FROM jobs WHERE status = 'queued' ORDER BY seq"):
        job_queue.append(j["id"])


# ===========================================================================
# Turning predictions.json into image records
# ===========================================================================

def image_id_for(filename):
    return hashlib.sha1(filename.encode()).hexdigest()[:16]


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


def _group_labels(data):
    """Labels in a predictions file that are a broad group rather than a species.
    Their taxonomy, "id;class;order;family;genus;species;common name", has no
    species. "blank" and "vehicle" have none either, but aren't animal groups."""
    labels = data.get("classification_categories", {})
    groups = set()
    for category, description in data.get("classification_category_descriptions", {}).items():
        parts = description.split(";")
        label = labels.get(category)
        if len(parts) == 7 and not parts[5] and label not in ("blank", "vehicle"):
            groups.add(label)
    return groups


def sync_batch(job_id, folder, predictions_file, positions, sightings, log):
    """Adds one batch's results to the Library and counts them on the job."""
    data = json.loads(predictions_file.read_text(encoding="utf-8"))
    class_cats = data.get("classification_categories", {})
    group_labels = _group_labels(data)
    scores_path = _scores_file(predictions_file)
    species_scores = json.loads(scores_path.read_text(encoding="utf-8")) if scores_path.exists() else None

    with db.transaction() as conn:
        conn.executemany("INSERT OR IGNORE INTO species(label) VALUES(?)",
                         [(label,) for label in class_cats.values()])
        if species_scores:
            conn.execute("INSERT OR IGNORE INTO species(label) VALUES(?)", (SPECIES_MODEL_LABEL,))

    folder = Path(folder)
    entries = data.get("images", [])
    log.write(f"\nReading metadata and making thumbnails for {len(entries)} photos...\n")
    log.flush()

    undated = failed = 0
    for img in entries:
        # A photo the detector couldn't read (a damaged file, a drive that went
        # away) isn't blank. Leaving it out means the next run retries it.
        if img.get("failure"):
            failed += 1
            log.write(f"  Couldn't read {img.get('file')}: {img['failure']}\n")
            continue
        # The file list gave the detector absolute paths.
        filename = Path(os.path.relpath(folder / img.get("file", ""), folder)).as_posix()
        path = folder / filename
        image_id = image_id_for(filename)
        ai_species, ai_conf, ai_det_conf, ai_count = _best_species(img.get("detections") or [], class_cats)
        speciesnet_species = ai_species
        fsj = (species_scores or {"photos": {}})["photos"].get(img.get("file"))
        fsj_score = fsj["score"] if fsj else None
        fsj_possible = 0
        if fsj and fsj_score >= species_scores["threshold"]:
            ai_species, ai_conf, ai_count = SPECIES_MODEL_LABEL, fsj_score, fsj["count"]
            fsj_possible = int(fsj_score < species_scores["review_below"])
        meta = cuddeback_exif.read_photo_metadata(path)
        has_thumb = make_thumbnail(path, image_id)

        existing = db.query_one("SELECT * FROM images WHERE id = ?", (image_id,))
        if existing and existing["metadata_edited"]:
            captured_at, time_source = existing["captured_at"], existing["time_source"]
        else:
            captured_at = meta["captured_at"]
            time_source = "exif" if captured_at else None
        if not captured_at:
            undated += 1
        corrected = existing["corrected_species"] if existing else None
        display = corrected or ai_species or "blank"

        exif_cols = ("camera_id", "image_seq", "hardware_id", "battery", "battery_source",
                     "trigger", "camera_model", "firmware", "exif_comment")
        ai_is_group = int(ai_species in group_labels)
        camelot = camelot_fields(filename, ai_species, ai_is_group, sightings)
        cols = ("id", "job_id", "folder", "filename", "stem", "position", *exif_cols,
                "captured_at", "time_source", "diel_period", "ai_species", "ai_is_group", "ai_conf", "ai_det_conf",
                "speciesnet_species", "fsj_score", "fsj_possible",
                "camelot_species", "camelot_agreement", "band_id",
                "display_species", "count", "has_thumbnail", "created_at")
        values = (image_id, job_id, str(folder), filename, photo_stem(filename), position_for(filename, positions),
                  *(meta[c] for c in exif_cols),
                  captured_at, time_source, compute_diel_period(captured_at), ai_species, ai_is_group,
                  round(ai_conf, 3) if ai_conf is not None else None,
                  round(ai_det_conf, 3) if ai_det_conf is not None else None,
                  speciesnet_species, round(fsj_score, 4) if fsj_score is not None else None, fsj_possible,
                  camelot["camelot_species"], camelot["camelot_agreement"], camelot["jay_ids"],
                  display, ai_count, int(has_thumb), now_iso())
        # On reprocessing, refresh machine-derived fields only; human edits
        # (corrections, count, notes, band ID, review flag) are left alone.
        # A photo SpeciesNet is seeing for the first time has no real count yet.
        refresh = (*exif_cols, "captured_at", "time_source", "diel_period", "ai_species", "ai_is_group",
                   "ai_conf", "ai_det_conf", "speciesnet_species", "fsj_score", "fsj_possible",
                   "camelot_species", "camelot_agreement", "display_species", "has_thumbnail")
        with db.transaction() as conn:
            conn.execute(
                f"""INSERT INTO images({", ".join(cols)}) VALUES({", ".join("?" * len(cols))})
                    ON CONFLICT(id) DO UPDATE SET {", ".join(f"{c} = excluded.{c}" for c in refresh)},
                        count = CASE WHEN images.ai_species = '{NOT_CLASSIFIED}' THEN excluded.count ELSE images.count END""",
                values)
    if undated:
        log.write(f"{undated} photo(s) had no EXIF capture time (not original camera files?) "
                  "and won't appear on the Timeline until dated by hand in the Library.\n")
    done = _job(job_id)["done_count"] + len(entries) - failed
    _set_job(job_id, done_count=done)
    log.write(f"{done:,} of {_job(job_id)['file_count']:,} photos finished.\n")
    log.flush()


# ===========================================================================
# Library
# ===========================================================================

IMAGE_PUBLIC_FIELDS = (
    "id", "job_id", "filename", "position", "camera_id", "image_seq", "captured_at", "time_source",
    "diel_period", "ai_species", "ai_conf", "ai_det_conf", "corrected_species", "corrected_by",
    "corrected_at", "display_species", "count", "notes", "band_id", "marked_for_review",
    "metadata_edited", "has_thumbnail", "hardware_id", "battery", "battery_source", "trigger",
    "camera_model", "firmware", "exif_comment", "camelot_species", "camelot_agreement",
    "speciesnet_species", "fsj_score", "fsj_possible",
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
    labels.discard(NOT_CLASSIFIED)  # not something to correct a photo to
    return jsonify(sorted(labels, key=str.lower))


@app.route("/api/images")
def list_images():
    """Query params: species, camelot_species, agreement=agree|group|differ|none,
    position, review=1, start/end (YYYY-MM-DD), sort=newest|oldest, offset, limit."""
    where, params = [], []
    if request.args.get("species"):
        where.append("display_species = ?"); params.append(request.args["species"])
    if request.args.get("camelot_species"):
        where.append("EXISTS (SELECT 1 FROM camelot_sightings s WHERE s.stem = images.stem AND s.species = ?)")
        params.append(request.args["camelot_species"])
    if request.args.get("agreement") == "none":
        where.append("camelot_agreement IS NULL")
    elif request.args.get("agreement"):
        where.append("camelot_agreement = ?"); params.append(request.args["agreement"])
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
    r = _get_image_or_404(image_id)
    sightings = db.query("SELECT species, quantity, jay_id, life_stage FROM camelot_sightings "
                         "WHERE stem = ? ORDER BY sighting_id", (r["stem"],))
    return jsonify({**public_image(r), "camelot_sightings": sightings})


@app.route("/api/camelot/compare")
def camelot_compare():
    """Library photos per Camelot species, by how the AI's label compares. A
    photo with sightings of two species counts under both. Photos with no AI
    answer to compare don't count, or a species' total would look badly missed."""
    species = {}
    for r in db.query(
            """SELECT s.species, i.camelot_agreement AS agreement,
                      COALESCE(i.ai_species, 'blank') AS ai, COUNT(DISTINCT i.id) AS n
               FROM images i JOIN camelot_sightings s ON s.stem = i.stem
               WHERE COALESCE(i.ai_species, '') != ? OR i.camelot_agreement IS NOT NULL
               GROUP BY s.species, agreement, ai""", (NOT_CLASSIFIED,)):
        row = species.setdefault(r["species"], {"species": r["species"], "agree": 0, "group": 0, "differ": 0,
                                                "total": 0, "called": collections.Counter()})
        row["total"] += r["n"]
        if r["agreement"]:
            row[r["agreement"]] += r["n"]
        if r["agreement"] == "differ":
            row["called"][r["ai"]] += r["n"]
    rows = sorted(species.values(), key=lambda row: -row["total"])
    for row in rows:
        row["called"] = [label for label, _ in row["called"].most_common(3)]
    totals = {r["agreement"] or "none": r["n"] for r in db.query(
        "SELECT camelot_agreement AS agreement, COUNT(*) AS n FROM images GROUP BY camelot_agreement")}
    return jsonify({"species": rows, "totals": totals})


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
    """Removes the library record and its thumbnail. The photo file itself stays
    in the workspace folder, and later runs won't add it back."""
    r = _get_image_or_404(image_id)
    with db.transaction() as conn:
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        conn.execute("INSERT OR IGNORE INTO dismissed_photos(filename) VALUES(?)", (r["filename"],))
    (THUMBNAILS_DIR / f"{image_id}.jpg").unlink(missing_ok=True)
    return jsonify({"deleted": image_id})


@app.route("/media/<image_id>")
def serve_media(image_id):
    r = _get_image_or_404(image_id)
    folder = Path(r["folder"])
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
        "unassigned_images": db.query_one("SELECT COUNT(*) AS n FROM images WHERE position IS NULL")["n"],
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
            port=int(os.environ.get("PORT", "5050")),
            use_reloader=False, threaded=True)
