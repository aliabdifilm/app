"""Background cleanup and enhancement."""

import cv2
import numpy as np


def detect_background_mask(image: np.ndarray) -> np.ndarray:
    """Detect background regions using GrabCut with edge-based initialization."""
    h, w = image.shape[:2]
    mask = np.zeros((h, w), np.uint8)

    border = int(min(h, w) * 0.05)
    rect = (border, border, w - 2 * border, h - 2 * border)

    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)

    small = cv2.resize(image, (min(w, 400), min(h, 400)))
    small_mask = np.zeros(small.shape[:2], np.uint8)
    sh, sw = small.shape[:2]
    small_border = int(min(sh, sw) * 0.05)
    small_rect = (small_border, small_border, sw - 2 * small_border, sh - 2 * small_border)

    try:
        cv2.grabCut(small, small_mask, small_rect, bgd_model, fgd_model, 3, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return np.zeros((h, w), np.uint8)

    bg_mask_small = np.where((small_mask == cv2.GC_BGD) | (small_mask == cv2.GC_PR_BGD), 255, 0).astype(np.uint8)
    mask = cv2.resize(bg_mask_small, (w, h), interpolation=cv2.INTER_LINEAR)
    _, mask = cv2.threshold(mask, 128, 255, cv2.THRESH_BINARY)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.GaussianBlur(mask, (15, 15), 0)

    return mask


def clean_background(image: np.ndarray, bg_mask: np.ndarray, strength: float = 0.5) -> np.ndarray:
    """Smooth and clean background while keeping it realistic."""
    if np.sum(bg_mask) == 0:
        return image.copy()

    sigma = int(5 + strength * 15)
    if sigma % 2 == 0:
        sigma += 1

    smoothed = cv2.GaussianBlur(image, (sigma, sigma), 0)

    bilateral = cv2.bilateralFilter(image, d=9, sigmaColor=75, sigmaSpace=75)

    bg_clean = cv2.addWeighted(smoothed, 0.4, bilateral, 0.6, 0)

    mask_norm = bg_mask.astype(np.float64) / 255.0
    mask_norm *= strength
    mask_3ch = np.stack([mask_norm] * 3, axis=-1)

    result = image.astype(np.float64) * (1.0 - mask_3ch) + bg_clean.astype(np.float64) * mask_3ch
    return np.clip(result, 0, 255).astype(np.uint8)


def remove_background_distractions(image: np.ndarray, bg_mask: np.ndarray) -> np.ndarray:
    """Remove small distracting objects from the background."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.bitwise_and(edges, bg_mask)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    dilated = cv2.dilate(edges, kernel, iterations=2)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    distraction_mask = np.zeros_like(bg_mask)

    img_area = image.shape[0] * image.shape[1]
    for c in contours:
        area = cv2.contourArea(c)
        if 50 < area < img_area * 0.005:
            cv2.drawContours(distraction_mask, [c], -1, 255, -1)

    if np.sum(distraction_mask) > 0:
        result = cv2.inpaint(image, distraction_mask, inpaintRadius=5, flags=cv2.INPAINT_TELEA)
    else:
        result = image.copy()

    return result
