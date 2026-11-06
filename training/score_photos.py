"""Score one batch of photos with a trained one-vs-all model. The app runs this after SpeciesNet.

    python training/score_photos.py --predictions <predictions.json> --folder <photos> --model <model.pt> --out <scores.json>

Output: {"threshold", "review_below", "photos": {<file as written in predictions>: {"score", "count"}}}.
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


def score_photo(extractor, box_head, frame_head, model, path, detections):
    photo = load_photo(path)
    boxes = [d for d in detections if d["conf"] >= model["floor"]]
    crops = np.stack([crop_box(photo, frame_box())] + [crop_box(photo, d["bbox"]) for d in boxes])
    features, _ = extractor(crops)
    frame_score = float(score(frame_head, features[:1], extractor.device)[0])
    box_scores = score(box_head, features[1:], extractor.device) if boxes else np.zeros(0)
    is_animal = np.array([d["category"] == "1" for d in boxes], dtype=bool)
    box_confs = np.array([d["conf"] for d in boxes], dtype=float)
    return photo_result(model, frame_score, box_scores, is_animal, box_confs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.predictions, encoding="utf-8") as f:
        images = json.load(f)["images"]
    extractor = FeatureExtractor()
    print(f"Scoring {len(images)} photos with {os.path.basename(args.model)} on {extractor.device}", flush=True)
    model, box_head, frame_head = load_heads(args.model, extractor.device)
    photos = {}
    for im in images:
        if im.get("failure"):
            continue
        try:
            photos[im["file"]] = score_photo(extractor, box_head, frame_head, model, os.path.join(args.folder, im["file"]), im.get("detections") or [])
        except OSError as e:
            print(f"Couldn't read {im['file']}: {e}", flush=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"species": model["species"], "threshold": model["threshold"], "review_below": model["review_below"], "photos": photos}, f)
    os.replace(tmp, args.out)
    print(f"Scored {len(photos)} photos", flush=True)


if __name__ == "__main__":
    main()
