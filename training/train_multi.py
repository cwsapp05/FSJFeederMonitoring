"""Train one multi-species model on SpeciesNet features, and compare it with the one-vs-all models.

    .venv-train\\Scripts\\python training\\train_multi.py --csv <camelot.csv>            (validation only)
    .venv-train\\Scripts\\python training\\train_multi.py --csv <camelot.csv> --evaluate-test [--save]

The species compete in one softmax, with "other" for everything else, so look-alikes (crow and
grackle) are told apart directly instead of by separate models that each say yes. It uses the same
saved features and the same one-hour-block split as train.py, and evaluates each species on exactly
the photos train.py would, so the numbers can be set side by side with the one-vs-all reports.
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
from train import (MIN_POSITIVE_BOX_CONF, RECALL_TARGET, REVIEW_BELOW, TRAIN_SEED, arrays_for, boundary_check,
                   evaluate, load_features, metrics, photo_labels, threshold_for_recall)

OTHER = "other"
SPECIES = ["Florida Scrub-Jay", "Common Grackle", "White-Winged Dove", "Red-Bellied Woodpecker",
           "Northern Mockingbird", "Blue Jay", "Crow Sp.", "Northern Cardinal"]
CLASSES = [OTHER] + SPECIES
# Too few separate visits to trust their accuracy figures, so every call on them asks for a check.
ALWAYS_CHECK = {"Crow Sp.", "Northern Cardinal"}
# The configuration is chosen on these, which have enough validation photos to rank settings.
SELECT_ON = ["Florida Scrub-Jay", "Common Grackle", "White-Winged Dove", "Red-Bellied Woodpecker",
             "Northern Mockingbird", "Blue Jay"]
# One-vs-all models to compare against, by their report file.
ONE_VS_ALL = {"Florida Scrub-Jay": "florida_scrub_jay", "Common Grackle": "common_grackle",
              "White-Winged Dove": "white_winged_dove"}
SLUG = "feeder_species"


def crop_labels(data, photos):
    """Class index per crop for the box head and the frame head, -1 to leave a crop out.

    Like train.py's rules, only clear cases are used. A photo with one species of ours: the whole
    frame and its N most confident animal boxes (N = the sighting quantity) get that class, and its
    other boxes are left out. A photo with none of our species: every crop is "other". Photos with
    an unknown sighting, or with one of our species alongside another species, are left out.
    """
    box_y = np.full(len(data["stem"]), -1, dtype=np.int64)
    frame_y = np.full(len(data["stem"]), -1, dtype=np.int64)
    boxes, frames = collections.defaultdict(list), {}
    for i, stem in enumerate(data["stem"].tolist()):
        if data["category"][i] == FRAME:
            frames[stem] = i
        else:
            boxes[stem].append(i)
    ours = set(SPECIES)
    for stem, frame in frames.items():
        names = usable_labels(photos[stem])
        if names is None:
            continue
        if not names & ours:
            frame_y[frame] = 0
            box_y[boxes[stem]] = 0
        elif len(names) == 1:
            species = next(iter(names))
            c = CLASSES.index(species)
            frame_y[frame] = c
            animals = sorted((i for i in boxes[stem] if data["category"][i] == 1), key=lambda i: -data["conf"][i])
            for rank, i in enumerate(animals[:photos[stem]["species"][species]]):
                if data["conf"][i] >= MIN_POSITIVE_BOX_CONF or rank == 0:
                    box_y[i] = c
    return box_y, frame_y


class MultiHead(nn.Module):
    """Softmax over CLASSES on top of the shared features."""

    def __init__(self, dim=1280, classes=len(CLASSES), hidden=0, dropout=0.3):
        super().__init__()
        if hidden:
            self.net = nn.Sequential(nn.Dropout(dropout), nn.Linear(dim, hidden), nn.ReLU(), nn.Dropout(dropout),
                                     nn.Linear(hidden, classes))
        else:
            self.net = nn.Linear(dim, classes)
        self.register_buffer("mean", torch.zeros(dim))
        self.register_buffer("std", torch.ones(dim))

    def forward(self, x):
        return self.net((x - self.mean) / self.std)


def fit_multi(x, y, hidden, weight_decay, epochs=40, device="cuda"):
    torch.manual_seed(TRAIN_SEED)
    x = torch.from_numpy(x).to(device)
    t = torch.from_numpy(y).to(device)
    head = MultiHead(x.shape[1], len(CLASSES), hidden).to(device)
    head.mean.copy_(x.mean(0))
    head.std.copy_(x.std(0).clamp_min(1e-6))
    # Square-root inverse frequency: rare species count for more without swamping "other".
    counts = torch.bincount(t, minlength=len(CLASSES)).float().clamp_min(1)
    weight = (counts.sum() / counts).sqrt()
    loss_fn = nn.CrossEntropyLoss(weight=weight / weight.mean())
    opt = torch.optim.AdamW(head.parameters(), lr=1e-3, weight_decay=weight_decay)
    steps = epochs * (len(x) // 512 + 1)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, 2e-3, total_steps=steps)
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
def probs_of(head, x, device="cuda"):
    t = torch.from_numpy(x)
    return np.concatenate([torch.softmax(head(t[i:i + 8192].to(device)), -1).cpu().numpy() for i in range(0, len(t), 8192)])


class Photos:
    """Per-photo class scores: for each class, its best crop (boxes above the floor, and the frame if used)."""

    def __init__(self, data):
        self.stems, self.index = np.unique(data["stem"], return_inverse=True)
        self.is_frame = data["category"] == FRAME
        self.conf = data["conf"]

    def scores(self, box_p, frame_p, floor, use_frame):
        out = np.zeros((len(self.stems), len(CLASSES)), dtype=np.float32)
        boxes = ~self.is_frame & (self.conf >= floor)
        np.maximum.at(out, self.index[boxes], box_p[boxes])
        if use_frame:
            np.maximum.at(out, self.index[self.is_frame], frame_p[self.is_frame])
        return out

    def score_map(self, out, species):
        c = CLASSES.index(species)
        return dict(zip(self.stems.tolist(), out[:, c].tolist()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--evaluate-test", action="store_true", help="Score the test set. Only once the configuration is settled.")
    ap.add_argument("--save", action="store_true", help=f"Write models/{SLUG}.pt (needs --evaluate-test)")
    args = ap.parse_args()

    photos = load_dataset(args.csv)
    data = load_features()
    box_y, frame_y = crop_labels(data, photos)
    split = np.array([photos[s]["split"] for s in data["stem"].tolist()])
    train_mask = split == "train"
    print("box labels (train):", {CLASSES[k]: v for k, v in sorted(collections.Counter(box_y[train_mask & (box_y >= 0)].tolist()).items())}, flush=True)
    print("frame labels (train):", {CLASSES[k]: v for k, v in sorted(collections.Counter(frame_y[train_mask & (frame_y >= 0)].tolist()).items())}, flush=True)
    labels = {s: photo_labels(photos, s) for s in SPECIES}
    stems_in = {name: sorted({s for s in data["stem"].tolist() if photos[s]["split"] == name}) for name in ("train", "val", "test")}
    view = Photos(data)

    def train_pair(mask, hidden, wd):
        b, f = mask & (box_y >= 0), mask & (frame_y >= 0)
        box = fit_multi(data["feature"][b], box_y[b], hidden, wd)
        frame = fit_multi(data["feature"][f], frame_y[f], hidden, wd)
        return box, frame, probs_of(box, data["feature"]), probs_of(frame, data["feature"])

    def per_species(out, stems, thresholds=None):
        rows = {}
        for sp in SPECIES:
            keep, s, y = arrays_for(stems, view.score_map(out, sp), labels[sp])
            th = thresholds[sp] if thresholds else threshold_for_recall(s, y, RECALL_TARGET)
            rows[sp] = dict(threshold=th, **metrics(s, y, th))
        return rows

    # Choose the configuration on validation: best mean precision at the recall target over SELECT_ON.
    results = []
    for hidden, wd in ((0, 0.01), (0, 0.1), (256, 0.01), (256, 0.1)):
        _, _, box_p, frame_p = train_pair(train_mask, hidden, wd)
        for floor in (0.005, 0.01, 0.02, 0.05):
            for use_frame in (False, True):
                rows = per_species(view.scores(box_p, frame_p, floor, use_frame), stems_in["val"])
                mean_precision = float(np.mean([rows[sp]["precision"] for sp in SELECT_ON]))
                results.append(dict(hidden=hidden, weight_decay=wd, floor=floor, use_frame=use_frame,
                                    mean_precision=mean_precision, species=rows))
                print(f"hidden={hidden} wd={wd} floor={floor} frame={use_frame}: mean precision {mean_precision:.4f} | "
                      + " ".join(f"{sp.split()[-1][:6]} {rows[sp]['precision']:.3f}" for sp in SELECT_ON), flush=True)
    best = max(results, key=lambda r: (r["mean_precision"], -r["floor"]))
    print("chosen on validation:", {k: v for k, v in best.items() if k != "species"}, flush=True)

    box_head, frame_head, box_p, frame_p = train_pair(train_mask, best["hidden"], best["weight_decay"])
    out = view.scores(box_p, frame_p, best["floor"], best["use_frame"])
    thresholds, report = {}, dict(classes=CLASSES, chosen={k: v for k, v in best.items() if k != "species"},
                                  all_val_results=results, val={}, test={})
    for sp in SPECIES:
        keep, s, y = arrays_for(stems_in["val"], view.score_map(out, sp), labels[sp])
        thresholds[sp] = threshold_for_recall(s, y, RECALL_TARGET)
        report["val"][sp] = evaluate("val", keep, s, y, photos, thresholds[sp], sp)
        (WORK_DIR / f"{SLUG}_val_scores_{sp.lower().replace(' ', '_').replace('.', '')}.json").write_text(json.dumps(
            [dict(stem=a, score=float(v), label=int(l), species=dict(photos[a]["species"])) for a, v, l in zip(keep, s, y)]))
    report["thresholds"] = thresholds

    if args.evaluate_test:
        for sp in SPECIES:
            k, s, y = arrays_for(stems_in["test"], view.score_map(out, sp), labels[sp])
            report["test"][sp] = evaluate("test", k, s, y, photos, thresholds[sp], sp)
            report["test"][sp]["excluding_near_boundary"] = boundary_check(k, s, y, photos, thresholds[sp], "test")

        # The same time-based checks as train.py: fit on the past, threshold on its validation part.
        first = {}
        for p in photos.values():
            first[p["block"]] = min(first.get(p["block"], p["taken"]), p["taken"])
        for name, cut in (("train_before_march_test_march_on", dt.datetime(2026, 3, 1)), ("train_jan_mar_test_september", dt.datetime(2026, 9, 1))):
            is_past = np.array([first[photos[s]["block"]] < cut for s in data["stem"].tolist()])
            _, _, b_p, f_p = train_pair(is_past & train_mask, best["hidden"], best["weight_decay"])
            o = view.scores(b_p, f_p, best["floor"], best["use_frame"])
            past_val = [s for s in stems_in["val"] if first[photos[s]["block"]] < cut]
            future = sorted({s for s in data["stem"].tolist() if first[photos[s]["block"]] >= cut})
            report[name] = {}
            for sp in SPECIES:
                k, s, y = arrays_for(past_val, view.score_map(o, sp), labels[sp])
                if not (y == 1).any():
                    report[name][sp] = f"skipped: no {sp} in validation photos before {cut:%Y-%m-%d}"
                    continue
                th = threshold_for_recall(s, y, RECALL_TARGET)
                k, s, y = arrays_for(future, view.score_map(o, sp), labels[sp])
                report[name][sp] = evaluate(name, k, s, y, photos, th, sp, full=False)

        # Side by side with the one-vs-all models, on the same photos.
        comparison = {}
        for sp, slug in ONE_VS_ALL.items():
            path = WORK_DIR / f"{slug}_report.json"
            if not path.exists():
                continue
            ova = json.loads(path.read_text())
            row = {}
            for key in ("test", "train_before_march_test_march_on", "train_jan_mar_test_september"):
                a, b = ova.get(key), (report["test"] if key == "test" else report[key]).get(sp)
                if isinstance(a, dict) and isinstance(b, dict):
                    row[key] = {m: dict(one_vs_all=round(a[m], 4), multi=round(b[m], 4))
                                for m in ("recall", "precision", "false_positive_rate")}
                    row[key]["photos"] = dict(one_vs_all=a["photos"], multi=b["photos"])
            comparison[sp] = row
        report["comparison_with_one_vs_all"] = comparison

        if args.save:
            MODELS_DIR.mkdir(exist_ok=True)
            torch.save({"kind": "multi", "classes": CLASSES, "hidden": best["hidden"],
                        "state_box": box_head.cpu().state_dict(), "state_frame": frame_head.cpu().state_dict(),
                        "use_frame": best["use_frame"], "floor": best["floor"], "thresholds": thresholds,
                        # A review cutoff above 1 means every call asks for a check.
                        "review_below": {sp: (1.01 if sp in ALWAYS_CHECK else REVIEW_BELOW) for sp in SPECIES},
                        "recall_target": RECALL_TARGET, "seed_split": "fsj-v1", "feature_layer": "SpeciesNet avg_pool"},
                       MODELS_DIR / f"{SLUG}.pt")

    (WORK_DIR / f"{SLUG}_report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps({k: report[k] for k in ("chosen", "thresholds")}, indent=1, default=str))
    for sp in SPECIES:
        for split_name in ("val", "test"):
            r = report[split_name].get(sp)
            if r:
                print(f"{split_name:4s} {sp:24s} present {r['present']:5d} recall {r['recall']:.4f} precision {r['precision']:.4f} fpr {r['false_positive_rate']:.4f}")
    if "comparison_with_one_vs_all" in report:
        print(json.dumps(report["comparison_with_one_vs_all"], indent=1))


if __name__ == "__main__":
    main()
