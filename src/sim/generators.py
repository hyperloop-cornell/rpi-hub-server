"""Fake sensor output for simulated bench devices.

Line formats match web-client/src/config/sensor-mappings.json so the GUI's sensor
auto-detection picks the intended sensor (the web client's browser mock uses the same ones).
"""

import math
import random
from typing import Callable, Dict

LineGenerator = Callable[[float], str]


def _wave(t: float, base: float, amplitude: float, period_s: float, phase: float = 0.0) -> float:
    return base + amplitude * math.sin(2 * math.pi * t / period_s + phase)


def _jitter(amplitude: float) -> float:
    return random.uniform(-amplitude, amplitude)


def dht22(t: float) -> str:
    return f"TEMP: {_wave(t, 22.5, 1.5, 90) + _jitter(0.15):.1f} HUMID: {_wave(t, 46, 4, 120, 1) + _jitter(0.4):.1f}"


def mpu6050(t: float) -> str:
    return " ".join(
        [
            f"AX: {_jitter(0.05):.2f}",
            f"AY: {_jitter(0.05):.2f}",
            f"AZ: {0.98 + _jitter(0.03):.2f}",
            f"GX: {_wave(t, 0, 15, 6) + _jitter(1):.1f}",
            f"GY: {_wave(t, 0, 10, 9, 2) + _jitter(1):.1f}",
            f"GZ: {_jitter(2):.1f}",
        ]
    )


def voltage(t: float) -> str:
    return f"VOLT: {_wave(t, 12.3, 0.25, 40) + _jitter(0.03):.2f}"


def bme280(t: float) -> str:
    return ",".join(
        [
            f"{_wave(t, 21.8, 1.2, 100) + _jitter(0.1):.1f}",
            f"{_wave(t, 52, 6, 150, 0.5) + _jitter(0.3):.1f}",
            f"{_wave(t, 1013.2, 2.5, 200, 1.5) + _jitter(0.1):.1f}",
        ]
    )


def hcsr04(t: float) -> str:
    return f"DIST: {_wave(t, 60, 25, 20) + _jitter(0.5):.1f}"


def current(t: float) -> str:
    return f"CURRENT: {_wave(t, 3.2, 0.8, 30) + _jitter(0.05):.2f}"


GENERATORS: Dict[str, LineGenerator] = {
    "dht22": dht22,
    "mpu6050": mpu6050,
    "voltage": voltage,
    "bme280": bme280,
    "hcsr04": hcsr04,
    "current": current,
}
