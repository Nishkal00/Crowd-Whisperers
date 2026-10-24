
"""P2: Crowd density calculations and zone classification."""

SAFE_LIMIT = 4.0
CRITICAL_LIMIT = 6.0


def calculate_density(person_count: int, area_m2: float) -> float:
    """Calculate people per square metre."""
    if person_count < 0:
        raise ValueError("Person count cannot be negative.")

    if area_m2 <= 0:
        raise ValueError("Area must be greater than zero.")

    return person_count / area_m2


def classify_density(density: float) -> str:
    """Classify a zone using the hackathon's proposed thresholds."""
    if density < SAFE_LIMIT:
        return "SAFE"

    if density < CRITICAL_LIMIT:
        return "WARNING"

    return "CRITICAL"


def analyze_zone(person_count: int, area_m2: float) -> dict:
    """Return the count, area, density and status for a zone."""
    density = calculate_density(person_count, area_m2)

    return {
        "person_count": person_count,
        "area_m2": area_m2,
        "density": round(density, 2),
        "status": classify_density(density),
    }


if __name__ == "__main__":
    sample_zones = {
        "Gate 1": {"count": 30, "area": 10},
        "Gate 2": {"count": 50, "area": 10},
        "Gate 3": {"count": 70, "area": 10},
    }

    for zone_name, data in sample_zones.items():
        result = analyze_zone(data["count"], data["area"])
        print(f"{zone_name}: {result}")
