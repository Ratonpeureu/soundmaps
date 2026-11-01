import math


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def db_to_power(level_db: float) -> float:
    return 10.0 ** (level_db / 10.0)


def leq_from_energy(energy_sum: float, total_duration: float) -> float:
    """Niveau équivalent à partir de somme(durée x 10^(L/10)) et de la durée totale."""
    if total_duration <= 0 or energy_sum <= 0:
        raise ValueError("énergie et durée doivent être strictement positives")
    return 10.0 * math.log10(energy_sum / total_duration)


def energy_average(levels: list[float], durations: list[float] | None = None) -> float:
    """Moyenne ÉNERGÉTIQUE (jamais arithmétique) de niveaux en dB, pondérée par la durée."""
    if not levels:
        raise ValueError("aucun niveau")
    durations = durations or [1.0] * len(levels)
    energy = sum(d * db_to_power(l) for l, d in zip(levels, durations))
    return leq_from_energy(energy, sum(durations))


def confidence_index(count: int, devices: int, calibrated_share: float, hours_since_last: float,
                     *, target_count: int, target_devices: int, recency_tau_hours: float,
                     w_count: float, w_devices: float, w_calibration: float, w_recency: float) -> float:
    return clamp01(
        w_count * min(1.0, count / target_count)
        + w_devices * min(1.0, devices / target_devices)
        + w_calibration * clamp01(calibrated_share)
        + w_recency * math.exp(-max(0.0, hours_since_last) / recency_tau_hours)
    )