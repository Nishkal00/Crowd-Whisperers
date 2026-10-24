
import sys
import time
from pathlib import Path

# `streamlit run p2/demo.py` puts p2/ on the path, not the repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2
import numpy as np
import streamlit as st

from p2.density import analyze_zone
from p2.heatmap import draw_heatmap
from p2.prediction import CrowdPredictor


st.set_page_config(
    page_title="Crowd Whisperers | P2",
    page_icon="🚨",
    layout="wide",
)

st.title("🚨 Crowd Whisperers — P2")
st.subheader("Crowd Density • Heatmap • Time-to-Critical Prediction")
st.info(
    "Prototype demo using simulated zone counts. "
    "Live YOLO/CCTV input is not connected yet."
)

if "predictor" not in st.session_state:
    st.session_state.predictor = CrowdPredictor()
if "start_time" not in st.session_state:
    st.session_state.start_time = time.time()

st.sidebar.header("Demo controls")
area = st.sidebar.number_input(
    "Area per zone (m²)", min_value=1.0, value=10.0, step=1.0
)
gate1 = st.sidebar.slider("Gate 1: people", 0, 150, 30)
gate2 = st.sidebar.slider("Gate 2: people", 0, 150, 50)
gate3 = st.sidebar.slider("Gate 3: people", 0, 150, 70)

if st.sidebar.button("Reset prediction history"):
    st.session_state.predictor = CrowdPredictor()
    st.session_state.start_time = time.time()

counts = {
    "Gate 1": {"count": gate1, "area_m2": area},
    "Gate 2": {"count": gate2, "area_m2": area},
    "Gate 3": {"count": gate3, "area_m2": area},
}

results = {
    name: analyze_zone(data["count"], data["area_m2"])
    for name, data in counts.items()
}

now = time.time()
elapsed = now - st.session_state.start_time
predictions = {}

for name, result in results.items():
    predictions[name] = st.session_state.predictor.update(
        name, result["density"], now
    )

st.header("1. Crowd density by zone")
columns = st.columns(3)

for col, (name, result) in zip(columns, results.items()):
    with col:
        st.metric(name, f"{result['density']:.1f} people/m²")
        st.write(f"**Status: {result['status']}**")
        st.caption(f"{result['person_count']} people / {area:.1f} m²")

st.header("2. Crowd heatmap")

frame = np.full((420, 1000, 3), 55, dtype=np.uint8)
zones = {
    "Gate 1": (20, 60, 320, 380),
    "Gate 2": (350, 60, 650, 380),
    "Gate 3": (680, 60, 980, 380),
}

heatmap = draw_heatmap(frame, zones, results)
st.image(
    cv2.cvtColor(heatmap, cv2.COLOR_BGR2RGB),
    caption="Illustrative zone heatmap — not live camera footage",
    use_container_width=True,
)

st.header("3. Time-to-critical prediction")

for name, result in predictions.items():
    eta = result["minutes_to_critical"]
    if result["message"] == "CRITICAL NOW":
        st.error(f"{name}: already at the critical threshold.")
    elif eta is not None:
        st.write(
            f"**{name}:** estimated critical threshold in "
            f"{eta:.2f} minutes, if the recent trend continues."
        )
    else:
        st.write(f"**{name}:** {result['message']}")

st.caption(
    "Demo inputs are simulated. Density thresholds are configurable "
    "starting assumptions, not guarantees of safety."
)
