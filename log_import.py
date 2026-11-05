"""
Turn the field Log workbook (Log_withbaffle.xlsx) into timeline events.

The workbook's "Log" sheet is a daily grid: one row per piece of equipment
(Camera1-25, Feeder1-25, Antenna1_1 ... Antenna25_2) per day, with status
columns that are mostly 0 or "NA". Plotting every row would be ~27k
meaningless markers, so rows are condensed into events:

    Nonfunctional = 1 on consecutive days     -> one "nonfunctional" span
    Download Data / Refilled / Cleaned = 1     -> a point event that day
    CamSwitch = 1                              -> a point event, with old/new
                                                  camera names from the
                                                  CamSwitch sheet when they match
    Cylinder Baffle / Pipe_ Feeder /
    Pipe_Campost                               -> a point only on the day the
                                                  0/1 state changes
    Cuddeback Website Flag (1, 2, 3)           -> a point event
    Fill Level / Fill Time                     -> attached to that day's refill

The "Metadata" sheet's hand-written tables are added as annotated events:
"Important Dates" (site-wide), "RFID Reader" issues, "Feeder Down" and
"Camera Issues" (all spans tied to a position).

Every event belongs to a position (the number in "Camera12", "Feeder12",
"Antenna12_1"), or to no position for site-wide notes.

The Date and Equipment columns of the Log sheet are Excel formulas. openpyxl
can only read the values Excel last cached when it saved the file, so the
workbook must have been saved by Excel (or LibreOffice) — a file written by a
script that never computed the formulas would have empty dates. If cached
values are missing, the formulas' own pattern (100 equipment rows per day,
starting 2026-01-19, equipment order from the Equipment sheet) is used as a
fallback.
"""

import re
from datetime import date, datetime, time, timedelta

import openpyxl

EQUIP_RE = re.compile(r"^(Camera|Feeder|Antenna)(\d+)(?:_(\d+))?$", re.IGNORECASE)

# Log column header -> internal key. Matched case/space/underscore-insensitively
# since the headers have small inconsistencies ("Pipe_ Feeder", "Refill" vs
# "Refilled" between sheets).
COLUMN_KEYS = {
    "date": "date",
    "equipment": "equipment",
    "nonfunctional": "nonfunctional",
    "downloaddata": "download",
    "refilled": "refill",
    "refill": "refill",
    "cylinderbaffle": "baffle",
    "pipefeeder": "pipe_feeder",
    "pipecampost": "pipe_campost",
    "cuddebackwebsiteflag": "website_flag",
    "filllevel": "fill_level",
    "filltime": "fill_time",
    "cleaned": "cleaned",
    "camswitch": "camswitch",
}

STATE_LABELS = {
    "baffle": ("Cylinder baffle", "added", "removed"),
    "pipe_feeder": ("Feeder pipe", "added", "removed"),
    "pipe_campost": ("Camera-post pipe", "added", "removed"),
}

# Shown in the timeline legend / filter, in this order.
EVENT_KINDS = {
    "nonfunctional": "Nonfunctional",
    "download": "Data download",
    "refill": "Refill",
    "cleaned": "Cleaned",
    "camswitch": "Camera switch",
    "baffle": "Baffle change",
    "pipe_feeder": "Feeder pipe change",
    "pipe_campost": "Camera-post pipe change",
    "website_flag": "Cuddeback website flag",
    "rfid_issue": "RFID reader issue",
    "feeder_down": "Feeder down",
    "camera_issue": "Camera issue",
    "important_date": "Important date",
}


def _norm_header(h):
    return re.sub(r"[^a-z]", "", str(h or "").lower())


def _flag(v):
    """1/0/None from a Log cell (which may be 1, 1.0, "1", 0, "NA", None)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s in ("1", "1.0"):
        return 1
    if s in ("0", "0.0"):
        return 0
    if s.isdigit():
        return int(s)
    return None


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(v.strip(), fmt).date()
            except ValueError:
                pass
    return None


def _parse_date_range(v):
    """A Metadata date cell: a real date, or text like '9/16/26--9/18/26'."""
    d = _as_date(v)
    if d:
        return d, None
    if isinstance(v, str) and "-" in v:
        parts = [p for p in re.split(r"-{1,}", v) if p.strip()]
        if len(parts) == 2:
            a, b = _as_date(parts[0]), _as_date(parts[1])
            if a and b:
                return a, b
    return None, None


def _fmt_time(v):
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, datetime):
        return v.strftime("%H:%M")
    return None


def equipment_position(name):
    m = EQUIP_RE.match(str(name or "").strip())
    if not m:
        return None, None
    return int(m.group(2)), m.group(1).capitalize()


def _event(kind, start, end=None, position=None, equipment=None, title="", detail=""):
    return {
        "kind": kind,
        "start": start.isoformat(),
        "end": end.isoformat() if end and end != start else None,
        "position": position,
        "equipment": equipment,
        "title": title,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Log sheet
# ---------------------------------------------------------------------------

def _read_log_rows(wb, wb_formulas):
    ws = wb["Log"]
    headers = [c.value for c in ws[1]]
    keys = [COLUMN_KEYS.get(_norm_header(h)) for h in headers]
    if "date" not in keys or "equipment" not in keys:
        raise ValueError("The Log sheet needs 'Date' and 'Equipment' columns in row 1.")

    equipment_order = []
    if "Equipment" in wb.sheetnames:
        for row in wb["Equipment"].iter_rows(min_row=2, values_only=True):
            if row and row[0]:
                equipment_order.append(str(row[0]).strip())

    fallback_start = _formula_start_date(wb_formulas)
    rows, fallback_used = [], 0
    for i, values in enumerate(ws.iter_rows(min_row=2, values_only=True)):
        rec = {k: v for k, v in zip(keys, values) if k}
        d = _as_date(rec.get("date"))
        equip = rec.get("equipment")
        if (d is None or not equip) and fallback_start and equipment_order:
            # Mirror the sheet's own formulas:
            #   Date      = DATE(2026,1,19) + INT((ROW()-2)/100)
            #   Equipment = INDEX(Equipment!A2:A101, MOD(ROW()-2,100)+1)
            n = len(equipment_order)
            d = d or fallback_start + timedelta(days=i // n)
            equip = equip or equipment_order[i % n]
            fallback_used += 1
        if d is None or not equip:
            continue
        rec["date"] = d
        rec["equipment"] = str(equip).strip()
        rows.append(rec)
    return rows, fallback_used


def _formula_start_date(wb_formulas):
    """The start date baked into the Log's Date formula, e.g. DATE(2026,1,19)."""
    if wb_formulas is None or "Log" not in wb_formulas.sheetnames:
        return None
    v = wb_formulas["Log"]["A2"].value
    m = re.search(r"DATE\((\d{4}),\s*(\d{1,2}),\s*(\d{1,2})\)", str(v or ""))
    return date(*map(int, m.groups())) if m else None


def _camswitch_details(wb):
    """(equipment, date) -> ["C23 → C76", ...] from the CamSwitch sheet."""
    out = {}
    if "CamSwitch" not in wb.sheetnames:
        return out
    for row in wb["CamSwitch"].iter_rows(min_row=2, values_only=True):
        if not row or not row[0]:
            continue
        d = _as_date(row[1])
        if not d:
            continue
        old = row[2] if len(row) > 2 else None
        new = row[3] if len(row) > 3 else None
        out.setdefault((str(row[0]).strip(), d), []).append(f"{old} → {new}")
    return out


def events_from_log_rows(rows, camswitch):
    events = []
    by_equip = {}
    for r in rows:
        by_equip.setdefault(r["equipment"], []).append(r)

    for equip, recs in by_equip.items():
        recs.sort(key=lambda r: r["date"])
        pos, etype = equipment_position(equip)

        # Nonfunctional spans: consecutive days with Nonfunctional = 1.
        span_start = prev_day = None
        for r in recs:
            if _flag(r.get("nonfunctional")) == 1:
                if span_start is None:
                    span_start = r["date"]
                elif prev_day is not None and (r["date"] - prev_day).days > 1:
                    events.append(_event("nonfunctional", span_start, prev_day, pos, equip,
                                         f"{equip} nonfunctional"))
                    span_start = r["date"]
                prev_day = r["date"]
            elif span_start is not None:
                events.append(_event("nonfunctional", span_start, prev_day, pos, equip,
                                     f"{equip} nonfunctional"))
                span_start = prev_day = None
        if span_start is not None:
            events.append(_event("nonfunctional", span_start, prev_day, pos, equip,
                                 f"{equip} nonfunctional"))

        last_state = {}
        for r in recs:
            d = r["date"]
            if _flag(r.get("download")) == 1:
                events.append(_event("download", d, None, pos, equip, f"Data downloaded — {equip}"))
            if _flag(r.get("refill")) == 1:
                parts = []
                lvl = r.get("fill_level")
                if lvl not in (None, "NA", ""):
                    parts.append(f"Fill level before refill: {lvl}%")
                t = _fmt_time(r.get("fill_time"))
                if t:
                    parts.append(f"Filled at {t}")
                events.append(_event("refill", d, None, pos, equip, f"Refilled — {equip}", "; ".join(parts)))
            if _flag(r.get("cleaned")) == 1:
                events.append(_event("cleaned", d, None, pos, equip, f"Cleaned — {equip}"))
            if _flag(r.get("camswitch")) == 1:
                detail = "; ".join(camswitch.get((equip, d), []))
                events.append(_event("camswitch", d, None, pos, equip, f"Camera switched — {equip}", detail))
            wf = _flag(r.get("website_flag"))
            if wf:
                events.append(_event("website_flag", d, None, pos, equip,
                                     f"Cuddeback website flag {wf} — {equip}"))
            for key, (label, on_word, off_word) in STATE_LABELS.items():
                v = _flag(r.get(key))
                if v not in (0, 1):
                    continue
                prev = last_state.get(key)
                if prev is None and v == 1:
                    events.append(_event(key, d, None, pos, equip, f"{label} present (first recorded) — {equip}"))
                elif prev is not None and v != prev:
                    events.append(_event(key, d, None, pos, equip,
                                         f"{label} {on_word if v else off_word} — {equip}"))
                last_state[key] = v
    return events


# ---------------------------------------------------------------------------
# Metadata sheet tables
# ---------------------------------------------------------------------------

def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def events_from_metadata(wb):
    if "Metadata" not in wb.sheetnames:
        return []
    rows = list(wb["Metadata"].iter_rows(values_only=True))
    events = []
    section = None
    for row in rows:
        row = list(row) + [None] * 8
        first = str(row[0]).strip() if row[0] is not None else ""
        second = str(row[1]).strip() if row[1] is not None else ""
        header = first.lower()
        if header == "important dates":
            section = "important"; continue
        if header == "rfid reader":
            section = "rfid"; continue
        if header == "feeder down":
            section = "feeder_down"; continue
        if second.lower() == "position" and str(row[0] or "").lower().startswith("camera issues"):
            section = "camera"; continue
        if header == "other tabs":
            section = None; continue
        if all(v is None for v in row[:6]):
            continue

        if section == "important":
            start, end = _parse_date_range(row[0])
            if start and row[1]:
                events.append(_event("important_date", start, end, None, None, str(row[1])))
        elif section == "rfid":
            pos = _int_or_none(row[0])
            start, end = _as_date(row[3]), _as_date(row[4])
            if pos and start:
                antenna = row[1]
                which = "both antennas" if str(antenna).lower() == "both" else f"antenna {antenna}"
                events.append(_event("rfid_issue", start, end, pos, f"Feeder{pos}",
                                     f"RFID reader {pos} ({which}): {row[2] or 'issue'}",
                                     str(row[5] or "")))
        elif section == "feeder_down":
            pos = _int_or_none(row[0])
            start, end = _as_date(row[1]), _as_date(row[2])
            if pos and start:
                events.append(_event("feeder_down", start, end, pos, f"Feeder{pos}",
                                     f"Feeder {pos} down", str(row[3] or "")))
        elif section == "camera":
            pos = _int_or_none(row[1])
            start, end = _as_date(row[3]), _as_date(row[4])
            if pos and start:
                events.append(_event("camera_issue", start, end, pos, f"Camera{pos}",
                                     f"Camera {pos}: {row[2] or 'issue'}", str(row[5] or "")))
    return events


# ---------------------------------------------------------------------------

def import_log(path):
    """
    Returns {"events": [...], "positions": [1, 2, ...], "summary": {...}}.
    Raises ValueError with a readable message if the workbook isn't a Log.
    """
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=False)
    except Exception as e:
        raise ValueError(f"Couldn't open the workbook: {e}")
    if "Log" not in wb.sheetnames:
        raise ValueError("This workbook has no 'Log' sheet.")
    try:
        wb_formulas = openpyxl.load_workbook(path, data_only=False, read_only=True)
    except Exception:
        wb_formulas = None

    rows, fallback_used = _read_log_rows(wb, wb_formulas)
    if not rows:
        raise ValueError("No dated rows found in the Log sheet.")

    events = events_from_log_rows(rows, _camswitch_details(wb))
    events += events_from_metadata(wb)
    events.sort(key=lambda e: (e["start"], e["position"] or 0, e["kind"]))

    positions = sorted({p for p, _ in (equipment_position(r["equipment"]) for r in rows) if p})
    dates = [r["date"] for r in rows]
    counts = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    return {
        "events": events,
        "positions": positions,
        "summary": {
            "rows": len(rows),
            "first_date": min(dates).isoformat(),
            "last_date": max(dates).isoformat(),
            "event_counts": counts,
            "fallback_rows": fallback_used,
        },
    }


if __name__ == "__main__":
    import json
    import sys
    result = import_log(sys.argv[1])
    print(json.dumps(result["summary"], indent=2))
    for e in result["events"][:20]:
        print(e)
