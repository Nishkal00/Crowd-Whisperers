import cv2
from ultralytics import YOLO

# Load pretrained models
person_model = YOLO("models/yolov8n.pt")
pose_model = YOLO("models/yolov8n-pose.pt")

# OpenCV face detector
face_detector = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)

# Project settings
CROWD_THRESHOLD = 6
CONFIDENCE_THRESHOLD = 0.5
REQUIRED_FRAMES = 10
crowd_frames = 0

# Open webcam
cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("Cannot open webcam. Check camera permissions.")
    raise SystemExit(1)

print("P1 running: person detection, pose estimation, face blur and crowd monitoring.")
print("Press q in the video window to quit.")

while True:
    success, frame = cap.read()
    if not success:
        break

    height, width = frame.shape[:2]

    # Define the monitored zone
    zone_left = int(width * 0.2)
    zone_right = int(width * 0.8)
    zone_top = int(height * 0.2)
    zone_bottom = int(height * 0.9)

    # Detect people
    people = person_model(
        frame,
        classes=[0],
        conf=CONFIDENCE_THRESHOLD,
        verbose=False
    )[0]

    # Count people inside the monitored zone
    people_count = 0

    for box in people.boxes:
        x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2

        if (zone_left <= center_x <= zone_right and
                zone_top <= center_y <= zone_bottom):
            people_count += 1

    # Require the condition to persist across frames
    if people_count >= CROWD_THRESHOLD:
        crowd_frames += 1
    else:
        crowd_frames = 0

    if crowd_frames >= REQUIRED_FRAMES:
        status = "CROWD THRESHOLD REACHED"
        color = (0, 0, 255)
    else:
        status = "Crowd threshold not reached"
        color = (0, 255, 0)

    # Estimate body poses
    poses = pose_model(frame, verbose=False)[0]
    output = poses.plot()

    # Detect and blur faces
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = face_detector.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(30, 30)
    )

    for (x, y, w, h) in faces:
        face = output[y:y+h, x:x+w]
        if face.size:
            output[y:y+h, x:x+w] = cv2.GaussianBlur(
                face, (51, 51), 30
            )

    # Draw person detection boxes
    output = people.plot(img=output)

    # Draw monitored zone
    cv2.rectangle(
        output,
        (zone_left, zone_top),
        (zone_right, zone_bottom),
        (255, 255, 0),
        2
    )

    # Display crowd status without showing the raw count
    cv2.putText(
        output,
        status,
        (20, 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        color,
        2
    )

    cv2.imshow("P1 - Crowd Whisperers", output)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()
