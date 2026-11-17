"""Loss-aware SDK JSONL and GFTRACE importers. No third-party packages.

GFTRACE1 support is exactly the little-endian layout in gf_trace.h (2026-11-14
local source). Its slot coordinates remain logical labels without an explicit,
verified mapping. Known SUB sites preserve software operation intent; chained
SUB starts replace the active step. No API range fabricates an async request.
"""
import hashlib
import json
import math
import struct
from pathlib import Path


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _base(path, origin, clock, data_origin):
    digest = _digest(path)
    return {
        "schema_version": "lxtrace.session.v1", "session_id": digest[:24],
        "data_origin": data_origin, "execution_mode": "user_only",
        "clock": {"id": clock, "unit": "ns", "origin_ns": origin,
                  "resolution_ns": None, "calibration_error_ns": None},
        "time_range": [0, 0], "threads": [], "events": [],
        "capability_manifest": [], "topology": {"mapping_status": "unknown"},
        "sources": [], "quality": {}, "app": {},
        "provenance": [{"filename": Path(path).name, "sha256": digest,
                        "importer_version": "lxtrace-d0.1"}],
    }


def _integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("invalid nonnegative integer " + label)
    return value


def _sdk_args_valid(args):
    """Keep malformed/non-interchangeable metadata from destroying its range."""
    if not isinstance(args, dict):
        return False
    pending = [args]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, float) and not math.isfinite(value):
            return False
        elif isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                return False
    return True


def _sdk_quality(quality, rows):
    """SDK snapshots are cumulative; historical unmarked quality rows are deltas."""
    totals, previous, revision = {}, {}, None
    legacy = {"dropped_records": 0, "protocol_errors": 0}
    clean = bool(rows)
    snapshots = 0
    for row in rows:
        quality.setdefault("quality_history", []).append(dict(row))
        clean &= row.get("clean_shutdown") is True
        quality["capacity"] = row.get("capacity")
        if row.get("quality_semantics") == "cumulative_snapshot":
            snapshots += 1
            current_revision = _integer(row.get("quality_revision"), "quality_revision")
            counters = {key: _integer(row.get(key, 0), key) for key in
                        ("dropped_records", "protocol_errors", "late_records", "incomplete_events")}
            invalid = revision is not None and (current_revision <= revision or
                       any(counters[key] < previous.get(key, 0) for key in counters))
            if invalid:
                quality["protocol_errors"] += 1
                quality["warnings"].append("SDK quality snapshots are not monotonic; retained counts are lower bounds")
                quality["quality_counts_are_lower_bounds"] = True
                clean = False
            revision = max(revision if revision is not None else 0, current_revision)
            previous = {key: max(previous.get(key, 0), value) for key, value in counters.items()}
            totals.update(previous)
        elif row.get("quality_semantics") not in (None, "delta"):
            raise ValueError("unsupported SDK quality semantics")
        else:
            for key in legacy:
                legacy[key] += _integer(row.get(key, 0), key)
        for key in ("unsupported_fork_count", "late_records"):
            if key in row:
                quality[key] = max(quality.get(key, 0), _integer(row[key], key))
        for key in ("unsupported_fork_count_is_lower_bound", "fork_capture_policy", "fork_tracking_status"):
            if key in row:
                quality[key] = row[key]
        if row.get("fork_tracking_status") == "unavailable":
            clean = False
            quality["warnings"].append("SDK fork tracking could not be installed; child coverage is unknown")
    for key in legacy:
        # Mixed formats have no declared relationship. Retain the greater known
        # bound rather than double counting a delta already covered by a snapshot.
        quality[key] += max(legacy[key], totals.get(key, 0))
    if snapshots and any(legacy.values()):
        quality["quality_counts_are_lower_bounds"] = True
        quality["protocol_errors"] += 1
        quality["warnings"].append("Mixed SDK delta and cumulative quality; totals are lower bounds")
        clean = False
    if snapshots:
        quality.update(quality_semantics="cumulative_snapshot", quality_revision=revision,
                       quality_snapshot_count=snapshots, reported_incomplete_events=totals.get("incomplete_events", 0))
    if quality.get("unsupported_fork_count"):
        clean = False
        quality["warnings"].append("SDK initialized before fork: child instrumentation is disabled until exec; "
                                   "child coverage is unknown and fork+exec is conservatively partial")
    if not rows:
        quality["warnings"].append("SDK shutdown quality record is missing; capture completeness is unknown")
    quality["clean_shutdown"] = clean


def load_sdk(path):
    """Read lxtrace.sdk.v1 JSONL; only observed, matched ends close events."""
    path = Path(path)
    with path.open('rb') as stream:
        if stream.read(4) == b'LXC1':
            from .compact import load_compact
            return load_compact(path)
    rows, parse_errors = [], []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("record is not an object")
                rows.append(row)
            except (ValueError, json.JSONDecodeError):
                parse_errors.append("invalid JSON record at line %d" % line_no)
    return _load_sdk_rows(path, rows, parse_errors)


def _load_sdk_rows(path, rows, parse_errors):
    """Shared canonical event decoder; compact records retain original provenance."""
    metas = [r for r in rows if r.get("type") == "meta"]
    if not metas or metas[0].get("schema_version") != "lxtrace.sdk.v1":
        raise ValueError("missing supported lxtrace.sdk.v1 header")
    meta = metas[0]
    stamps = [_integer(r["ts"], "ts") for r in rows if "ts" in r]
    origin = min(stamps, default=0)
    session = _base(path, origin, meta.get("clock_id", "unknown"), "application")
    session["app"] = {"name": meta.get("app_name", "unknown"),
                      "build_id": meta.get("build_id", "unknown"),
                      "workload_config": {"frame_definition": "application-defined"}}
    threads, events, order, stacks = {}, {}, [], {}
    quality = {"dropped_records": 0, "protocol_errors": len(parse_errors), "clean_shutdown": False,
               "warnings": parse_errors, "wait_annotation_coverage": "unknown"}
    last, quality_rows = 0, []
    for row in rows:
        typ = row.get("type")
        if typ == "thread":
            th = str(row["thread_id"])
            threads[th] = {k: row.get(k) for k in ("pid", "tid", "name", "role")}
            threads[th]["thread_id"] = th
        elif typ in ("begin", "instant", "counter"):
            eid, th = str(row["event_id"]), str(row["thread_id"])
            if eid in events:
                quality["protocol_errors"] += 1
                continue
            ts = _integer(row["ts"], "ts") - origin
            last = max(last, ts)
            kind = row.get("kind", "range") if typ == "begin" else typ
            args = row.get("args", {})
            if not _sdk_args_valid(args):
                args = {"invalid_args": True}
                quality["protocol_errors"] += 1
            e = {"event_id": eid, "thread_id": th, "pid": row.get("pid"),
                 "tid": row.get("tid"), "kind": kind,
                 "category": row.get("category", "unknown"), "name": str(row.get("name", kind)),
                 "begin_ts": ts, "end_ts": None if typ == "begin" else ts,
                 "parent_id": row.get("parent_id"), "frame_id": row.get("frame_id"),
                 "status": "incomplete" if typ == "begin" else "complete",
                 "source_id": "sdk", "evidence": "observed", "args": args}
            if kind == "request":
                e["args"]["request_id"] = eid  # process + monotonic generation; correlation is separate
                e["args"]["submit_boundary"] = "application_async_begin"
                e["args"]["completion_boundary"] = "completion_observed"
            events[eid] = e
            order.append(eid)
            if typ == "begin" and kind != "request":
                stacks.setdefault(th, []).append(eid)
            threads.setdefault(th, {"thread_id": th, "pid": e["pid"], "tid": e["tid"],
                                    "name": th, "role": "unknown"})
        elif typ == "end":
            ts = _integer(row["ts"], "ts") - origin
            last = max(last, ts)
            e = events.get(str(row.get("event_id")))
            if not e or e["end_ts"] is not None or ts < e["begin_ts"]:
                quality["protocol_errors"] += 1
                continue
            if e["kind"] != "request" and row.get("tid") != e["tid"]:
                quality["protocol_errors"] += 1
                continue
            if e["kind"] != "request":
                stack = stacks.get(e["thread_id"], [])
                if not stack or stack[-1] != e["event_id"]:
                    quality["protocol_errors"] += 1
                    continue
                stack.pop()
            e["end_ts"], e["status"] = ts, "complete"
            if e["kind"] == "request":
                e["args"]["completion_tid"] = row.get("tid")
        elif typ == "quality":
            quality_rows.append(row)
    _sdk_quality(quality, quality_rows)
    # Worker frame references must be explicit application IDs, never proximity in time.
    frame_names = {}
    duplicate_names = set()
    for e in events.values():
        name = e["args"].get("application_frame_id")
        if e["kind"] != "frame" or name is None:
            continue
        if name in frame_names:
            duplicate_names.add(name)
        frame_names[name] = e["event_id"]
    for name in duplicate_names:
        frame_names.pop(name, None)
    if duplicate_names:
        quality["warnings"].append("duplicate application frame labels; worker association left unknown")
    for e in events.values():
        explicit_frame = e["args"].get("application_frame_id")
        if not e["frame_id"] and explicit_frame is not None and explicit_frame in frame_names:
            e["frame_id"] = frame_names[explicit_frame]
        # A worker frame can be opened on another thread than its driver
        # frame.  Preserve an explicit application-level parent relationship
        # without guessing from clock overlap.
        parent_label = e["args"].get("parent_application_frame_id")
        if (not e.get("parent_id") and parent_label is not None
                and parent_label in frame_names and e["kind"] == "frame"):
            e["parent_id"] = frame_names[parent_label]
    # Propagate only through explicit parent IDs (the parent may have been written
    # later by a different source); never infer association from overlapping clocks.
    for e in events.values():
        if e["frame_id"]:
            continue
        parent, visited = events.get(str(e.get("parent_id"))), set()
        while parent is not None and parent["event_id"] not in visited:
            visited.add(parent["event_id"])
            if parent.get("frame_id"):
                e["frame_id"] = parent["frame_id"]
                break
            parent = events.get(str(parent.get("parent_id")))
    session["events"] = [events[k] for k in order]
    session["threads"] = list(threads.values())
    quality["incomplete_events"] = max(sum(e["end_ts"] is None for e in session["events"]),
                                       quality.get("reported_incomplete_events", 0))
    if quality["dropped_records"] or quality["protocol_errors"] or quality["incomplete_events"] or quality.get("late_records"):
        quality["clean_shutdown"] = False
    if quality.get("late_records"):
        quality["warnings"].append("SDK rejected %d record attempts after shutdown; capture is partial" % quality["late_records"])
    if quality["incomplete_events"]:
        quality["warnings"].append("SDK capture has %d unclosed ranges; no end boundary was fabricated" % quality["incomplete_events"])
    quality["capture_partial"] = not quality["clean_shutdown"]
    quality["loss_position_known"] = False
    quality["source_continuity"] = "unknown" if (
        quality["dropped_records"] or quality["protocol_errors"]
        or quality["incomplete_events"] or not quality["clean_shutdown"]
    ) else "complete_recorded_pairs"
    session["quality"] = quality
    session["time_range"] = [0, last]
    session["sources"] = [{"source_id": "sdk", "kind": "application_annotation",
                            "last_reliable_ts": last, "open_prefix_proven": False,
                            "clock_id": session["clock"]["id"], "resolution_ns": None,
                            "loss": quality["dropped_records"], "capture_end_known": quality["clean_shutdown"]}]
    return session


GF_PAIRS = {
    1: (2, "range", "unknown", "pool.call"),
    3: (4, "wait", "wait", "barrier"),
    5: (6, "wait", "wait", "cross_domain_flag_wait"),
    7: (8, "wait", "wait", "spin"),
    9: (10, "frame", "unknown", "frame"),
    11: (12, "wait", "wait", "driver.input_wait"),
    13: (14, "range", "unknown", "embedding"),
    15: (16, "range", "unknown", "layer.call"),
    17: (18, "wait", "wait", "driver.slot_wait"),
    19: (20, "exchange", "cx", "push.api"),
    21: (22, "range", "unknown", "head"),
    23: (24, "exchange", "cx", "subexchange.api"),
}

# The original exchange sites 1/2 retain their meaning. The producer's later
# substep extension uses the same 16-byte event layout and adds sites 3..9.
# These are software intent labels, not hardware activity boundaries.
GF_SUB_SITES = {
    1: ("push", "cx"), 2: ("gath", "cx"),
    3: ("deqg", "vec"), 4: ("mmg", "mat"),
    5: ("deqd", "vec"), 6: ("mmd", "mat"),
    7: ("swi", "vec"), 8: ("pkg", "vec"), 9: ("pkd", "vec"),
}


def _metadata(path):
    if path is None:
        return {}, {}, {}
    if isinstance(path, dict):
        return path, {}, {}
    p = Path(path)
    if not p.exists():
        raise ValueError("GFTRACE metadata does not exist: " + str(p))
    if p.suffix.lower() == ".json":
        return json.loads(p.read_text(encoding="utf-8")), {}, {}
    stages, layers, categories, properties, frames = {}, {}, {}, {}, {}
    for line in p.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if len(words) >= 3 and words[0] == "run":
            properties["run"] = words[1]
            if "freq" in words:
                i = words.index("freq")
                if i + 1 < len(words):
                    properties["frequency_hz"] = int(words[i + 1])
        elif len(words) >= 3 and words[0] == "stage":
            stages[int(words[1])] = words[2]
        elif len(words) >= 3 and words[0] == "layer":
            layers[int(words[1])] = " ".join(words[2:])
        elif len(words) == 3 and words[0] == "category":
            categories[int(words[1])] = words[2]
        elif words and words[0] == "frame":
            if len(words) < 4 or words[2] != "counting" or words[3] not in ("0", "1") or len(words) % 2:
                raise ValueError("invalid GFTRACE frame counting metadata")
            frame = {"counting": words[3] == "1"}
            for i in range(4, len(words), 2):
                if words[i] not in ("parts", "tokens"):
                    raise ValueError("invalid GFTRACE frame metadata field")
                frame[words[i]] = _integer(int(words[i + 1]), "frame " + words[i])
            frame_id = str(_integer(int(words[1]), "frame index"))
            if frame_id in frames and frames[frame_id] != frame:
                raise ValueError("conflicting GFTRACE frame counting metadata")
            frames[frame_id] = frame
        elif len(words) == 2 and words[0] == "data_origin":
            properties["original_data_origin"] = words[1]
        elif len(words) == 2 and words[0] in ("host_id", "boot_id", "run_id", "counter_domain_id"):
            properties[words[0]] = words[1]
    properties["categories"] = categories
    properties["frames"] = frames
    return properties, stages, layers


def _gf_frame_metadata(meta):
    if not isinstance(meta, dict):
        raise ValueError("GFTRACE metadata must be an object")
    frames = meta.get("frames", {})
    if not isinstance(frames, dict):
        raise ValueError("GFTRACE frames metadata must be an object")
    for frame in frames.values():
        if not isinstance(frame, dict):
            raise ValueError("GFTRACE frame metadata must be an object")
        if "counting" in frame:
            counting = frame["counting"]
            if not isinstance(counting, bool) and (type(counting) is not int or counting not in (0, 1)):
                raise ValueError("invalid GFTRACE frame counting metadata")
        for field in ("parts", "tokens"):
            if field in frame:
                _integer(frame[field], "frame " + field)
    return frames


def load_gftrace(path, metadata=None, mapping=None, windows=True, run_id=None):
    """Keep GFTRACE1's v1 API; GFTRACE2 imports directly to the native v2 schema."""
    path = Path(path)
    raw = path.read_bytes()
    if raw[:8] == b"GFTRACE2":
        from .gftrace import load_gftrace2
        return load_gftrace2(path, metadata, mapping, windows=windows, run_id=run_id)
    if len(raw) < 24:
        raise ValueError("truncated GFTRACE1 header")
    magic, frequency, count, reserved = struct.unpack_from("<8sQii", raw)
    if magic != b"GFTRACE1" or frequency <= 0 or count < 0 or count > 100000:
        raise ValueError("invalid GFTRACE1 magic/frequency/buffer count")
    if reserved != 0:
        raise ValueError("unsupported GFTRACE1 reserved header field")
    if metadata is None and path.with_suffix(".txt").exists():
        metadata = path.with_suffix(".txt")
    meta, stages, layers = _metadata(metadata)
    frame_metadata = _gf_frame_metadata(meta)
    if meta.get("frequency_hz", frequency) != frequency:
        raise ValueError("GFTRACE1 metadata and binary clock frequency disagree")
    mappings = mapping if isinstance(mapping, dict) else json.loads(Path(mapping).read_text(encoding="utf-8")) if mapping else {}
    mapping_threads = mappings.get("threads", {})
    offset, buffers, minimum, maximum = 24, [], None, 0
    for _ in range(count):
        if offset + 16 > len(raw):
            raise ValueError("truncated GFTRACE1 buffer header")
        die, domain, slot, n = struct.unpack_from("<iiii", raw, offset)
        offset += 16
        if n < 0 or n > (len(raw) - offset) // 16:
            raise ValueError("truncated GFTRACE1 event buffer or invalid event count")
        records = list(struct.iter_unpack("<QHHi", raw[offset:offset + n * 16]))
        offset += n * 16
        if any(records[i][0] < records[i - 1][0] for i in range(1, n)):
            raise ValueError("GFTRACE1 timestamps decrease within a logical thread")
        if records:
            minimum = records[0][0] if minimum is None else min(minimum, records[0][0])
            maximum = max(maximum, records[-1][0])
        buffers.append((die, domain, slot, records))
    if offset != len(raw):
        raise ValueError("trailing bytes after GFTRACE1 buffers")
    minimum = minimum or 0
    origin_ns = minimum * 1000000000 // frequency
    session = _base(path, origin_ns, "GFTRACE_GENERIC_TIMER" if frequency != 1000000000 else "GFTRACE_SOURCE_CLOCK_UNVERIFIED", "imported")
    session["clock"].update({"original_unit": "ticks", "frequency_hz": frequency,
                              "native_clock_id": "CNTVCT_EL0",
                              "origin_ticks": minimum, "conversion": "(ticks-origin_ticks)*1e9//frequency",
                              "resolution_ns": max(1, (1000000000 + frequency - 1) // frequency)})
    for key in ("host_id", "boot_id", "run_id"):
        if meta.get(key):
            session[key] = meta[key]
    if run_id is not None:
        session["run_id"] = run_id
    if meta.get("counter_domain_id"):
        session["clock"]["counter_domain_id"] = meta["counter_domain_id"]
    session["app"] = {"name": "GFTRACE1 imported logical timeline", "build_id": "unknown", "workload_config": {}}
    if meta.get("original_data_origin"):
        session["original_data_origin"] = meta["original_data_origin"]
        if meta["original_data_origin"] == "synthetic":
            session["app"]["name"] = "GFTRACE1 imported · synthetic format fixture"
        session["provenance"].append({"original_data_origin": meta["original_data_origin"], "fixture_only": True})
    warnings, errors, unknown_kinds, events, threads = [], 0, 0, [], []
    end_kinds = {v[0]: k for k, v in GF_PAIRS.items()}
    categories = meta.get("categories", {})
    for die, domain, slot, records in buffers:
        key = "%d:%d:%d" % (die, domain, slot)
        thread_key = "gf:" + key
        declared = mapping_threads.get(key, {})
        verified = declared.get("verified") is True
        thread = {"thread_id": thread_key, "pid": declared.get("pid") if verified else None,
                  "tid": declared.get("tid") if verified else None,
                  "name": "GF logical die%s / domain%s / slot%s" % (die, domain, slot),
                  "role": "frame_driver" if domain < 0 else "worker",
                  "legacy_logical_slot": {"die": die, "domain": domain, "thread": slot},
                  "physical_mapping_status": "verified" if verified else "unknown"}
        if verified:
            thread["static_mapping"] = {k: declared.get(k) for k in ("cpu", "package", "die", "compute_domain", "core")}
            thread["static_mapping"]["source"] = "explicit_mapping_file; not residency"
        threads.append(thread)
        stack, open_by_kind, frame_id = [], {}, None
        for index, (tick, kind, code, aux) in enumerate(records):
            ts = (tick - minimum) * 1000000000 // frequency
            if kind in GF_PAIRS:
                endkind, eventkind, category, name = GF_PAIRS[kind]
                previous_step = open_by_kind.get(23)
                if (kind == 23 and previous_step is not None and stack
                        and stack[-1] is previous_step
                        and previous_step["args"]["code"] in GF_SUB_SITES
                        and code in GF_SUB_SITES):
                    # SUB_B is a mode switch in the producer. Only the active
                    # top-level step can be replaced; never close an unrelated
                    # wait or recover a crossing/malformed synchronous scope.
                    stack.pop()
                    del open_by_kind[23]
                    previous_step.update(end_ts=ts, status="complete")
                    previous_step["args"].update(
                        raw_end_ticks=tick,
                        end_boundary="next_sub_begin",
                        end_boundary_event_index=index,
                    )
                eid = "gf:%s:%d" % (key, index)
                if kind == 1:
                    name += ":" + str(layers.get(code, "layer.%d" % code))
                if kind == 9:
                    name = "frame.%d" % aux
                if kind == 23:
                    mode, category = GF_SUB_SITES.get(code, ("site.%d" % code, "unknown"))
                    name = "subexchange.api" if code in (1, 2) else "substep." + mode
                category = categories.get(str(kind), categories.get(kind, category))
                if category not in ("mat", "vec", "cc", "cx", "wait", "drv", "unknown"):
                    category = "unknown"
                if kind == 23:
                    eventkind = "exchange" if category == "cx" else "wait" if category == "wait" else "range"
                e = {"event_id": eid, "thread_id": thread_key, "pid": thread["pid"], "tid": thread["tid"],
                     "kind": eventkind, "category": category, "name": name,
                     "begin_ts": ts, "end_ts": None, "parent_id": stack[-1]["event_id"] if stack else None,
                     "frame_id": frame_id, "status": "incomplete", "evidence": "observed", "source_id": "gftrace",
                     "args": {"legacy_kind": kind, "code": code, "aux": aux,
                              "raw_begin_ticks": tick, "logical_slot": key}}
                if kind == 9:
                    frame_id = eid
                    e["frame_id"] = eid
                    e["args"]["driver_calls_complete"] = False
                    declared_frame = frame_metadata.get(str(aux), frame_metadata.get(aux, {}))
                    if "counting" in declared_frame:
                        e["args"].update(counting=bool(declared_frame["counting"]), counting_source="frame_metadata")
                    for field in ("parts", "tokens"):
                        if field in declared_frame:
                            e["args"][field] = _integer(declared_frame[field], "frame " + field)
                if kind == 23:
                    e["args"].update(substep_mode=mode,
                                     category_source="metadata" if str(kind) in categories or kind in categories else "sub_site_table" if code in GF_SUB_SITES else "unknown_site")
                if eventkind == "wait":
                    e["args"]["wait_object"] = stages.get(code, "site.%d" % code)
                if kind in open_by_kind:
                    errors += 1
                    warnings.append("nested duplicate GFTRACE kind in " + key)
                open_by_kind[kind] = e
                stack.append(e)
                events.append(e)
            elif kind in end_kinds:
                begin_kind = end_kinds[kind]
                e = open_by_kind.pop(begin_kind, None)
                if e is None:
                    errors += 1
                    continue
                if e["args"]["code"] != code or (begin_kind in (1, 9) and e["args"]["aux"] != aux):
                    errors += 1
                    continue
                if not stack or stack[-1] is not e:
                    errors += 1
                    # Crossing intervals have no safe synchronous closure.
                    if e in stack:
                        stack.remove(e)
                    continue
                stack.pop()
                e.update({"end_ts": ts, "status": "complete"})
                e["args"]["raw_end_ticks"] = tick
                if begin_kind == 9:
                    frame_id = None
            else:
                unknown_kinds += 1
                events.append({"event_id": "gf:%s:%d" % (key, index), "thread_id": thread_key,
                               "pid": thread["pid"], "tid": thread["tid"], "kind": "instant",
                               "category": "unknown", "name": "unknown GFTRACE kind %d" % kind,
                               "begin_ts": ts, "end_ts": ts, "parent_id": None, "frame_id": frame_id,
                               "status": "complete", "evidence": "observed", "source_id": "gftrace",
                               "args": {"raw_ticks": tick, "code": code, "aux": aux}})
    if not mapping:
        warnings.append("TID/CPU/die/domain mapping unavailable; GF slot labels are logical only")
    if unknown_kinds:
        warnings.append("unknown GFTRACE kinds preserved as instant markers")
    warnings.append("GFTRACE1 source has no loss counter or request lifecycle IDs; continuity is unverified")
    session["threads"], session["events"] = threads, events
    last = (maximum - minimum) * 1000000000 // frequency if maximum >= minimum else 0
    session["time_range"] = [0, last]
    session["quality"] = {"dropped_records": None, "protocol_errors": errors,
                          "incomplete_events": sum(e["end_ts"] is None for e in events),
                          "clean_shutdown": None, "source_continuity": "unverified_legacy_format",
                          "wait_annotation_coverage": "unknown", "warnings": warnings}
    session["sources"] = [{"source_id": "gftrace", "kind": "imported_application_annotation",
                            "last_reliable_ts": last, "open_prefix_proven": False,
                            "loss": None, "clock_id": session["clock"]["id"]}]
    if metadata is not None and not isinstance(metadata, dict):
        session["provenance"].append({"filename": Path(metadata).name, "sha256": _digest(metadata), "kind": "metadata"})
    if mapping is not None and not isinstance(mapping, dict):
        session["provenance"].append({"filename": Path(mapping).name, "sha256": _digest(mapping), "kind": "mapping"})
    return session
