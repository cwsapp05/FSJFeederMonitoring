"""Get MegaDetector boxes for every photo, reusing boxes the app has already computed.

Same model and threshold as the app (MDV5A, 0.005). Safe to re-run: it resumes from a checkpoint.

    .venv-train\\Scripts\\python training\\detect.py --photos <folder>
"""
import argparse
import glob
import json
import os
import subprocess
import sys

from common import ROOT, WORK_DIR, stem_of


def existing_boxes():
    """Boxes from finished app batches, keyed by photo stem."""
    boxes = {}
    for path in sorted(glob.glob(str(ROOT / "runs" / "jobs" / "*" / "predictions-*.json"))):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for im in data["images"]:
            if "failure" in im or im.get("detections") is None:
                continue
            boxes[stem_of(im["file"])] = [
                {"category": d["category"], "conf": d["conf"], "bbox": d["bbox"]} for d in im["detections"]
            ]
    return boxes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", required=True)
    args = ap.parse_args()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    out = WORK_DIR / "md_new.json"
    checkpoint = WORK_DIR / "md_new.checkpoint.json"

    boxes = existing_boxes()
    all_files = sorted(f for f in os.listdir(args.photos) if f.lower().endswith((".jpg", ".jpeg")))
    todo = [os.path.join(args.photos, f).replace("\\", "/") for f in all_files if stem_of(f) not in boxes]
    print(f"{len(all_files)} photos, {len(boxes)} already have boxes, {len(todo)} to detect", flush=True)

    if todo and not out.exists():
        list_file = WORK_DIR / "md_todo.json"
        list_file.write_text(json.dumps(todo))
        cmd = [sys.executable, "-m", "megadetector.detection.run_detector_batch", "MDV5A", str(list_file), str(out),
               "--threshold", "0.005", "--quiet", "--checkpoint_frequency", "1000", "--checkpoint_path", str(checkpoint),
               "--allow_checkpoint_overwrite"]
        if checkpoint.exists():
            cmd += ["--resume_from_checkpoint", str(checkpoint)]
        subprocess.run(cmd, check=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    failures = []
    if out.exists():
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        for im in data["images"]:
            if im.get("failure") or im.get("detections") is None:
                failures.append(im["file"])
                continue
            boxes[stem_of(im["file"])] = [
                {"category": d["category"], "conf": d["conf"], "bbox": d["bbox"]} for d in im["detections"]
            ]
    (WORK_DIR / "boxes.json").write_text(json.dumps(boxes))
    print(f"boxes for {len(boxes)} photos; failures: {len(failures)} {failures[:5]}", flush=True)


if __name__ == "__main__":
    main()
