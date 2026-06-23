"""Clothing enhancement: lint removal, wrinkle smoothing, sharpness improvement."""

import cv2
import numpy as np

from .skin import detect_skin_mask
from .background import detect_background_mask


def detect_clothing_mask(image: np.ndarray) -> np.ndarray:
    """Detect clothing regions (non-skin, non-background areas)."""
    skin_mask = detect_skin_mask(image)
    bg_mask = detect_background_mask(image)

    clothing_mask = cv2.bitwise_not(cv2.bitwise_or(skin_mask, bg_mask))

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    clothing_mask = cv2.morphologyEx(clothing_mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    clothing_mask = cv2.morphologyEx(clothing_mask, cv2.MORPH_OPEN, kernel, iterations=1)

    return clothing_mask


def remove_lint_and_dust(image: np.ndarray, clothing_mask: np.ndarray, sensitivity: float = 0.5) -> np.ndarray:
    """Remove lint, dust, and small particles from clothing."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    median = cv2.medianBlur(gray, 5)
    diff = cv2.absdiff(gray, median)

    threshold = int(25 - sensitivity * 12)
    _, particle_mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)

    particle_mask = cv2.bitwise_and(particle_mask, clothing_mask)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    particle_mask = cv2.morphologyEx(particle_mask, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(particle_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    clean_mask = np.zeros_like(particle_mask)
    max_area = (image.shape[0] * image.shape[1]) * 0.001
    for c in contours:
        area = cv2.contourArea(c)
        if 3 < area < max_area:
            cv2.drawContours(clean_mask, [c], -1, 255, -1)

    if np.sum(clean_mask) > 0:
        result = cv2.inpaint(image, clean_mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)
    else:
        result = image.copy()

    return result


def smooth_clothing_wrinkles(image: np.ndarray, clothing_mask: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """Subtly smooth minor wrinkles in clothing while preserving texture."""
    bilateral = cv2.bilateralFilter(image, d=7, sigmaColor=40, sigmaSpace=40)

    mask_norm = clothing_mask.astype(np.float64) / 255.0
    mask_norm *= strength * 0.5
    mask_3ch = np.stack([mask_norm] * 3, axis=-1)

    result = image.astype(np.float64) * (1.0 - mask_3ch) + bilateral.astype(np.float64) * mask_3ch
    return np.clip(result, 0, 255).astype(np.uint8)


def sharpen_clothing(image: np.ndarray, clothing_mask: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """Sharpen clothing details to improve texture appearance."""
    blurred = cv2.GaussianBlur(image, (0, 0), 3)
    sharpened = cv2.addWeighted(image, 1.0 + strength * 0.5, blurred, -strength * 0.5, 0)

    mask_norm = clothing_mask.astype(np.float64) / 255.0
    mask_3ch = np.stack([mask_norm] * 3, axis=-1)

    result = image.astype(np.float64) * (1.0 - mask_3ch) + sharpened.astype(np.float64) * mask_3ch
    return np.clip(result, 0, 255).astype(np.uint8)
