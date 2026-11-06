"""Is the model seeing the bird, or the scene around it? Uses only train and val photos, never test.

1. Whole calendar weeks held out: a model that memorized a day's light or feeder state falls apart here.
2. Birds blacked out: if the frame model still says "scrub-jay" with the bird gone, it's using context.

    .venv-train\\Scripts\\python training\\diagnose.py --csv <camelot.csv> --photos <folder>
"""
import argparse
import collections
import json
import os
import random

import numpy as np
import torch

from common import WORK_DIR, load_dataset
from features import FRAME, FeatureExtractor, crop_box, frame_box, load_photo
from train import RECALL_TARGET, arrays_for, fit_head, load_features, metrics, photo_labels, photo_scores, box_training_labels, frame_training_labels, scores_of, threshold_for_recall

SPECIES = "Florida Scrub-Jay"
HIDDEN, WD, FLOOR = 256, 0.01, 0.01


def modes(data, box, frame):
    return {"boxes only": photo_scores(data, box, frame, FLOOR, False),
            "frame only": _frame_only(data, frame),
            "boxes + frame": photo_scores(data, box, frame, FLOOR, True)}


def _frame_only(data, frame):
    return {str(data["stem"][i]): float(frame[i]) for i in np.flatnonzero(data["category"] == FRAME)}


def at_recall(score_map, stems, labels, target):
    _, s, y = arrays_for(stems, score_map, labels)
    th = threshold_for_recall(s, y, target)
    return th, metrics(s, y, th)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--photos", required=True)
    args = ap.parse_args()
    photos = load_dataset(args.csv)
    labels = photo_labels(photos, SPECIES)
    data = load_features()
    box_y = box_training_labels(data, photos, labels, SPECIES)
    frame_y = frame_training_labels(data, photos, labels, SPECIES)
    stems = data["stem"].tolist()
    split = np.array([photos[s]["split"] for s in stems])
    week = np.array([photos[s]["taken"].isocalendar()[1] for s in stems])
    val_stems = sorted({s for s in stems if photos[s]["split"] == "val"})
    results = {}

    def fit(mask):
        return (fit_head(data["feature"][mask & (box_y >= 0)], box_y[mask & (box_y >= 0)], HIDDEN, WD),
                fit_head(data["feature"][mask & (frame_y >= 0)], frame_y[mask & (frame_y >= 0)], HIDDEN, WD))

    # The random-block model, for comparison, at several recall levels.
    box_head, frame_head = fit(split == "train")
    box_sc, frame_sc = scores_of(box_head, data["feature"]), scores_of(frame_head, data["feature"])
    results["random_blocks_val"] = {}
    for name, sm in modes(data, box_sc, frame_sc).items():
        results["random_blocks_val"][name] = {str(t): {k: round(v, 4) for k, v in at_recall(sm, val_stems, labels, t)[1].items() if k in ("recall", "precision", "false_positive_rate")}
                                              for t in (0.80, 0.90, 0.95, 0.98)}

    # 1. Whole weeks held out. Train on the train split of the other weeks, threshold on their val split,
    # and score every non-test photo from the held-out weeks.
    jan_mar = sorted({int(w) for w in week if w <= 14})
    folds = [jan_mar[i::4] for i in range(4)]
    print("weeks", jan_mar, "folds", folds, flush=True)
    pooled = collections.defaultdict(lambda: ([], []))
    per_fold = []
    for fold in folds:
        held = np.isin(week, fold)
        b, f = fit((split == "train") & ~held)
        bs, fs = scores_of(b, data["feature"]), scores_of(f, data["feature"])
        inner = sorted({s for s, sp, h in zip(stems, split, held) if sp == "val" and not h})
        outer = sorted({s for s, sp, h in zip(stems, split, held) if sp != "test" and h})
        row = dict(weeks=fold)
        for name, sm in modes(data, bs, fs).items():
            th, _ = at_recall(sm, inner, labels, RECALL_TARGET)
            _, s, y = arrays_for(outer, sm, labels)
            m = metrics(s, y, th)
            pooled[name][0].append(s >= th); pooled[name][1].append(y)
            row[name] = {k: round(m[k], 4) for k in ("recall", "precision", "false_positive_rate")}
            row[name]["present"] = int((y == 1).sum())
        per_fold.append(row)
        print(row, flush=True)
    results["weeks_held_out"] = per_fold
    results["weeks_held_out_pooled"] = {}
    for name, (hits, ys) in pooled.items():
        h, y = np.concatenate(hits), np.concatenate(ys)
        results["weeks_held_out_pooled"][name] = {k: round(v, 4) if isinstance(v, float) else v for k, v in metrics(h.astype(float), y, 0.5).items()}

    # 2. Black out the animals on val photos and ask the frame head again.
    boxes = json.loads((WORK_DIR / "boxes.json").read_text())
    rng = random.Random(11)
    # Only photos where the detector found as many solid animal boxes as Camelot counted birds, so the masks really cover them.
    def clean(s):
        p = photos[s]
        solid = [b for b in boxes[s] if b["category"] == "1" and b["conf"] >= 0.2]
        if labels.get(s) == 1:
            return set(p["species"]) == {SPECIES} and len(solid) >= p["species"][SPECIES]
        return False
    pos = [s for s in val_stems if clean(s)]
    pos = rng.sample(pos, min(400, len(pos)))
    fx = FeatureExtractor()
    original, masked = [], []
    for s in pos:
        photo = load_photo(os.path.join(args.photos, s + ".jpg"))
        original.append(crop_box(photo, frame_box()))
        _, h, w = photo.shape
        for b in boxes[s]:
            if b["category"] == "1" and b["conf"] >= 0.05:
                x, y, bw, bh = b["bbox"]
                photo[:, int(y * h):int((y + bh) * h) + 1, int(x * w):int((x + bw) * w) + 1] = 0
        masked.append(crop_box(photo, frame_box()))
    th_frame, _ = at_recall(_frame_only(data, frame_sc), val_stems, labels, RECALL_TARGET)
    so = scores_of(frame_head, fx(np.stack(original))[0])
    sm = scores_of(frame_head, fx(np.stack(masked))[0])
    th_combined, _ = at_recall(photo_scores(data, box_sc, frame_sc, FLOOR, True), val_stems, labels, RECALL_TARGET)
    results["birds_blacked_out"] = dict(photos=len(pos), frame_threshold=th_frame, combined_threshold=th_combined,
                                        flagged_original=float((so >= th_frame).mean()), flagged_masked=float((sm >= th_frame).mean()),
                                        flagged_original_at_combined=float((so >= th_combined).mean()),
                                        flagged_masked_at_combined=float((sm >= th_combined).mean()),
                                        median_original=float(np.median(so)), median_masked=float(np.median(sm)))
    (WORK_DIR / "diagnose_report.json").write_text(json.dumps(results, indent=1))
    print(json.dumps(results, indent=1), flush=True)


if __name__ == "__main__":
    torch.manual_seed(0)
    main()
