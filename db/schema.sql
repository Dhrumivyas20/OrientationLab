-- MySQL 8+ reference schema. The SQLAlchemy metadata in models.py creates the
-- same tables for both managed MySQL and local SQLite.
CREATE TABLE sessions (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  title VARCHAR(120) NOT NULL DEFAULT 'Untitled run',
  started_at DATETIME(6) NOT NULL,
  ended_at DATETIME(6) NULL,
  step_count INT NOT NULL DEFAULT 0,
  peak_error DOUBLE NOT NULL DEFAULT 0
);
CREATE TABLE sensor_logs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  session_id BIGINT NOT NULL,
  step_index INT NOT NULL,
  x_deg DOUBLE NOT NULL, y_deg DOUBLE NOT NULL, z_deg DOUBLE NOT NULL,
  order_name VARCHAR(3) NOT NULL DEFAULT 'XYZ',
  quantize_decimals INT NULL,
  created_at DATETIME(6) NOT NULL,
  INDEX ix_sensor_logs_session_id (session_id),
  CONSTRAINT fk_sensor_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE TABLE orientations (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  session_id BIGINT NOT NULL,
  step_index INT NOT NULL,
  matrix JSON NOT NULL,
  orthogonality_error DOUBLE NOT NULL,
  determinant DOUBLE NOT NULL,
  repaired BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DATETIME(6) NOT NULL,
  INDEX ix_orientations_session_id (session_id),
  CONSTRAINT fk_orientation_session FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
