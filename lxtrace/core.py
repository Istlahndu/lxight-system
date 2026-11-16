"""Versioned sessions and half-open, closed-subset wall-clock analysis."""
import copy
import csv
import hashlib
import html
import json
import math
import os
import platform
import socket
import time
import uuid
from pathlib import Path

SCHEMA = "lxtrace.session.v1"
ROOT = Path(__file__).resolve().parent.parent
COMPUTE = {"mat", "vec", "cc"}


def union(intervals):
    result = []
    for a, b in sorted((a, b) for a, b in intervals if b > a):
        if result and a <= result[-1][1]:
            result[-1] = (result[-1][0], max(b, result[-1][1]))
        else:
            result.append((a, b))
    return result


def intersection(a, b):
    a, b = union(a), union(b)
    i = j = 0
    result = []
    while i < len(a) and j < len(b):
        x, y = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if y > x:
            result.append((x, y))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return result


def subtract(a, b):
    a, b = union(a), union(b)
    result, j = [], 0
    for start, end in a:
        while j < len(b) and b[j][1] <= start:
            j += 1
        k, cursor = j, start
        while k < len(b) and b[k][0] < end:
            if b[k][0] > cursor:
                result.append((cursor, min(end, b[k][0])))
            cursor = max(cursor, b[k][1])
            if cursor >= end:
                break
            k += 1
        if cursor < end:
            result.append((cursor, end))
    return result


def length(intervals):
    return sum(b - a for a, b in intervals)


def parallel_intervals(thread_intervals):
    """Wall-clock intervals where at least two distinct compute threads coexist."""
    boundaries = {}
    for intervals in thread_intervals:
        for a, b in union(intervals):
            boundaries[a] = boundaries.get(a, 0) + 1
            boundaries[b] = boundaries.get(b, 0) - 1
    result, active, previous = [], 0, None
    for timestamp, delta in sorted(boundaries.items()):
        if previous is not None and active >= 2 and timestamp > previous:
            result.append((previous, timestamp))
        active += delta
        previous = timestamp
    return union(result)


def has_loss(session):
    q = session.get("quality", {})
    return bool(q.get("dropped_records") or q.get("lost_events") or q.get("protocol_errors")
                or any(src.get("dropped_records") or src.get("lost_events") or src.get("protocol_errors")
                       for src in session.get("sources", [])))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def capability_defaults():
    def cap(feature, availability, reason):
        return dict(feature_id=feature, availability=availability, reason_code=reason,
                    failure_stage=None, tested_event=None, scope="own_launch_only", filter=None)
    return [cap("application_ranges", "available", "explicit_software_annotations"),
            cap("schedule", "unsupported", "not_implemented_in_D0"),
            cap("on_cpu", "unverified", "no_continuous_scheduler_source"),
            cap("pmu", "unverified", "optional_probe_not_enabled"),
            cap("spe", "unverified", "optional_probe_not_enabled"),
            cap("spe_thread_association", "unverified", "CONTEXT_association_not_validated"),
            cap("uncore", "unverified", "not_collected"),
            cap("device_transfer", "unverified", "no_device_trace"),
            cap("die_domain_mapping", "unverified", "unknown_no_cpu_number_inference")]


def finalize_session(session):
    """Validate, deduplicate and derive guarded software complements in a copy."""
    s = copy.deepcopy(session)
    if s.get("schema_version", SCHEMA) != SCHEMA:
        raise ValueError("unsupported session schema_version")
    s["schema_version"] = SCHEMA
    s.setdefault("session_id", str(uuid.uuid4()))
    s.setdefault("run_id", s["session_id"])
    s.setdefault("data_origin", "imported")
    if s["data_origin"] not in ("application", "imported", "synthetic"):
        raise ValueError("data_origin must be application/imported/synthetic")
    s.setdefault("execution_mode", "user_only")
    s.setdefault("capture_profile", "basic")
    s.setdefault("tool_version", "0.1.0-d0")
    s.setdefault("app", {"name": "Imported session", "build_id": None, "workload_config": {}})
    s.setdefault("clock", {"id": "unknown", "unit": "ns", "origin_ns": 0, "resolution_ns": None})
    if s["clock"].get("unit") != "ns":
        raise ValueError("normalized session timestamps must use ns")
    s.setdefault("threads", [])
    s.setdefault("sources", [])
    s.setdefault("quality", {})
    s.setdefault("provenance", [])
    if not s.get("capability_manifest"):
        s["capability_manifest"] = capability_defaults()
    s.setdefault("topology", {"source": "unverified", "package": None, "die": None,
                               "compute_domain": None, "cpu_residency": "unavailable"})
    s.setdefault("job_id", None)
    s.setdefault("verified_allocation", None)
    s.setdefault("allocation_status", "unverified")
    s.setdefault("deployment_policy_version", None)
    s.setdefault("deployment_status", "not_deployed")
    s.setdefault("authorized_uid", None)
    s.setdefault("schema_units", {"time": "ns", "bytes": "logical_bytes"})
    events, seen, duplicate_count = [], {}, 0
    for e in s.get("events", []):
        if e.get("args", {}).get("lxtrace_derived_driver"):
            continue
        if not isinstance(e.get("event_id"), str):
            raise ValueError("event_id must be a string")
        if e["event_id"] in seen:
            if e != seen[e["event_id"]]:
                raise ValueError("conflicting duplicate event_id: " + e["event_id"])
            duplicate_count += 1
            continue
        seen[e["event_id"]] = copy.deepcopy(e)
        for field in ("begin_ts", "end_ts"):
            value = e.get(field)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError("invalid nonnegative integer timestamp: " + field)
        if e.get("begin_ts") is None:
            raise ValueError("begin_ts is required")
        if e.get("end_ts") is not None and e["end_ts"] < e["begin_ts"]:
            raise ValueError("end before begin")
        e.setdefault("thread_id", "unknown")
        e["thread_id"] = str(e["thread_id"])
        e.setdefault("kind", "range")
        e.setdefault("category", "unknown")
        e.setdefault("name", e["event_id"])
        e.setdefault("parent_id", None)
        e.setdefault("frame_id", None)
        e.setdefault("args", {})
        e.setdefault("source_id", "unknown")
        e.setdefault("evidence", "observed")
        e["status"] = "incomplete" if e.get("end_ts") is None else e.get("status", "complete")
        events.append(e)
    s["events"] = events
    q = s["quality"]
    q["duplicate_events_removed"] = q.get("duplicate_events_removed", 0) + duplicate_count
    q["incomplete_events"] = sum(e.get("end_ts") is None for e in events)
    q.setdefault("dropped_records", 0)
    q.setdefault("protocol_errors", 0)
    q.setdefault("wait_exclusion_coverage", "unknown")
    q.setdefault("clock_uncertainty_ns", None)
    lost = has_loss(s)
    by_thread = {}
    for e in events:
        by_thread.setdefault(e["thread_id"], []).append(e)
    roles = {str(t["thread_id"]): t.get("role") for t in s["threads"]}
    for frame in list(events):
        if (frame["kind"] != "frame" or frame.get("end_ts") is None or lost
                or roles.get(frame["thread_id"]) != "frame_driver"
                or not frame["args"].get("driver_calls_complete")):
            continue
        calls = [e for e in by_thread[frame["thread_id"]] if e["event_id"] != frame["event_id"]
                 and e.get("frame_id") == (frame.get("frame_id") or frame["event_id"])
                 and e["kind"] in ("range", "wait", "exchange")]
        if any(e.get("end_ts") is None for e in calls):
            continue
        inside = intersection([(frame["begin_ts"], frame["end_ts"])],
                              [(e["begin_ts"], e["end_ts"]) for e in calls])
        for i, (a, b) in enumerate(subtract([(frame["begin_ts"], frame["end_ts"])], inside)):
            events.append(dict(event_id=frame["event_id"] + ":outside:" + str(i),
                               begin_ts=a, end_ts=b, kind="driver_outside", category="drv",
                               name="驱动线程调用外", thread_id=frame["thread_id"],
                               pid=frame.get("pid"), tid=frame.get("tid"), parent_id=frame["event_id"],
                               frame_id=frame.get("frame_id") or frame["event_id"], status="complete",
                               source_id=frame["source_id"], evidence="observed",
                               args={"lxtrace_derived_driver": True, "definition": "frame minus complete marked calls; wall time"}))
    events.sort(key=lambda e: (e["begin_ts"], e["event_id"]))
    last = max((e.get("end_ts") if e.get("end_ts") is not None else e["begin_ts"] for e in events), default=0)
    s.setdefault("time_range", [0, last])
    s["time_range"][1] = max(s["time_range"][1], last)
    return s


def analyze(session, start, end, options=None):
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in (start, end)) or end <= start or start < 0:
        raise ValueError("selection must be finite 0 <= start < end")
    options = options or {}
    tids = set(map(str, options.get("thread_ids", [])))
    # Frame/operator filters are optional semantic selectors.  They operate on
    # explicit IDs carried by the source; time proximity is never used to infer
    # membership.  Accepting the application label as well as the normalized
    # event ID lets framework adapters expose stable names without rewriting
    # the session first.
    frame_ids = set(map(str, options.get("frame_ids", [])))
    operator_ids = set(map(str, options.get("operator_ids", [])))
    frame_by_id = {str(e["event_id"]): e for e in session.get("events", []) if e.get("kind") == "frame"}
    for frame_key, frame in frame_by_id.items():
        label = (frame.get("args") or {}).get("application_frame_id")
        if label is not None and str(label) in frame_ids:
            frame_ids.add(frame_key)

    def matches_semantic_filter(event):
        args = event.get("args") or {}
        if frame_ids:
            frame_values = {
                str(value) for value in (
                    event.get("event_id") if event.get("kind") == "frame" else None,
                    event.get("frame_id"),
                    args.get("application_frame_id"), args.get("frame_id"),
                    args.get("frame_key"),
                ) if value is not None
            }
            if not frame_values.intersection(frame_ids):
                return False
        if operator_ids:
            frame_args = (frame_by_id.get(str(event.get("frame_id")), {}).get("args") or {})
            operator_values = {
                str(value) for value in (
                    args.get("operator_id"), args.get("operator"),
                    args.get("operator_instance_id"),
                    frame_args.get("operator_id"), frame_args.get("operator"),
                    frame_args.get("operator_instance_id"),
                ) if value is not None
            }
            if not operator_values.intersection(operator_ids):
                return False
        return True

    selected, seen = [], set()
    for e in session.get("events", []):
        if (e["event_id"] in seen or (tids and str(e["thread_id"]) not in tids)
                or not matches_semantic_filter(e)):
            continue
        seen.add(e["event_id"])
        if e["begin_ts"] < end and (e.get("end_ts") is None or e["end_ts"] > start
                                     or e["begin_ts"] == e["end_ts"] >= start):
            selected.append(e)
    closed = [e for e in selected if e.get("end_ts") is not None and e.get("status", "complete") == "complete"]
    def iv(e):
        return (max(start, e["begin_ts"]), min(end, e["end_ts"]))
    groups = {}
    for e in closed:
        g = groups.setdefault(str(e["thread_id"]), {"compute": [], "exclude": [], "wait": []})
        if e["kind"] == "range" and e["category"] in COMPUTE:
            g["compute"].append(iv(e))
        if e["kind"] in ("wait", "exchange"):
            g["exclude"].append(iv(e))
        if e["kind"] == "wait":
            g["wait"].append(iv(e))
    open_masks = 0
    for e in selected:
        if e["kind"] in ("wait", "exchange") and (e.get("end_ts") is None or e.get("status", "complete") != "complete"):
            # A possible open wait is not an observed interval; conservatively
            # remove its unknown suffix from confirmed compute lower bounds.
            g = groups.setdefault(str(e["thread_id"]), {"compute": [], "exclude": [], "wait": []})
            g["exclude"].append((max(start, e["begin_ts"]), end))
            open_masks += 1
    raw = union([x for g in groups.values() for x in g["compute"]])
    per_thread_compute = [subtract(g["compute"], g["exclude"]) for g in groups.values()]
    compute = union([x for intervals in per_thread_compute for x in intervals])
    parallel = parallel_intervals(per_thread_compute)
    if has_loss(session):
        # An unlocated lost wait/exchange might invalidate any purported
        # compute suffix. Keep raw annotations, but certify no compute time.
        compute = []
        parallel = []
    requests = union([iv(e) for e in closed if e["kind"] == "request"])
    wait = union([iv(e) for e in closed if e["kind"] == "wait"])
    overlap = intersection(compute, requests)
    q = session.get("quality", {})
    incomplete = sum(e.get("end_ts") is None or e.get("status", "complete") != "complete" for e in selected)
    uncertain = bool(incomplete or has_loss(session))
    c, r, o = length(compute), length(requests), length(overlap)
    warnings = []
    if incomplete:
        warnings.append("未闭合事件未计入确定时长；数值仅覆盖已闭合区间。")
    if open_masks:
        warnings.append("未闭合等待/交换后的同线程计算后缀未知，已从确定计算下界中保守排除。")
    if has_loss(session):
        warnings.append("源存在无位置丢失或协议错误，计算排除覆盖未知；确定计算下界置零，原始标注和闭合请求保留。")
    exclusion = q.get("wait_exclusion_coverage", "unknown")
    if exclusion != "complete":
        warnings.append("显式等待/交换标注覆盖未知；只扣除已标记的同线程区间。")
    warnings.append("线程墙钟语义重叠；不是 on-CPU、硬件传输活动或通信隐藏收益。")
    children = {}
    for e in selected:
        if e.get("parent_id"):
            children.setdefault(e["parent_id"], []).append(e)
    event_stats = []
    for e in selected:
        complete = e.get("end_ts") is not None and e.get("status", "complete") == "complete"
        inclusive = length(union([iv(e)])) if complete else None
        same_thread_children = [x for x in children.get(e["event_id"], []) if x["thread_id"] == e["thread_id"]
                                and x["kind"] not in ("request", "driver_outside", "instant", "counter")]
        open_child = any(x.get("end_ts") is None or x.get("status", "complete") != "complete" for x in same_thread_children)
        child = [iv(x) for x in same_thread_children if x.get("end_ts") is not None]
        own = length(subtract([iv(e)], child)) if complete and not open_child and not has_loss(session) else None
        event_stats.append(dict(event_id=e["event_id"], name=e["name"], kind=e["kind"], category=e["category"],
                                thread_id=e["thread_id"], begin_ts=e["begin_ts"], end_ts=e.get("end_ts"),
                                parent_id=e.get("parent_id"), frame_id=e.get("frame_id"),
                                inclusive_wall_ns=inclusive, self_wall_ns=own, status=e.get("status", "complete"),
                                evidence=e.get("evidence", "observed"), source_id=e.get("source_id")))
    selected_ids = {e["event_id"] for e in selected}
    context_ids = set(selected_ids)
    by_id = {e["event_id"]: e for e in session.get("events", [])}
    for e in selected:
        p, visited = e.get("parent_id"), set()
        while p in by_id and p not in visited:
            visited.add(p)
            context_ids.add(p)
            p = by_id[p].get("parent_id")
    context = [dict(e, selection_context=e["event_id"] not in selected_ids)
               for e in session.get("events", []) if e["event_id"] in context_ids]
    return dict(interval=[start, end], wall_ns=end-start, compute_raw_ns=length(raw), compute_ns=c,
                compute_parallel_ns=length(parallel),
                request_ns=r, overlap_ns=o, raw_overlap_ns=length(intersection(raw, requests)), wait_ns=length(wait),
                request_overlap_ratio=o/r if r and not uncertain else None,
                compute_overlap_ratio=o/c if c and not uncertain else None,
                thread_ids=sorted(groups), frame_ids=sorted(frame_ids), operator_ids=sorted(operator_ids),
                event_count=len(selected), incomplete_count=incomplete,
                exact=not uncertain, known_subset_only=uncertain, exclusion_coverage=exclusion,
                on_cpu_ns=None, cpu_residency="unavailable", transfer_overlap_ns=None,
                denominator_request_ns=r, denominator_compute_ns=c,
                warnings=warnings, events=event_stats, selected_events=context,
                intervals={"compute": compute, "requests": requests, "overlap": overlap, "parallel": parallel})


def export_trace(session):
    events = []
    for t in session.get("threads", []):
        events.append(dict(ph="M", name="thread_name", pid=t.get("pid") or 0,
                           tid=t.get("tid") or t["thread_id"], args={"name": t.get("name", t["thread_id"])}))
    for e in session["events"]:
        item = dict(name=e["name"], cat=e["category"], ts=e["begin_ts"] / 1000,
                    pid=e.get("pid") or 0, tid=e.get("tid") or e["thread_id"],
                    args=dict(e.get("args", {}), event_id=e["event_id"], source_id=e["source_id"],
                              evidence=e.get("evidence"), status=e["status"]))
        if e.get("end_ts") is None:
            item.update(ph="i", s="t")
            item["args"].update(end_ts=None, incomplete_begin=True)
        elif e["kind"] in ("instant", "counter"):
            item.update(ph="i", s="t")
        elif e["kind"] == "request":
            item.update(ph="b", id=e["event_id"])
            events.append(item)
            item = dict(item, ph="e", ts=e["end_ts"] / 1000,
                        tid=e.get("args", {}).get("completion_tid") or item["tid"])
        else:
            item.update(ph="X", dur=(e["end_ts"] - e["begin_ts"]) / 1000)
        events.append(item)
    return dict(traceEvents=events, displayTimeUnit="ms", lxtrace={k: v for k, v in session.items() if k != "events"})


def write_json(data, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_report(session, path):
    template = (ROOT / "legacy_web" / "index.html").read_text(encoding="utf-8")
    payload = json.dumps(session, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    template = template.replace('<!--LXTRACE_SESSION-->', '<script data-lxtrace-session>window.LXTRACE_SESSION=' + payload + ';</script>')
    import re
    template = re.sub(r'<link\b[^>]*href=[\"\'](?:\./)?style\.css[\"\'][^>]*>',
                      lambda _: '<style>' + (ROOT / "legacy_web/style.css").read_text(encoding="utf-8") + '</style>', template)
    for name in ("analysis.js", "viewer.js"):
        template = re.sub(r'<script\b[^>]*src=[\"\'](?:\./)?' + re.escape(name) + r'[\"\'][^>]*>\s*</script>',
                          lambda _, name=name: '<script>' + (ROOT / "legacy_web" / name).read_text(encoding="utf-8").replace('</script', '<\\/script') + '</script>', template)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(template, encoding="utf-8")


def write_csv(result, path):
    fields = ["event_id", "name", "kind", "category", "thread_id", "begin_ts", "end_ts", "parent_id", "frame_id", "inclusive_wall_ns", "self_wall_ns", "status", "evidence", "source_id"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for event in result["events"]:
            # Avoid interpreting user-controlled names as spreadsheet formulas.
            w.writerow({k: ("'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v) for k, v in event.items()})


def doctor():
    """Read-only self/process topology snapshot. Never attaches or runs perf."""
    caps = capability_defaults()
    tracepaths = [Path("/sys/kernel/tracing/events/sched/sched_switch/id"),
                  Path("/sys/kernel/debug/tracing/events/sched/sched_switch/id")]
    metadata = dict(feature_id="schedule_metadata", availability="unverified", reason_code="not_probed_on_this_OS", failure_stage="metadata_read")
    if os.name == "posix":
        errors = []
        for p in tracepaths:
            try:
                p.read_text()
                metadata.update(availability="available", reason_code="sched_switch_id_readable", evidence_ref=str(p))
                break
            except OSError as exc:
                errors.append(dict(path=str(p), errno=exc.errno, reason=str(exc)))
        else:
            denied = any(x["errno"] in (1, 13) for x in errors)
            metadata.update(availability="denied" if denied else "unverified", reason_code="metadata_permission_denied" if denied else "metadata_not_accessible", errors=errors)
    caps += [metadata, dict(feature_id="schedule_capture", availability="unverified", reason_code="blocked_by_metadata" if metadata["availability"] != "available" else "not_implemented_in_D0", failure_stage="not_attempted")]
    def read(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    return dict(schema_version="lxtrace.capability.v1", checked_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                host_id=socket.gethostname(), node=socket.gethostname(), kernel=platform.release(), machine=platform.machine(),
                python=platform.python_version(), authorized_uid=os.getuid() if hasattr(os, "getuid") else None,
                boot_id=read("/proc/sys/kernel/random/boot_id"), capability_manifest=caps,
                clock={"id": "CLOCK_MONOTONIC", "resolution_ns": int(time.get_clock_info("monotonic").resolution * 1e9)},
                topology={"source": "self_affinity_and_readonly_sysfs", "online_cpu_list": read("/sys/devices/system/cpu/online"),
                          "self_affinity": affinity, "verified_allocation": None, "allocation_status": "unverified",
                          "package": None, "die": None, "compute_domain": None, "cpu_residency": "unavailable"},
                execution_mode="user_only", capture_profile="basic", intrusive_probes_enabled=False)


def synthetic(count=100000):
    if not 1 <= count <= 1000000:
        raise ValueError("synthetic count must be 1..1000000")
    threads = [dict(thread_id="synthetic:" + str(i), pid=1, tid=i+1, name="模拟 worker " + str(i), role="worker") for i in range(16)]
    events = []
    for i in range(count):
        slot, thread = divmod(i, 16)
        start = slot * 200000 + thread * 1500
        kind, cat = ("wait", "wait") if slot % 9 == 6 else (("request", "cx") if slot % 9 == 3 else ("range", "mat" if slot % 2 else "vec"))
        name = ("GEMM", "RMSNorm", "queue.wait", "exchange.submit")[slot % 4]
        if i == 0:
            name = '<script>alert("标签转义")</script> & GEMM'
        events.append(dict(event_id="syn:" + str(i), thread_id=threads[thread]["thread_id"], pid=1, tid=thread+1,
                           kind=kind, category=cat, name=name, begin_ts=start, end_ts=start + 150000,
                           source_id="synthetic-fixture", evidence="observed", parent_id=None, frame_id=None,
                           args={"request_id": "syn-request:" + str(i)} if kind == "request" else {}))
    return finalize_session(dict(data_origin="synthetic", execution_mode="user_only", threads=threads, events=events,
                                 app={"name": "100k software-event fixture", "build_id": "synthetic-v1", "workload_config": {"event_count": count}},
                                 sources=[{"source_id": "synthetic-fixture", "description": "UI/math fixture; not hardware measurement"}],
                                 quality={"wait_exclusion_coverage": "complete", "dropped_records": 0}))
