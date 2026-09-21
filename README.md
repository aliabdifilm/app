# Auto Retouch Pro

> **Note:** this repository also contains **[ApexAlgo](trading-bot/)** — an
> autonomous MetaTrader 5 trading bot with a mobile control panel. It is a
> separate, self-contained project under `trading-bot/`.


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
