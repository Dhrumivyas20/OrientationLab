"""Small, inspectable 3D rotation algorithms; no linalg black boxes."""
from __future__ import annotations

import math
from typing import Iterable, Mapping, TypedDict

import numpy as np


class OrderComparison(TypedDict):
    xyz: list[list[float]]
    zyx: list[list[float]]
    difference: float


def rotation_matrix(axis: str, angle_radians: float) -> np.ndarray:
    """Return the right-handed active rotation about one principal axis."""
    c, s = math.cos(float(angle_radians)), math.sin(float(angle_radians))
    axis = axis.upper()
    if axis == "X":
        values = [[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]]
    elif axis == "Y":
        values = [[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]]
    elif axis == "Z":
        values = [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]
    else:
        raise ValueError("axis must be X, Y, or Z")
    return np.asarray(values, dtype=float)


def matrix_multiply(left: Iterable[Iterable[float]], right: Iterable[Iterable[float]]) -> np.ndarray:
    """Multiply two 3x3 matrices with explicit row/column accumulation."""
    a, b = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    if a.shape != (3, 3) or b.shape != (3, 3):
        raise ValueError("matrix_multiply expects two 3x3 matrices")
    out = [[0.0] * 3 for _ in range(3)]
    for row in range(3):
        for col in range(3):
            total = 0.0
            for k in range(3):
                total += float(a[row, k]) * float(b[k, col])
            out[row][col] = total
    return np.asarray(out, dtype=float)


def compose_order(angles_degrees: Mapping[str, float], order: str = "XYZ", current=None,
                  quantize_decimals: int | None = None) -> np.ndarray:
    """Right-compose rotations in order into `current`; XYZ means Q <- Q Rx Ry Rz.

    For column vectors this explicit convention is easy to compare against other
    conventions. Quantization is only used for a finite-precision stress demo.
    """
    order = order.upper().replace("→", "").replace(",", "").replace(" ", "")
    if len(order) != 3 or set(order) != {"X", "Y", "Z"}:
        raise ValueError("order must be a permutation of XYZ")
    result = np.eye(3, dtype=float) if current is None else np.asarray(current, dtype=float).copy()
    for axis in order:
        degrees = angles_degrees.get(axis.lower(), angles_degrees.get(axis, 0.0))
        result = matrix_multiply(result, rotation_matrix(axis, math.radians(float(degrees))))
        if quantize_decimals is not None:
            result = np.round(result, int(quantize_decimals))
    return result


def orthogonality_error(matrix) -> float:
    """Frobenius distance ||QᵀQ − I||, evaluated with explicit loops."""
    q = np.asarray(matrix, dtype=float)
    if q.shape != (3, 3):
        raise ValueError("orientation must be 3x3")
    squared = 0.0
    for row in range(3):
        for col in range(3):
            dot = sum(float(q[k, row]) * float(q[k, col]) for k in range(3))
            delta = dot - (1.0 if row == col else 0.0)
            squared += delta * delta
    return math.sqrt(squared)


def determinant_3x3(matrix) -> float:
    """Evaluate a 3x3 determinant by cofactor expansion."""
    q = np.asarray(matrix, dtype=float)
    if q.shape != (3, 3):
        raise ValueError("orientation must be 3x3")
    a, b, c = (float(q[0, i]) for i in range(3))
    d, e, f = (float(q[1, i]) for i in range(3))
    g, h, i = (float(q[2, j]) for j in range(3))
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def gram_schmidt(matrix) -> np.ndarray:
    """Re-orthonormalise matrix columns, preserving a right-handed frame."""
    q = np.asarray(matrix, dtype=float)
    if q.shape != (3, 3):
        raise ValueError("orientation must be 3x3")
    columns: list[list[float]] = []
    for col in range(3):
        vector = [float(q[row, col]) for row in range(3)]
        for basis in columns:
            projection = sum(vector[k] * basis[k] for k in range(3))
            vector = [vector[k] - projection * basis[k] for k in range(3)]
        length = math.sqrt(sum(value * value for value in vector))
        if length < 1e-12:
            raise ValueError("cannot repair a matrix with dependent columns")
        columns.append([value / length for value in vector])
    repaired = np.asarray([[columns[col][row] for col in range(3)] for row in range(3)])
    if determinant_3x3(repaired) < 0:
        for row in range(3):
            repaired[row, 2] *= -1.0
    return repaired


def compare_orders(angles_degrees: Mapping[str, float]) -> OrderComparison:
    xyz = compose_order(angles_degrees, "XYZ")
    zyx = compose_order(angles_degrees, "ZYX")
    difference = math.sqrt(sum((float(xyz[r, c]) - float(zyx[r, c])) ** 2
                               for r in range(3) for c in range(3)))
    return {"xyz": xyz.tolist(), "zyx": zyx.tolist(), "difference": difference}


def simulate_steps(count: int, seed: int = 28) -> list[dict[str, float]]:
    """Repeatable small rotations, suitable for a precision-stress run."""
    if count < 0 or count > 10000:
        raise ValueError("count must be between 0 and 10000")
    rng = np.random.default_rng(seed)
    return [{axis: float(rng.uniform(-0.35, 0.35)) for axis in ("x", "y", "z")}
            for _ in range(count)]
