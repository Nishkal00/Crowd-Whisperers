import cv2
from ultralytics import YOLO

model = YOLO("yolov8s.pt")

videos = [
    "data/Crowd-Activity-All.avi",
    "data/AbnormalCrowdActivity-Mall.avi",
]

for video_path in videos:
    cap = cv2.VideoCapture(video_path)

    if not cap.isOpened():
        print(f"Could not open: {video_path}")
        continue

    print(f"Testing: {video_path}")
    print("Press q to skip to the next video, or close the window to stop.")

    while True:
        success, frame = cap.read()

        if not success:
            break

        results = model(
            frame,
            classes=[0],
            conf=0.5,
            verbose=False
        )

        # Draw boxes only: no class names, confidence scores, or count
        output = results[0].plot(labels=False, conf=False)

        cv2.imshow("Crowd Whisperers - Video Test", output)

        if cv2.waitKey(25) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()

cv2.destroyAllWindows()
print("Finished testing both videos.")
