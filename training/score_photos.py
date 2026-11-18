"""Score one batch of photos with trained one-vs-all models. The app runs this after SpeciesNet.

    python training/score_photos.py --predictions <predictions.json> --folder <photos> --model <a.pt> [--model <b.pt>] --out <scores.json>

Each crop goes through SpeciesNet once, and every model scores the same features.
Output: {"models": [{"species", "threshold", "review_below", "photos": {<file as written in predictions>: {"score", "count"}}}]}.
A photo that can't be read is left out, so the app keeps SpeciesNet's answer for it.
"""
import argparse
import json
import os
import sys

import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from features import FeatureExtractor, crop_box, frame_box, load_photo  # noqa: E402
from train import Head  # noqa: E402

COUNT_MIN_DETECTOR_CONF = 0.2  # weak boxes often surround a bird that already has a good one


def load_heads(path, device):
    model = torch.load(path, map_location=device, weights_only=True)
    heads = []
    for key in ("state_box", "state_frame"):
        head = Head(hidden=model["hidden"]).to(device).eval()
        head.load_state_dict(model[key])
        heads.append(head)
    return model, heads[0], heads[1]


@torch.inference_mode()
def score(head, features, device):
    return torch.sigmoid(head(torch.from_numpy(features).to(device))).cpu().numpy()


def photo_result(model, frame_score, box_scores, is_animal, box_confs):
    """A photo's score is the best of its boxes and the whole frame.

    Count is its solid animal boxes over the threshold, at least 1. It's rough: on test photos it matched
    Camelot's quantity 75% of the time and was within one 95% of the time.
    """
    best = max([frame_score] + box_scores.tolist())
    solid = is_animal & (box_confs >= COUNT_MIN_DETECTOR_CONF)
    return {"score": best, "count": max(int((box_scores[solid] >= model["threshold"]).sum()), 1)}


def score_photo(extractor, models, path, detections):
    """One result per model. Crops above the lowest model floor are featurized once."""
    photo = load_photo(path)
    boxes = [d for d in detections if d["conf"] >= min(m["floor"] for m, _, _ in models)]
    crops = np.stack([crop_box(photo, frame_box())] + [crop_box(photo, d["bbox"]) for d in boxes])
    features, _ = extractor(crops)
    box_features = features[1:]
    is_animal = np.array([d["category"] == "1" for d in boxes], dtype=bool)
    box_confs = np.array([d["conf"] for d in boxes], dtype=float)
    results = []
    for model, box_head, frame_head in models:
        keep = box_confs >= model["floor"]
        frame_score = float(score(frame_head, features[:1], extractor.device)[0])
        box_scores = score(box_head, box_features[keep], extractor.device) if keep.any() else np.zeros(0)
        results.append(photo_result(model, frame_score, box_scores, is_animal[keep], box_confs[keep]))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--model", required=True, action="append", help="repeat for each model")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.predictions, encoding="utf-8") as f:
        images = json.load(f)["images"]
    extractor = FeatureExtractor()
    names = ", ".join(os.path.basename(m) for m in args.model)
    print(f"Scoring {len(images)} photos with {names} on {extractor.device}", flush=True)
    models = [load_heads(path, extractor.device) for path in args.model]
    photos = [{} for _ in models]
    for im in images:
        if im.get("failure"):
            continue
        try:
            results = score_photo(extractor, models, os.path.join(args.folder, im["file"]), im.get("detections") or [])
        except OSError as e:
            print(f"Couldn't read {im['file']}: {e}", flush=True)
            continue
        for out, result in zip(photos, results):
            out[im["file"]] = result
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"models": [{"species": m["species"], "threshold": m["threshold"], "review_below": m["review_below"], "photos": p}
                              for (m, _, _), p in zip(models, photos)]}, f)
    os.replace(tmp, args.out)
    print(f"Scored {len(photos[0])} photos", flush=True)


if __name__ == "__main__":
    main()
