# FSJFeederMonitoring
A project in collaboration with Mosaic Co. and Archbold Biological Station investigating Florida Scrub Jays.  This repo is for automatically creating by-species counts from camera trap photos, as well as re-identifying banded Florida Scrub Jay individuals.

The app is adapted from [ArbCameraTrapCV](https://github.com/cwsapp05/ArbCameraTrapCV), cut down to still photos from Cuddeback feeder cameras and to three tabs.

## The tabs

- **Workspace**: choose the folder holding all your photos and the CSV that gives each photo's **position**, then press Process. Photos stay where they are, and run through [MegaDetector + SpeciesNet](https://github.com/google/cameratrapai), geofenced to Florida, in batches. Progress and the live log are below. The field **Log** workbook is uploaded here too.
- **Library**: photos grouped by species. Click a photo to see it full size next to a panel where you can correct the species, fix the date/time or position, and set count, band ID, notes and the marked-for-review flag.
- **Timeline**: one row per position (plus a site-wide row), scrolling through time. The Log's events and the photos sit on the same rows:
  - **Spans:** equipment outages (runs of Nonfunctional days) and the RFID / Feeder Down / Camera Issues tables from the Log's Metadata sheet.
  - **Point markers:** downloads, refills (with fill level/time), cleanings, camera switches, and baffle/pipe changes (shown only on the day the state changes).
  - **Photos:** zoomed out, each day shows a bar of photo counts colored by species. Zoomed in, individual photos appear at their capture time, and bursts are grouped into a numbered badge. Click it to see the photos in that burst.

Use the legend chips to hide or show event types. Drag or Shift+scroll to pan, and Ctrl+scroll to zoom.

## The workspace

The workspace is one folder of photos, including any subfolders. The app reads photos and serves them from there, and never copies or changes them.

**Position CSV.** The filenames don't say which feeder a photo came from, so positions come from a Camelot export. Its `Media Filename` column is matched to each photo's filename (without the extension), and the number at the end of `Trap Station Name` is the position, so `WCP-13` is position 13. A photo that isn't in the CSV still gets processed, but has no position until you set one in the Library viewer or choose a newer CSV. The Workspace tab shows how many photos that applies to.

**Long runs.** Photos are processed `PROCESS_BATCH_SIZE` (default 500) at a time, and each batch's results are saved before the next one starts:

- Results show up in the Library and Timeline while the run continues.
- If you cancel, press Process to continue. If the server stops, the run continues on its own when it restarts. Either way, only the batch in progress is lost.
- Corrections, notes and other edits you make during a run are kept.
- A photo that can't be read (a damaged file, a drive that was unplugged) isn't recorded as blank. The job log names it, and it's retried the next time you press Process.

**Changing the workspace.** Press **Use this workspace** again to pick up photos added to the folder, or a newer CSV. If you move the folder, choose its new location and all results are kept, because a photo's identity is its path inside the folder. The app refuses a folder that's missing photos already in the Library, so results can't get attached to the wrong photos.

Deleting an entry in the Library leaves the photo file alone, and later runs won't add it back.

## Camelot labels

The same CSV supplies the species and Jay IDs people entered in Camelot, one record per sighting. A photo can have several.

- **Band ID** is filled with the photo's Jay IDs exactly as typed, including `UNID` and partial reads like `?-IT`. A Band ID you've entered yourself is never replaced. If you clear one, it fills in again the next time you press **Use this workspace**.
- **Species** stays the AI's label, unless you correct it. The Camelot label appears next to it in the viewer, so the two can be compared.
- **AI vs Camelot**, at the bottom of the Library, counts photos per Camelot species by whether the AI agrees. Click a count to see those photos. The filter of the same name works inside any species too.

How a photo is compared:

| Result | Meaning |
|---|---|
| AI agrees | The AI's label matches any of the photo's sightings. `Nothing Nothing` counts as `blank`, and `Crow Sp.` matches any crow species. |
| AI only gave a group | The AI said something broad like `bird` or `corvidae family`. It isn't checked against the species, because Camelot's labels have no taxonomy. |
| AI disagrees | Anything else, including a broad animal label on a photo people marked as empty or human. |
| Nothing to compare | No Camelot label, or only `Unknown Sp`. |

**Which columns are used.** `full_filtered.csv` has two copies of the label columns. `Genus`, `Species`, `Sighting Quantity`, `Jay ID` and `Life stage` are unreliable on photos with several sightings: they repeat one sighting and lose the others. Checked against `CamelotExport__101426.xlsx`, they're wrong on 721 of the 36,040 photos. `Species Common Name` and the `(paths file)` columns match that export on every photo, so those are the ones imported. A plain Camelot export, without `(paths file)` columns, works too.

## The scrub-jay model

SpeciesNet's label list has no Florida Scrub-Jay, so it mostly answers `bird` or `corvidae family`. A one-vs-all model trained on the Camelot labels runs after it. If `models/florida_scrub_jay.pt` is missing, the pipeline uses SpeciesNet alone.

- It scores each detector box and the whole photo, and a photo's score is the highest of these. At or above the threshold (0.848), the AI label becomes `florida scrub-jay`. Otherwise SpeciesNet's label stands. Your corrections are never replaced.
- SpeciesNet's own answer and the model's score are stored on every scored photo and shown in the viewer. A call scoring under 0.99 says **possible, please check**: on validation data, 42% of those were false, against 1% above it.
- The count on a call is rough: 75% exact against Camelot's quantity, 95% within one.
- It needs about 5 crops per photo, so run it on a GPU. The app uses `.venv-train` when it exists, or `SPECIES_MODEL_PYTHON` to name another Python. Setup is at the top of `requirements-train.txt`.

**How it did**, with the threshold set for 98% recall on validation data:

| Test | Scrub-jay photos | Recall | Precision | Other photos wrongly flagged |
|---|---:|---:|---:|---:|
| Held-out one-hour blocks (test set) | 1,620 | 98.3% | 98.3% | 0.8% |
| Trained before March, tested on March and September | 2,604 | 97.3% | 69.9% | 5.6% |
| Trained January to March, tested on September | 28 | 96.4% | 6.9% | 6.7% |

Expect the time-based rows on future photos, not the first. False alarms rise when birds or seasons appear that training hasn't seen, and most of them are White-winged Doves (never in the first two training sets) and Common Grackles. Recall has barely been tested outside January to March, and no scrub-jay in the data is labeled as a juvenile. Retraining with newly labeled photos should help, especially with false alarms.

The **AI vs Camelot** table overstates accuracy on these 36,040 photos, because the model trained on 70% of them. Use it as a true measure only on new photos.

**Retraining:**

```
.venv-train\Scripts\python training\detect.py --photos <folder>
.venv-train\Scripts\python training\extract.py --photos <folder> --workers 2
.venv-train\Scripts\python training\train.py --csv <camelot.csv> --evaluate-test
.venv-train\Scripts\python training\diagnose.py --csv <camelot.csv> --photos <folder>
```

Photos are grouped into one-hour blocks per station, each starting at its first photo, and whole blocks go to train, validation or test. The camera's info bar is blacked out so a model can't read dates or camera IDs from it. `train.py` picks its settings on validation data and only scores the test set with `--evaluate-test`, so use that flag once per dataset. It replaces `models/florida_scrub_jay.pt`, whose `review_below` value comes from `REVIEW_BELOW`. Check the score bands again after retraining, because that value and the threshold depend on the model. `diagnose.py` checks whether the model is seeing the bird or the scene around it. Intermediate files go in `runs/species_training/`.

**Labeling the workspace from saved features:**

```
.venv\Scripts\python training\relabel_library.py
```

This applies the model in minutes, without rerunning the pipeline. It backs up the database to `runs/fsj-before-relabel-<date>.db` first, and photos that already have a score are left alone, so it can be rerun. Photos the pipeline hadn't reached are added without a SpeciesNet answer, and those the model doesn't call show as `not classified`. They count as unprocessed, so **Process remaining** still runs SpeciesNet on them later and keeps your edits. Until then, only a jay Camelot has that the model missed counts in the **AI vs Camelot** table among them.

## Where photo metadata comes from

Cuddeback cameras write everything into each JPEG's EXIF, and `cuddeback_exif.py` reads it from there (there's no OCR):

| Field | EXIF source | Example |
|---|---|---|
| Capture date + time (to the second) | `DateTimeOriginal` | `2026:03:21 19:05:34` |
| Camera name | `UserComment` → `ID=` | `C57` |
| Image number | `UserComment` → `LO=` | `013` |
| Hardware ID (stays with the physical unit if it's renamed) | `UserComment` → `MA=` | `151778739346` |
| Battery level / source | `UserComment` → `BP=` / `BS=` | `50%` / `EXT` |
| Trigger | `UserComment` → `TR=` | `MOTION` |
| Model / firmware | `Model` / `Software` | `LL-3A FW 3.4.0` |

The full `UserComment` is stored too, so other Cuddeback keys can be used later without reprocessing.

**Use the original files from the SD card.** Photos that have been re-saved or exported (photo editors, messaging apps, some cloud sync) usually lose their EXIF. Those still get species predictions, but they have no date. The job log reports how many photos had no date, and you can set their time by hand in the Library viewer.

The **position** comes from the position CSV, not from the photo. The camera name and hardware ID are stored for reference.

To check one photo: `python cuddeback_exif.py path/to/photo.jpg`

## The field Log

Upload the Log workbook (`.xlsx`) on the Workspace tab whenever it changes. Each upload **replaces** the previous one completely. Excel stays the master copy, and the app shows the Log read-only. `log_import.py` condenses the daily equipment grid (~27k rows) into events:

| Log column | Timeline event |
|---|---|
| Nonfunctional = 1 on consecutive days | one outage span |
| Download Data / Refilled / Cleaned = 1 | point on that day (refills include Fill Level / Fill Time) |
| CamSwitch = 1 | point, with old → new camera from the CamSwitch sheet |
| Cylinder Baffle, Pipe_ Feeder, Pipe_Campost | point only when the 0/1 state changes |
| Cuddeback Website Flag (1–3) | point |
| Metadata sheet: Important Dates, RFID Reader, Feeder Down, Camera Issues | annotated points/spans |

The Date and Equipment columns are formulas, so the workbook must have been **saved by Excel** (or LibreOffice) so their values are cached in the file. If they're missing, the importer falls back to the formulas' own pattern.

To check an import from the command line: `python log_import.py Log_withbaffle.xlsx`

## Setup

```bash
pip install -r requirements.txt
```

If a job log ends with `cannot import name 'runtime_version' from 'google.protobuf'`, `pip` has installed a protobuf that's too old: `ultralytics-yolov5` asks for `<=3.20.1` while `onnx` needs `>=6.31.1`. Running the pipeline with 6.33.6 works:

```bash
pip install "protobuf==6.33.6"
```

Processing takes roughly 3 seconds per photo on CPU, which is more than a day for 36,000 photos. If the job log says `PTDetector using device cpu` on a machine with an NVIDIA GPU, the installed PyTorch is the CPU-only build. Installing a CUDA build from [pytorch.org](https://pytorch.org/get-started/locally/) makes both models use the GPU.

This is a single-user app for your own computer: there is no sign-in, and it only listens on `127.0.0.1`, so other machines can't reach it.

Run:

```bash
python app.py                              # development
gunicorn -c gunicorn.conf.py wsgi:app      # production, Linux/macOS
python serve_windows.py                    # production, Windows
```

Then open http://localhost:5050. (Port 5050 keeps it clear of other apps on 5000; set `PORT` to change it.)

> **Single process only.** The job worker starts when `app.py` is imported. A second process would run a second SpeciesNet job at the same time. See `gunicorn.conf.py`.

### Optional environment variables

| Variable | Default | Purpose |
|---|---|---|
| `NUM_POSITIONS` | `25` | Positions offered before a Log is uploaded (afterwards, the Log's positions are used) |
| `PROCESS_BATCH_SIZE` | `500` | Photos per pipeline run: the most an interruption can lose. Each batch reloads the models (about 20 seconds on CPU) |
| `SPECIESNET_COUNTRY` / `SPECIESNET_ADMIN1` | `USA` / `FL` | SpeciesNet geofencing; set the country to empty to disable |
| `SITE_LAT` / `SITE_LON` | unset | If set, each photo gets a Dawn/Day/Dusk/Night label |
| `TIMEZONE` | `America/New_York` | Used with the above |
| `HOST` / `PORT` | `127.0.0.1` / `5050` | Server address (keep `127.0.0.1` — there is no sign-in) |

## Storage

Photos stay in the workspace folder. Everything the app generates lives under `runs/` (gitignored):

```
runs/fsj.db           SQLite: photos, species corrections, jobs, Log events, the workspace paths
runs/thumbnails/      480px thumbnails for the grids and timeline
runs/jobs/<job>/      log.txt and one predictions-*.json per batch
runs/log/latest.xlsx  the most recently uploaded Log
```

SQLite replaces ArbCameraTrapCV's JSON files, which slowed down past ~10k entries.

## Project structure

```
app.py              Flask backend: workspace, job queue, library, Log + timeline APIs
db.py               SQLite schema and helpers
cuddeback_exif.py   reads date/time, camera name and other fields from Cuddeback EXIF
log_import.py       Log workbook → timeline events
models/             trained one-vs-all species models
training/           training, evaluation and the scoring step the app runs
wsgi.py, gunicorn.conf.py, serve_windows.py   production entry points
templates/index.html, static/script.js, static/style.css   frontend (no build step)
```

## Not yet built

- Banded-jay re-identification. Each photo already has a **Band ID** field to fill in by hand.
- A per-camera clock-offset correction, for cases like the Sept 16–18 cameras that recorded wrong dates.
- The Log's Battery Check, Inferred Detections and RFID time-adjust sheets aren't shown on the timeline.
