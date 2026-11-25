-- Latest sessions, newest first
SELECT id, title, started_at, ended_at, step_count, peak_error
FROM sessions ORDER BY started_at DESC LIMIT 12;

-- Sensor trace for one saved run
SELECT step_index, x_deg, y_deg, z_deg, order_name, quantize_decimals, created_at
FROM sensor_logs WHERE session_id = :session_id ORDER BY step_index;

-- Drift / determinant time series for one saved run
SELECT step_index, orthogonality_error, determinant, repaired
FROM orientations WHERE session_id = :session_id ORDER BY step_index;

-- Per-run repair and peak-drift summary
SELECT s.id, s.title, s.step_count, s.peak_error,
       SUM(CASE WHEN o.repaired THEN 1 ELSE 0 END) AS repair_count
FROM sessions s LEFT JOIN orientations o ON o.session_id = s.id
GROUP BY s.id, s.title, s.step_count, s.peak_error ORDER BY s.started_at DESC;
