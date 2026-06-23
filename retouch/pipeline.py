"""Main retouching pipeline that orchestrates all processing steps."""

import cv2
import numpy as np

from .skin import detect_skin_mask, smooth_skin, remove_blemishes, correct_skin_tone
from .background import detect_background_mask, clean_background, remove_background_distractions
from .clothing import detect_clothing_mask, remove_lint_and_dust, smooth_clothing_wrinkles, sharpen_clothing
from .enhance import (
    enhance_contrast,
    enhance_color_balance,
    enhance_exposure,
    enhance_sharpness,
    enhance_eyes,
    add_studio_polish,
)


class RetouchPipeline:
    """Full automatic portrait retouching pipeline."""

    def __init__(
        self,
        skin_smoothing: float = 0.5,
        blemish_removal: float = 0.5,
        skin_tone: float = 0.3,
        background_clean: float = 0.5,
        clothing_lint: float = 0.5,
        clothing_wrinkles: float = 0.3,
        clothing_sharpness: float = 0.3,
        contrast: float = 0.3,
        color_balance: float = 0.2,
        exposure: float = 0.2,
        sharpness: float = 0.3,
        eye_enhance: float = 0.3,
        studio_polish: float = 0.2,
    ):
        self.skin_smoothing = skin_smoothing
        self.blemish_removal = blemish_removal
        self.skin_tone = skin_tone
        self.background_clean = background_clean
        self.clothing_lint = clothing_lint
        self.clothing_wrinkles = clothing_wrinkles
        self.clothing_sharpness = clothing_sharpness
        self.contrast = contrast
        self.color_balance = color_balance
        self.exposure = exposure
        self.sharpness = sharpness
        self.eye_enhance = eye_enhance
        self.studio_polish = studio_polish

    def process(self, image: np.ndarray, progress_callback=None) -> np.ndarray:
        """Run the full retouching pipeline on an image."""
        steps = [
            ("Detecting skin regions", self._step_detect_skin),
            ("Removing blemishes", self._step_blemishes),
            ("Smoothing skin", self._step_smooth_skin),
            ("Correcting skin tone", self._step_skin_tone),
            ("Detecting background", self._step_detect_background),
            ("Cleaning background", self._step_clean_background),
            ("Detecting clothing", self._step_detect_clothing),
            ("Cleaning clothing", self._step_clean_clothing),
            ("Enhancing contrast", self._step_contrast),
            ("Enhancing color balance", self._step_color),
            ("Improving exposure", self._step_exposure),
            ("Enhancing eyes", self._step_eyes),
            ("Sharpening", self._step_sharpen),
            ("Adding studio polish", self._step_polish),
        ]

        self._current_image = image.copy()
        self._skin_mask = None
        self._bg_mask = None
        self._clothing_mask = None

        for i, (name, step_fn) in enumerate(steps):
            if progress_callback:
                progress_callback(name, i + 1, len(steps))
            step_fn()

        result = self._current_image
        self._current_image = None
        self._skin_mask = None
        self._bg_mask = None
        self._clothing_mask = None

        return result

    def _step_detect_skin(self):
        self._skin_mask = detect_skin_mask(self._current_image)

    def _step_blemishes(self):
        if self.blemish_removal > 0 and self._skin_mask is not None:
            self._current_image = remove_blemishes(
                self._current_image, self._skin_mask, self.blemish_removal
            )

    def _step_smooth_skin(self):
        if self.skin_smoothing > 0 and self._skin_mask is not None:
            self._current_image = smooth_skin(
                self._current_image, self._skin_mask, self.skin_smoothing
            )

    def _step_skin_tone(self):
        if self.skin_tone > 0 and self._skin_mask is not None:
            self._current_image = correct_skin_tone(
                self._current_image, self._skin_mask, self.skin_tone
            )

    def _step_detect_background(self):
        self._bg_mask = detect_background_mask(self._current_image)

    def _step_clean_background(self):
        if self.background_clean > 0 and self._bg_mask is not None:
            self._current_image = remove_background_distractions(
                self._current_image, self._bg_mask
            )
            self._current_image = clean_background(
                self._current_image, self._bg_mask, self.background_clean
            )

    def _step_detect_clothing(self):
        self._clothing_mask = detect_clothing_mask(self._current_image)

    def _step_clean_clothing(self):
        if self._clothing_mask is not None:
            if self.clothing_lint > 0:
                self._current_image = remove_lint_and_dust(
                    self._current_image, self._clothing_mask, self.clothing_lint
                )
            if self.clothing_wrinkles > 0:
                self._current_image = smooth_clothing_wrinkles(
                    self._current_image, self._clothing_mask, self.clothing_wrinkles
                )
            if self.clothing_sharpness > 0:
                self._current_image = sharpen_clothing(
                    self._current_image, self._clothing_mask, self.clothing_sharpness
                )

    def _step_contrast(self):
        if self.contrast > 0:
            self._current_image = enhance_contrast(self._current_image, self.contrast)

    def _step_color(self):
        if self.color_balance > 0:
            self._current_image = enhance_color_balance(self._current_image, self.color_balance)

    def _step_exposure(self):
        if self.exposure > 0:
            self._current_image = enhance_exposure(self._current_image, self.exposure)

    def _step_eyes(self):
        if self.eye_enhance > 0:
            self._current_image = enhance_eyes(self._current_image, self.eye_enhance)

    def _step_sharpen(self):
        if self.sharpness > 0:
            self._current_image = enhance_sharpness(self._current_image, self.sharpness)

    def _step_polish(self):
        if self.studio_polish > 0:
            self._current_image = add_studio_polish(self._current_image, self.studio_polish)
