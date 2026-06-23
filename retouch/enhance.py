"""Global image enhancement: contrast, color balance, exposure, sharpness."""

import cv2
import numpy as np


def enhance_contrast(image: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """Improve contrast using CLAHE (Contrast Limited Adaptive Histogram Equalization)."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l_channel = lab[:, :, 0]

    clip_limit = 1.5 + strength * 2.0
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(8, 8))
    enhanced_l = clahe.apply(l_channel)

    blend = cv2.addWeighted(l_channel, 1.0 - strength, enhanced_l, strength, 0)
    lab[:, :, 0] = blend

    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def enhance_color_balance(image: np.ndarray, strength: float = 0.2) -> np.ndarray:
    """Subtle color balance improvement via white balance correction."""
    result = image.astype(np.float64)

    for i in range(3):
        channel = result[:, :, i]
        low = np.percentile(channel, 1)
        high = np.percentile(channel, 99)
        if high - low > 0:
            stretched = (channel - low) / (high - low) * 255.0
            result[:, :, i] = channel * (1.0 - strength) + stretched * strength

    return np.clip(result, 0, 255).astype(np.uint8)


def enhance_exposure(image: np.ndarray, strength: float = 0.2) -> np.ndarray:
    """Improve exposure by lifting shadows and recovering highlights."""
    img_float = image.astype(np.float64) / 255.0

    shadows = np.power(img_float, 0.85)
    highlights = np.power(img_float, 1.15)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float64) / 255.0
    gray_3ch = np.stack([gray] * 3, axis=-1)

    shadow_weight = (1.0 - gray_3ch) * strength
    highlight_weight = gray_3ch * strength * 0.5

    result = img_float + (shadows - img_float) * shadow_weight - (img_float - highlights) * highlight_weight

    return (np.clip(result, 0, 1) * 255).astype(np.uint8)


def enhance_sharpness(image: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """Apply gentle unsharp mask for overall sharpness."""
    gaussian = cv2.GaussianBlur(image, (0, 0), 2.0)
    amount = 0.3 + strength * 0.4
    sharpened = cv2.addWeighted(image, 1.0 + amount, gaussian, -amount, 0)
    return np.clip(sharpened, 0, 255).astype(np.uint8)


def enhance_eyes(image: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """Enhance eye brightness and clarity using face/eye detection."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")

    faces = face_cascade.detectMultiScale(gray, 1.3, 5)
    result = image.copy()

    for (x, y, w, h) in faces:
        roi_gray = gray[y:y + h, x:x + w]
        roi_color = result[y:y + h, x:x + w]

        eyes = eye_cascade.detectMultiScale(roi_gray, 1.1, 4)
        for (ex, ey, ew, eh) in eyes:
            eye_roi = roi_color[ey:ey + eh, ex:ex + ew]

            lab = cv2.cvtColor(eye_roi, cv2.COLOR_BGR2LAB).astype(np.float64)
            lab[:, :, 0] = np.clip(lab[:, :, 0] + strength * 15, 0, 255)

            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
            lab[:, :, 0] = clahe.apply(lab[:, :, 0].astype(np.uint8)).astype(np.float64)

            enhanced_eye = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2BGR)

            mask = np.zeros((eh, ew), dtype=np.float64)
            cv2.ellipse(mask, (ew // 2, eh // 2), (ew // 3, eh // 3), 0, 0, 360, 1.0, -1)
            mask = cv2.GaussianBlur(mask, (5, 5), 0) * strength
            mask_3ch = np.stack([mask] * 3, axis=-1)

            blended = eye_roi.astype(np.float64) * (1.0 - mask_3ch) + enhanced_eye.astype(np.float64) * mask_3ch
            roi_color[ey:ey + eh, ex:ex + ew] = np.clip(blended, 0, 255).astype(np.uint8)

    return result


def add_studio_polish(image: np.ndarray, strength: float = 0.2) -> np.ndarray:
    """Add subtle high-end editorial polish: vignette + micro-contrast."""
    h, w = image.shape[:2]
    Y, X = np.ogrid[:h, :w]
    cx, cy = w / 2, h / 2
    distance = np.sqrt((X - cx) ** 2 + (Y - cy) ** 2)
    max_dist = np.sqrt(cx ** 2 + cy ** 2)
    vignette = 1.0 - (distance / max_dist) ** 2 * strength * 0.4
    vignette = np.stack([vignette] * 3, axis=-1)

    result = image.astype(np.float64) * vignette
    result = np.clip(result, 0, 255).astype(np.uint8)

    lab = cv2.cvtColor(result, cv2.COLOR_BGR2LAB).astype(np.float64)
    l_channel = lab[:, :, 0]
    local_mean = cv2.GaussianBlur(l_channel, (0, 0), 15)
    micro_contrast = (l_channel - local_mean) * strength * 0.3
    lab[:, :, 0] = np.clip(l_channel + micro_contrast, 0, 255)
    result = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2BGR)

    return result
