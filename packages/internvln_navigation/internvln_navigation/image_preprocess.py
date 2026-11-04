"""What the robot sends to the DualVLN server as "the camera sees this".

``full_frame`` is the default: send the camera frame as it came, and let the
server resize it to ``--resize_w 384 --resize_h 384``.  That is what the
deployment document's own client does (its example posts the frame straight
from the camera) and what the Go2 skill does (the primitive's ``snapshot``
tool, no local geometry at all).  The server's ``--resize_w/h`` is documented
as "输入图像缩放" -- a resize, with no crop of ours to mirror.

``center_crop_height`` is the older mode: resize the long edge to 384 and
centre-crop a 384x384 square locally.  It was written on the belief that the
server cropped too; the deployment document does not say that, and no upstream
client does it.  Kept because it is the only other framing that has ever been
run, and switching framing is exactly the kind of silent change that has
already cost this project two debugging sessions.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from .core import (
    MODEL_CROP_SIZE,
    MODEL_INPUT_MODE,
    MODEL_INPUT_MODE_CENTER_CROP,
)


def prepare_center_crop_height(
    image: Any,
    *,
    crop_size: int = MODEL_CROP_SIZE,
) -> tuple[Any, tuple[int, int, int, int]]:
    """Resize to ``crop_size`` high, then center-crop a square.

    This mirrors OpenBMB's Go2 client preprocessing. Color order is irrelevant
    to the geometry, so the ROS bridge can apply it directly to a BGR frame
    before JPEG encoding.
    """

    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("expected an HxWx3 image")
    source_height, source_width = image.shape[:2]
    size = int(crop_size)
    if source_height <= 0 or source_width <= 0:
        raise ValueError("image dimensions must be positive")
    if size <= 0:
        raise ValueError("crop size must be positive")

    target_height = size
    target_width = max(
        1,
        int(round(source_width * (target_height / float(source_height)))),
    )
    if target_width < size:
        target_width = size
        target_height = max(
            1,
            int(round(source_height * (target_width / float(source_width)))),
        )
    resized = cv2.resize(
        image,
        (target_width, target_height),
        interpolation=cv2.INTER_AREA,
    )
    if target_width < size or target_height < size:
        raise ValueError(
            f"resized frame {target_width}x{target_height} is smaller than "
            f"crop {size}x{size}"
        )
    x_offset = (target_width - size) // 2
    y_offset = (target_height - size) // 2
    crop = np.ascontiguousarray(
        resized[
            y_offset : y_offset + size,
            x_offset : x_offset + size,
        ]
    )
    return crop, (target_width, target_height, x_offset, y_offset)


def prepare_full_frame(image: Any) -> tuple[Any, tuple[int, int, int, int]]:
    """Leave the frame alone; the server does the resizing.

    Geometry is reported as ``(width, height, 0, 0)`` so the status topic can
    still say what was sent without a second code path.
    """

    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("expected an HxWx3 image")
    source_height, source_width = image.shape[:2]
    if source_height <= 0 or source_width <= 0:
        raise ValueError("image dimensions must be positive")
    return image, (source_width, source_height, 0, 0)


def prepare_model_input(
    image: Any,
    *,
    mode: str = MODEL_INPUT_MODE,
    crop_size: int = MODEL_CROP_SIZE,
) -> tuple[Any, tuple[int, int, int, int]]:
    """Dispatch on the configured ``model_input_mode``."""

    if mode == MODEL_INPUT_MODE:
        return prepare_full_frame(image)
    if mode == MODEL_INPUT_MODE_CENTER_CROP:
        return prepare_center_crop_height(image, crop_size=crop_size)
    raise ValueError(f"unknown model_input_mode {mode!r}")
