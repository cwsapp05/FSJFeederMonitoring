# FSJFeederMonitoring
A project in collaboration with Mosaic Co. and Archbold Biological Station investigating Florida Scrub Jays.  This repo is for automatically creating by-species counts from camera trap photos, as well as re-identifying banded Florida Scrub Jay individuals.

The app is adapted from [ArbCameraTrapCV](https://github.com/cwsapp05/ArbCameraTrapCV), cut down to still photos from Cuddeback feeder cameras and to three tabs.

## The tabs

- **Upload**: pick a folder of photos (one SD-card download from one feeder position), pick the **position** it came from, and submit. Photos are chunk-uploaded to the server, queued, and run through [MegaDetector + SpeciesNet](https://github.com/google/cameratrapai), geofenced to Florida. The processing queue and live log are below. The field **Log** workbook is uploaded here too.
- **Library**: photos grouped by species. Click a photo to see it full size next to a panel where you can correct the species, fix the date/time or position, and set count, band ID, notes and the marked-for-review flag.
- **Timeline**: one row per position (plus a site-wide row), scrolling through time. The Log's events and the photos sit on the same rows:
  - **Spans:** equipment outages (runs of Nonfunctional days) and the RFID / Feeder Down / Camera Issues tables from the Log's Metadata sheet.
  - **Point markers:** downloads, refills (with fill level/time), cleanings, camera switches, and baffle/pipe changes (shown only on the day the state changes).
  - **Photos:** zoomed out, each day shows a bar of photo counts colored by species. Zoomed in, individual photos appear at their capture time, and bursts are grouped into a numbered badge. Click it to see the photos in that burst.

Use the legend chips to hide or show event types. Drag or Shift+scroll to pan, and Ctrl+scroll to zoom.

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

**Upload the original files from the SD card.** Photos that have been re-saved or exported (photo editors, messaging apps, some cloud sync) usually lose their EXIF. Those still get species predictions, but they have no date. The job log reports how many photos had no date, and you can set their time by hand in the Library viewer.

The **position** is chosen at upload, not read from the photo. The camera name and hardware ID are stored for reference. Mapping them to positions automatically would need a starting camera-to-position table, since the Log's CamSwitch sheet only records changes.

To check one photo: `python cuddeback_exif.py path/to/photo.jpg`

## The field Log

Upload the Log workbook (`.xlsx`) on the Upload tab whenever it changes. Each upload **replaces** the previous one completely. Excel stays the master copy, and the app shows the Log read-only. `log_import.py` condenses the daily equipment grid (~27k rows) into events:

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
| `SPECIESNET_COUNTRY` / `SPECIESNET_ADMIN1` | `USA` / `FL` | SpeciesNet geofencing; set the country to empty to disable |
| `SITE_LAT` / `SITE_LON` | unset | If set, each photo gets a Dawn/Day/Dusk/Night label |
| `TIMEZONE` | `America/New_York` | Used with the above |
| `HOST` / `PORT` | `127.0.0.1` / `5050` | Server address (keep `127.0.0.1` — there is no sign-in) |

## Storage

Everything lives under `runs/` (gitignored):

```
runs/fsj.db           SQLite: photos, species corrections, jobs, Log events
runs/uploads/<batch>/ uploaded photos, served from here (never copied again)
runs/thumbnails/      480px thumbnails for the grids and timeline
runs/jobs/<job>/      predictions.json and log.txt per processing job
runs/log/latest.xlsx  the most recently uploaded Log
```

SQLite replaces ArbCameraTrapCV's JSON files, which slowed down past ~10k entries.

## Project structure

```
app.py              Flask backend: upload, job queue, library, Log + timeline APIs
db.py               SQLite schema and helpers
cuddeback_exif.py   reads date/time, camera name and other fields from Cuddeback EXIF
log_import.py       Log workbook → timeline events
wsgi.py, gunicorn.conf.py, serve_windows.py   production entry points
templates/index.html, static/script.js, static/style.css   frontend (no build step)
```

## Not yet built

- Banded-jay re-identification. Each photo already has a **Band ID** field to fill in by hand.
- Automatic camera-name → position mapping (see above).
- A per-camera clock-offset correction, for cases like the Sept 16–18 cameras that recorded wrong dates.
- The Log's Battery Check, Inferred Detections and RFID time-adjust sheets aren't shown on the timeline.
- No Empty Trash or storage management yet; uploaded photos stay on the server.
