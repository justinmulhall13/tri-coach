"""Interval-aware execution analysis for completed Garmin activities.

Garmin's activity summary blends warm-up, work, recovery, and cool-down into one
average heart rate. That number is useful for a steady session, but it cannot
grade intervals. This module joins recorded interval boundaries, the timestamped
HR trace, and the associated structured workout so Coach can evaluate each bout.
"""
from __future__ import annotations

import datetime
import re
from functools import lru_cache
from typing import Any, Iterable

from . import garmin_source


_HR_RANGE_RE = re.compile(
    r"(?:@\s*)?(?:HR|heart\s*rate)\s*[:@]?\s*(\d{2,3})\s*[\-–—]\s*(\d{2,3})",
    re.IGNORECASE,
)


def explicit_hr_range(*texts: str | None) -> tuple[int, int] | None:
    """Return the first explicit, physiologically plausible HR range in text."""
    for text in texts:
        match = _HR_RANGE_RE.search(text or "")
        if not match:
            continue
        lo, hi = int(match.group(1)), int(match.group(2))
        if 60 <= lo < hi <= 230:
            return lo, hi
    return None


def _walk_steps(steps: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for step in steps:
        if not isinstance(step, dict):
            continue
        yield step
        yield from _walk_steps(step.get("workoutSteps") or [])


def _structured_work_target(workout: dict[str, Any]) -> tuple[int, int] | None:
    """Read the bpm bounds actually encoded into Garmin's ACTIVE work step."""
    for segment in workout.get("workoutSegments") or []:
        for step in _walk_steps(segment.get("workoutSteps") or []):
            kind = ((step.get("stepType") or {}).get("stepTypeKey") or "").lower()
            target_kind = ((step.get("targetType") or {}).get("workoutTargetTypeKey") or "").lower()
            lo, hi = step.get("targetValueOne"), step.get("targetValueTwo")
            if kind != "interval" or "heart" not in target_kind:
                continue
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo < hi:
                return int(round(lo)), int(round(hi))
    return None


def _descriptor_indexes(detail: dict[str, Any]) -> dict[str, int]:
    return {
        d.get("key"): d.get("metricsIndex")
        for d in detail.get("metricDescriptors") or []
        if isinstance(d, dict) and isinstance(d.get("metricsIndex"), int)
    }


def _hr_points(detail: dict[str, Any]) -> list[tuple[float, float]]:
    indexes = _descriptor_indexes(detail)
    time_i, hr_i = indexes.get("directTimestamp"), indexes.get("directHeartRate")
    if time_i is None or hr_i is None:
        return []
    points: list[tuple[float, float]] = []
    for row in detail.get("activityDetailMetrics") or []:
        metrics = row.get("metrics") if isinstance(row, dict) else None
        if not isinstance(metrics, list) or max(time_i, hr_i) >= len(metrics):
            continue
        stamp, hr = metrics[time_i], metrics[hr_i]
        if isinstance(stamp, (int, float)) and isinstance(hr, (int, float)):
            # directTimestamp is epoch milliseconds.
            points.append((float(stamp) / 1000.0, float(hr)))
    return sorted(points)


def _utc_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _phase_name(split: dict[str, Any]) -> str:
    return str(split.get("type") or split.get("intensityType") or "").upper()


def _active_splits(typed_splits: dict[str, Any]) -> list[dict[str, Any]]:
    rows = typed_splits.get("splits") or typed_splits.get("lapDTOs") or []
    return [s for s in rows if isinstance(s, dict) and "ACTIVE" in _phase_name(s)]


def _weighted_window(
    points: list[tuple[float, float]],
    start: float,
    end: float,
    target: tuple[int, int] | None,
) -> dict[str, Any]:
    """Duration-weight an HR trace inside one interval.

    Each sample owns the time until the next sample. Gaps over 30 seconds are
    treated as missing instead of pretending the last HR value held indefinitely.
    """
    observed = below = inside = above = weighted_hr = 0.0
    maximum: float | None = None
    point_count = 0
    for i, (stamp, hr) in enumerate(points):
        next_stamp = points[i + 1][0] if i + 1 < len(points) else end
        seg_start, seg_end = max(stamp, start), min(next_stamp, end)
        if seg_end <= seg_start or next_stamp - stamp > 30:
            continue
        seconds = seg_end - seg_start
        observed += seconds
        weighted_hr += hr * seconds
        maximum = hr if maximum is None else max(maximum, hr)
        point_count += 1
        if target:
            lo, hi = target
            if hr < lo:
                below += seconds
            elif hr > hi:
                above += seconds
            else:
                inside += seconds

    out: dict[str, Any] = {
        "observed_sec": round(observed, 1),
        "trace_points": point_count,
        "avg_hr": round(weighted_hr / observed, 1) if observed else None,
        "max_hr": int(round(maximum)) if maximum is not None else None,
        "coverage_pct": round(observed / max(end - start, 1) * 100),
    }
    if target:
        out.update({
            "time_below_target_min": round(below / 60, 2),
            "time_in_target_min": round(inside / 60, 2),
            "time_above_target_min": round(above / 60, 2),
            "time_at_or_above_target_floor_min": round((inside + above) / 60, 2),
            "target_time_pct": round(inside / observed * 100) if observed else None,
            "at_or_above_target_floor_pct": round((inside + above) / observed * 100) if observed else None,
        })
    return out


def analyze(
    activity: dict[str, Any],
    typed_splits: dict[str, Any],
    detail: dict[str, Any],
    workout: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Create a compact, model-ready interval execution record from Garmin data."""
    active = _active_splits(typed_splits)
    if not active:
        return None

    workout = workout or {}
    description = str(workout.get("description") or "")
    displayed_target = explicit_hr_range(description, workout.get("workoutName"))
    device_target = _structured_work_target(workout)
    target = displayed_target or device_target
    points = _hr_points(detail)

    intervals: list[dict[str, Any]] = []
    for number, split in enumerate(active, 1):
        start = _utc_timestamp(split.get("startTimeGMT"))
        duration = split.get("duration") or split.get("elapsedDuration") or 0
        if start is None or not isinstance(duration, (int, float)) or duration <= 0:
            continue
        window = _weighted_window(points, start, start + float(duration), target)
        # Garmin's split average remains a useful fallback when chart coverage is
        # sparse; normally the duration-weighted trace is more precise.
        if window.get("avg_hr") is None and isinstance(split.get("averageHR"), (int, float)):
            window["avg_hr"] = round(float(split["averageHR"]), 1)
        if window.get("max_hr") is None and isinstance(split.get("maxHR"), (int, float)):
            window["max_hr"] = int(round(split["maxHR"]))
        intervals.append({
            "interval": number,
            "duration_min": round(float(duration) / 60, 2),
            **window,
        })

    if not intervals:
        return None

    summary = activity.get("summaryDTO") or {}
    all_splits = typed_splits.get("splits") or typed_splits.get("lapDTOs") or []
    phase_timeline = [
        {
            "phase": _phase_name(split).replace("INTERVAL_", "").lower(),
            "duration_min": round(float(split.get("duration") or 0) / 60, 2),
        }
        for split in all_splits
        if isinstance(split, dict) and isinstance(split.get("duration"), (int, float))
    ]
    result: dict[str, Any] = {
        "activity_id": activity.get("activityId"),
        "associated_workout_id": (activity.get("metadataDTO") or {}).get("associatedWorkoutId"),
        "workout_name": workout.get("workoutName"),
        "workout_description": description or None,
        "recorded_interval_count": len(intervals),
        "recorded_phase_timeline": phase_timeline,
        "prescribed_work_target_bpm": list(target) if target else None,
        "intervals": intervals,
        "whole_session_avg_hr": summary.get("averageHR"),
        "grading_rule": (
            "Grade this structured session from each ACTIVE interval's duration and HR trace. "
            "Do not compare the whole-session average HR with the work-interval target; warm-up, "
            "recoveries, and cool-down intentionally lower it."
        ),
    }
    if displayed_target and device_target and displayed_target != device_target:
        result["structured_target_mismatch"] = {
            "prescribed_in_workout_text_bpm": list(displayed_target),
            "encoded_on_device_bpm": list(device_target),
            "meaning": "This is a workout-construction mismatch, not athlete execution failure.",
        }
    return result


def _safe(call, default):
    try:
        return call()
    except Exception:  # Garmin detail is best-effort context, never a chat blocker.
        return default


@lru_cache(maxsize=64)
def get(activity_id: int) -> dict[str, Any] | None:
    """Fetch and cache interval execution for an immutable completed activity."""
    client = garmin_source.get_client()
    activity = _safe(lambda: client.get_activity(activity_id), {}) or {}
    typed = _safe(lambda: client.get_activity_typed_splits(activity_id), {}) or {}
    if not _active_splits(typed):
        # Older activity types expose the same phase labels on regular lap splits.
        typed = _safe(lambda: client.get_activity_splits(activity_id), {}) or {}
    if not _active_splits(typed):
        return None
    detail = _safe(lambda: client.get_activity_details(activity_id, maxchart=2000, maxpoly=0), {}) or {}
    workout_id = (activity.get("metadataDTO") or {}).get("associatedWorkoutId")
    workout = _safe(lambda: client.get_workout_by_id(workout_id), {}) if workout_id else {}
    return analyze(activity, typed, detail, workout)
