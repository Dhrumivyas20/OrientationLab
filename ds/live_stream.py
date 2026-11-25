"""Real-time iPhone SensorLog / Sensor Logger ingestion and SSE broadcast hub."""
from __future__ import annotations

import math
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from ds.stream_buffer import SensorQueue, WindowedBuffer

VALID_ORDERS = {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _wrap_pi(radians: float) -> float:
    return (radians + math.pi) % (2.0 * math.pi) - math.pi


DISCRETE_ORIENTATION_ANGLES = {
    "portrait": 0.0,
    "landscapeleft": -0.5 * math.pi,
    "landscaperight": 0.5 * math.pi,
    "portraitupsidedown": math.pi,
}


def _accel_to_tilt_radians(ax: float, ay: float, az: float) -> tuple[float, float, float]:
    """Derive (pitch_x, yaw_y, roll_z) in radians from CoreMotion acceleration (Gs)."""
    roll_z = math.atan2(-ax, -ay)
    pitch_x = math.atan2(az, math.hypot(ax, ay))
    return (pitch_x, 0.0, roll_z)


def _parse_single_dict(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract normalized rotation samples from one SensorLog, Sensor Logger, or generic dict."""
    # 1. Sensor Logger (Kelvin Choi) batch payload format: {"payload": [{"name": ..., "time": ns, "values": {...}}]}
    if isinstance(data.get("payload"), list):
        samples: list[dict[str, Any]] = []
        device_id = str(data.get("deviceId") or data.get("deviceid") or "iphone")
        for item in data["payload"]:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name", "")).lower()
            values = item.get("values")
            if not isinstance(values, Mapping):
                continue
            ts_ns = _to_float(item.get("time"))
            ts_sec = ts_ns / 1e9 if ts_ns is not None else None
            if name in {"gyroscope", "gyroscopeuncalibrated"}:
                x, y, z = _to_float(values.get("x")), _to_float(values.get("y")), _to_float(values.get("z"))
                if x is not None and y is not None and z is not None:
                    samples.append({
                        "kind": "rate_rad_s",
                        "x": x,
                        "y": y,
                        "z": z,
                        "timestamp": ts_sec,
                        "source": "Sensor Logger · Gyro",
                        "device_id": device_id,
                    })
            elif name == "orientation":
                pitch = _to_float(values.get("pitch"))
                roll = _to_float(values.get("roll"))
                yaw = _to_float(values.get("yaw"))
                if pitch is not None and roll is not None and yaw is not None:
                    samples.append({
                        "kind": "attitude_rad",
                        "x": pitch,
                        "y": roll,
                        "z": yaw,
                        "timestamp": ts_sec,
                        "source": "Sensor Logger · Attitude",
                        "device_id": device_id,
                    })
        return samples

    device_id = str(data.get("deviceid") or data.get("deviceId") or "iphone")
    logging_ts = _parse_iso_timestamp(data.get("loggingTime"))

    # 2. SensorLog (Bernd Thomas) unbiased CoreMotion rotation rate (rad/s)
    m_rx = _to_float(data.get("motionRotationRateX"))
    m_ry = _to_float(data.get("motionRotationRateY"))
    m_rz = _to_float(data.get("motionRotationRateZ"))
    if m_rx is not None and m_ry is not None and m_rz is not None:
        ts = _to_float(data.get("motionTimestamp_sinceReboot")) or logging_ts
        return [{
            "kind": "rate_rad_s",
            "x": m_rx,
            "y": m_ry,
            "z": m_rz,
            "timestamp": ts,
            "source": "SensorLog · Motion Gyro",
            "device_id": device_id,
        }]

    # 2.5. Swift CoreMotion nested CMDeviceMotion rotationRate: {"rotationRate": {"x": ..., "y": ..., "z": ...}}
    rot_rate = data.get("rotationRate")
    if isinstance(rot_rate, Mapping):
        rx, ry, rz = _to_float(rot_rate.get("x")), _to_float(rot_rate.get("y")), _to_float(rot_rate.get("z"))
        if rx is not None and ry is not None and rz is not None:
            ts = _to_float(data.get("timestamp")) or _to_float(data.get("motionTimestamp_sinceReboot")) or logging_ts
            return [{
                "kind": "rate_rad_s",
                "x": rx,
                "y": ry,
                "z": rz,
                "timestamp": ts,
                "source": "Swift CoreMotion · Gyro",
                "device_id": device_id,
            }]

    # 3. SensorLog raw gyroscope rotation rate (rad/s)
    g_rx = _to_float(data.get("gyroRotationX"))
    g_ry = _to_float(data.get("gyroRotationY"))
    g_rz = _to_float(data.get("gyroRotationZ"))
    if g_rx is not None and g_ry is not None and g_rz is not None:
        ts = _to_float(data.get("gyroTimestamp_sinceReboot")) or logging_ts
        return [{
            "kind": "rate_rad_s",
            "x": g_rx,
            "y": g_ry,
            "z": g_rz,
            "timestamp": ts,
            "source": "SensorLog · Raw Gyro",
            "device_id": device_id,
        }]

    # 4. SensorLog CoreMotion attitude angles (radians) -> convert consecutive deltas
    pitch = _to_float(data.get("motionPitch"))
    roll = _to_float(data.get("motionRoll"))
    yaw = _to_float(data.get("motionYaw"))
    if pitch is not None and roll is not None and yaw is not None:
        ts = _to_float(data.get("motionTimestamp_sinceReboot")) or logging_ts
        return [{
            "kind": "attitude_rad",
            "x": pitch,
            "y": roll,
            "z": yaw,
            "timestamp": ts,
            "source": "SensorLog · Attitude",
            "device_id": device_id,
        }]

    # 4.5. Swift CoreMotion nested attitude: {"attitude": {"pitch": ..., "roll": ..., "yaw": ...}}
    attitude = data.get("attitude")
    if isinstance(attitude, Mapping):
        ap = _to_float(attitude.get("pitch"))
        ar = _to_float(attitude.get("roll"))
        ay = _to_float(attitude.get("yaw"))
        if ap is not None and ar is not None and ay is not None:
            ts = _to_float(data.get("timestamp")) or logging_ts
            return [{
                "kind": "attitude_rad",
                "x": ap,
                "y": ar,
                "z": ay,
                "timestamp": ts,
                "source": "Swift CoreMotion · Attitude",
                "device_id": device_id,
            }]

    # 4.6. Swift CMAccelerometerData / SensorLog accelerometer tilt fallback (Gs)
    accel = data.get("acceleration")
    if isinstance(accel, Mapping):
        ax, ay, az = _to_float(accel.get("x")), _to_float(accel.get("y")), _to_float(accel.get("z"))
        if ax is not None and ay is not None and az is not None:
            px, wy, rz = _accel_to_tilt_radians(ax, ay, az)
            ts = _to_float(data.get("timestamp")) or logging_ts
            return [{
                "kind": "attitude_rad",
                "x": px,
                "y": wy,
                "z": rz,
                "timestamp": ts,
                "source": "Swift · Accelerometer Tilt",
                "device_id": device_id,
            }]

    s_ax = _to_float(data.get("accelerometerAccelerationX"))
    s_ay = _to_float(data.get("accelerometerAccelerationY"))
    s_az = _to_float(data.get("accelerometerAccelerationZ"))
    if s_ax is not None and s_ay is not None and s_az is not None:
        px, wy, rz = _accel_to_tilt_radians(s_ax, s_ay, s_az)
        ts = _to_float(data.get("accelerometerTimestamp_sinceReboot")) or logging_ts
        return [{
            "kind": "attitude_rad",
            "x": px,
            "y": wy,
            "z": rz,
            "timestamp": ts,
            "source": "SensorLog · Accelerometer Tilt",
            "device_id": device_id,
        }]

    # 4.7. Swift OrientationTracker discrete UIDeviceOrientation / angleRadians
    angle_rad = _to_float(data.get("angleRadians"))
    orient_raw = data.get("deviceOrientation") or data.get("orientation")
    if angle_rad is not None or isinstance(orient_raw, str):
        if angle_rad is None and isinstance(orient_raw, str):
            angle_rad = DISCRETE_ORIENTATION_ANGLES.get(orient_raw.strip().lower())
        if angle_rad is not None:
            ts = _to_float(data.get("timestamp")) or logging_ts
            return [{
                "kind": "attitude_rad",
                "x": 0.0,
                "y": 0.0,
                "z": angle_rad,
                "timestamp": ts,
                "source": "Swift · DeviceOrientation",
                "device_id": device_id,
            }]

    # 5. Generic JSON / URL-encoded x, y, z step
    gx, gy, gz = _to_float(data.get("x")), _to_float(data.get("y")), _to_float(data.get("z"))
    if gx is not None and gy is not None and gz is not None:
        unit = str(data.get("unit", "deg")).lower()
        is_rate = str(data.get("rate", "")).lower() in {"1", "true", "yes"} or data.get("rate") is True
        ts = _to_float(data.get("timestamp")) or logging_ts
        dt = _to_float(data.get("dt"))
        if is_rate:
            if unit.startswith("rad"):
                return [{
                    "kind": "rate_rad_s",
                    "x": gx,
                    "y": gy,
                    "z": gz,
                    "timestamp": ts,
                    "dt": dt,
                    "source": "Live Stream · Gyro",
                    "device_id": device_id,
                }]
            dt_val = min(max(dt if dt is not None else 1.0 / 30.0, 0.001), 0.25)
            return [{
                "kind": "delta_deg",
                "x": gx * dt_val,
                "y": gy * dt_val,
                "z": gz * dt_val,
                "timestamp": ts,
                "source": "Live Stream · Gyro",
                "device_id": device_id,
            }]
        factor = 180.0 / math.pi if unit.startswith("rad") else 1.0
        return [{
            "kind": "delta_deg",
            "x": gx * factor,
            "y": gy * factor,
            "z": gz * factor,
            "timestamp": ts,
            "source": "Live Stream · Step",
            "device_id": device_id,
        }]

    return []


def parse_sensor_samples(payload: Any) -> list[dict[str, Any]]:
    """Parse one JSON object, array of objects, or form dict into normalized samples."""
    if isinstance(payload, list):
        samples: list[dict[str, Any]] = []
        for item in payload:
            if isinstance(item, Mapping):
                samples.extend(_parse_single_dict(item))
        return samples
    if isinstance(payload, Mapping):
        return _parse_single_dict(payload)
    return []


class LiveStreamHub:
    """Thread-safe state coordinator for real-time iPhone telemetry and SSE clients."""

    def __init__(self, max_dt: float = 0.25, default_dt: float = 1.0 / 30.0):
        self.lock = threading.RLock()
        self.max_dt = max_dt
        self.default_dt = default_dt
        self.listening: bool = True
        self.order: str = "XYZ"
        self.active_session_id: int | None = None
        self.step_count: int = 0
        self.matrix: list[list[float]] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        self.orthogonality_error: float = 0.0
        self.determinant: float = 1.0
        self.last_source: str | None = None
        self.last_angles: dict[str, float] | None = None
        self.last_seen_epoch: float | None = None
        self.rate_hz: float = 0.0
        self._last_device_ts: dict[str, float] = {}
        self._last_attitude: dict[str, tuple[float, float, float]] = {}
        self._arrival_times: WindowedBuffer[float] = WindowedBuffer(capacity=30)
        self._activity_window: WindowedBuffer[dict[str, Any]] = WindowedBuffer(capacity=60)
        self._ingest_queue: SensorQueue[dict[str, Any]] = SensorQueue(capacity=512)
        self._subscribers: set[queue.Queue[tuple[str, dict[str, Any]]]] = set()

    def set_order(self, order: str) -> str:
        clean = str(order).upper().strip()
        if clean not in VALID_ORDERS:
            raise ValueError("order must contain X, Y, and Z exactly once")
        with self.lock:
            self.order = clean
            self._broadcast_locked("status", self.snapshot_locked())
            return self.order

    def set_listening(self, listening: bool) -> bool:
        with self.lock:
            self.listening = bool(listening)
            self._broadcast_locked("status", self.snapshot_locked())
            return self.listening

    def sync_session(
        self,
        *,
        session_id: int | None,
        step_count: int,
        matrix: list[list[float]],
        orthogonality_error: float,
        determinant: float,
        angles: dict[str, float] | None = None,
        source: str | None = None,
        broadcast: bool = True,
    ) -> dict[str, Any]:
        with self.lock:
            if session_id != self.active_session_id:
                self._last_device_ts.clear()
                self._last_attitude.clear()
                self._arrival_times = WindowedBuffer(capacity=30)
                if session_id is None:
                    self._activity_window = WindowedBuffer(capacity=60)
            self.active_session_id = session_id
            self.step_count = int(step_count)
            self.matrix = [[float(v) for v in row] for row in matrix]
            self.orthogonality_error = float(orthogonality_error)
            self.determinant = float(determinant)
            if angles is not None:
                self.last_angles = {k: float(v) for k, v in angles.items()}
            if source is not None:
                self.last_source = source
            snap = self.snapshot_locked()
            if broadcast:
                self._broadcast_locked("snapshot", snap)
            return snap

    def clear_session(self, broadcast: bool = True) -> dict[str, Any]:
        return self.sync_session(
            session_id=None,
            step_count=0,
            matrix=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
            orthogonality_error=0.0,
            determinant=1.0,
            angles=None,
            source=None,
            broadcast=broadcast,
        )

    def to_delta_degrees(self, sample: Mapping[str, Any]) -> dict[str, float]:
        """Convert one normalized sample into incremental (x, y, z) degrees."""
        with self.lock:
            device_id = str(sample.get("device_id") or "iphone")
            kind = sample["kind"]
            now = time.monotonic()
            self._arrival_times.append(now)
            arrivals = self._arrival_times.values()
            if len(arrivals) >= 2:
                span = arrivals[-1] - arrivals[0]
                if span > 1e-4:
                    self.rate_hz = round((len(arrivals) - 1) / span, 1)

            self.last_seen_epoch = time.time()
            self.last_source = str(sample.get("source") or "iPhone SensorLog")

            if kind == "delta_deg":
                dx, dy, dz = float(sample["x"]), float(sample["y"]), float(sample["z"])
            elif kind == "rate_rad_s":
                ts = sample.get("timestamp")
                explicit_dt = sample.get("dt")
                if explicit_dt is not None:
                    dt = min(max(float(explicit_dt), 0.001), self.max_dt)
                elif ts is not None:
                    prev_ts = self._last_device_ts.get(device_id)
                    self._last_device_ts[device_id] = float(ts)
                    if prev_ts is not None and float(ts) > prev_ts:
                        dt = min(max(float(ts) - prev_ts, 0.001), self.max_dt)
                    else:
                        dt = self.default_dt
                else:
                    dt = self.default_dt
                dx = math.degrees(float(sample["x"]) * dt)
                dy = math.degrees(float(sample["y"]) * dt)
                dz = math.degrees(float(sample["z"]) * dt)
            elif kind == "attitude_rad":
                curr = (float(sample["x"]), float(sample["y"]), float(sample["z"]))
                prev = self._last_attitude.get(device_id)
                self._last_attitude[device_id] = curr
                if prev is None:
                    dx = dy = dz = 0.0
                else:
                    dx = math.degrees(_wrap_pi(curr[0] - prev[0]))
                    dy = math.degrees(_wrap_pi(curr[1] - prev[1]))
                    dz = math.degrees(_wrap_pi(curr[2] - prev[2]))
            else:
                raise ValueError(f"Unsupported sample kind: {kind}")

            for axis_val in (dx, dy, dz):
                if not math.isfinite(axis_val) or abs(axis_val) > 3600.0:
                    raise ValueError("Sample delta out of finite ±3600° range")

            return {"x": dx, "y": dy, "z": dz}

    def enqueue_and_record(
        self,
        *,
        session_id: int,
        step_index: int,
        angles: dict[str, float],
        matrix: list[list[float]],
        orthogonality_error: float,
        determinant: float,
        order: str,
        source: str,
    ) -> dict[str, Any]:
        with self.lock:
            item = {
                "session_id": session_id,
                "step_index": step_index,
                "angles": angles,
                "matrix": matrix,
                "orthogonality_error": orthogonality_error,
                "determinant": determinant,
                "order": order,
                "source": source,
                "rate_hz": self.rate_hz,
            }
            if len(self._ingest_queue) >= self._ingest_queue.capacity:
                self._ingest_queue.dequeue()
            self._ingest_queue.enqueue(item)
            self._ingest_queue.dequeue()

            self.active_session_id = session_id
            self.step_count = step_index
            self.matrix = [[float(v) for v in row] for row in matrix]
            self.orthogonality_error = float(orthogonality_error)
            self.determinant = float(determinant)
            self.last_angles = dict(angles)
            self.last_source = source
            self._activity_window.append({
                "step_index": step_index,
                "angles": angles,
                "orthogonality_error": orthogonality_error,
                "determinant": determinant,
            })

            payload = {
                **item,
                "listening": self.listening,
                "window": self._activity_window.values(),
            }
            self._broadcast_locked("step", payload)
            return payload

    def snapshot_locked(self) -> dict[str, Any]:
        return {
            "listening": self.listening,
            "order": self.order,
            "session_id": self.active_session_id,
            "step_count": self.step_count,
            "matrix": self.matrix,
            "orthogonality_error": self.orthogonality_error,
            "determinant": self.determinant,
            "last_source": self.last_source,
            "last_angles": self.last_angles,
            "last_seen_epoch": self.last_seen_epoch,
            "rate_hz": self.rate_hz,
            "window": self._activity_window.values(),
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self.snapshot_locked()

    def subscribe(self) -> queue.Queue[tuple[str, dict[str, Any]]]:
        q: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=128)
        with self.lock:
            self._subscribers.add(q)
            q.put(("snapshot", self.snapshot_locked()))
        return q

    def unsubscribe(self, q: queue.Queue[tuple[str, dict[str, Any]]]) -> None:
        with self.lock:
            self._subscribers.discard(q)

    def _broadcast_locked(self, event: str, data: dict[str, Any]) -> None:
        dead: list[queue.Queue[tuple[str, dict[str, Any]]]] = []
        for sub in self._subscribers:
            try:
                if sub.full():
                    try:
                        sub.get_nowait()
                    except queue.Empty:
                        pass
                sub.put_nowait((event, data))
            except Exception:
                dead.append(sub)
        for sub in dead:
            self._subscribers.discard(sub)
