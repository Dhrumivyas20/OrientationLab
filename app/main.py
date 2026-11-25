"""Flask interface and persistence API for Orientation Lab."""
from __future__ import annotations
import io
import json
import math
import os
import queue
import socket
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parents[1] / "instance" / "mpl"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from db.models import (OrientationRecord, SensorLog, TrackingSession, engine,
                       get_session, init_db)
from ds.live_stream import LiveStreamHub, VALID_ORDERS, parse_sensor_samples
from math_engine.rotations import (compare_orders, compose_order, determinant_3x3,
                                   gram_schmidt, orthogonality_error, simulate_steps)

ROOT = Path(__file__).resolve().parents[1]
app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_SORT_KEYS"] = False
live_hub = LiveStreamHub()


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def _ingest_urls() -> dict[str, str]:
    port = os.environ.get("PORT", "3000")
    lan_ip = _local_ip()
    host = request.host if request else f"{lan_ip}:{port}"
    scheme = request.scheme if request else "http"
    return {
        "lan_ip": lan_ip,
        "port": str(port),
        "lan_url": f"http://{lan_ip}:{port}/api/sensorlog",
        "origin_url": f"{scheme}://{host}/api/sensorlog",
    }


def _json_error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _float_angle(value, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric")
    if not math.isfinite(result) or abs(result) > 3600:
        raise ValueError(f"{name} must be finite and within ±3600°")
    return result


def _session_summary(row: TrackingSession) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "step_count": row.step_count,
        "peak_error": row.peak_error,
    }


def _get_record(db, session_id: int):
    return db.get(TrackingSession, session_id)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/manus-routes.json")
def route_manifest():
    return send_file(ROOT / "public" / "manus-routes.json", mimetype="application/json")


@app.get("/api/health")
def health():
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        return jsonify({"ok": True, "database": "ready"})
    except SQLAlchemyError as exc:
        app.logger.exception("database health check failed")
        return jsonify({"ok": False, "database": "unavailable", "detail": str(exc)}), 503


@app.get("/api/sessions")
def list_sessions():
    db = get_session()
    try:
        rows = db.scalars(select(TrackingSession).order_by(TrackingSession.started_at.desc()).limit(30)).all()
        return jsonify({"sessions": [_session_summary(row) for row in rows]})
    finally:
        db.close()


@app.post("/api/sessions")
def create_session():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "Sensor run")).strip()[:120] or "Sensor run"
    db = get_session()
    try:
        row = TrackingSession(title=title, started_at=datetime.now(timezone.utc), step_count=0, peak_error=0.0)
        db.add(row)
        db.commit()
        db.refresh(row)
        identity_matrix = np.eye(3).tolist()
        live_hub.sync_session(
            session_id=row.id,
            step_count=0,
            matrix=identity_matrix,
            orthogonality_error=0.0,
            determinant=1.0,
            source="Manual session",
            broadcast=False,
        )
        return jsonify({"session": _session_summary(row), "matrix": identity_matrix}), 201
    except SQLAlchemyError:
        db.rollback()
        app.logger.exception("session creation failed")
        return _json_error("Could not save this session. Check the database connection.", 503)
    finally:
        db.close()


@app.get("/api/sessions/<int:session_id>")
def get_session_detail(session_id: int):
    db = get_session()
    try:
        row = _get_record(db, session_id)
        if row is None:
            return _json_error("Session not found", 404)
        sensors = db.scalars(select(SensorLog).where(SensorLog.session_id == session_id)
                             .order_by(SensorLog.step_index, SensorLog.id)).all()
        orientations = db.scalars(select(OrientationRecord).where(OrientationRecord.session_id == session_id)
                                  .order_by(OrientationRecord.step_index, OrientationRecord.id)).all()
        return jsonify({
            "session": _session_summary(row),
            "sensor_logs": [{"step_index": s.step_index, "x": s.x_deg, "y": s.y_deg,
                             "z": s.z_deg, "order": s.order_name,
                             "quantize_decimals": s.quantize_decimals} for s in sensors],
            "orientations": [{"step_index": o.step_index, "matrix": o.matrix,
                              "orthogonality_error": o.orthogonality_error,
                              "determinant": o.determinant, "repaired": o.repaired}
                             for o in orientations],
        })
    finally:
        db.close()


@app.post("/api/sessions/<int:session_id>/steps")
def append_step(session_id: int):
    data = request.get_json(silent=True) or {}
    angles = {}
    try:
        for axis in ("x", "y", "z"):
            angles[axis] = _float_angle(data.get(axis, 0.0), axis)
        order = str(data.get("order", "XYZ")).upper()
        if order not in {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}:
            raise ValueError("order must contain X, Y, and Z exactly once")
        decimals = data.get("quantize_decimals")
        if decimals is not None:
            decimals = int(decimals)
            if decimals < 0 or decimals > 12:
                raise ValueError("quantize_decimals must be between 0 and 12")
    except (ValueError, TypeError) as exc:
        return _json_error(str(exc))

    db = get_session()
    try:
        row = _get_record(db, session_id)
        if row is None:
            return _json_error("Session not found", 404)
        last = db.scalar(select(OrientationRecord).where(OrientationRecord.session_id == session_id)
                         .order_by(OrientationRecord.step_index.desc(), OrientationRecord.id.desc()).limit(1))
        current = np.asarray(last.matrix, dtype=float) if last else np.eye(3, dtype=float)
        matrix = compose_order(angles, order, current=current, quantize_decimals=decimals)
        error = orthogonality_error(matrix)
        determinant = determinant_3x3(matrix)
        step_index = row.step_count + 1
        db.add(SensorLog(session_id=session_id, step_index=step_index,
                         x_deg=angles["x"], y_deg=angles["y"], z_deg=angles["z"],
                         order_name=order, quantize_decimals=decimals))
        db.add(OrientationRecord(session_id=session_id, step_index=step_index, matrix=matrix.tolist(),
                                 orthogonality_error=error, determinant=determinant, repaired=False))
        row.step_count = step_index
        row.peak_error = max(row.peak_error, error)
        db.commit()
        matrix_list = matrix.tolist()
        live_hub.sync_session(
            session_id=session_id,
            step_count=step_index,
            matrix=matrix_list,
            orthogonality_error=error,
            determinant=determinant,
            angles=angles,
            source="Workbench step",
            broadcast=False,
        )
        return jsonify({"step_index": step_index, "matrix": matrix_list,
                        "orthogonality_error": error, "determinant": determinant,
                        "order": order, "quantize_decimals": decimals})
    except SQLAlchemyError:
        db.rollback()
        app.logger.exception("sensor step save failed")
        return _json_error("Could not persist this sensor step.", 503)
    finally:
        db.close()


@app.post("/api/sessions/<int:session_id>/stress")
def precision_stress(session_id: int):
    from ds.stream_buffer import SensorQueue, WindowedBuffer

    data = request.get_json(silent=True) or {}
    try:
        count = int(data.get("count", 1500))
        decimals = int(data.get("decimals", 6))
        seed = int(data.get("seed", 28))
        order = str(data.get("order", "XYZ")).upper()
        if count < 1 or count > 3000:
            raise ValueError("stress count must be between 1 and 3000")
        if decimals < 0 or decimals > 12:
            raise ValueError("decimals must be between 0 and 12")
        if order not in {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}:
            raise ValueError("order must contain X, Y, and Z exactly once")
        steps = simulate_steps(count, seed)
    except (ValueError, TypeError) as exc:
        return _json_error(str(exc))

    db = get_session()
    try:
        row = _get_record(db, session_id)
        if row is None:
            return _json_error("Session not found", 404)
        last = db.scalar(select(OrientationRecord).where(OrientationRecord.session_id == session_id)
                         .order_by(OrientationRecord.step_index.desc(), OrientationRecord.id.desc()).limit(1))
        matrix = np.asarray(last.matrix, dtype=float) if last else np.eye(3, dtype=float)
        first_step = row.step_count
        peak = row.peak_error
        sensor_queue = SensorQueue(capacity=count)
        for sample in steps:
            sensor_queue.enqueue(sample)
        recent_window = WindowedBuffer(capacity=60)
        for offset in range(1, count + 1):
            angles = sensor_queue.dequeue()
            matrix = compose_order(angles, order, current=matrix, quantize_decimals=decimals)
            error = orthogonality_error(matrix)
            determinant = determinant_3x3(matrix)
            step_index = first_step + offset
            db.add(SensorLog(session_id=session_id, step_index=step_index,
                             x_deg=angles["x"], y_deg=angles["y"], z_deg=angles["z"],
                             order_name=order, quantize_decimals=decimals))
            db.add(OrientationRecord(session_id=session_id, step_index=step_index,
                                     matrix=matrix.tolist(), orthogonality_error=error,
                                     determinant=determinant, repaired=False))
            peak = max(peak, error)
            recent_window.append({"step_index": step_index, "angles": angles,
                                  "orthogonality_error": error, "determinant": determinant})
        row.step_count = first_step + count
        row.peak_error = peak
        db.commit()
        final_error = orthogonality_error(matrix)
        final_det = determinant_3x3(matrix)
        matrix_list = matrix.tolist()
        live_hub.sync_session(
            session_id=session_id,
            step_count=row.step_count,
            matrix=matrix_list,
            orthogonality_error=final_error,
            determinant=final_det,
            source="Precision stress",
            broadcast=False,
        )
        return jsonify({"step_index": row.step_count, "matrix": matrix_list,
                        "orthogonality_error": final_error,
                        "determinant": final_det, "count": count,
                        "quantize_decimals": decimals, "order": order,
                        "window": recent_window.values()})
    except SQLAlchemyError:
        db.rollback()
        app.logger.exception("precision stress persistence failed")
        return _json_error("Could not persist the precision stress run.", 503)
    finally:
        db.close()


@app.post("/api/sessions/<int:session_id>/repair")
def repair_session(session_id: int):
    db = get_session()
    try:
        row = _get_record(db, session_id)
        if row is None:
            return _json_error("Session not found", 404)
        last = db.scalar(select(OrientationRecord).where(OrientationRecord.session_id == session_id)
                         .order_by(OrientationRecord.step_index.desc(), OrientationRecord.id.desc()).limit(1))
        if last is None:
            return _json_error("Apply at least one sensor step before repairing")
        repaired = gram_schmidt(last.matrix)
        error = orthogonality_error(repaired)
        determinant = determinant_3x3(repaired)
        db.add(OrientationRecord(session_id=session_id, step_index=row.step_count, matrix=repaired.tolist(),
                                 orthogonality_error=error, determinant=determinant, repaired=True))
        row.peak_error = max(row.peak_error, last.orthogonality_error)
        db.commit()
        repaired_list = repaired.tolist()
        live_hub.sync_session(
            session_id=session_id,
            step_count=row.step_count,
            matrix=repaired_list,
            orthogonality_error=error,
            determinant=determinant,
            source="Gram–Schmidt repair",
            broadcast=True,
        )
        return jsonify({"matrix": repaired_list, "orthogonality_error": error,
                        "determinant": determinant, "before_error": last.orthogonality_error,
                        "repaired": True})
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        app.logger.exception("orientation repair failed")
        return _json_error(str(exc), 500 if isinstance(exc, SQLAlchemyError) else 400)
    finally:
        db.close()


@app.post("/api/sessions/<int:session_id>/finish")
def finish_session(session_id: int):
    db = get_session()
    try:
        row = _get_record(db, session_id)
        if row is None:
            return _json_error("Session not found", 404)
        row.ended_at = datetime.now(timezone.utc)
        db.commit()
        if live_hub.active_session_id == session_id:
            live_hub.clear_session(broadcast=True)
        return jsonify({"session": _session_summary(row)})
    except SQLAlchemyError:
        db.rollback()
        app.logger.exception("session finish failed")
        return _json_error("Could not finish this session.", 503)
    finally:
        db.close()


@app.get("/api/sessions/<int:session_id>/plot.png")
def session_plot(session_id: int):
    db = get_session()
    try:
        row = _get_record(db, session_id)
        if row is None:
            return _json_error("Session not found", 404)
        points = db.scalars(select(OrientationRecord).where(OrientationRecord.session_id == session_id)
                            .order_by(OrientationRecord.step_index, OrientationRecord.id)).all()
        fig, ax = plt.subplots(figsize=(8.3, 2.45), dpi=140)
        fig.patch.set_facecolor("#f7f7f2")
        ax.set_facecolor("#f7f7f2")
        ax.spines[["top", "right"]].set_visible(False)
        ax.spines[["left", "bottom"]].set_color("#d4d9d7")
        ax.tick_params(colors="#697572", labelsize=8)
        ax.grid(axis="y", color="#dce2df", linewidth=0.7, alpha=0.8)
        if points:
            x = [p.step_index for p in points]
            y = [max(p.orthogonality_error, 1e-18) for p in points]
            ax.plot(x, y, color="#12a995", linewidth=2, marker="o", markersize=2.5)
            repaired_x = [p.step_index for p in points if p.repaired]
            repaired_y = [max(p.orthogonality_error, 1e-18) for p in points if p.repaired]
            if repaired_x:
                ax.scatter(repaired_x, repaired_y, marker="D", s=34, color="#ff7869", zorder=4, label="Gram–Schmidt repair")
                ax.legend(frameon=False, fontsize=8)
        ax.set_yscale("log")
        ax.set_xlabel("sensor step", color="#54625f", fontsize=8)
        ax.set_ylabel("‖QᵀQ − I‖ (log)", color="#54625f", fontsize=8)
        ax.set_title(row.title, color="#233330", fontsize=10, loc="left", pad=8)
        fig.tight_layout(pad=1.2)
        output = io.BytesIO()
        fig.savefig(output, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        output.seek(0)
        return send_file(output, mimetype="image/png", max_age=0)
    finally:
        db.close()


@app.post("/api/compare")
def compare():
    data = request.get_json(silent=True) or {}
    try:
        angles = {axis: _float_angle(data.get(axis, 0.0), axis) for axis in ("x", "y", "z")}
        results = compare_orders(angles)
        return jsonify({"angles": angles, **results})
    except (ValueError, TypeError) as exc:
        return _json_error(str(exc))


def _extract_ingest_payload():
    data = request.get_json(force=True, silent=True)
    if data is not None:
        return data
    if request.form:
        return request.form.to_dict(flat=True)
    if request.args:
        return request.args.to_dict(flat=True)
    return {}


@app.get("/api/stream/info")
def stream_info():
    return jsonify({**_ingest_urls(), **live_hub.snapshot()})


@app.post("/api/stream/control")
def stream_control():
    data = request.get_json(silent=True) or {}
    try:
        if "order" in data:
            live_hub.set_order(data["order"])
        if "listening" in data:
            live_hub.set_listening(bool(data["listening"]))
        if data.get("action") == "reset":
            live_hub.clear_session(broadcast=True)
        return jsonify({**_ingest_urls(), **live_hub.snapshot()})
    except ValueError as exc:
        return _json_error(str(exc))


@app.get("/api/stream/events")
def stream_events():
    sub = live_hub.subscribe()

    def generate():
        try:
            while True:
                try:
                    event_name, payload = sub.get(timeout=15.0)
                    yield f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            live_hub.unsubscribe(sub)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/sensorlog", methods=["GET", "POST"])
@app.route("/api/stream/ingest", methods=["GET", "POST"])
def sensorlog_ingest():
    if request.method == "GET" and not request.args:
        return jsonify({"ok": True, "ready": True, **_ingest_urls(), **live_hub.snapshot()})

    if not live_hub.listening:
        return jsonify({"ok": True, "accepted": False, "paused": True})

    payload = _extract_ingest_payload()
    samples = parse_sensor_samples(payload)
    if not samples:
        return _json_error(
            "No valid SensorLog or orientation fields found. Enable Gyro or Device Motion in SensorLog.",
            400,
        )

    order_raw = request.args.get("order") or (payload.get("order") if isinstance(payload, dict) else None)
    order = str(order_raw or live_hub.order).upper().strip()
    if order not in VALID_ORDERS:
        return _json_error("order must contain X, Y, and Z exactly once", 400)

    requested_sid = request.args.get("session_id") or (payload.get("session_id") if isinstance(payload, dict) else None)
    if isinstance(requested_sid, str) and not requested_sid.isdigit():
        requested_sid = None

    with live_hub.lock:
        db = get_session()
        try:
            row = None
            target_sid = int(requested_sid) if requested_sid is not None else live_hub.active_session_id
            if target_sid is not None:
                candidate = _get_record(db, target_sid)
                if candidate is not None and candidate.ended_at is None:
                    row = candidate
            if row is None:
                title = "iPhone SensorLog"
                if isinstance(payload, dict) and payload.get("title"):
                    title = str(payload["title"]).strip()[:120] or title
                row = TrackingSession(
                    title=title,
                    started_at=datetime.now(timezone.utc),
                    step_count=0,
                    peak_error=0.0,
                )
                db.add(row)
                db.flush()

            last = db.scalar(
                select(OrientationRecord)
                .where(OrientationRecord.session_id == row.id)
                .order_by(OrientationRecord.step_index.desc(), OrientationRecord.id.desc())
                .limit(1)
            )
            current = np.asarray(last.matrix, dtype=float) if last else np.eye(3, dtype=float)
            step_index = row.step_count
            peak = row.peak_error

            last_result = None
            for sample in samples:
                angles = live_hub.to_delta_degrees(sample)
                current = compose_order(angles, order, current=current)
                error = orthogonality_error(current)
                determinant = determinant_3x3(current)
                step_index += 1
                peak = max(peak, error)
                matrix_list = current.tolist()
                db.add(SensorLog(
                    session_id=row.id,
                    step_index=step_index,
                    x_deg=angles["x"],
                    y_deg=angles["y"],
                    z_deg=angles["z"],
                    order_name=order,
                    quantize_decimals=None,
                ))
                db.add(OrientationRecord(
                    session_id=row.id,
                    step_index=step_index,
                    matrix=matrix_list,
                    orthogonality_error=error,
                    determinant=determinant,
                    repaired=False,
                ))
                last_result = {
                    "session_id": row.id,
                    "step_index": step_index,
                    "angles": angles,
                    "matrix": matrix_list,
                    "orthogonality_error": error,
                    "determinant": determinant,
                    "order": order,
                    "source": str(sample.get("source") or "iPhone SensorLog"),
                }

            row.step_count = step_index
            row.peak_error = peak
            db.commit()

            if last_result is not None:
                broadcast_payload = live_hub.enqueue_and_record(**last_result)
                return jsonify({"ok": True, "accepted": True, "ingested": len(samples), **broadcast_payload})
            return jsonify({"ok": True, "accepted": True, "ingested": 0})
        except ValueError as exc:
            db.rollback()
            return _json_error(str(exc), 400)
        except SQLAlchemyError:
            db.rollback()
            app.logger.exception("sensorlog ingest persistence failed")
            return _json_error("Could not persist incoming SensorLog sample.", 503)
        finally:
            db.close()


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return _json_error("Not found", 404)
    return render_template("index.html"), 404


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
