"""Skin retouching: blemish removal, natural smoothing, tone correction."""

import cv2
import numpy as np


def detect_skin_mask(image: np.ndarray) -> np.ndarray:
    """Detect skin regions using HSV and YCrCb color spaces."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    ycrcb = cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)

    hsv_mask = cv2.inRange(hsv, np.array([0, 30, 60]), np.array([25, 170, 255]))
    hsv_mask2 = cv2.inRange(hsv, np.array([165, 30, 60]), np.array([180, 170, 255]))
    hsv_mask = cv2.bitwise_or(hsv_mask, hsv_mask2)

    ycrcb_mask = cv2.inRange(ycrcb, np.array([0, 135, 85]), np.array([255, 180, 135]))

    combined = cv2.bitwise_and(hsv_mask, ycrcb_mask)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, kernel, iterations=2)
    combined = cv2.morphologyEx(combined, cv2.MORPH_OPEN, kernel, iterations=1)

    combined = cv2.GaussianBlur(combined, (9, 9), 0)
    return combined


def smooth_skin(image: np.ndarray, skin_mask: np.ndarray, strength: float = 0.5) -> np.ndarray:
    """
    Apply frequency-separation-based skin smoothing.
    Preserves texture (high frequency) while smoothing color/tone (low frequency).
    """
    strength = np.clip(strength, 0.0, 1.0)
    if strength == 0:
        return image.copy()

    sigma = int(10 + strength * 20)
    if sigma % 2 == 0:
        sigma += 1

    img_float = image.astype(np.float64)

    low_freq = cv2.GaussianBlur(img_float, (0, 0), sigma)

    high_freq = img_float - low_freq + 128.0

    bilateral = cv2.bilateralFilter(
        image, d=9, sigmaColor=50 + int(strength * 30), sigmaSpace=50 + int(strength * 30)
    )
    bilateral_float = bilateral.astype(np.float64)

    low_freq_smoothed = cv2.GaussianBlur(bilateral_float, (0, 0), sigma)
    recombined = low_freq_smoothed + high_freq - 128.0
    recombined = np.clip(recombined, 0, 255).astype(np.uint8)

    mask_norm = skin_mask.astype(np.float64) / 255.0
    mask_norm = mask_norm * strength
    mask_3ch = np.stack([mask_norm] * 3, axis=-1)

    result = (image.astype(np.float64) * (1.0 - mask_3ch) + recombined.astype(np.float64) * mask_3ch)
    return np.clip(result, 0, 255).astype(np.uint8)


def remove_blemishes(image: np.ndarray, skin_mask: np.ndarray, sensitivity: float = 0.5) -> np.ndarray:
    """Remove small blemishes/spots from skin areas using median filtering + inpainting."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    blurred = cv2.GaussianBlur(gray, (15, 15), 0)
    diff = cv2.absdiff(gray, blurred)

    threshold = int(20 - sensitivity * 10)
    _, blemish_mask = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)

    blemish_mask = cv2.bitwise_and(blemish_mask, skin_mask)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    blemish_mask = cv2.morphologyEx(blemish_mask, cv2.MORPH_OPEN, kernel)
    blemish_mask = cv2.dilate(blemish_mask, kernel, iterations=1)

    contours, _ = cv2.findContours(blemish_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filtered_mask = np.zeros_like(blemish_mask)
    max_area = (image.shape[0] * image.shape[1]) * 0.002
    for c in contours:
        area = cv2.contourArea(c)
        if 5 < area < max_area:
            cv2.drawContours(filtered_mask, [c], -1, 255, -1)

    if np.sum(filtered_mask) > 0:
        result = cv2.inpaint(image, filtered_mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)
    else:
        result = image.copy()

    return result


def correct_skin_tone(image: np.ndarray, skin_mask: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """Subtle skin tone evening using LAB color space."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB).astype(np.float64)
    mask_bool = skin_mask > 128

    if not np.any(mask_bool):
        return image.copy()

    for ch in [1, 2]:
        channel = lab[:, :, ch]
        skin_mean = np.mean(channel[mask_bool])
        deviation = channel - skin_mean
        correction = deviation * strength
        channel_corrected = channel - correction
        lab[:, :, ch] = np.where(
            np.stack([mask_bool], axis=-1).squeeze(-1) if mask_bool.ndim == 2 else mask_bool,
            channel_corrected, channel
        )

    lab = np.clip(lab, 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
