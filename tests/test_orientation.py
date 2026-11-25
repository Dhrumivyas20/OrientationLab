from __future__ import annotations
import math
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base
from ds.live_stream import LiveStreamHub, parse_sensor_samples
from ds.stream_buffer import SensorQueue, WindowedBuffer
from math_engine.rotations import (compare_orders, compose_order, determinant_3x3,
                                   gram_schmidt, matrix_multiply, orthogonality_error,
                                   rotation_matrix, simulate_steps)


class RotationEngineTests(unittest.TestCase):
    def test_axis_rotation_matrices_are_proper_rotations(self):
        for axis in "XYZ":
            matrix = rotation_matrix(axis, math.radians(37))
            self.assertAlmostEqual(orthogonality_error(matrix), 0.0, places=14)
            self.assertAlmostEqual(determinant_3x3(matrix), 1.0, places=14)

    def test_manual_matrix_product_matches_numpy_validation(self):
        a = rotation_matrix("X", 0.31)
        b = rotation_matrix("Z", -0.7)
        np.testing.assert_allclose(matrix_multiply(a, b), np.matmul(a, b), atol=1e-15)

    def test_rotation_order_changes_final_orientation(self):
        result = compare_orders({"x": 28, "y": 42, "z": 65})
        self.assertGreater(result["difference"], 0.1)
        self.assertFalse(np.allclose(result["xyz"], result["zyx"]))

    def test_composition_remains_orthogonal_without_quantization(self):
        q = np.eye(3)
        for angles in simulate_steps(700, seed=4):
            q = compose_order(angles, "XYZ", q)
        self.assertLess(orthogonality_error(q), 1e-12)
        self.assertAlmostEqual(determinant_3x3(q), 1.0, places=12)

    def test_precision_stress_is_detected_and_gram_schmidt_repairs_it(self):
        q = np.eye(3)
        for angles in simulate_steps(1500, seed=28):
            q = compose_order(angles, "XYZ", q, quantize_decimals=6)
        before = orthogonality_error(q)
        repaired = gram_schmidt(q)
        after = orthogonality_error(repaired)
        self.assertGreater(before, 1e-6)
        self.assertLess(after, 1e-12)
        self.assertAlmostEqual(determinant_3x3(repaired), 1.0, places=12)
        self.assertAlmostEqual(np.linalg.det(repaired), 1.0, places=12)
        self.assertAlmostEqual(np.linalg.norm(repaired.T @ repaired - np.eye(3)), after, places=14)

    def test_gram_schmidt_rejects_dependent_columns(self):
        with self.assertRaises(ValueError):
            gram_schmidt([[1, 1, 0], [0, 0, 1], [0, 0, 0]])


class DataStructureTests(unittest.TestCase):
    def test_queue_fifo_and_full_condition(self):
        queue = SensorQueue[int](2)
        queue.enqueue(11); queue.enqueue(22)
        with self.assertRaises(OverflowError):
            queue.enqueue(33)
        self.assertEqual(queue.dequeue(), 11)
        self.assertEqual(queue.dequeue(), 22)
        with self.assertRaises(IndexError):
            queue.dequeue()

    def test_window_retains_only_latest_samples(self):
        window = WindowedBuffer[int](3)
        for value in range(5):
            window.append(value)
        self.assertEqual(window.values(), [2, 3, 4])


class PersistenceApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "test.sqlite3"
        self.test_engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.test_engine)
        self.factory = sessionmaker(bind=self.test_engine, expire_on_commit=False)
        self.session_patch = patch("db.models.SessionLocal", self.factory)
        self.engine_patch = patch("app.main.engine", self.test_engine)
        self.session_patch.start(); self.engine_patch.start()
        from app.main import app, live_hub
        live_hub.clear_session(broadcast=False)
        live_hub.set_listening(True)
        live_hub.set_order("XYZ")
        app.config.update(TESTING=True)
        self.live_hub = live_hub
        self.client = app.test_client()

    def tearDown(self):
        self.session_patch.stop(); self.engine_patch.stop()
        self.test_engine.dispose(); self.tmp.cleanup()

    def test_session_sensor_steps_repair_and_retrieval(self):
        created = self.client.post("/api/sessions", json={"title": "Unit-test trace"})
        self.assertEqual(created.status_code, 201)
        created_data = json.loads(created.get_data(as_text=True))
        session_id = created_data["session"]["id"]
        step = self.client.post(f"/api/sessions/{session_id}/steps",
                                json={"x": 18, "y": 24, "z": 31, "order": "XYZ"})
        self.assertEqual(step.status_code, 200)
        step_data = json.loads(step.get_data(as_text=True))
        self.assertAlmostEqual(step_data["determinant"], 1.0, places=12)
        # Induce deterministic matrix drift, then verify the API records the repair.
        stress = self.client.post(f"/api/sessions/{session_id}/stress",
                                  json={"count": 250, "decimals": 4, "seed": 9, "order": "XYZ"})
        self.assertEqual(stress.status_code, 200)
        stress_data = json.loads(stress.get_data(as_text=True))
        self.assertEqual(len(stress_data["window"]), 60)
        self.assertEqual(stress_data["window"][-1]["step_index"], 251)
        repaired = self.client.post(f"/api/sessions/{session_id}/repair", json={})
        self.assertEqual(repaired.status_code, 200)
        repaired_data = json.loads(repaired.get_data(as_text=True))
        self.assertLess(repaired_data["orthogonality_error"], 1e-12)
        detail = self.client.get(f"/api/sessions/{session_id}")
        self.assertEqual(detail.status_code, 200)
        detail_data = json.loads(detail.get_data(as_text=True))
        self.assertEqual(detail_data["session"]["step_count"], 251)
        self.assertEqual(len(detail_data["sensor_logs"]), 251)
        self.assertTrue(detail_data["orientations"][-1]["repaired"])
        plot = self.client.get(f"/api/sessions/{session_id}/plot.png")
        self.assertEqual(plot.status_code, 200)
        self.assertTrue(plot.content_type.startswith("image/png"))

    def test_invalid_angles_are_rejected(self):
        created = self.client.post("/api/sessions", json={})
        created_data = json.loads(created.get_data(as_text=True))
        session_id = created_data["session"]["id"]
        response = self.client.post(f"/api/sessions/{session_id}/steps", json={"x": "NaN", "y": 0, "z": 0})
        self.assertEqual(response.status_code, 400)

    def test_sensorlog_json_and_sse_broadcast(self):
        sub = self.live_hub.subscribe()
        try:
            # First event is always the initial snapshot
            event_name, _ = sub.get_nowait()
            self.assertEqual(event_name, "snapshot")

            first = self.client.post("/api/sensorlog", json={
                "deviceid": "iphone-15",
                "motionTimestamp_sinceReboot": 100.0,
                "motionRotationRateX": 0.5,
                "motionRotationRateY": 0.0,
                "motionRotationRateZ": -0.25,
            })
            self.assertEqual(first.status_code, 200)
            first_data = json.loads(first.get_data(as_text=True))
            self.assertTrue(first_data["accepted"])
            self.assertEqual(first_data["step_index"], 1)
            session_id = first_data["session_id"]

            second = self.client.post("/api/sensorlog", json={
                "deviceid": "iphone-15",
                "motionTimestamp_sinceReboot": 100.1,
                "motionRotationRateX": 1.0,
                "motionRotationRateY": 0.5,
                "motionRotationRateZ": 0.0,
            })
            self.assertEqual(second.status_code, 200)
            second_data = json.loads(second.get_data(as_text=True))
            self.assertEqual(second_data["session_id"], session_id)
            self.assertEqual(second_data["step_index"], 2)
            # 1.0 rad/s * 0.1 s -> 5.72957795 deg
            self.assertAlmostEqual(second_data["angles"]["x"], math.degrees(0.1), places=5)
            self.assertAlmostEqual(second_data["determinant"], 1.0, places=12)

            event1, payload1 = sub.get_nowait()
            event2, payload2 = sub.get_nowait()
            self.assertEqual((event1, event2), ("step", "step"))
            self.assertEqual(payload1["step_index"], 1)
            self.assertEqual(payload2["step_index"], 2)
        finally:
            self.live_hub.unsubscribe(sub)

    def test_sensorlog_form_and_sensor_logger_batch_and_pause(self):
        form_resp = self.client.post("/api/sensorlog", data={
            "gyroTimestamp_sinceReboot": "50.0",
            "gyroRotationX": "0.2",
            "gyroRotationY": "-0.1",
            "gyroRotationZ": "0.0",
        })
        self.assertEqual(form_resp.status_code, 200)
        form_data = json.loads(form_resp.get_data(as_text=True))
        self.assertEqual(form_data["step_index"], 1)

        logger_resp = self.client.post("/api/stream/ingest", json={
            "deviceId": "iphone-logger",
            "payload": [
                {"name": "gyroscope", "time": 1700000000000000000, "values": {"x": 0.1, "y": 0.2, "z": 0.3}},
                {"name": "gyroscope", "time": 1700000000050000000, "values": {"x": 0.4, "y": 0.0, "z": -0.2}},
            ],
        })
        self.assertEqual(logger_resp.status_code, 200)
        logger_data = json.loads(logger_resp.get_data(as_text=True))
        self.assertEqual(logger_data["ingested"], 2)
        self.assertEqual(logger_data["step_index"], 3)

        paused = self.client.post("/api/stream/control", json={"listening": False, "order": "ZYX"})
        self.assertEqual(paused.status_code, 200)
        self.assertFalse(json.loads(paused.get_data(as_text=True))["listening"])

        ignored = self.client.post("/api/sensorlog", json={"x": 10, "y": 0, "z": 0})
        self.assertEqual(ignored.status_code, 200)
        self.assertFalse(json.loads(ignored.get_data(as_text=True))["accepted"])

        bad = self.client.post("/api/stream/control", json={"listening": True})
        self.assertEqual(bad.status_code, 200)
        invalid = self.client.post("/api/sensorlog", json={"unrelated": 123})
        self.assertEqual(invalid.status_code, 400)


class LiveStreamUnitTests(unittest.TestCase):
    def test_attitude_wrapping_and_dt_clamping(self):
        hub = LiveStreamHub(max_dt=0.2, default_dt=0.02)
        samples = parse_sensor_samples([
            {"motionPitch": 3.1, "motionRoll": 0.0, "motionYaw": -3.1},
            {"motionPitch": -3.1, "motionRoll": 0.0, "motionYaw": 3.1},
        ])
        self.assertEqual(len(samples), 2)
        first = hub.to_delta_degrees(samples[0])
        second = hub.to_delta_degrees(samples[1])
        self.assertEqual(first, {"x": 0.0, "y": 0.0, "z": 0.0})
        # Wrapped delta from +3.1 to -3.1 rad is +(2*pi - 6.2) rad, not -6.2 rad
        self.assertAlmostEqual(second["x"], math.degrees(2 * math.pi - 6.2), places=5)
        self.assertAlmostEqual(second["z"], -math.degrees(2 * math.pi - 6.2), places=5)

        rate_samples = parse_sensor_samples([
            {"gyroTimestamp_sinceReboot": 10.0, "gyroRotationX": 1.0, "gyroRotationY": 0.0, "gyroRotationZ": 0.0},
            {"gyroTimestamp_sinceReboot": 25.0, "gyroRotationX": 1.0, "gyroRotationY": 0.0, "gyroRotationZ": 0.0},
        ])
        hub.to_delta_degrees(rate_samples[0])
        clamped = hub.to_delta_degrees(rate_samples[1])
        # 15s gap is clamped to max_dt=0.2s
        self.assertAlmostEqual(clamped["x"], math.degrees(0.2), places=6)

    def test_swift_coremotion_accelerometer_and_discrete_orientation_payloads(self):
        hub = LiveStreamHub(max_dt=0.25, default_dt=0.05)

        gyro_samples = parse_sensor_samples({
            "deviceid": "iphone-swift",
            "timestamp": 12.5,
            "rotationRate": {"x": 0.5, "y": -0.25, "z": 0.1},
        })
        self.assertEqual(len(gyro_samples), 1)
        self.assertEqual(gyro_samples[0]["kind"], "rate_rad_s")
        self.assertIn("Swift CoreMotion", gyro_samples[0]["source"])

        accel_samples = parse_sensor_samples([
            {"deviceid": "iphone-accel", "acceleration": {"x": 0.0, "y": -1.0, "z": 0.0}},
            {"deviceid": "iphone-accel", "acceleration": {"x": 1.0, "y": 0.0, "z": 0.0}},
        ])
        self.assertEqual(len(accel_samples), 2)
        self.assertEqual(hub.to_delta_degrees(accel_samples[0]), {"x": 0.0, "y": 0.0, "z": 0.0})
        rotated = hub.to_delta_degrees(accel_samples[1])
        # Portrait (0,-1,0) -> landscapeLeft (1,0,0) matches -90° Z rotation
        self.assertAlmostEqual(rotated["z"], -90.0, places=5)

        discrete_samples = parse_sensor_samples([
            {"deviceid": "iphone-discrete", "deviceOrientation": "portrait"},
            {"deviceid": "iphone-discrete", "deviceOrientation": "landscapeRight"},
        ])
        self.assertEqual(hub.to_delta_degrees(discrete_samples[0]), {"x": 0.0, "y": 0.0, "z": 0.0})
        discrete_delta = hub.to_delta_degrees(discrete_samples[1])
        self.assertAlmostEqual(discrete_delta["z"], 90.0, places=5)


if __name__ == "__main__":
    unittest.main()
