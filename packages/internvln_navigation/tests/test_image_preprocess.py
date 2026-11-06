from __future__ import annotations

import unittest

import numpy as np

from internvln_navigation.image_preprocess import (
    prepare_center_crop_height,
    prepare_full_frame,
    prepare_model_input,
)


class FullFrameTests(unittest.TestCase):
    """The default framing: send what the camera gave us, crop nothing."""

    def test_the_frame_is_passed_through_untouched(self) -> None:
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        image[0, 0] = (1, 2, 3)
        sent, geometry = prepare_full_frame(image)
        self.assertIs(sent, image)  # no copy, no resize: nothing was changed
        self.assertEqual(geometry, (640, 480, 0, 0))

    def test_an_invalid_frame_is_still_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "HxWx3"):
            prepare_full_frame(np.zeros((480, 640), dtype=np.uint8))

    def test_the_dispatcher_uses_the_configured_mode(self) -> None:
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        sent, geometry = prepare_model_input(image, mode="full_frame")
        self.assertEqual(geometry, (640, 480, 0, 0))

        cropped, geometry = prepare_model_input(
            image, mode="center_crop_height", crop_size=384
        )
        self.assertEqual(geometry, (512, 384, 64, 0))
        self.assertEqual(cropped.shape, (384, 384, 3))

    def test_an_unknown_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown model_input_mode"):
            prepare_model_input(np.zeros((4, 4, 3), dtype=np.uint8), mode="stretch")


class OfficialImagePreprocessTests(unittest.TestCase):
    def test_four_by_three_frame_becomes_centered_384_square(self) -> None:
        image = np.zeros((480, 640, 3), dtype=np.uint8)
        crop, geometry = prepare_center_crop_height(image)
        self.assertEqual(geometry, (512, 384, 64, 0))
        self.assertEqual(crop.shape, (384, 384, 3))
        self.assertTrue(crop.flags.c_contiguous)

    def test_portrait_frame_scales_width_first_then_crops_height(self) -> None:
        image = np.zeros((480, 320, 3), dtype=np.uint8)
        crop, geometry = prepare_center_crop_height(image)
        self.assertEqual(geometry, (384, 576, 0, 96))
        self.assertEqual(crop.shape, (384, 384, 3))

    def test_invalid_frame_or_crop_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "HxWx3"):
            prepare_center_crop_height(np.zeros((480, 640), dtype=np.uint8))
        with self.assertRaisesRegex(ValueError, "positive"):
            prepare_center_crop_height(
                np.zeros((10, 10, 3), dtype=np.uint8),
                crop_size=0,
            )


if __name__ == "__main__":
    unittest.main()
