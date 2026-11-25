-- Optional manual seed for a fresh MySQL schema. Timestamps use UTC.
INSERT INTO sessions (id, title, started_at, ended_at, step_count, peak_error)
VALUES (9001, 'Reference tri-axis sample', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), 1, 1.7e-16);
INSERT INTO sensor_logs (session_id, step_index, x_deg, y_deg, z_deg, order_name, quantize_decimals, created_at)
VALUES (9001, 1, 30, 45, 60, 'XYZ', NULL, UTC_TIMESTAMP(6));
INSERT INTO orientations (session_id, step_index, matrix, orthogonality_error, determinant, repaired, created_at)
VALUES (9001, 1, JSON_ARRAY(JSON_ARRAY(0.353553, -0.612372, 0.707107), JSON_ARRAY(0.926777, 0.126826, -0.353553), JSON_ARRAY(0.126826, 0.780330, 0.612372)), 1.7e-16, 1.0, FALSE, UTC_TIMESTAMP(6));
