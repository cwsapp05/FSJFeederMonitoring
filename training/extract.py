"""Compute features for every detector box and for each whole photo. Resumes where it left off.

    .venv-train\\Scripts\\python training\\extract.py --photos <folder>
"""
import argparse
import json
import os
import time

import numpy as np
import torch

from common import WORK_DIR
from features import FRAME, FeatureExtractor, crop_box, frame_box, load_photo, speciesnet_path

DETECTOR_FLOOR = 0.005
PART_SIZE = 2000  # photos per saved file


class PhotoCrops(torch.utils.data.Dataset):
    def __init__(self, folder, stems, boxes):
        self.folder, self.stems, self.boxes = folder, stems, boxes

    def __len__(self):
        return len(self.stems)

    def __getitem__(self, i):
        stem = self.stems[i]
        items = [(FRAME, 1.0, frame_box())]
        items += [(int(b["category"]), b["conf"], b["bbox"]) for b in self.boxes[stem] if b["conf"] >= DETECTOR_FLOOR]
        try:
            photo = load_photo(os.path.join(self.folder, stem + ".jpg"))
            crops = np.stack([crop_box(photo, bbox) for _, _, bbox in items])
        except Exception as e:  # A damaged photo shouldn't stop the run.
            print(f"skipped {stem}: {e}", flush=True)
            return stem, None, None
        return stem, items, crops


def identity(batch):
    return batch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()

    out_dir = WORK_DIR / "features"
    out_dir.mkdir(parents=True, exist_ok=True)
    boxes = json.loads((WORK_DIR / "boxes.json").read_text())
    done = set()
    for part in sorted(out_dir.glob("part-*.npz")):
        done.update(np.load(part, allow_pickle=False)["photo_stems"].tolist())
    stems = sorted(set(boxes) - done)[:args.limit]
    next_part = len(list(out_dir.glob("part-*.npz")))
    print(f"{len(boxes)} photos with boxes, {len(done)} done, {len(stems)} to go", flush=True)
    if not stems:
        return

    extractor = FeatureExtractor()
    labels = open(speciesnet_path().replace(".pt", ".labels.20260609.txt"), encoding="utf-8").read().splitlines()
    california = next(i for i, line in enumerate(labels) if ";aphelocoma;californica;" in line)

    loader = torch.utils.data.DataLoader(PhotoCrops(args.photos, stems, boxes), batch_size=8, num_workers=args.workers,
                                         collate_fn=identity, prefetch_factor=4 if args.workers else None)
    buf = {k: [] for k in ("stem", "category", "conf", "bbox", "feature", "california")}
    photo_stems = []
    started, seen = time.time(), 0

    def flush():
        nonlocal next_part
        if not photo_stems:
            return
        tmp = out_dir / f"tmp-{next_part:05d}.npz"
        np.savez(tmp, photo_stems=np.array(photo_stems), stem=np.array(buf["stem"]), category=np.array(buf["category"], dtype=np.int8),
                 conf=np.array(buf["conf"], dtype=np.float32), bbox=np.array(buf["bbox"], dtype=np.float32),
                 feature=np.concatenate(buf["feature"]).astype(np.float32), california=np.array(buf["california"], dtype=np.float32))
        os.replace(tmp, out_dir / f"part-{next_part:05d}.npz")
        next_part += 1
        photo_stems.clear()
        for v in buf.values():
            v.clear()

    for batch in loader:
        good = [b for b in batch if b[1] is not None]
        if good:
            feats, logits = extractor(np.concatenate([b[2] for b in good]), args.batch)
            probs = torch.softmax(torch.from_numpy(logits), dim=-1)[:, california].numpy()
            buf["feature"].append(feats)
            buf["california"].extend(probs.tolist())
            for stem, items, _ in good:
                for category, conf, bbox in items:
                    buf["stem"].append(stem); buf["category"].append(category); buf["conf"].append(conf); buf["bbox"].append(bbox)
                photo_stems.append(stem)
        seen += len(batch)
        if seen % 400 < len(batch):
            print(f"{seen}/{len(stems)} photos, {seen / (time.time() - started):.1f}/s", flush=True)
        if len(photo_stems) >= PART_SIZE:
            flush()
    flush()
    print("done", flush=True)


if __name__ == "__main__":
    main()
