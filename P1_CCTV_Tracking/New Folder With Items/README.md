# P1 CCTV Tracking

Reusable person detection, multi-object tracking, pose estimation,
best-effort face blurring, and a simple crowd-threshold warning.

## Setup
Use Python 3.11 or 3.12 in a virtual environment, then install:
pip install -r requirements.txt

The included YOLO pose model is in models/yolov8n-pose.pt.

## Webcam
python cctv_tracking.py

## Recorded video
python cctv_tracking.py --source "/absolute/path/to/video.mp4"

## Authorized CCTV stream
python cctv_tracking.py --source "rtsp://YOUR_CAMERA_STREAM"

## Configure crowd warning
python cctv_tracking.py --threshold 6 --frames 10

## Faster mode without face blurring
python cctv_tracking.py --no-blur

Blue boxes = detected people; IDs = tracker assignments;
green lines = estimated pose keypoints.
Face detection can miss faces and is not guaranteed anonymization.
The crowd warning is a fixed threshold, not a crowd-crush prediction.
