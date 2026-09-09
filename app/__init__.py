# Cric-Lab backend package
import os

# Headless OpenCV for POST /balltrack/detect-stumps (still photo, not the video pipeline).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
