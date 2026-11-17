"""Fixed offline presentation for aggregate production attribution.

Timeline bins project block/mode estimates onto the timing run. They never
establish hardware activity boundaries or manufacture unobserved pool threads.
"""
import copy
import json
import math
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MATRIX_KEYS = ("mat", "vec", "mem", "comm_c", "fe", "comm_x", "wait", "idle", "unknown")
ISSUE_KEYS = ("mopa", "ld_l1", "ld_l2", "ld_mem", "ld_sib", "ld_xpkg", "fe", "other", "comm_x", "wait", "idle", "unknown")


def _finite(value, default=0.0):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else default


def _die(row):
    return str(row.get("die_id", row.get("die", "unknown")))


def build_timeline(analysis, bin_ns=200000):
    """Bin already-derived segment estimates, retaining their aggregate status.

    Model values on a segment are cycle amounts, not rates. An intersecting bin
    receives overlap/segment-duration of those estimates. Unit utilization uses
    the corresponding cycle weight and is not normalized across unit names.
    Original raw PMU values are untouched; no selected-window raw count is made.
    """
    if not isinstance(bin_ns, int) or isinstance(bin_ns, bool) or bin_ns <= 0:
        raise ValueError("timeline bin_ns must be a positive integer")
    segments = analysis.get("segments", [])
    lanes, phase_bands = {}, []
    time_start, time_end, zero_segments = None, None, 0
    for segment in segments:
        a, b = segment.get("begin_ns"), segment.get("end_ns")
        if not isinstance(a, int) or not isinstance(b, int) or a < 0 or b < a:
            raise ValueError("attribution segment requires integer 0 <= begin_ns <= end_ns")
        if b == a:
            zero_segments += 1
            continue
        time_start = a if time_start is None else min(time_start, a)
        time_end = b if time_end is None else max(time_end, b)
        die = _die(segment)
        lane = lanes.setdefault(die, {"die_id": die, "bins": {}, "threads": set()})
        thread = str(segment.get("thread_id", segment.get("thread", segment.get("buffer_id", "unknown"))))
        lane["threads"].add(thread)
        matrix = segment.get("matrix_first", {})
        issue = segment.get("issue", {})
        units = segment.get("unit_load", segment.get("units", {}))
        cycle_weight = _finite(segment.get("cycles"), sum(_finite(x) for x in matrix.values()))
        unit_weight = _finite(segment.get("unit_denominator_cycles"), cycle_weight)
        stage = str(segment.get("stage", segment.get("phase", "unknown")))
        entry_id = str(segment.get("entry_id", ""))
        first, last = a // bin_ns, (b - 1) // bin_ns
        for index in range(first, last + 1):
            x, y = max(a, index * bin_ns), min(b, (index + 1) * bin_ns)
            fraction = (y - x) / (b - a)
            bucket = lane["bins"].setdefault(index, {
                "begin_ns": index * bin_ns, "end_ns": (index + 1) * bin_ns,
                "matrix_first": defaultdict(float), "issue": defaultdict(float),
                "unit_sum": defaultdict(float), "unit_weights": defaultdict(float),
                "phase_cycles": defaultdict(float), "sampled_thread_ns": 0,
                "threads": set(), "entry_ids": set(), "cycles": 0.0,
            })
            for key, value in matrix.items():
                bucket["matrix_first"][key] += _finite(value) * fraction
            for key, value in issue.items():
                bucket["issue"][key] += _finite(value) * fraction
            for key, value in units.items():
                if isinstance(value, (int, float)) and math.isfinite(value):
                    weight = unit_weight * fraction
                    bucket["unit_sum"][key] += value * weight
                    bucket["unit_weights"][key] += weight
            bucket["phase_cycles"][stage] += cycle_weight * fraction
            bucket["cycles"] += cycle_weight * fraction
            bucket["sampled_thread_ns"] += y - x
            bucket["threads"].add(thread)
            if entry_id:
                bucket["entry_ids"].add(entry_id)
    # Driver intervals are software phase boundaries, never classifier output.
    drivers = analysis.get("drivers", [])
    if isinstance(drivers, dict):
        drivers = [dict(row, die_id=die) for die, rows in drivers.items() for row in rows]
    for row in drivers:
        a, b = row.get("begin_ns"), row.get("end_ns")
        if isinstance(a, int) and isinstance(b, int) and b > a:
            die = _die(row)
            lanes.setdefault(die, {"die_id": die, "bins": {}, "threads": set()})
            phase_bands.append({"die_id": die, "begin_ns": a, "end_ns": b,
                                "name": str(row.get("stage", row.get("phase", row.get("name", row.get("kind", "software"))))),
                                "kind": row.get("kind", "software"), "evidence": "software_boundary"})
            time_start = a if time_start is None else min(time_start, a)
            time_end = b if time_end is None else max(time_end, b)
    output = []
    for die, lane in sorted(lanes.items(), key=lambda p: (not p[0].isdigit(), int(p[0]) if p[0].isdigit() else p[0])):
        bins = []
        for _, bucket in sorted(lane["bins"].items()):
            weights = bucket.pop("unit_weights")
            sums = bucket.pop("unit_sum")
            bucket["units"] = {key: value / weights[key] for key, value in sums.items() if weights[key] > 0}
            bucket["unit_denominator_cycles"] = max(weights.values(), default=0.0)
            bucket["matrix_first"] = dict(bucket["matrix_first"])
            bucket["issue"] = dict(bucket["issue"])
            bucket["phase_cycles"] = dict(bucket["phase_cycles"])
            bucket["phase"] = max(bucket["phase_cycles"], key=bucket["phase_cycles"].get, default="unknown")
            bucket["observed_threads"] = len(bucket.pop("threads"))
            bucket["entry_ids"] = sorted(bucket["entry_ids"])
            bucket["evidence"] = "model_calibrated:uniform_rate_within_block_mode"
            bucket["representation"] = "aggregate"
            bins.append(bucket)
        output.append({"die_id": die, "observed_pool_threads": len(lane["threads"]),
                       "status": "observed_subset" if bins else "no_observed_pool_data", "bins": bins})
    windows = analysis.get("windows", analysis.get("alignment", {}).get("windows", {}))
    window_rows = [row for row in windows.values() if isinstance(row, (list, tuple)) and len(row) == 2 and row[1] > row[0]]
    default_view = [min(row[0] for row in window_rows), max(row[1] for row in window_rows)] if window_rows else [time_start or 0, time_end or bin_ns]
    if window_rows:
        time_start = min(default_view[0], time_start if time_start is not None else default_view[0])
        time_end = max(default_view[1], time_end if time_end is not None else default_view[1])
    return {"bin_ns": bin_ns, "time_range": [time_start or 0, time_end or bin_ns], "default_view_range": default_view,
            "representation": "aggregate", "evidence": "model_calibrated:uniform_rate_within_block_mode",
            "boundary_semantics": "timing-run software boundaries; class fractions are uniform projections of block/mode estimates",
            "raw_counter_selection": "not_prorated", "zero_duration_segments_not_binned": zero_segments,
            "dies": output, "phase_bands": phase_bands}


def report_data(analysis):
    """Keep display evidence; detailed raw/block records remain in the sidecar."""
    data = copy.deepcopy({k: v for k, v in analysis.items() if k not in ("entries", "segments", "drivers")})
    data.setdefault("entry_count", len(analysis.get("entries", [])))
    data.setdefault("segment_count", len(analysis.get("segments", [])))
    if isinstance(data.get("alignment"), dict):
        for key in ("stage_modes", "run_counts", "driver_lanes"):
            data["alignment"].pop(key, None)
    data["report_detail_scope"] = "display_aggregates; complete block and rate evidence remains in the analysis sidecar"
    for lane in data.get("timeline", {}).get("dies", []):
        for bucket in lane.get("bins", []):
            refs = bucket.get("entry_ids", [])
            bucket["entry_count"] = len(refs)
            bucket["entry_ids"] = refs[:8]
    return data


def write_report(analysis, path, overwrite=False):
    """Write the versioned product template with safely embedded analysis JSON."""
    if not isinstance(analysis, dict):
        raise ValueError("production attribution analysis must be an object")
    payload = json.dumps(report_data(analysis), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    web = ROOT / "web" / "attribution"
    page = (web / "index.html").read_text(encoding="utf-8")
    page = page.replace("<!--ATTRIBUTION_STYLE-->", "<style>" + (web / "style.css").read_text(encoding="utf-8") + "</style>")
    page = page.replace("<!--ATTRIBUTION_DATA-->", '<script type="application/json" id="attribution-data">' + payload + "</script>")
    scripts = "\n".join("<script>" + (web / name).read_text(encoding="utf-8").replace("</script", "<\\/script") + "</script>"
                        for name in ("analysis.js", "viewer.js"))
    page = page.replace("<!--ATTRIBUTION_SCRIPTS-->", scripts)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w" if overwrite else "x", encoding="utf-8") as output:
        output.write(page)
    return path
