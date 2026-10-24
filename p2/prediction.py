
"""P2: Estimate time until a crowd zone reaches critical density."""

from collections import defaultdict, deque

import numpy as np


CRITICAL_DENSITY = 6.0  # people per square metre


class CrowdPredictor:
    """Predict when a zone may reach the critical density threshold."""

    def __init__(self, history_seconds=180, minimum_samples=3, min_span_seconds=10,
                 critical_density=CRITICAL_DENSITY, max_minutes=30):
        self.history_seconds = history_seconds
        self.minimum_samples = minimum_samples
        # A trend fitted over a second or two of noisy counts is meaningless.
        self.min_span_seconds = min_span_seconds
        self.critical_density = critical_density
        # A tiny upward wobble projects to "critical in 16,000 minutes"; that's not a warning.
        self.max_minutes = max_minutes
        self.history = defaultdict(deque)

    def update(self, zone_id, density, timestamp):
        """Add a density measurement and estimate time to critical.

        timestamp is measured in seconds. Use time.time() in live code.
        """
        density = float(density)
        timestamp = float(timestamp)

        if density < 0:
            raise ValueError("Density cannot be negative.")

        samples = self.history[zone_id]

        # Ignore duplicate or out-of-order timestamps.
        if samples and timestamp <= samples[-1][0]:
            return self._result(density, None, "WAITING FOR DATA")

        samples.append((timestamp, density))

        cutoff = timestamp - self.history_seconds
        while samples and samples[0][0] < cutoff:
            samples.popleft()

        if density >= self.critical_density:
            return self._result(density, 0.0, "CRITICAL NOW")

        if len(samples) < self.minimum_samples:
            return self._result(density, None, "COLLECTING DATA")

        times = np.array([t for t, _ in samples], dtype=float)
        values = np.array([d for _, d in samples], dtype=float)

        elapsed = times - times[0]

        if elapsed[-1] <= 0 or elapsed[-1] < self.min_span_seconds:
            return self._result(density, None, "COLLECTING DATA")

        # Fit a straight line to recent density measurements.
        slope = float(np.polyfit(elapsed, values, 1)[0])

        if slope <= 0:
            return self._result(density, None, "NO INCREASING TREND")

        seconds_left = (self.critical_density - density) / slope

        if self.max_minutes is not None and seconds_left > self.max_minutes * 60:
            return self._result(density, None, "NO NEAR-TERM RISK")

        return self._result(
            density, seconds_left, "RISK INCREASING"
        )

    @staticmethod
    def _result(density, seconds_left, message):
        return {
            "density": round(density, 2),
            "seconds_to_critical": (
                round(seconds_left, 1)
                if seconds_left is not None
                else None
            ),
            "minutes_to_critical": (
                round(seconds_left / 60, 2)
                if seconds_left is not None
                else None
            ),
            "message": message,
        }


if __name__ == "__main__":
    predictor = CrowdPredictor()

    # Simulated measurements: density rises by 1 person/m² each minute.
    simulated_data = [
        (0, 2.0),
        (60, 3.0),
        (120, 4.0),
        (180, 5.0),
    ]

    print("P2 Time-to-Critical Prediction Demo\n")

    for timestamp, density in simulated_data:
        result = predictor.update(
            "Gate 1", density, timestamp
        )

        print(
            f"Time: {timestamp:>3}s | "
            f"Density: {density:.1f} people/m² | "
            f"Status: {result['message']} | "
            f"Minutes to critical: "
            f"{result['minutes_to_critical']}"
        )
