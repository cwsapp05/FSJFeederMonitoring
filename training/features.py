"""SpeciesNet's backbone as a feature extractor. Used by both training and the app's pipeline.

The features are the 1280 values before SpeciesNet's final layer, so we reuse what it learned about
animals without being limited to its label list (which has no Florida Scrub-Jay).
"""
import glob
import os

import numpy as np
import torch
import torch.nn.functional as TF
from PIL import Image, ImageOps

IMG_SIZE = 480
FEATURE_LAYER = "SpeciesNet/efficientnetv2-m/avg_pool/Mean_Squeeze__3825"
# The camera's info bar shows the date and camera ID. Blanking it stops a model memorizing those.
INFO_BAR_FRACTION = 0.045
FRAME = 0  # category code for the whole-photo "box"; 1, 2, 3 are MegaDetector's animal, person, vehicle


def speciesnet_path():
    pattern = os.path.expanduser("~/.cache/kagglehub/models/google/speciesnet/pyTorch/*/1/always_crop_*.pt")
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise FileNotFoundError(f"SpeciesNet model not found at {pattern}. Run the app once so it downloads.")
    return paths[-1]


class FeatureExtractor:
    def __init__(self, device=None):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = torch.load(speciesnet_path(), map_location=self.device, weights_only=False).eval()
        for p in self.model.parameters():
            p.requires_grad = False
        self._features = None
        dict(self.model.named_modules())[FEATURE_LAYER].register_forward_hook(self._keep)

    def _keep(self, _module, _inputs, output):
        self._features = output

    @torch.inference_mode()
    def __call__(self, crops, batch_size=16):
        """crops: uint8 array (N, 480, 480, 3). Returns (features N x 1280, logits N x labels)."""
        features, logits = [], []
        for i in range(0, len(crops), batch_size):
            x = torch.from_numpy(crops[i:i + batch_size]).to(self.device).float() / 255.0
            logits.append(self.model(x).cpu())
            features.append(self._features.cpu())
        return torch.cat(features).numpy(), torch.cat(logits).numpy()


def load_photo(path):
    """The photo as a CHW uint8 tensor, upright and with the info bar blacked out."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        arr = np.array(im)
    arr[int(arr.shape[0] * (1 - INFO_BAR_FRACTION)):] = 0
    return torch.from_numpy(arr).permute(2, 0, 1)


def crop_box(photo, bbox):
    """Crop a normalized [x, y, w, h] box and resize it the way SpeciesNet does. Returns HWC uint8."""
    _, h, w = photo.shape
    x0, y0 = max(int(bbox[0] * w), 0), max(int(bbox[1] * h), 0)
    x1, y1 = min(x0 + max(int(bbox[2] * w), 1), w), min(y0 + max(int(bbox[3] * h), 1), h)
    crop = photo[:, y0:y1, x0:x1].unsqueeze(0).float()
    crop = TF.interpolate(crop, size=(IMG_SIZE, IMG_SIZE), mode="bilinear", antialias=False, align_corners=False)
    return crop[0].round().clamp(0, 255).byte().permute(1, 2, 0).numpy()


def frame_box():
    """The whole photo above the info bar."""
    return [0.0, 0.0, 1.0, 1.0 - INFO_BAR_FRACTION]
