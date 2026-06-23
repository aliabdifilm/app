# Auto Retouch Pro

Professional automatic portrait retouching web application.

## Features

- **Skin Retouching**: Blemish removal, natural smoothing, tone correction
- **Clothing Enhancement**: Lint/dust removal, wrinkle smoothing, sharpness improvement
- **Background Cleanup**: Distraction removal, smoothing, consistency
- **Global Enhancement**: Contrast, color balance, exposure, sharpness, eye enhancement, studio polish
- **Before/After Comparison**: Interactive slider comparison view

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000 in your browser.

## Tech Stack

- **Backend**: Python, Flask, OpenCV, Pillow, NumPy, SciPy
- **Frontend**: HTML5, CSS3, JavaScript (vanilla)
- **Processing**: Frequency separation, bilateral filtering, CLAHE, GrabCut segmentation, inpainting
