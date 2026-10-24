import cv2
from ultralytics import YOLO

video_path = "data/Crowd-Activity-All.avi"
model = YOLO("yolov8s.pt")
cap = cv2.VideoCapture(video_path)

if not cap.isOpened():
    print("ERROR: Could not open video.")
    raise SystemExit(1)

print("Testing YOLOv8s on crowd video...")
print("Press q in the video window to quit.")

while True:
    success, frame = cap.read()
    if not success:
        break

    results = model(frame, classes=[0], conf=0.5, verbose=False)
    output = results[0].plot(conf=False)
    count = len(results[0].boxes)

    cv2.putText(
        output, f"People detected: {count}",
        (20, 35), cv2.FONT_HERSHEY_SIMPLEX,
        0.8, (0, 255, 0), 2
    )

    cv2.imshow("YOLOv8s - Crowd Video Test", output)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()
print("Video test finished.")
