# Orientation Lab — Live 3D Device Orientation Tracker

Orientation Lab visualises how a phone, cube, VR headset, or other rigid body tracks its 3D attitude as angular steps around the **x**, **y**, and **z** axes are composed in real time. Each step is converted into an explicit 3 × 3 rotation matrix and multiplied into the running orientation frame (`Q ← Q Rₐ Rᵦ R𝚌`), making non-commutativity, numerical drift, and Gram–Schmidt repair tangible.

You can drive the instrument manually, replay pasted sensor traces, run a finite-precision stress test, or stream **live telemetry from an iPhone** over Wi-Fi using **SensorLog** (or any HTTP/CoreMotion client) with real-time Server-Sent Events (SSE) updates and live device posture classification (`PORTRAIT`, `LANDSCAPE LEFT`, `LANDSCAPE RIGHT`, `UPSIDE DOWN`, `FACE UP`, `FACE DOWN`).

### Desktop workbench

![Orientation Lab desktop screenshot](screenshots/orientation-lab-desktop.png)

### Mobile workbench

![Orientation Lab mobile screenshot](screenshots/orientation-lab-mobile.png)

---

## Key Capabilities

- **Explicit 3×3 Rotation Engine**: Right-handed active rotations about X, Y, and Z, manual 3×3 matrix multiplication, Frobenius orthogonality error `‖QᵀQ − I‖`, cofactor determinant, and explicit Gram–Schmidt re-orthonormalisation (no `numpy.linalg` black boxes in production math).
- **Order Sensitivity Comparison**: Live side-by-side comparison of `XYZ` vs `ZYX` products and their Frobenius distance, plus step application in any of the 6 axis permutations.
- **Finite-Precision Drift & Repair**: Precision stress mode applies 1,500 small steps rounded to 6 decimal places to expose orthogonality drift, then repairs the frame back to an orthonormal right-handed basis with Gram–Schmidt.
- **Live iPhone Streaming (SSE)**: Accepts live HTTP POST packets at `/api/sensorlog` (from iOS **SensorLog**, **Sensor Logger**, or custom Swift CoreMotion clients), integrates gyro rates or attitude deltas, persists steps, and pushes live updates to the browser via `/api/stream/events`.
- **Live Device Posture Readout**: Derives the phone's physical posture from the rotated world-up vector in body coordinates using a `0.55` threshold with hysteresis.
- **Persistent Session Archive**: Saves sessions, per-step sensor logs, and orientation snapshots to MySQL (`DATABASE_URL`) or local SQLite (`instance/orientation_lab.sqlite3`), with Matplotlib log-scale drift plots.

---

## Stack & Repository Map

- **Backend**: Python 3.10+, Flask 3.1, NumPy, Matplotlib, SQLAlchemy, PyMySQL / SQLite (WAL mode)
- **Frontend**: Server-rendered HTML/CSS, vanilla JavaScript, HTML5 Canvas 3D renderer, `EventSource` (SSE)

```text
orientrack/
├── app/
│   ├── main.py              # Flask routes, SensorLog ingest, SSE stream, Matplotlib plots
│   ├── templates/index.html # Single-page instrument dashboard
│   └── static/
│       ├── app.js           # 3D canvas renderer, live SSE client, posture classifier
│       └── styles.css       # Field-notebook responsive styling
├── math_engine/
│   └── rotations.py         # Axis matrices, compose_order, drift metrics, Gram–Schmidt
├── ds/
│   ├── stream_buffer.py     # Bounded FIFO SensorQueue and rolling WindowedBuffer
│   └── live_stream.py       # SensorLog / CoreMotion payload parser & LiveStreamHub
├── db/
│   ├── models.py            # SQLAlchemy models & SQLite/MySQL engine setup
│   ├── schema.sql           # MySQL 8+ reference DDL
│   ├── sample_data.sql      # Example seed data
│   └── queries.sql          # Reference analytical queries
└── tests/
    ├── test_orientation.py  # Unit & isolated Flask API validation suite
    └── validation_results.md
```

---

## Setup & Running Locally

### Using the existing virtual environment

If `.venv` is already present in the repository root:

```bash
.venv/bin/python -m app.main
```

### Fresh installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app.main
```

Open **`http://localhost:3000`** in your browser.

### Environment options

- `PORT`: Override the listening port (default `3000`), e.g. `PORT=5000 .venv/bin/python -m app.main`.
- `DATABASE_URL`: Supply a MySQL connection string (`mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE`) for managed/standalone MySQL. When omitted, SQLite is created automatically at `instance/orientation_lab.sqlite3`.

---

## Connecting an iPhone (Real-Time Stream)

1. Connect your computer and iPhone to the **same Wi-Fi network**.
2. Start Orientation Lab and open `http://localhost:3000`.
3. In section **04 / SENSOR LOG**, copy the displayed **SENSORLOG HTTP POST URL** (e.g. `http://192.168.x.x:3000/api/sensorlog`) using the **COPY URL** button.
4. In the iOS **SensorLog** app:
   - Enable **Motion** and/or **Gyro** logging (20–60 Hz recommended).
   - Enable **HTTP POST** in **JSON** format (URL-encoded form POSTs are also supported).
   - Paste the copied URL and start logging.
5. Rotate your iPhone — the 3D model, orientation matrix, posture indicator, activity bars, and diagnostics update live.

### Supported Ingest Payloads (`POST /api/sensorlog` or `POST /api/stream/ingest`)

| Source | Accepted Fields | Notes |
| --- | --- | --- |
| **SensorLog (CoreMotion Gyro)** | `motionRotationRateX/Y/Z` (rad/s), `motionTimestamp_sinceReboot` | Preferred unbiased gyro stream |
| **SensorLog (Raw Gyro)** | `gyroRotationX/Y/Z` (rad/s), `gyroTimestamp_sinceReboot` | Integrated via clamped `dt` |
| **SensorLog (Attitude)** | `motionPitch`, `motionRoll`, `motionYaw` (rad) | Wrapped angular deltas |
| **Sensor Logger** | `{"payload": [{"name": "gyroscope" \| "orientation", "time": ns, "values": {...}}]}` | Batch array support |
| **Swift / CoreMotion JSON** | `{"rotationRate": {"x","y","z"}}`, `{"attitude": {"pitch","roll","yaw"}}`, `{"acceleration": {"x","y","z"}}`, or `{"deviceOrientation": "portrait" \| ...}` | Accelerometer tilt & discrete orientation supported |
| **Generic Step** | `{"x": deg, "y": deg, "z": deg}` | Optional `unit: "rad"`, `rate: true`, `dt` |

---

## API Endpoints

| Method & Path | Purpose |
| --- | --- |
| `GET /api/health` | Check database connectivity |
| `GET /api/sessions` | List recent tracking sessions |
| `POST /api/sessions` | Start a new tracking session |
| `GET /api/sessions/<id>` | Fetch session metadata, sensor logs, and orientation matrices |
| `POST /api/sessions/<id>/steps` | Apply and persist one `(x, y, z)` degree step |
| `POST /api/sessions/<id>/stress` | Run quantized precision-stress simulation |
| `POST /api/sessions/<id>/repair` | Apply Gram–Schmidt re-orthonormalisation |
| `POST /api/sessions/<id>/finish` | Mark session archived |
| `GET /api/sessions/<id>/plot.png` | Render Matplotlib log-scale drift trace |
| `POST /api/compare` | Compare `XYZ` vs `ZYX` matrices and Frobenius distance |
| `GET /api/stream/info` | Get LAN ingest URL and live stream snapshot |
| `POST /api/stream/control` | Pause/resume `listening`, change live `order`, or `action: "reset"` |
| `GET /api/stream/events` | Subscribe to live SSE updates (`snapshot`, `step`, `status`) |
| `GET \| POST /api/sensorlog` | Ingest live iPhone telemetry (also `/api/stream/ingest`) |

---

## Validation & Tests

Run the unit and isolated API test suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Syntax and bytecode checks:

```bash
.venv/bin/python -m compileall -q math_engine ds db app tests
node --check app/static/app.js
```

All API tests run against an isolated temporary SQLite database and do not modify `instance/orientation_lab.sqlite3` or any configured `DATABASE_URL`. See [`tests/validation_results.md`](tests/validation_results.md) for benchmarked drift and repair measurements.
