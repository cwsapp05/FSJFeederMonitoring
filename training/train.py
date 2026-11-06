"""Train one-vs-all heads on SpeciesNet features and evaluate them per photo.

    .venv-train\\Scripts\\python training\\train.py --csv <camelot.csv> --species "Florida Scrub-Jay"

The test set is only scored for the configuration chosen on validation data.
"""
import argparse
import collections
import datetime as dt
import json

import numpy as np
import torch
from torch import nn

from common import MODELS_DIR, WORK_DIR, load_dataset, usable_labels
from features import FRAME

RECALL_TARGET = 0.98
# On validation, calls from the threshold up to 0.99 were 42% false; calls at or above it were 1% false.
REVIEW_BELOW = 0.99
MIN_POSITIVE_BOX_CONF = 0.1  # below this a box on a positive photo is too likely to be background
TRAIN_SEED = 7


def load_features():
    parts = sorted((WORK_DIR / "features").glob("part-*.npz"))
    if not parts:
        raise SystemExit("No features yet. Run extract.py first.")
    keys = ("stem", "category", "conf", "feature", "california")
    out = {k: [] for k in keys}
    for path in parts:
        with np.load(path) as z:
            for k in keys:
                out[k].append(z[k])
    out = {k: np.concatenate(v) for k, v in out.items()}
    print(f"{len(out['stem'])} crops from {len(set(out['stem'].tolist()))} photos", flush=True)
    return out


def photo_labels(photos, species):
    """1 = species present, 0 = absent, -1 = can't say (unlabeled, or an unknown sighting without the species)."""
    labels = {}
    for stem, p in photos.items():
        names = usable_labels(p)
        if species in p["species"]:
            labels[stem] = 1
        elif names is None:
            labels[stem] = -1
        else:
            labels[stem] = 0
    return labels


def box_training_labels(data, photos, labels, species):
    """Which crops to train the box head on, and with what label.

    Camelot labels photos, not boxes, so only clear cases are used:
    - Positives: on photos where the species is alone, the N most confident animal boxes, with N the
      sighting quantity. MegaDetector's person and vehicle boxes are nearly always the feeder.
    - Negatives: every box on a photo without the species.
    - Skipped: everything else, including photos where the species shares the frame with another.
    """
    y = np.full(len(data["stem"]), -1, dtype=np.int8)
    by_photo = collections.defaultdict(list)
    for i, stem in enumerate(data["stem"].tolist()):
        if data["category"][i] != FRAME:
            by_photo[stem].append(i)
    for stem, idx in by_photo.items():
        label = labels.get(stem, -1)
        if label == 0:
            y[idx] = 0
        elif label == 1 and set(photos[stem]["species"]) == {species}:
            animals = sorted((i for i in idx if data["category"][i] == 1), key=lambda i: -data["conf"][i])
            for rank, i in enumerate(animals[:photos[stem]["species"][species]]):
                if data["conf"][i] >= MIN_POSITIVE_BOX_CONF or rank == 0:
                    y[i] = 1
    return y


def frame_training_labels(data, photos, labels, species):
    y = np.full(len(data["stem"]), -1, dtype=np.int8)
    for i in np.flatnonzero(data["category"] == FRAME):
        stem = str(data["stem"][i])
        label = labels.get(stem, -1)
        if label == 0 or (label == 1 and set(photos[stem]["species"]) == {species}):
            y[i] = label
    return y


class Head(nn.Module):
    """One-vs-all classifier on top of the shared features."""

    def __init__(self, dim=1280, hidden=0, dropout=0.3):
        super().__init__()
        if hidden:
            self.net = nn.Sequential(nn.Dropout(dropout), nn.Linear(dim, hidden), nn.ReLU(), nn.Dropout(dropout), nn.Linear(hidden, 1))
        else:
            self.net = nn.Linear(dim, 1)
        self.register_buffer("mean", torch.zeros(dim))
        self.register_buffer("std", torch.ones(dim))

    def forward(self, x):
        return self.net((x - self.mean) / self.std).squeeze(-1)


def fit_head(x, y, hidden, weight_decay, epochs=40, device="cuda"):
    torch.manual_seed(TRAIN_SEED)
    x = torch.from_numpy(x).to(device)
    t = torch.from_numpy(y.astype(np.float32)).to(device)
    head = Head(x.shape[1], hidden).to(device)
    head.mean.copy_(x.mean(0))
    head.std.copy_(x.std(0).clamp_min(1e-6))
    opt = torch.optim.AdamW(head.parameters(), lr=1e-3, weight_decay=weight_decay)
    steps = epochs * (len(x) // 512 + 1)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, 2e-3, total_steps=steps)
    pos_weight = (t == 0).sum() / (t == 1).sum().clamp_min(1)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    gen = torch.Generator(device=device).manual_seed(TRAIN_SEED)
    head.train()
    for _ in range(epochs):
        order = torch.randperm(len(x), generator=gen, device=device)
        for i in range(0, len(x), 512):
            b = order[i:i + 512]
            opt.zero_grad()
            loss_fn(head(x[b]), t[b]).backward()
            opt.step()
            if sched.last_epoch < steps - 1:
                sched.step()
    return head.eval()


@torch.inference_mode()
def scores_of(head, x, device="cuda"):
    t = torch.from_numpy(x)
    return np.concatenate([torch.sigmoid(head(t[i:i + 8192].to(device))).cpu().numpy() for i in range(0, len(t), 8192)])


def photo_scores(data, box_scores, frame_scores, floor, use_frame):
    """Score of a photo: its best box above the detector floor, optionally also the whole-photo score."""
    best = collections.defaultdict(float)
    is_frame = data["category"] == FRAME
    for i, stem in enumerate(data["stem"].tolist()):
        if is_frame[i]:
            if use_frame:
                best[stem] = max(best[stem], float(frame_scores[i]))
            else:
                best[stem] = max(best[stem], 0.0)
        elif data["conf"][i] >= floor:
            best[stem] = max(best[stem], float(box_scores[i]))
    return best


def threshold_for_recall(scores, y, target):
    pos = np.sort(scores[y == 1])
    return float(pos[int(np.floor(len(pos) * (1 - target)))])


def metrics(scores, y, threshold):
    hit = scores >= threshold
    tp, fp = int((hit & (y == 1)).sum()), int((hit & (y == 0)).sum())
    fn, tn = int((~hit & (y == 1)).sum()), int((~hit & (y == 0)).sum())
    return dict(recall=tp / max(tp + fn, 1), precision=tp / max(tp + fp, 1), false_positive_rate=fp / max(fp + tn, 1), tp=tp, fp=fp, fn=fn, tn=tn)


def block_bootstrap(scores, y, blocks, threshold, rounds=1000):
    """95% intervals resampling whole blocks, because photos in a block aren't independent."""
    by_block = collections.defaultdict(list)
    for i, b in enumerate(blocks):
        by_block[b].append(i)
    groups = [np.array(v) for v in by_block.values()]
    rng = np.random.default_rng(TRAIN_SEED)
    recalls, precisions = [], []
    for _ in range(rounds):
        idx = np.concatenate([groups[j] for j in rng.integers(0, len(groups), len(groups))])
        m = metrics(scores[idx], y[idx], threshold)
        recalls.append(m["recall"]); precisions.append(m["precision"])
    return dict(recall_95=[float(np.percentile(recalls, q)) for q in (2.5, 97.5)],
                precision_95=[float(np.percentile(precisions, q)) for q in (2.5, 97.5)])


def group_report(values, scores, y, threshold):
    rows = {}
    values = np.array(values)
    for v in sorted(set(values.tolist())):
        mask = values == v
        m = metrics(scores[mask], y[mask], threshold)
        rows[v] = dict(photos=int(mask.sum()), species_present=int((y[mask] == 1).sum()),
                       recall=round(m["recall"], 4), flagged=round(float((scores[mask] >= threshold).mean()), 4))
    return rows


def arrays_for(stems, score_map, labels):
    keep = [s for s in stems if labels.get(s, -1) >= 0 and s in score_map]
    return keep, np.array([score_map[s] for s in keep]), np.array([labels[s] for s in keep])


def evaluate(name, keep, scores, y, photos, threshold, species, full=True):
    report = dict(evaluation=name, threshold=threshold, photos=len(keep), present=int((y == 1).sum()), **metrics(scores, y, threshold))
    blocks = [photos[s]["block"] for s in keep]
    report.update(block_bootstrap(scores, y, blocks, threshold))
    # A visit-level view: did the model flag at least one photo of the species in each block with it?
    with_species = {photos[s]["block"] for s, label in zip(keep, y) if label == 1}
    flagged = {photos[s]["block"] for s, label, sc in zip(keep, y, scores) if label == 1 and sc >= threshold}
    report["blocks_with_species"] = len(with_species)
    report["block_recall"] = len(flagged) / max(len(with_species), 1)
    if full:
        negatives = [i for i, label in enumerate(y) if label == 0]
        by_species = collections.defaultdict(lambda: [0, 0])
        for i in negatives:
            for sp in photos[keep[i]]["species"]:
                by_species[sp][0] += 1
                by_species[sp][1] += int(scores[i] >= threshold)
        report["false_alarms_by_species"] = {k: dict(photos=v[0], flagged=v[1], rate=round(v[1] / v[0], 4))
                                             for k, v in sorted(by_species.items(), key=lambda kv: -kv[1][1])}
        report["by_station"] = group_report([photos[s]["station"] for s in keep], scores, y, threshold)
        report["by_month"] = group_report([photos[s]["taken"].strftime("%Y-%m") for s in keep], scores, y, threshold)
        report["by_hour_of_day"] = group_report([photos[s]["taken"].hour for s in keep], scores, y, threshold)
        qty = [min(photos[s]["species"].get(species, 0), 4) for s in keep]
        report["recall_by_quantity_(4=4+)"] = group_report(qty, scores, y, threshold)
    return report


def boundary_check(keep, scores, y, photos, threshold, split_name, minutes=5):
    """Your blocks can split a visit at a boundary. Re-score without photos close to one in another split."""
    by_station = collections.defaultdict(list)
    for p in photos.values():
        by_station[p["station"]].append((p["taken"], p["split"], p["stem"]))
    near = set()
    gap = dt.timedelta(minutes=minutes)
    for items in by_station.values():
        items.sort()
        for (t0, s0, a), (t1, s1, b) in zip(items, items[1:]):
            if t1 - t0 <= gap and s0 != s1:
                if s0 == split_name:
                    near.add(a)
                if s1 == split_name:
                    near.add(b)
    mask = np.array([s not in near for s in keep])
    return dict(minutes=minutes, removed=int((~mask).sum()), **metrics(scores[mask], y[mask], threshold))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--species", default="Florida Scrub-Jay")
    ap.add_argument("--slug", default="florida_scrub_jay")
    ap.add_argument("--evaluate-test", action="store_true", help="Score the test set. Only do this once the configuration is settled.")
    args = ap.parse_args()

    photos = load_dataset(args.csv)
    labels = photo_labels(photos, args.species)
    data = load_features()
    box_y = box_training_labels(data, photos, labels, args.species)
    frame_y = frame_training_labels(data, photos, labels, args.species)
    split = np.array([photos[s]["split"] for s in data["stem"].tolist()])
    print("box labels (train):", collections.Counter(box_y[split == "train"].tolist()), flush=True)
    print("frame labels (train):", collections.Counter(frame_y[split == "train"].tolist()), flush=True)
    stems_in = {name: sorted({s for s in data["stem"].tolist() if photos[s]["split"] == name}) for name in ("train", "val", "test")}

    def train_pair(mask, hidden, wd):
        b = mask & (box_y >= 0)
        f = mask & (frame_y >= 0)
        return fit_head(data["feature"][b], box_y[b], hidden, wd), fit_head(data["feature"][f], frame_y[f], hidden, wd)

    # Choose the configuration on validation data: fewest false alarms at the recall target.
    results = []
    train_mask = split == "train"
    for hidden, wd in ((0, 0.01), (0, 0.1), (256, 0.01), (256, 0.1)):
        box_head, frame_head = train_pair(train_mask, hidden, wd)
        box_sc, frame_sc = scores_of(box_head, data["feature"]), scores_of(frame_head, data["feature"])
        for floor in (0.005, 0.01, 0.02, 0.05, 0.1):
            for use_frame in (False, True):
                keep, s, y = arrays_for(stems_in["val"], photo_scores(data, box_sc, frame_sc, floor, use_frame), labels)
                th = threshold_for_recall(s, y, RECALL_TARGET)
                m = metrics(s, y, th)
                results.append(dict(hidden=hidden, weight_decay=wd, floor=floor, use_frame=use_frame, threshold=th, **m))
                print(f"hidden={hidden} wd={wd} floor={floor} frame={use_frame}: recall {m['recall']:.4f} precision {m['precision']:.4f} fpr {m['false_positive_rate']:.4f} th {th:.4f}", flush=True)
    best = max(results, key=lambda r: (r["precision"], -r["floor"]))
    print("chosen on validation:", best, flush=True)

    # SpeciesNet alone: its raw California Scrub-Jay score, as the baseline to beat.
    california_map = photo_scores(data, data["california"], data["california"], best["floor"], False)
    keep, s, y = arrays_for(stems_in["val"], california_map, labels)
    th = threshold_for_recall(s, y, RECALL_TARGET)
    baseline = dict(name="SpeciesNet raw California Scrub-Jay score", threshold=th, **metrics(s, y, th))
    print("baseline:", baseline, flush=True)

    box_head, frame_head = train_pair(train_mask, best["hidden"], best["weight_decay"])
    box_sc, frame_sc = scores_of(box_head, data["feature"]), scores_of(frame_head, data["feature"])
    score_map = photo_scores(data, box_sc, frame_sc, best["floor"], best["use_frame"])
    val_keep, val_s, val_y = arrays_for(stems_in["val"], score_map, labels)
    threshold = threshold_for_recall(val_s, val_y, RECALL_TARGET)
    report = dict(species=args.species, chosen=best, baseline_on_val=baseline, all_val_results=results,
                  val=evaluate("val", val_keep, val_s, val_y, photos, threshold, args.species))

    # For looking at mistakes and picking a lower threshold band.
    review = [dict(stem=s, score=float(v), label=int(l), station=photos[s]["station"], species=dict(photos[s]["species"]))
              for s, v, l in zip(val_keep, val_s, val_y)]
    (WORK_DIR / f"{args.slug}_val_scores.json").write_text(json.dumps(review))

    if args.evaluate_test:
        k, s, y = arrays_for(stems_in["test"], score_map, labels)
        report["test"] = evaluate("test", k, s, y, photos, threshold, args.species)
        report["test_excluding_near_boundary"] = boundary_check(k, s, y, photos, threshold, "test")
        review = [dict(stem=a, score=float(v), label=int(l), station=photos[a]["station"], species=dict(photos[a]["species"])) for a, v, l in zip(k, s, y)]
        (WORK_DIR / f"{args.slug}_test_scores.json").write_text(json.dumps(review))

        # Time-based checks, the closest match to future photos: fit on the past, threshold on its validation part.
        for name, cut in (("train_before_march_test_march_on", dt.datetime(2026, 3, 1)), ("train_jan_mar_test_september", dt.datetime(2026, 9, 1))):
            first = {}
            for p in photos.values():
                first[p["block"]] = min(first.get(p["block"], p["taken"]), p["taken"])
            is_past = np.array([first[photos[s]["block"]] < cut for s in data["stem"].tolist()])
            b_head, f_head = train_pair(is_past & train_mask, best["hidden"], best["weight_decay"])
            b_sc, f_sc = scores_of(b_head, data["feature"]), scores_of(f_head, data["feature"])
            sm = photo_scores(data, b_sc, f_sc, best["floor"], best["use_frame"])
            past_val = [s for s in stems_in["val"] if first[photos[s]["block"]] < cut]
            k, s, y = arrays_for(past_val, sm, labels)
            th = threshold_for_recall(s, y, RECALL_TARGET)
            future = sorted({s for s in data["stem"].tolist() if first[photos[s]["block"]] >= cut})
            k, s, y = arrays_for(future, sm, labels)
            report[name] = evaluate(name, k, s, y, photos, th, args.species)

        MODELS_DIR.mkdir(exist_ok=True)
        torch.save({"species": args.species, "hidden": best["hidden"], "state_box": box_head.cpu().state_dict(),
                    "state_frame": frame_head.cpu().state_dict(), "use_frame": best["use_frame"], "floor": best["floor"],
                    "threshold": threshold, "review_below": REVIEW_BELOW, "recall_target": RECALL_TARGET,
                    "seed_split": "fsj-v1", "feature_layer": "SpeciesNet avg_pool"}, MODELS_DIR / f"{args.slug}.pt")

    (WORK_DIR / f"{args.slug}_report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: v for k, v in report.items() if k not in ("all_val_results",)}, indent=1, default=str), flush=True)


if __name__ == "__main__":
    main()
