#!/usr/bin/env python3
"""Recover a part's displacement by comparing seam scans against a taught reference.

The question this answers
-------------------------
Does the laser's orientation relative to a seam have to be known and controlled,
or is it enough to scan a taught reference part and later compare a new part
against it?

Answer, in short: comparing against a reference does remove the need for any
*particular* orientation, but it cannot remove two hard geometric facts.

1. One laser plane crossing one seam yields exactly **two** numbers. The
   component of the displacement along the seam direction slides the seam
   along itself and moves nothing in the image. That direction is invisible to
   that scan, no matter how good the sensor is.
2. Because each seam donates two constraints with a one-dimensional blind
   spot, several seams with **different directions** are needed to pin down a
   full rigid displacement. Parallel seams share the same blind spot and never
   fill it in, however many are scanned.

Model
-----
A fillet seam is a line: corner point ``P`` with unit direction ``s``. The laser
sweeps a plane with unit normal ``n``. The sensor observes where the seam
pierces that plane, expressed in the plane's own two axes — which is exactly
what a corner fit on a profile delivers.

If the part moves rigidly by translation ``t`` and small rotation ``omega``
about a reference point, material at ``P`` moves by

    d = t + omega x (P - P0)

and the observed piercing point shifts by

    A d,   where  A = I - s n^T / (n . s)

The subtraction is the slide along the seam that carries the point back onto the
plane. Projecting ``A d`` onto the plane's two axes gives the two measurements.
Stacking those over several seams and solving in the least-squares sense
recovers the displacement.

Everything here is geometry. It assumes the robot repeats its taught poses and
the sensor is rigidly mounted, both of which are ordinary.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass

Vector = tuple[float, float, float]


# --------------------------------------------------------------------------
# small vector helpers, kept dependency-free like the rest of the host tools
# --------------------------------------------------------------------------

def add(a: Vector, b: Vector) -> Vector:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def subtract(a: Vector, b: Vector) -> Vector:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a: Vector, factor: float) -> Vector:
    return (a[0] * factor, a[1] * factor, a[2] * factor)


def dot(a: Vector, b: Vector) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vector, b: Vector) -> Vector:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm(a: Vector) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Vector) -> Vector:
    length = norm(a)
    if length < 1e-12:
        raise ValueError("cannot normalise a zero-length vector")
    return scale(a, 1.0 / length)


def plane_axes(normal: Vector) -> tuple[Vector, Vector]:
    """Two orthonormal axes spanning the plane with the given normal."""
    normal = unit(normal)
    seed = (1.0, 0.0, 0.0) if abs(normal[0]) < 0.9 else (0.0, 1.0, 0.0)
    first = unit(subtract(seed, scale(normal, dot(seed, normal))))
    return first, cross(normal, first)


def solve_least_squares(rows: list[list[float]], values: list[float]) -> list[float]:
    """Normal equations with partial pivoting. Small systems only."""
    unknowns = len(rows[0])
    matrix = [[0.0] * unknowns for _ in range(unknowns)]
    vector = [0.0] * unknowns
    for row, value in zip(rows, values, strict=True):
        for i in range(unknowns):
            vector[i] += row[i] * value
            for j in range(unknowns):
                matrix[i][j] += row[i] * row[j]

    augmented = [matrix[i][:] + [vector[i]] for i in range(unknowns)]
    for column in range(unknowns):
        pivot = max(range(column, unknowns), key=lambda r: abs(augmented[r][column]))
        if abs(augmented[pivot][column]) < 1e-9:
            raise ValueError(
                "singular system: the scanned seams do not constrain every "
                "unknown. Parallel seams are the usual cause."
            )
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row_index in range(unknowns):
            if row_index == column:
                continue
            factor = augmented[row_index][column]
            augmented[row_index] = [
                left - factor * right
                for left, right in zip(augmented[row_index], augmented[column], strict=True)
            ]
    return [augmented[i][-1] for i in range(unknowns)]


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SeamScan:
    """One taught scan: where a seam is, and which plane the laser sweeps."""
    name: str
    corner: Vector          # a point on the seam line
    direction: Vector       # unit vector along the seam
    plane_normal: Vector    # unit normal of the laser plane

    def crossing_angle_deg(self) -> float:
        """Angle between the laser plane and the seam. 90 deg is perpendicular."""
        return math.degrees(math.asin(min(1.0, abs(dot(unit(self.direction), unit(self.plane_normal))))))


def observation_matrix(scan: SeamScan, reference_point: Vector) -> list[list[float]]:
    """The 2x6 map from [translation, rotation] to the two observed numbers."""
    direction = unit(scan.direction)
    normal = unit(scan.plane_normal)
    denominator = dot(normal, direction)
    if abs(denominator) < 1e-9:
        raise ValueError(
            f"seam '{scan.name}' lies in the laser plane; it is never crossed"
        )
    axes = plane_axes(normal)
    lever = subtract(scan.corner, reference_point)

    rows: list[list[float]] = []
    for axis in axes:
        # axis . A, where A = I - s n^T / (n . s)
        projected = subtract(axis, scale(normal, dot(axis, direction) / denominator))
        # d = t + omega x lever  ->  omega contributes cross(lever, projected)
        moment = cross(lever, projected)
        rows.append([projected[0], projected[1], projected[2],
                     moment[0], moment[1], moment[2]])
    return rows


def observe(scan: SeamScan, translation: Vector, rotation: Vector,
            reference_point: Vector) -> tuple[float, float]:
    """The two numbers this scan would report for the given displacement."""
    rows = observation_matrix(scan, reference_point)
    parameters = list(translation) + list(rotation)
    return tuple(sum(a * b for a, b in zip(row, parameters, strict=True)) for row in rows)


def solve_displacement(
    scans: list[SeamScan],
    observations: list[tuple[float, float]],
    reference_point: Vector,
    translation_only: bool = False,
) -> tuple[Vector, Vector]:
    """Least-squares rigid displacement from several scans."""
    rows: list[list[float]] = []
    values: list[float] = []
    for scan, measured in zip(scans, observations, strict=True):
        for row, value in zip(observation_matrix(scan, reference_point), measured, strict=True):
            rows.append(row[:3] if translation_only else row)
            values.append(value)
    if len(rows) < len(rows[0]):
        raise ValueError(
            f"{len(rows)} measurements cannot determine {len(rows[0])} unknowns; "
            f"scan more seams"
        )
    solution = solve_least_squares(rows, values)
    if translation_only:
        return (solution[0], solution[1], solution[2]), (0.0, 0.0, 0.0)
    return (solution[0], solution[1], solution[2]), (solution[3], solution[4], solution[5])


def blind_direction(scan: SeamScan) -> Vector:
    """The displacement direction this scan cannot see: along the seam."""
    return unit(scan.direction)


def profile_corner_angle_deg(face_normal_a: Vector, face_normal_b: Vector,
                             plane_normal: Vector) -> float:
    """Included angle of the corner as it appears in the extracted profile.

    The laser plane cuts each face in a line; the profile is those two lines.
    How sharp the corner looks depends on how the plane is oriented, and a flat
    corner is much harder to locate than a sharp one.
    """
    first = cross(unit(plane_normal), unit(face_normal_a))
    second = cross(unit(plane_normal), unit(face_normal_b))
    if norm(first) < 1e-9 or norm(second) < 1e-9:
        return 0.0
    cosine = abs(dot(unit(first), unit(second)))
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def corner_precision_penalty(profile_corner_deg: float) -> float:
    """How much worse the corner fit gets at this profile angle.

    Two fitted legs meeting at angle gamma intersect with an uncertainty that
    scales as 1 / sin(gamma). A right-angled corner in the profile costs
    nothing; a shallow one multiplies the error.
    """
    sine = math.sin(math.radians(profile_corner_deg))
    if sine < 1e-6:
        return float("inf")
    return 1.0 / sine


DOF_NAMES = ("tx", "ty", "tz", "rx", "ry", "rz")


def _stacked_rows(scans: list[SeamScan], reference_point: Vector,
                  translation_only: bool) -> list[list[float]]:
    rows: list[list[float]] = []
    for scan in scans:
        for row in observation_matrix(scan, reference_point):
            rows.append(row[:3] if translation_only else row)
    return rows


def observability(scans: list[SeamScan], reference_point: Vector,
                  translation_only: bool = False,
                  tolerance: float = 1e-7) -> tuple[int, list[list[float]]]:
    """Rank of the scan set, plus a basis of what it cannot see.

    Every returned null vector is a displacement the whole scan set is blind
    to. An empty list means the set determines the displacement completely.
    """
    rows = _stacked_rows(scans, reference_point, translation_only)
    unknowns = len(rows[0])
    # Normal matrix, then reduce to row echelon form to read off the null space.
    normal = [[0.0] * unknowns for _ in range(unknowns)]
    for row in rows:
        for i in range(unknowns):
            for j in range(unknowns):
                normal[i][j] += row[i] * row[j]
    scale_factor = max((abs(value) for row in normal for value in row), default=1.0)
    if scale_factor <= 0.0:
        scale_factor = 1.0
    work = [[value / scale_factor for value in row] for row in normal]

    pivots: list[int] = []
    pivot_row = 0
    for column in range(unknowns):
        candidate = None
        best = tolerance
        for row_index in range(pivot_row, unknowns):
            if abs(work[row_index][column]) > best:
                best = abs(work[row_index][column])
                candidate = row_index
        if candidate is None:
            continue
        work[pivot_row], work[candidate] = work[candidate], work[pivot_row]
        divisor = work[pivot_row][column]
        work[pivot_row] = [value / divisor for value in work[pivot_row]]
        for row_index in range(unknowns):
            if row_index == pivot_row:
                continue
            factor = work[row_index][column]
            if factor != 0.0:
                work[row_index] = [
                    left - factor * right
                    for left, right in zip(work[row_index], work[pivot_row], strict=True)
                ]
        pivots.append(column)
        pivot_row += 1
        if pivot_row == unknowns:
            break

    free_columns = [column for column in range(unknowns) if column not in pivots]
    basis: list[list[float]] = []
    for free in free_columns:
        vector = [0.0] * unknowns
        vector[free] = 1.0
        for row_index, column in enumerate(pivots):
            vector[column] = -work[row_index][free]
        length = math.sqrt(sum(value * value for value in vector))
        basis.append([value / length for value in vector])
    return len(pivots), basis


def describe_null_vector(vector: list[float]) -> str:
    """Name the degrees of freedom a null direction actually mixes."""
    parts = [
        f"{DOF_NAMES[index]}={value:+.3f}"
        for index, value in enumerate(vector)
        if abs(value) > 1e-6
    ]
    return "  ".join(parts)


PLANAR_COLUMNS = (0, 1, 5)  # tx, ty, rotation about z


def planar_condition(scans: list[SeamScan], reference_point: Vector) -> tuple[int, float]:
    """Rank and noise amplification when only in-plane motion is solved for.

    A plate bolted flat to a table can realistically only slide in x and y and
    rotate about z; the table constrains the rest. Returns (rank, gain), with
    an infinite gain when the scans do not determine all three.
    """
    rows = [[row[column] for column in PLANAR_COLUMNS]
            for row in _stacked_rows(scans, reference_point, False)]
    # Rows that carry no in-plane information at all cannot help.
    rows = [row for row in rows if any(abs(value) > 1e-12 for value in row)]
    if len(rows) < len(PLANAR_COLUMNS):
        return len(rows), float("inf")
    worst = 0.0
    for index in range(len(rows)):
        values = [1.0 if i == index else 0.0 for i in range(len(rows))]
        try:
            solution = solve_least_squares(rows, values)
        except ValueError:
            return len(rows), float("inf")
        worst = max(worst, math.sqrt(sum(value * value for value in solution)))
    return len(PLANAR_COLUMNS), worst


def condition_estimate(scans: list[SeamScan], reference_point: Vector,
                       translation_only: bool = False) -> float:
    """How much measurement noise is amplified into the solved displacement.

    Probes the system with a unit error on each measurement in turn and reports
    the worst-case gain. Large values mean some direction is barely constrained
    even though it is technically observable.
    """
    rows = _stacked_rows(scans, reference_point, translation_only)
    worst = 0.0
    for index in range(len(rows)):
        values = [1.0 if i == index else 0.0 for i in range(len(rows))]
        try:
            solution = solve_least_squares(rows, values)
        except ValueError:
            return float("inf")
        worst = max(worst, math.sqrt(sum(value * value for value in solution)))
    return worst


# --------------------------------------------------------------------------
# demonstration scenarios
# --------------------------------------------------------------------------

def _scan(name: str, corner: Vector, direction: Vector, plane_normal: Vector) -> SeamScan:
    return SeamScan(name, corner, unit(direction), unit(plane_normal))


def scenario_parallel_seams() -> list[SeamScan]:
    """Ten seams, all running the same way. The classic fixture mistake."""
    return [
        _scan(f"parallel_{index}", (0.0, index * 40.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0))
        for index in range(10)
    ]


def scenario_crossed_but_centred() -> list[SeamScan]:
    """Three seam directions, but every scan sits on an axis through the origin.

    Directions differ, so this looks safe. It is not: a rotation about z slides
    each of these seams exactly along itself, so the whole set is blind to it.
    Where a seam is scanned matters as much as which way it runs.
    """
    return [
        _scan("front_x", (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        _scan("back_x", (0.0, 220.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        _scan("left_y", (-140.0, 110.0, 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
        _scan("right_y", (140.0, 110.0, 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
        _scan("riser_z", (0.0, 110.0, 90.0), (0.0, 0.0, 1.0), (0.0, 0.0, 1.0)),
    ]


def scenario_mixed_seams() -> list[SeamScan]:
    """A workable set: three directions, scanned well away from the centre.

    The two scans on each long seam sit at opposite ends, which is what makes
    rotation observable at all.
    """
    return [
        _scan("front_left", (-120.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        _scan("front_right", (120.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        _scan("side_near", (-150.0, -90.0, 30.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
        _scan("side_far", (-150.0, 90.0, 30.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
        _scan("riser_a", (110.0, 70.0, 60.0), (0.0, 0.0, 1.0), (0.0, 0.0, 1.0)),
        _scan("riser_b", (-110.0, 70.0, 60.0), (0.0, 0.0, 1.0), (0.0, 0.0, 1.0)),
    ]


def scenario_base_plate_corners(
    length_mm: float = 400.0,
    width_mm: float = 300.0,
    inset_mm: float = 60.0,
) -> list[SeamScan]:
    """Scans across the four edges of a bolted base plate, near its corners.

    Geometrically an edge is the same thing as a seam: a straight line the laser
    plane crosses. What makes a rectangular plate a far better locating target
    than a set of welds is that its edges run in two perpendicular directions
    and are long enough to scan at widely separated points. Both of the
    degeneracies that trap seam-only scanning are ruled out by construction.
    """
    half_length = length_mm / 2.0
    half_width = width_mm / 2.0

    def reach(half: float) -> float:
        return half - inset_mm

    return [
        # front and back edges run along x, so each is crossed by a plane whose
        # normal is x; scanned near both ends
        _scan("front_left", (-reach(half_length), -half_width, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        _scan("front_right", (reach(half_length), -half_width, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        _scan("back_left", (-reach(half_length), half_width, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        _scan("back_right", (reach(half_length), half_width, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
        # left and right edges run along y
        _scan("left_front", (-half_length, -reach(half_width), 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
        _scan("left_back", (-half_length, reach(half_width), 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
        _scan("right_front", (half_length, -reach(half_width), 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
        _scan("right_back", (half_length, reach(half_width), 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
    ]


def report(title: str, lines: list[str]) -> None:
    print(title)
    print("-" * len(title))
    for line in lines:
        print(f"  {line}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--translation-mm", nargs=3, type=float, default=(1.8, -0.6, 0.4),
                        metavar=("X", "Y", "Z"))
    parser.add_argument("--rotation-mrad", nargs=3, type=float, default=(0.0, 0.0, 2.5),
                        metavar=("RX", "RY", "RZ"))
    parser.add_argument("--json", type=str)
    args = parser.parse_args()

    translation: Vector = tuple(args.translation_mm)
    rotation: Vector = tuple(value / 1000.0 for value in args.rotation_mrad)
    origin: Vector = (0.0, 110.0, 0.0)
    results: dict[str, object] = {
        "true_translation_mm": list(translation),
        "true_rotation_mrad": list(args.rotation_mrad),
    }

    print(f"True part displacement: translation {translation} mm, "
          f"rotation {args.rotation_mrad} mrad\n")

    # 1. one seam: two numbers, one blind direction
    single = scenario_mixed_seams()[0]
    measured = observe(single, translation, (0.0, 0.0, 0.0), origin)
    along_seam = scale(blind_direction(single), 5.0)
    blind_measured = observe(single, along_seam, (0.0, 0.0, 0.0), origin)
    report("1. A single seam sees two numbers and has one blind direction", [
        f"seam direction            {single.direction}",
        f"observed for the true shift  ({measured[0]:+.4f}, {measured[1]:+.4f})",
        f"observed for a 5 mm shift straight along the seam:"
        f"  ({blind_measured[0]:+.4f}, {blind_measured[1]:+.4f})",
        "a displacement along the seam is invisible, exactly as expected",
    ])
    results["single_seam_blind_response"] = list(blind_measured)

    # 2. ten parallel seams still cannot see it
    parallel = scenario_parallel_seams()
    rank, null_basis = observability(parallel, origin, translation_only=True)
    report("2. Ten parallel seams do not fix it", [
        f"scans                     {len(parallel)}  ({2 * len(parallel)} measurements)",
        f"translation rank          {rank} of 3",
        *[f"blind to                  {describe_null_vector(vector)}"
          for vector in null_basis],
        "every seam shares the same blind direction, so scanning more of them",
        "adds measurements but no new information",
    ])
    results["parallel_seams_translation_rank"] = rank

    # 3. two non-parallel seams already determine a pure translation
    two = [scenario_mixed_seams()[0], scenario_mixed_seams()[2]]
    solved_t, _ = solve_displacement(
        two, [observe(s, translation, (0.0, 0.0, 0.0), origin) for s in two],
        origin, translation_only=True,
    )
    error = norm(subtract(solved_t, translation))
    report("3. Two non-parallel seams determine a pure translation", [
        f"seams                     {', '.join(s.name for s in two)}",
        f"recovered translation     ({solved_t[0]:+.6f}, {solved_t[1]:+.6f}, {solved_t[2]:+.6f}) mm",
        f"error                     {error:.2e} mm",
    ])
    results["two_seam_translation_error_mm"] = error

    # 4. differing directions are not sufficient on their own
    centred = scenario_crossed_but_centred()
    rank, null_basis = observability(centred, origin)
    report("4. Three directions are still not enough if the scans are centred", [
        f"scans                     {len(centred)}  ({2 * len(centred)} measurements)",
        f"rigid-displacement rank   {rank} of 6",
        *[f"blind to                  {describe_null_vector(vector)}"
          for vector in null_basis],
        "each of these seams is scanned where a rotation about z slides it",
        "along its own length, so that rotation leaves no trace anywhere",
    ])
    results["centred_rank"] = rank

    # 5. full six degrees of freedom from a workable layout
    mixed = scenario_mixed_seams()
    rank, null_basis = observability(mixed, origin)
    observations = [observe(s, translation, rotation, origin) for s in mixed]
    solved_t, solved_r = solve_displacement(mixed, observations, origin)
    t_error = norm(subtract(solved_t, translation))
    r_error = norm(subtract(solved_r, rotation)) * 1000.0
    report("5. Spread the scans out and all six degrees of freedom come back", [
        f"scans                     {len(mixed)}  ({2 * len(mixed)} measurements, 6 unknowns)",
        f"rigid-displacement rank   {rank} of 6"
        + ("" if null_basis else "   (fully determined)"),
        f"recovered translation     ({solved_t[0]:+.6f}, {solved_t[1]:+.6f}, {solved_t[2]:+.6f}) mm",
        f"recovered rotation        ({solved_r[0]*1000:+.4f}, {solved_r[1]*1000:+.4f}, "
        f"{solved_r[2]*1000:+.4f}) mrad",
        f"translation error         {t_error:.2e} mm",
        f"rotation error            {r_error:.2e} mrad",
        f"noise amplification       {condition_estimate(mixed, origin):.4f}",
    ])
    results["six_dof_rank"] = rank
    results["six_dof_translation_error_mm"] = t_error
    results["six_dof_rotation_error_mrad"] = r_error

    # 6. the laser's own orientation does not change the answer
    report("6. Rotating the laser about its own axis changes nothing", [
        "the same part displacement, measured with the laser plane rolled to",
        "several different angles against each seam:",
    ])
    print(f"  {'crossing angle':>15} {'|t| error':>13} {'noise gain':>13}")
    rolls = []
    for roll_deg in (0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 85.0):
        roll = math.radians(roll_deg)
        rolled = []
        for scan in mixed:
            direction = unit(scan.direction)
            first, _second = plane_axes(direction)
            # tilt the plane normal away from the seam direction by roll
            normal = add(scale(direction, math.cos(roll)), scale(first, math.sin(roll)))
            rolled.append(SeamScan(scan.name, scan.corner, direction, unit(normal)))
        crossing = rolled[0].crossing_angle_deg()
        try:
            solved, _ = solve_displacement(
                rolled, [observe(s, translation, rotation, origin) for s in rolled], origin
            )
            failure = norm(subtract(solved, translation))
            gain = condition_estimate(rolled, origin)
            print(f"  {crossing:>11.1f} deg {failure:>11.1e} mm {gain:>13.3f}")
            rolls.append({"crossing_deg": crossing, "error_mm": failure, "noise_gain": gain})
        except ValueError as exc:
            print(f"  {crossing:>11.1f} deg  {str(exc).split(':')[0]}")
            rolls.append({"crossing_deg": crossing, "error_mm": None})
    print()
    print("  The recovered displacement stays exact at every angle, and the")
    print("  geometric noise gain barely moves. Orientation really is free as")
    print("  far as this solve is concerned.")
    print()
    results["laser_roll_sweep"] = rolls

    # 7. where orientation does cost something: the shape of the profile
    face_a: Vector = (0.0, 0.0, 1.0)   # horizontal plate
    face_b: Vector = (0.0, 1.0, 0.0)   # upright plate
    seam: Vector = (1.0, 0.0, 0.0)
    bisector = unit((0.0, 1.0, 1.0))
    report("7. What orientation does cost: the sharpness of the corner", [
        "the solve above assumed the corner position was measured equally well",
        "at every angle. Extraction does not work that way.",
    ])
    print(f"  {'crossing angle':>15} {'profile corner':>16} {'precision penalty':>19}")
    shapes = []
    for roll_deg in (0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 85.0):
        roll = math.radians(roll_deg)
        normal = unit(add(scale(seam, math.cos(roll)), scale(bisector, math.sin(roll))))
        crossing = math.degrees(math.asin(min(1.0, abs(dot(normal, seam)))))
        corner = profile_corner_angle_deg(face_a, face_b, normal)
        penalty = corner_precision_penalty(corner)
        print(f"  {crossing:>11.1f} deg {corner:>12.1f} deg {penalty:>17.2f} x")
        shapes.append({"crossing_deg": crossing, "profile_corner_deg": corner,
                       "penalty": penalty})
    print()
    print("  Tilting the plane towards the corner bisector flattens the profile")
    print("  corner and multiplies the fit error. Keep the crossing angle above")
    print("  about 45 degrees and the penalty stays under 1.1x; let it fall to")
    print("  15 degrees and every measurement is twice as noisy.")
    print()
    results["profile_shape_sweep"] = shapes

    # 8. locating the fixture itself instead of the welds
    plate = scenario_base_plate_corners()
    rank, null_basis = observability(plate, (0.0, 0.0, 0.0))
    plate_observations = [observe(s, translation, rotation, (0.0, 0.0, 0.0)) for s in plate]
    plate_t, plate_r = solve_displacement(plate, plate_observations, (0.0, 0.0, 0.0))
    report("8. Locating the base plate instead of the welds", [
        f"scans                     {len(plate)}  ({2 * len(plate)} measurements)",
        f"rigid-displacement rank   {rank} of 6"
        + ("   (fully determined)" if not null_basis else ""),
        f"translation error         {norm(subtract(plate_t, translation)):.2e} mm",
        f"rotation error            {norm(subtract(plate_r, rotation)) * 1000:.2e} mrad",
        f"noise amplification       {condition_estimate(plate, (0.0, 0.0, 0.0)):.4f}",
        "",
        "two perpendicular edge directions, each scanned near both ends, rule",
        "out both traps by construction: no shared blind direction, and long",
        "lever arms for every rotation",
    ])
    results["base_plate_rank"] = rank
    results["base_plate_noise_gain"] = condition_estimate(plate, (0.0, 0.0, 0.0))

    # A bolted plate realistically only moves in its own plane.
    planar_rank, planar_gain = planar_condition(plate, (0.0, 0.0, 0.0))
    report("   ... and the realistic case is easier still", [
        "a plate bolted flat to a table moves in x, y and rotation about z.",
        f"in-plane rank             {planar_rank} of 3",
        f"noise amplification       {planar_gain:.4f}",
        "the remaining three degrees of freedom are held by the table, not by",
        "the sensor, which is what makes this the easy first milestone",
    ])
    results["base_plate_planar_noise_gain"] = planar_gain

    # Two corners, then one, to show where the floor is.
    for label, chosen in (
        ("two opposite corners", [plate[0], plate[4], plate[3], plate[7]]),
        ("a single corner", [plate[0], plate[4]]),
    ):
        rank, _ = observability(chosen, (0.0, 0.0, 0.0))
        p_rank, p_gain = planar_condition(chosen, (0.0, 0.0, 0.0))
        gain_text = "unsolvable" if p_gain == float("inf") else f"{p_gain:.4f}"
        report(f"   ... {label}", [
            f"scans                     {len(chosen)}  ({2 * len(chosen)} measurements)",
            f"rigid-displacement rank   {rank} of 6",
            f"in-plane rank             {p_rank} of 3",
            f"in-plane noise gain       {gain_text}",
        ])
        results[f"{label.replace(' ', '_')}_planar_gain"] = (
            None if p_gain == float("inf") else p_gain
        )
    print("  A single corner locates a point but not an orientation: its two")
    print("  scans give two useful in-plane numbers for three unknowns. Two")
    print("  opposite corners are the practical minimum, four the comfortable")
    print("  choice.")
    print()

    report("Conclusion", [
        "Teaching a reference and comparing against it does remove the need for",
        "any particular laser orientation. The reference absorbs it, and the",
        "recovered displacement is exact at every crossing angle tested.",
        "",
        "Three things survive that argument and have to be designed for:",
        "",
        "  1. Each scan is blind along its own seam. Scan seams running in at",
        "     least two genuinely different directions, or that component of",
        "     the shift is simply lost.",
        "  2. Direction alone is not enough. Scans clustered where a rotation",
        "     slides each seam along itself leave that rotation unobservable,",
        "     even with three directions present. Spread them across the part.",
        "  3. Orientation is free for correctness but not for precision. A",
        "     shallow crossing flattens the profile corner and multiplies the",
        "     fit error by 1/sin(corner angle).",
    ])

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
