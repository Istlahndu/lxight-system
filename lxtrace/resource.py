"""Resource-first v2 sessions; evidence types never imply one another.

All timestamps are integer nanoseconds in a declared normalized clock.  Interval
math uses half-open bounds.  The legacy SDK remains an optional software source.
"""
import copy
import hashlib
import json
import math
import re
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from .core import union, intersection, subtract, length

SCHEMA = "lxtrace.resource-session.v2"
ANALYSIS_RULES_VERSION = "resource-v2.0.1-demo"
INDEX_VERSION = "resource-index.v2.0.0-demo"
CATEGORIES = ("mat", "vec", "mem", "cc", "cx", "fe", "wait", "drv")
STATES = ("target", "other", "idle", "unknown")
SCHEDULE = {"schedule", "sched", "scheduling", "residency", "on_cpu"}
ROOT = Path(__file__).resolve().parents[1]


def _ns(value, label):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(label + " must be a nonnegative integer nanosecond value")
    return value


def _cpu(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("logical CPU IDs must be nonnegative integers")
    return value


def validate_session(session):
    """Return a validated copy; reject conflicting IDs and invented boundaries."""
    if not isinstance(session, dict) or session.get("schema_version") != SCHEMA:
        raise ValueError("unsupported resource session schema_version")
    s = copy.deepcopy(session)
    for key in ("session_id", "run_id"):
        if not isinstance(s.get(key), str) or not s[key]:
            raise ValueError(key + " is required")
    if s.get("data_origin") not in ("synthetic", "imported", "live"):
        raise ValueError("data_origin must be synthetic/imported/live")
    bounds = s.get("time_range")
    if not isinstance(bounds, list) or len(bounds) != 2:
        raise ValueError("time_range must contain two nanosecond timestamps")
    if _ns(bounds[1], "time_range end") <= _ns(bounds[0], "time_range begin"):
        raise ValueError("time_range must be nonempty")
    for key in ("resources", "sources", "clocks", "records", "capability_manifest", "provenance"):
        s.setdefault(key, [])
        if not isinstance(s[key], list):
            raise ValueError(key + " must be an array")
    resources = {}
    cpus = set()
    for r in s["resources"]:
        rid = r.get("resource_id")
        if not isinstance(rid, str) or not rid or rid in resources:
            raise ValueError("resource_id must be a unique nonempty string")
        resources[rid] = r
        r.setdefault("parent_id", None)
        r.setdefault("mapping_source", "unknown")
        if r.get("type") == "logical_cpu":
            cpu = _cpu(r.get("logical_cpu"))
            if cpu in cpus:
                raise ValueError("duplicate logical CPU resource")
            cpus.add(cpu)
    for rid, r in resources.items():
        parent, seen = r.get("parent_id"), {rid}
        while parent is not None:
            if parent not in resources or parent in seen:
                raise ValueError("unknown or cyclic resource parent")
            seen.add(parent)
            parent = resources[parent].get("parent_id")
    allocation = s.setdefault("allocation", {"status": "unverified", "cpu_ids": []})
    allocation.setdefault("status", "unverified")
    allocation.setdefault("cpu_ids", [])
    allocated = set(map(_cpu, allocation["cpu_ids"]))
    if not allocated <= cpus:
        raise ValueError("allocation references a missing logical CPU resource")
    for window in allocation.get("intervals", []):
        if _ns(window["end_ns"], "allocation end") <= _ns(window["begin_ns"], "allocation begin"):
            raise ValueError("allocation interval must be nonempty")
        if not set(map(_cpu, window["cpu_ids"])) <= allocated:
            raise ValueError("dynamic allocation exceeds declared CPU set")
    sources = {}
    for source in s["sources"]:
        sid = source.get("source_id")
        if not isinstance(sid, str) or not sid or sid in sources:
            raise ValueError("source_id must be a unique nonempty string")
        sources[sid] = source
    clock_ids = set()
    for clock in s["clocks"]:
        cid = clock.get("clock_id", clock.get("id"))
        if not isinstance(cid, str) or not cid or cid in clock_ids:
            raise ValueError("clock_id must be a unique nonempty string")
        clock_ids.add(cid)
        if clock.get("error_ns") is not None:
            _ns(clock["error_ns"], "clock error")
        validity = clock.get("calibration_interval_ns")
        if validity is not None and (not isinstance(validity, list) or len(validity) != 2
                or _ns(validity[1], "calibration end") <= _ns(validity[0], "calibration begin")):
            raise ValueError("clock calibration interval must be nonempty")
    if "raw_files" in s:
        if not isinstance(s["raw_files"], dict):
            raise ValueError("raw_files must map archive names to UTF-8 content")
        for name, raw in s["raw_files"].items():
            if not isinstance(name, str) or (not isinstance(raw, str) and
                    not (isinstance(raw, dict) and set(raw) == {"content"} and isinstance(raw["content"], str))):
                raise ValueError("raw_files accepts UTF-8 content only; filesystem source_path is unsupported")
    seen = {}
    records = []
    for r in s["records"]:
        raw_record = copy.deepcopy(r)
        rid = r.get("record_id")
        if not isinstance(rid, str) or not rid:
            raise ValueError("record_id must be a nonempty string")
        if rid in seen:
            if seen[rid] != r:
                raise ValueError("conflicting duplicate record_id: " + rid)
            continue
        if r.get("resource_id") not in resources or r.get("source_id") not in sources:
            raise ValueError("record references unknown resource or source")
        if r.get("layer") not in ("os", "hardware", "software"):
            raise ValueError("record layer must be os/hardware/software")
        representation = r.get("representation")
        if representation not in ("interval", "counter_window", "sample", "aggregate"):
            raise ValueError("unsupported evidence representation")
        if representation in ("interval", "counter_window"):
            _ns(r.get("begin_ns"), "record begin")
            if "end_ns" not in r:
                raise ValueError("end_ns must be explicit, including null for open records")
            if r["end_ns"] is not None and _ns(r["end_ns"], "record end") < r["begin_ns"]:
                raise ValueError("record end precedes begin")
        elif representation == "sample":
            _ns(r.get("timestamp_ns"), "sample timestamp")
        elif r.get("begin_ns") is not None or r.get("end_ns") is not None:
            raise ValueError("aggregate evidence must not invent interval boundaries")
        if r.get("layer") == "os" and r.get("category") in SCHEDULE:
            if representation != "interval" or r.get("state") not in STATES:
                raise ValueError("scheduler record requires an interval and known state label")
        if r.get("uncertainty_ns") is not None:
            _ns(r["uncertainty_ns"], "record uncertainty")
        r.setdefault("raw_refs", [])
        r.setdefault("correlation_ids", {})
        seen[rid] = raw_record
        records.append(r)
    s["records"] = records
    s.setdefault("quality", {})
    if not isinstance(s["quality"], dict):
        raise ValueError("quality must be an object")
    for window in s["quality"].get("unknown_windows", []):
        if _ns(window["end_ns"], "unknown end") <= _ns(window["begin_ns"], "unknown begin"):
            raise ValueError("unknown window must be nonempty")
    return s


def _selected(s, resource_id):
    by_id = {r["resource_id"]: r for r in s["resources"]}
    if resource_id is None or resource_id == "all":
        ids = set(by_id)
    else:
        if resource_id not in by_id:
            raise ValueError("unknown resource_id: " + str(resource_id))
        ids = {resource_id}
        while True:
            children = {r["resource_id"] for r in s["resources"] if r.get("parent_id") in ids}
            children.update(r["resource_id"] for r in s["resources"] if any(
                (link if isinstance(link, str) else link.get("resource_id", link.get("target_id"))) in ids
                for link in r.get("links", []) if isinstance(link, (str, dict))))
            if children <= ids:
                break
            ids.update(children)
    return ids, {r["logical_cpu"]: r["resource_id"] for r in s["resources"]
                 if r["resource_id"] in ids and r.get("type") == "logical_cpu"}


def _clip(a, b, start, end):
    return (max(a, start), min(b, end))


def _allocation(s, cpu, start, end):
    allocation = s["allocation"]
    if not str(allocation.get("status", "")).startswith("verified") or cpu not in allocation["cpu_ids"]:
        return []
    windows = allocation.get("intervals")
    if windows is None:
        return union([_clip(s["time_range"][0], s["time_range"][1], start, end)])
    return union([_clip(w["begin_ns"], w["end_ns"], start, end)
                  for w in windows if cpu in w["cpu_ids"]])


def _has_loss(source):
    return any((isinstance(source.get(k), (int, float)) and source[k] > 0)
               or (isinstance(source.get(k), dict) and source[k].get("count", 0) > 0)
               for k in ("loss", "lost_events", "lost_records", "dropped_records", "loss_count"))


def _unknown(s, cpu, resource_id, source_ids, start, end):
    windows = s["quality"].get("unknown_windows", [])
    masks = []
    for w in windows:
        if w.get("resource_id") not in (None, resource_id):
            continue
        if w.get("cpu", w.get("logical_cpu")) not in (None, cpu):
            continue
        if w.get("source_id") is not None and w["source_id"] not in source_ids:
            continue
        masks.append(_clip(w["begin_ns"], w["end_ns"], start, end))
    for source in s["sources"]:
        if source["source_id"] not in source_ids or not _has_loss(source):
            continue
        if not any(w.get("source_id") in (None, source["source_id"]) for w in windows):
            masks.append((start, end))
    if _has_loss(s["quality"]) and not windows:
        masks.append((start, end))
    return union(masks)


def _scheduler(s, cpu, rid, start, end):
    allocated = _allocation(s, cpu, start, end)
    records = [r for r in s["records"] if r["resource_id"] == rid
               and r["layer"] == "os" and r["representation"] == "interval"
               and r.get("category") in SCHEDULE and r["begin_ns"] < end
               and (r["end_ns"] is None or r["end_ns"] > start)]
    sources = {r["source_id"] for r in records}
    # Even an empty source may disclose lost scheduler records for this CPU.
    sources.update(x["source_id"] for x in s["sources"]
                   if x.get("source_layer") in ("os", "scheduler", "scheduling")
                   and x.get("resource_id") in (None, rid))
    masks = _unknown(s, cpu, rid, sources, start, end)
    boundaries = {start, end}
    for a, b in allocated + masks:
        boundaries.update((a, b))
    for r in records:
        boundaries.add(max(start, r["begin_ns"]))
        boundaries.add(min(end, r["end_ns"] if r["end_ns"] is not None else end))
    points = sorted(boundaries)
    intervals = {state: [] for state in STATES}
    for a, b in zip(points, points[1:]):
        if b <= a or not any(x <= a and b <= y for x, y in allocated):
            continue
        state = "unknown"
        if not any(x < b and a < y for x, y in masks):
            active = [r for r in records if r["begin_ns"] <= a
                      and (r["end_ns"] is None or r["end_ns"] >= b)]
            labels = {r["state"] for r in active}
            instances = {r.get("thread_instance_id") for r in active if r["state"] == "target"}
            if active and all(r["end_ns"] is not None for r in active) and len(labels) == 1 and len(instances) <= 1:
                state = active[0]["state"]
        intervals[state].append((a, b))
    intervals = {key: union(value) for key, value in intervals.items()}
    result = {"logical_cpu": cpu, "resource_id": rid, "capacity_cpu_ns": length(allocated),
              "allocated_intervals": allocated, "intervals": intervals}
    result.update({state + "_cpu_ns": length(intervals[state]) for state in STATES})
    return result


def _concurrency(per_cpu, start, end):
    boundaries = {start, end}
    for cpu in per_cpu:
        for a, b in cpu["allocated_intervals"] + cpu["intervals"]["target"]:
            boundaries.update((a, b))
    points = sorted(boundaries)
    result = []
    for a, b in zip(points, points[1:]):
        target = sum(any(x <= a and b <= y for x, y in cpu["intervals"]["target"]) for cpu in per_cpu)
        allocated = sum(any(x <= a and b <= y for x, y in cpu["allocated_intervals"]) for cpu in per_cpu)
        row = {"begin_ns": a, "end_ns": b, "target_cpus": target, "allocated_cpus": allocated}
        if result and all(result[-1][k] == row[k] for k in ("target_cpus", "allocated_cpus")):
            result[-1]["end_ns"] = b
        else:
            result.append(row)
    return result


def _hardware_verified(record, source):
    semantics = record.get("semantics_status", source.get("semantics_status"))
    boundary = record.get("boundary_support", source.get("boundary_support"))
    return (record["layer"] == "hardware" and record["representation"] == "interval"
            and semantics in ("verified", "verified_fixture")
            and boundary in (True, "verified", "interval", "begin_end", "supported"))


def _hardware_overlap(s, records, start, end):
    sources = {x["source_id"]: x for x in s["sources"]}
    clocks = {x.get("clock_id", x.get("id")): x for x in s["clocks"]}
    verified = [r for r in records if _hardware_verified(r, sources[r["source_id"]])
                and r.get("end_ns") is not None]
    compute = [r for r in verified if r["category"] in ("mat", "vec")]
    transfer = [r for r in verified if r["category"] == "cx"]
    out = {"status": "unavailable", "reason": "missing_verified_compute_or_transfer_intervals",
           "attribution_scope": "selected_resource_set_concurrency_only",
           "attribution_note": "所选资源集合内活动并存；时间相交不推定同一核、任务或帧归属。",
           "compute_ns": None, "transfer_ns": None, "overlap_ns": None,
           "lower_bound_ns": None, "upper_bound_ns": None,
           "compute_overlap_ratio": None, "transfer_overlap_ratio": None}
    if not compute or not transfer:
        return out
    used = compute + transfer
    if any(r.get("uncertainty_ns") is None for r in used):
        out["reason"] = "timing_uncertainty_unknown"
        return out
    clock_ids = {sources[r["source_id"]].get("clock_id", "unknown") for r in used}
    targets = {clocks.get(cid, {}).get("aligned_to") for cid in clock_ids}
    if "unknown" in clock_ids or None in clock_ids or (len(clock_ids) > 1 and (
            not all(clocks.get(cid, {}).get("calibration_status") in ("verified", "verified_fixture")
                    for cid in clock_ids) or len(targets) != 1 or None in targets or "" in targets)):
        out["reason"] = "clocks_not_aligned"
        return out
    if any(clocks.get(cid, {}).get("error_ns") is None for cid in clock_ids):
        out["reason"] = "timing_uncertainty_unknown"
        return out
    if len(clock_ids) > 1 and any(
            clocks.get(cid, {}).get("calibration_interval_ns") is not None and
            not (clocks[cid]["calibration_interval_ns"][0] <= start and end <= clocks[cid]["calibration_interval_ns"][1])
            for cid in clock_ids):
        out["reason"] = "clock_calibration_outside_validity"
        return out
    used_source_ids = {r["source_id"] for r in used}
    used_resource_ids = {r["resource_id"] for r in used}
    unknown_windows = s["quality"].get("unknown_windows", [])
    located_loss = any(w["begin_ns"] < end and start < w["end_ns"]
                       and w.get("source_id") in (None, *used_source_ids)
                       and w.get("resource_id") in (None, *used_resource_ids)
                       for w in unknown_windows)
    if (any(_has_loss(sources[r["source_id"]]) for r in used) or located_loss
            or (_has_loss(s["quality"]) and not unknown_windows)):
        out["reason"] = "hardware_source_loss"
        return out
    def intervals(items, mode=0):
        result = []
        for r in items:
            clock = clocks.get(sources[r["source_id"]].get("clock_id"), {})
            error = r["uncertainty_ns"] + clock["error_ns"]
            result.append(_clip(r["begin_ns"] + mode * error,
                                r["end_ns"] - mode * error, start, end))
        return union(result)
    c, d = intervals(compute), intervals(transfer)
    nominal = length(intersection(c, d))
    lower = length(intersection(intervals(compute, 1), intervals(transfer, 1)))
    upper = length(intersection(intervals(compute, -1), intervals(transfer, -1)))
    out.update(status="available" if lower == upper else "bounded", reason=None,
               compute_ns=length(c), transfer_ns=length(d), overlap_ns=nominal,
               lower_bound_ns=lower, upper_bound_ns=upper,
               compute_overlap_ratio=nominal / length(c) if length(c) else None,
               transfer_overlap_ratio=nominal / length(d) if length(d) else None)
    if any(_hardware_verified(r, sources[r["source_id"]]) and r.get("end_ns") is None
           and r.get("category") in ("mat", "vec", "cx") for r in records):
        out.update(status="bounded", reason="incomplete_hardware_intervals", overlap_ns=None,
                   upper_bound_ns=end-start, compute_overlap_ratio=None, transfer_overlap_ratio=None)
    return out


def _counter_window(record, start, end):
    """Keep window coverage separate from perf's counter-running coverage."""
    r = copy.deepcopy(record)
    complete = r["end_ns"] is not None
    full = complete and r["begin_ns"] >= start and r["end_ns"] <= end

    def number(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)

    enabled, running = r.get("enabled_ns"), r.get("running_ns")
    timing_reported = "enabled_ns" in r or "running_ns" in r
    status = "not_reported"
    if timing_reported:
        if not number(enabled) or not number(running) or running > enabled:
            status = "counter_timing_unavailable"
        elif running == 0:
            status = "counter_not_running"
        else:
            status = "multiplexed_estimate" if running < enabled else "not_multiplexed"
    scaled = next((r[k] for k in ("scaled_count", "scaled") if number(r.get(k))), None)
    raw = next((r[k] for k in ("raw_count", "raw", "value") if number(r.get(k))), None)
    value = scaled if scaled is not None else raw
    if status == "multiplexed_estimate" and scaled is None and raw is not None:
        value = raw * enabled / running
    if status in ("counter_not_running", "counter_timing_unavailable"):
        value = None
    r["counter_status"] = status
    r["window_value"] = value
    r["window_fully_selected"] = full
    r["selection_status"] = (
        "open_window" if not complete else "crosses_selection" if not full
        else status if status in ("multiplexed_estimate", "counter_not_running", "counter_timing_unavailable")
        else "complete_window")
    r["included_in_totals"] = full and value is not None and status in ("not_reported", "not_multiplexed")
    r["selection_value"] = value if full else None
    r["selected_value"] = r["selection_value"]
    return r


def analyze_resources(session, start=None, end=None, resource_id=None):
    """Conserve allocated CPU time and keep counters, samples and activities separate."""
    s = validate_session(session)
    start = s["time_range"][0] if start is None else start
    end = s["time_range"][1] if end is None else end
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (start, end)) or start < 0 or end <= start:
        raise ValueError("selection must be finite and 0 <= start < end")
    selected, cpus = _selected(s, resource_id)
    per_cpu = [_scheduler(s, cpu, rid, start, end) for cpu, rid in sorted(cpus.items())]
    concurrency = _concurrency(per_cpu, start, end)
    capacity = sum(x["capacity_cpu_ns"] for x in per_cpu)
    totals = {state + "_cpu_ns": sum(x[state + "_cpu_ns"] for x in per_cpu) for state in STATES}
    target = union([iv for row in per_cpu for iv in row["intervals"]["target"]])
    completions = [row["intervals"]["target"][-1][1] for row in per_cpu if row["intervals"]["target"]]
    records = [r for r in s["records"] if r["resource_id"] in selected]
    relevant = [r for r in records if (
        r["representation"] == "aggregate" or
        (r["representation"] == "sample" and start <= r["timestamp_ns"] < end) or
        (r["representation"] in ("interval", "counter_window") and r["begin_ns"] < end
         and (r["end_ns"] is None or r["end_ns"] > start)))]
    counters = []
    for record in relevant:
        if record["representation"] != "counter_window":
            continue
        counters.append(_counter_window(record, start, end))
    sources = {x["source_id"]: x for x in s["sources"]}
    categories = []
    for category in CATEGORIES:
        group = [r for r in relevant if r.get("category") == category]
        hw = [r for r in group if _hardware_verified(r, sources[r["source_id"]])]
        forms = sorted({r["representation"] for r in group})
        status = ("hardware_intervals" if hw else "counter_only" if "counter_window" in forms
                  else "sample_only" if "sample" in forms else "software_only" if any(r["layer"] == "software" for r in group)
                  else "aggregate_only" if "aggregate" in forms else "unverified" if group else "unavailable")
        intervals = union([_clip(r["begin_ns"], r["end_ns"], start, end) for r in hw if r["end_ns"] is not None])
        categories.append({"category": category, "status": status, "representation": forms,
                           "record_count": len(group), "known_interval_ns": length(intervals) if hw else None,
                           "open_count": sum(r.get("end_ns") is None for r in hw),
                           "record_ids": [r["record_id"] for r in group]})
    peak = max((x["target_cpus"] for x in concurrency), default=0)
    capture_quality = copy.deepcopy(s.get("quality", {}))
    warnings = [str(w) for w in capture_quality.get("warnings", [])]
    if (capture_quality.get("capture_partial") or capture_quality.get("clean_shutdown") is False
            or any(capture_quality.get(k, 0) for k in
                   ("late_records", "reported_incomplete_events", "protocol_errors"))):
        warnings.append("Capture quality is partial or abnormal; surviving records do not certify complete coverage.")
    if not str(s["allocation"].get("status", "")).startswith("verified"):
        warnings.append("Allocation is unverified; no CPU capacity is certified.")
    if totals["unknown_cpu_ns"]:
        warnings.append("Unknown scheduler time is retained; missing data is not idle.")
    if any(not x["window_fully_selected"] for x in counters):
        warnings.append("Partial counter windows retain their full boundaries and are not prorated.")
    if any(x["counter_status"] == "multiplexed_estimate" for x in counters):
        warnings.append("Multiplexed counter values are scaled estimates, excluded from exact totals.")
    if any(x["counter_status"] in ("counter_not_running", "counter_timing_unavailable") for x in counters):
        warnings.append("Counter did not run or enabled/running timing is invalid or incomplete; selection value is unavailable.")
    if s["data_origin"] == "synthetic":
        warnings.append("Synthetic fixture validates analysis and presentation, not live hardware capability.")
    allocation_known = str(s["allocation"].get("status", "")).startswith("verified")
    out = dict(schema_version="lxtrace.resource-analysis.v2", session_id=s["session_id"],
                analysis_rules_version=ANALYSIS_RULES_VERSION,
                data_origin=s["data_origin"], interval_ns=[start, end], resource_id=resource_id,
                cpu_ids=sorted(cpus), wall_ns=end-start, capacity_cpu_ns=capacity,
                effective_capacity_cpu_ns=capacity, known_capacity_cpu_ns=capacity-totals["unknown_cpu_ns"],
                unallocated_cpu_ns=len(cpus)*(end-start)-capacity,
                coverage_ratio=(capacity-totals["unknown_cpu_ns"])/capacity if capacity else None,
                unknown_capacity_ratio=totals["unknown_cpu_ns"]/capacity if capacity else None,
                utilization=totals["target_cpu_ns"]/capacity if capacity else None,
                target_wall_ns=length(target), peak_target_cpus=peak, peak_concurrency=peak,
                mean_target_cpus=totals["target_cpu_ns"]/(end-start),
                completion_skew_ns=max(completions)-min(completions) if completions else None,
                per_cpu=per_cpu, concurrency=concurrency, categories=categories,
                counters=counters, samples=[r for r in relevant if r["representation"] == "sample"],
                records=relevant, hardware_overlap=_hardware_overlap(s, relevant, start, end),
                capture_quality=capture_quality, warnings=warnings, **totals)
    if not allocation_known:
        for key in ("target_cpu_ns", "other_cpu_ns", "idle_cpu_ns", "unknown_cpu_ns", "capacity_cpu_ns",
                    "effective_capacity_cpu_ns", "known_capacity_cpu_ns", "unallocated_cpu_ns", "target_wall_ns",
                    "peak_target_cpus", "peak_concurrency", "mean_target_cpus", "completion_skew_ns"):
            out[key] = None
    return out


def demo_session(case="resource"):
    """Small noncontiguous-CPU fixtures; all hardware evidence is explicitly synthetic."""
    if case not in ("resource", "parallel", "serial", "aggregate", "sparse", "loss", "open", "counters", "uncalibrated"):
        raise ValueError("unknown demo case")
    ms = 1000000
    s = dict(schema_version=SCHEMA, session_id="synthetic-v2-"+case, run_id="fixture-run-1",
             data_origin="synthetic", time_range=[0, 12*ms], app={"name": "Resource evidence fixture", "build_id": "v2-fixture"},
             resources=[{"resource_id": "node:fixture", "type": "node", "name": "Synthetic node", "parent_id": None, "mapping_source": "fixture"},
                        {"resource_id": "domain:D0", "type": "domain", "name": "D0 · synthetic mapping", "parent_id": "node:fixture", "mapping_source": "fixture-only; not CPU-number inference"}],
             allocation={"status": "verified", "cpu_ids": [2, 6, 14, 30], "evidence_ref": "fixture-only"},
             sources=[{"source_id": "sched", "source_layer": "os", "scope": "core", "clock_id": "mono", "loss": 0, "boundary_semantics": "synthetic switch intervals"},
                      {"source_id": "pmu", "source_layer": "hardware", "scope": "core", "clock_id": "mono", "semantics_status": "verified_fixture", "boundary_support": False},
                      {"source_id": "activity", "source_layer": "hardware", "scope": "domain", "clock_id": "mono", "semantics_status": "verified_fixture", "boundary_support": True},
                      {"source_id": "device", "source_layer": "hardware", "scope": "device", "clock_id": "device-ticks", "semantics_status": "verified_fixture", "boundary_support": True},
                      {"source_id": "sdk", "source_layer": "software", "scope": "task", "clock_id": "mono"}],
             clocks=[{"clock_id": "mono", "unit": "ns", "resolution_ns": 100, "aligned_to": "session", "calibration_status": "verified_fixture", "error_ns": 0},
                     {"clock_id": "device-ticks", "unit": "ns", "raw_unit": "ticks", "aligned_to": "session", "calibration_status": "verified_fixture", "error_ns": 1000,
                      "transform": {"scale": 1, "offset_ns": 0}, "calibration_interval_ns": [0, 12*ms]}],
             records=[], capability_manifest=[], quality={"unknown_windows": []},
             provenance=[{"kind": "synthetic_fixture", "description": "Hand-authored evidence types; no platform measurement or acceptance claim."}])
    for cpu in (2, 6, 14, 30):
        s["resources"].append({"resource_id": "cpu:"+str(cpu), "type": "logical_cpu", "logical_cpu": cpu,
                               "name": "CPU "+str(cpu), "parent_id": "domain:D0", "mapping_source": "fixture"})
    s["resources"].append({"resource_id": "dma:0", "type": "device", "name": "DMA 0 · synthetic", "parent_id": "node:fixture", "mapping_source": "fixture", "links": ["domain:D0"]})
    serial = 0
    def add(rid, begin, end, **fields):
        nonlocal serial
        serial += 1
        row = dict(record_id="r"+str(serial), resource_id=rid, source_id="sched", category="schedule",
                   layer="os", representation="interval", begin_ns=int(begin*ms), end_ns=None if end is None else int(end*ms),
                   raw_refs=["fixture:record:"+str(len(s["records"])+1)], uncertainty_ns=0, correlation_ids={})
        row.update(fields)
        s["records"].append(row)
        return row
    for cpu, states in {
        2: [(0, 1, "idle", None), (1, 4, "target", "worker:A:1"), (4, 7, "other", "anonymous-other"), (7, 10, "target", "worker:C:1"), (10, 12, "idle", None)],
        6: [(0, 2, "idle", None), (2, 6, "target", "worker:B:1"), (6, 12, "idle", None)],
        14: [(0, 4, "idle", None), (4, 8, "target", "worker:A:1"), (8, 12, "idle", None)],
        30: [(0, 3, "other", "anonymous-other"), (3, 9, "target", "worker:D:1"), (9, 12, "idle", None)],
    }.items():
        for a, b, state, thread in states:
            add("cpu:"+str(cpu), a, b, state=state, thread_instance_id=thread)
    add("cpu:2", 0, 6, source_id="pmu", layer="hardware", representation="counter_window", category="mat",
        event="cycles", encoding="0x11", unit="count", raw_count=8000000, scaled_count=10000000, enabled_ns=6*ms, running_ns=4800000)
    add("cpu:2", 6, 12, source_id="pmu", layer="hardware", representation="counter_window", category="mem",
        event="cache-misses", encoding="fixture-only", unit="count", raw_count=3000, scaled_count=3000, enabled_ns=6*ms, running_ns=6*ms)
    sample = add("cpu:14", 0, 0, source_id="pmu", layer="hardware", representation="sample", category="mem", timestamp_ns=5500000,
                 event="memory-sample", pc="0x1000", sample_period=100000, context_status="unknown", frame_id=None)
    sample.pop("begin_ns"); sample.pop("end_ns")
    add("domain:D0", 2, 7, source_id="activity", layer="hardware", category="mat", boundary_semantics="synthetic matrix activity boundaries")
    add("domain:D0", 5, 9, source_id="activity", layer="hardware", category="vec", boundary_semantics="synthetic vector activity boundaries")
    add("dma:0", 4, 8, source_id="device", layer="hardware", category="cx", boundary_semantics="synthetic DMA activity boundaries", uncertainty_ns=1000,
        correlation_ids={"domain_id": "domain:D0", "request_id": "req:1"})
    add("cpu:2", 1, 9, source_id="sdk", layer="software", category="frame", name="microbatch.0 / layer.0", frame_id="frame:0", operator_id="gate_up", definition="one microbatch one layer")
    add("cpu:2", 1, 4, source_id="sdk", layer="software", category="mat", name="gate_up software call", frame_id="frame:0")
    add("cpu:6", 2, 8, source_id="sdk", layer="software", category="cx", kind="request", name="request lifetime", frame_id="frame:0", correlation_ids={"request_id": "req:1"})
    if case in ("sparse", "loss"):
        s["quality"]["unknown_windows"] = [{"resource_id": "cpu:14", "source_id": "sched", "begin_ns": 5*ms, "end_ns": 7*ms, "reason": "fixture scheduler loss"}]
        s["sources"][0]["loss"] = 2
    if case == "sparse":
        s["allocation"]["intervals"] = [{"begin_ns": 0, "end_ns": 6*ms, "cpu_ids": [2, 6, 14]}, {"begin_ns": 6*ms, "end_ns": 12*ms, "cpu_ids": [2, 14, 30]}]
    if case == "open":
        s["records"] = [r for r in s["records"] if not (r["resource_id"] == "cpu:2" and r["source_id"] == "sched" and r["begin_ns"] >= 7*ms)]
        add("cpu:2", 7, None, state="target", thread_instance_id="worker:C:1")
    if case == "counters":
        s["records"] = [r for r in s["records"] if r["source_id"] not in ("activity", "device")]
    if case == "uncalibrated":
        s["clocks"][1].update(calibration_status="unverified", aligned_to=None)
    if case in ("parallel", "serial"):
        s["time_range"] = [0, 8*ms]
        s["allocation"]["cpu_ids"] = [2, 6]
        s["records"] = []
        for cpu, a, b in ((2, 0, 4), (6, 0, 4) if case == "parallel" else (6, 4, 8)):
            if a:
                add("cpu:"+str(cpu), 0, a, state="idle")
            add("cpu:"+str(cpu), a, b, state="target", thread_instance_id="worker:"+str(cpu)+":1")
            if b < 8:
                add("cpu:"+str(cpu), b, 8, state="idle")
    if case == "aggregate":
        s["records"] = [dict(record_id="aggregate:"+category, resource_id="domain:D0", source_id="pmu",
                            category=category, layer="hardware", representation="aggregate", value=value,
                            unit="percent", aggregation_rule="whole-run fixture summary; no activity boundaries",
                            raw_refs=["fixture:summary"], correlation_ids={}, uncertainty_ns=None)
                        for category, value in (("mat", 47), ("cx", 26))]
    for category in CATEGORIES:
        supported = category in ("mat", "vec", "cx") and case not in ("counters", "parallel", "serial", "aggregate")
        s["capability_manifest"].append({"feature": "hardware."+category, "category": category,
            "availability": "available" if supported else "unavailable", "implementation_status": "fixture_only",
            "semantics_status": "verified_fixture" if supported else "unverified", "boundary_support": supported,
            "correlation_status": "synthetic_only", "reason_code": "synthetic_fixture_not_platform_support"})
    return validate_session(s)


def migrate_v1(old):
    """Legacy software ranges retain software provenance and acquire no CPU mapping."""
    if old.get("schema_version") != "lxtrace.session.v1":
        raise ValueError("unsupported legacy session schema_version")
    bounds = copy.deepcopy(old.get("time_range", [0, 1]))
    if bounds[1] <= bounds[0]:
        bounds[1] = bounds[0]+1
    s = dict(schema_version=SCHEMA, session_id=str(old.get("session_id", uuid.uuid4()))+"-v2",
             run_id=str(old.get("run_id", old.get("session_id", "legacy"))),
             data_origin="synthetic" if old.get("data_origin") == "synthetic" else "imported", time_range=bounds,
             app=copy.deepcopy(old.get("app", {})), resources=[{"resource_id": "legacy:root", "type": "node", "parent_id": None, "name": "Legacy software session", "mapping_source": "unmapped_legacy"}],
             allocation={"status": "unverified", "cpu_ids": []}, sources=[], clocks=[{"clock_id": "legacy", "unit": "ns", "resolution_ns": old.get("clock", {}).get("resolution_ns")}],
             records=[], capability_manifest=copy.deepcopy(old.get("capability_manifest", [])), quality=copy.deepcopy(old.get("quality", {})),
             provenance=copy.deepcopy(old.get("provenance", []))+[{"kind": "schema_migration", "from": "lxtrace.session.v1", "rule": "software evidence preserved; no inferred CPU or hardware activity"}])
    s["raw_files"] = {"legacy/session-v1.json": _dump(old)}
    s["clocks"][0]["original_clock"] = copy.deepcopy(old.get("clock", {}))
    sources, threads = set(), set()
    for index, e in enumerate(old.get("events", [])):
        tid, sid = str(e.get("thread_id", "unknown")), str(e.get("source_id", "legacy"))
        if tid not in threads:
            name = next((t.get("name", tid) for t in old.get("threads", []) if str(t["thread_id"]) == tid), tid)
            s["resources"].append({"resource_id": "thread:"+tid, "type": "thread", "parent_id": "legacy:root", "name": name, "mapping_source": "legacy_thread_identity_only"})
            threads.add(tid)
        if sid not in sources:
            s["sources"].append({"source_id": sid, "source_layer": "software", "scope": "task", "clock_id": "legacy", "original_source": sid})
            sources.add(sid)
        r = dict(record_id=str(e["event_id"]), resource_id="thread:"+tid, source_id=sid,
                 category="frame" if e.get("kind") == "frame" else e.get("category", "unknown"), layer="software", representation="interval",
                 begin_ns=e["begin_ts"], end_ns=e.get("end_ts"), name=e.get("name"), kind=e.get("kind"),
                 frame_id=e.get("frame_id"), parent_id=e.get("parent_id"), thread_instance_id=tid,
                 uncertainty_ns=None, raw_refs=["raw/legacy/session-v1.json#events/"+str(index)], correlation_ids={}, args=copy.deepcopy(e.get("args", {})),
                 legacy_status=e.get("status"), legacy_evidence=e.get("evidence"))
        if e.get("kind") in ("instant", "counter"):
            r.update(representation="sample", timestamp_ns=r.pop("begin_ns"))
            r.pop("end_ns")
        s["records"].append(r)
    return validate_session(s)


def _dump(data):
    return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+"\n"


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _archive_path(root, relative):
    p = PurePosixPath(str(relative).replace("\\", "/"))
    if p.is_absolute() or not p.parts or any(part in (".", "..") for part in p.parts) or ":" in str(p):
        raise ValueError("unsafe archive member path")
    target = root.joinpath(*p.parts)
    target.resolve().relative_to(root.resolve())
    return target


def write_session(session, path):
    """Write JSON, or an immutable checksummed .lxtrace directory archive.

    Optional raw_files maps archive-relative paths to UTF-8 content or
    {content: text}.  Data never causes reads from an external filesystem path.
    """
    s, path = validate_session(session), Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(_dump(s), encoding="utf-8")
        return path
    if path.exists():
        raise ValueError("session archive already exists; choose a new output")
    staging = Path(tempfile.mkdtemp(prefix=".lxtrace-", dir=str(path.parent)))
    try:
        metadata = {k: v for k, v in s.items() if k not in ("resources", "allocation", "clocks", "records", "quality", "raw_files")}
        members = {"topology.json": {"resources": s["resources"], "allocation": s["allocation"]},
                   "clocks.json": s["clocks"], "normalized/records.json": s["records"],
                   "quality.json": s["quality"], "derived/analysis.json": analyze_resources(s),
                   "index/resources.json": {"resource_ids": [r["resource_id"] for r in s["resources"]], "rebuildable": True,
                                            "index_version": INDEX_VERSION, "analysis_rules_version": ANALYSIS_RULES_VERSION}}
        for name, value in members.items():
            target = _archive_path(staging, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_dump(value), encoding="utf-8")
        for name, value in (s.get("raw_files") or {}).items():
            if not str(name).replace("\\", "/").startswith("raw/"):
                name = "raw/"+str(name)
            target = _archive_path(staging, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = value["content"] if isinstance(value, dict) else value
            target.write_bytes(content.encode("utf-8"))
        (staging / "reports").mkdir()
        (staging / "raw").mkdir(exist_ok=True)
        metadata["archive_format"] = "lxtrace.directory.v2"
        metadata["files"] = {p.relative_to(staging).as_posix(): _digest(p) for p in sorted(staging.rglob("*")) if p.is_file()}
        (staging / "manifest.json").write_text(_dump(metadata), encoding="utf-8")
        staging.rename(path)
    except Exception:
        staging.resolve().relative_to(path.parent.resolve())
        shutil.rmtree(staging)
        raise
    return path


def load_session(path, allow_missing_derived=False):
    """Verify an archive; explicit recovery may omit only derived/index members."""
    path = Path(path)
    if not path.is_dir():
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return migrate_v1(data) if data.get("schema_version") == "lxtrace.session.v1" else validate_session(data)
    manifest_path = _archive_path(path, "manifest.json")
    if manifest_path.is_symlink():
        raise ValueError("archive manifest must not be a symlink")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("archive_format") != "lxtrace.directory.v2":
        raise ValueError("unsupported resource archive format")
    required = {"topology.json", "clocks.json", "normalized/records.json", "quality.json"}
    files = manifest.get("files", {})
    if not required <= set(files):
        raise ValueError("archive manifest omits required session members")
    missing_derived = []
    for name, expected in files.items():
        member = _archive_path(path, name)
        if (allow_missing_derived and name.startswith(("derived/", "index/"))
                and not member.exists() and not member.is_symlink()):
            missing_derived.append(name)
            continue
        if member.is_symlink() or not member.is_file() or _digest(member) != expected:
            raise ValueError("archive checksum mismatch: " + name)
    def read(name):
        return json.loads(_archive_path(path, name).read_text(encoding="utf-8"))
    topology = read("topology.json")
    s = {k: v for k, v in manifest.items() if k not in ("archive_format", "files")}
    s.update(resources=topology["resources"], allocation=topology["allocation"], clocks=read("clocks.json"),
             records=read("normalized/records.json"), quality=read("quality.json"))
    s["archive"] = {"path": str(path.resolve()), "files": files, "integrity": "verified"}
    if missing_derived:
        s["archive"].update(integrity="verified_required_sources_with_missing_derived", missing_derived=missing_derived)
    s["raw_files"] = {}
    for name in files:
        if name.startswith("raw/"):
            try:
                s["raw_files"][name] = {"content": _archive_path(path, name).read_bytes().decode("utf-8")}
            except UnicodeDecodeError as exc:
                raise ValueError("binary raw member is unsupported: " + name) from exc
    return validate_session(s)


def write_report(session, path, overwrite=True):
    """Inline the shared v2 viewer with safely escaped session JSON."""
    s = validate_session(session)
    web = ROOT / "web"
    page = (web / "index.html").read_text(encoding="utf-8")
    def stylesheet(match):
        name = match.group(1)
        file = _archive_path(web, name)
        return "<style>"+file.read_text(encoding="utf-8")+"</style>"
    def script(match):
        name = match.group(1)
        file = _archive_path(web, name)
        return "<script>"+file.read_text(encoding="utf-8").replace("</script", "<\\/script")+"</script>"
    page = re.sub(r'<link\b[^>]*href=[\"\']([^\"\']+\.css)[\"\'][^>]*>', stylesheet, page, flags=re.I)
    payload = _dump(s).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    bootstrap = '<script data-lxtrace-session>window.LXTRACE_SESSION='+payload+';</script>'
    if "<!--LXTRACE_SESSION-->" in page:
        page = page.replace("<!--LXTRACE_SESSION-->", bootstrap)
    else:
        page = page.replace("</head>", bootstrap+"</head>")
    page = re.sub(r'<script\b[^>]*src=[\"\']([^\"\']+)[\"\'][^>]*>\s*</script>', script, page, flags=re.I)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w" if overwrite else "x", encoding="utf-8") as report:
        report.write(page)
    return path
