"""Native GFTRACE2 software events and cumulative PMU group readings.

CNTVCT timestamps and PMU readings are independent evidence. A PMU read window
does not describe hardware activity boundaries. All arithmetic before display
uses integers; the exact input bytes remain in the native session archive.
"""
import base64
import hashlib
import json
import struct
from collections import defaultdict
from pathlib import Path

from .resource import SCHEMA, validate_session

NOSTAGE = 0xFFFF
KINDS = ("CALL_B", "CALL_E", "BAR_A", "BAR_L", "XW_B", "XW_E", "SPIN_B", "SPIN_E",
         "FRAME_B", "FRAME_E", "WIN_B", "WIN_E", "EMB_B", "EMB_E", "LAY_B", "LAY_E",
         "WSLOT_B", "WSLOT_E", "PUSH_B", "PUSH_E", "HEAD_B", "HEAD_E", "SUB_B", "SUB_E")
K = {name: i + 1 for i, name in enumerate(KINDS)}
SITES = {1: "push", 2: "gath", 3: "deqg", 4: "mmg", 5: "deqd", 6: "mmd", 7: "swi", 8: "pkg", 9: "pkd"}
MODE_CATEGORY = {"busy": "unknown", "push": "cx", "gath": "cx", "bar": "wait", "xw": "wait", "spin": "wait",
                 "deqg": "vec", "mmg": "mat", "deqd": "vec", "mmd": "mat", "swi": "vec", "pkg": "vec", "pkd": "vec"}
MODE_KIND = {"push": "exchange", "gath": "exchange", "bar": "wait", "xw": "wait", "spin": "wait"}
DRIVER_PAIRS = {9: ("frame", "frame"), 11: ("driver.input_wait", "wait"), 13: ("embedding", "unknown"),
                15: ("layer", "unknown"), 17: ("driver.slot_wait", "wait"), 19: ("push", "cx"), 21: ("head", "unknown")}


def _nonnegative(value, label):
    if type(value) is not int or value < 0:
        raise ValueError("invalid GFTRACE2 " + label)
    return value


def read_gftrace2(path):
    """Read and validate the complete binary, retaining exact cumulative rows."""
    return _read_gftrace2(Path(path).read_bytes())


def _read_gftrace2(raw):
    if len(raw) < 24 or raw[:8] != b"GFTRACE2":
        raise ValueError("invalid or truncated GFTRACE2 header")
    freq, count, reserved = struct.unpack_from("<Qii", raw, 8)
    if freq <= 0 or count < 0 or count > (len(raw) - 24) // 24 or reserved:
        raise ValueError("invalid GFTRACE2 frequency/buffer count/reserved field")
    offset, buffers, slots = 24, [], set()
    for index in range(count):
        if offset + 24 > len(raw):
            raise ValueError("truncated GFTRACE2 buffer header")
        die, dom, thr, n, nv, zero = struct.unpack_from("<6i", raw, offset)
        offset += 24
        if die < 0 or dom < -1 or thr < -1 or (dom >= 0 and thr < 0) or n < 0 or nv not in (0, 9) or zero:
            raise ValueError("invalid GFTRACE2 buffer coordinates/count/value columns/reserved field")
        slot = (die, dom, thr)
        if slot in slots:
            raise ValueError("duplicate GFTRACE2 logical thread buffer")
        slots.add(slot)
        if n * (16 + nv * 8) > len(raw) - offset:
            raise ValueError("truncated GFTRACE2 events or PMU readings")
        event_offset = offset
        events = list(struct.iter_unpack("<QHHi", raw[offset:offset + n * 16]))
        offset += n * 16
        value_offset = offset
        values = list(struct.iter_unpack("<9Q", raw[offset:offset + n * nv * 8])) if nv else None
        offset += n * nv * 8
        if any(events[j][0] < events[j - 1][0] for j in range(1, n)):
            raise ValueError("GFTRACE2 timestamps decrease within a logical thread")
        if values and any(values[j][c] < values[j - 1][c] for j in range(1, n) for c in range(nv)):
            raise ValueError("GFTRACE2 cumulative counter decreases")
        buffers.append(dict(die=die, dom=dom, thr=thr, ev=events, v=values, nv=nv,
                            buffer_index=index, event_offset=event_offset, value_offset=value_offset))
    if offset != len(raw):
        raise ValueError("trailing bytes after GFTRACE2 buffers")
    return freq, buffers


def _meta(metadata):
    text, name = None, None
    if isinstance(metadata, dict):
        m = dict(metadata)
    elif metadata is None:
        m = {}
    else:
        path = Path(metadata)
        text, name = path.read_text(encoding="utf-8"), path.name
        if path.suffix.lower() == ".json":
            m = json.loads(text)
        else:
            m = {"stages": {}, "layers": {}, "frames": {}}
            for line in text.splitlines():
                w = line.split()
                if not w:
                    continue
                key = w[0]
                try:
                    if key == "run":
                        m["run"] = w[1]
                        if "freq" in w:
                            m["frequency_hz"] = int(w[w.index("freq") + 1])
                    elif key in ("stage", "layer"):
                        if len(w) < 3:
                            raise ValueError("missing stage/layer name")
                        m["stages" if key == "stage" else "layers"][str(int(w[1]))] = " / ".join(w[2:])
                    elif key == "frame":
                        if len(w) < 4 or len(w) % 2 or w[2] != "counting" or w[3] not in ("0", "1"):
                            raise ValueError("invalid frame counting metadata")
                        frame = {"counting": w[3] == "1"}
                        for j in range(4, len(w), 2):
                            if w[j] not in ("parts", "tokens"):
                                raise ValueError("unknown frame metadata field")
                            frame[w[j]] = _nonnegative(int(w[j + 1]), "frame " + w[j])
                        fid = str(_nonnegative(int(w[1]), "frame index"))
                        if fid in m["frames"] and frame != m["frames"][fid]:
                            raise ValueError("conflicting frame metadata")
                        m["frames"][fid] = frame
                    elif key == "pmu":
                        if len(w) != 2 or ("pmu" in m and m["pmu"] != w[1]):
                            raise ValueError("invalid or conflicting PMU metadata")
                        m["pmu"] = w[1]
                    elif key in ("host_id", "boot_id", "run_id", "counter_domain_id", "native_clock_id"):
                        if len(w) != 2 or (key in m and m[key] != w[1]):
                            raise ValueError("invalid or conflicting identity metadata")
                        m[key] = w[1]
                except (IndexError, TypeError) as exc:
                    raise ValueError("malformed GFTRACE2 metadata line: " + line) from exc
    if not isinstance(m, dict):
        raise ValueError("GFTRACE2 metadata must be an object")
    for key in ("stages", "layers", "frames"):
        m.setdefault(key, {})
        if not isinstance(m[key], dict):
            raise ValueError("GFTRACE2 " + key + " metadata must be an object")
    for frame in m["frames"].values():
        if not isinstance(frame, dict):
            raise ValueError("invalid GFTRACE2 frame metadata")
        if "counting" in frame and not (isinstance(frame["counting"], bool) or type(frame["counting"]) is int and frame["counting"] in (0, 1)):
            raise ValueError("invalid GFTRACE2 frame counting metadata")
        for field in ("parts", "tokens"):
            if field in frame:
                _nonnegative(frame[field], "frame " + field)
    for key in ("host_id", "boot_id", "run_id", "counter_domain_id", "native_clock_id"):
        if key in m and (not isinstance(m[key], str) or not m[key]):
            raise ValueError("invalid GFTRACE2 identity " + key)
    codes = m.get("pmu", m.get("codes"))
    state = "unknown" if codes is None else "ticks_only" if codes == "ticks" else "disabled" if codes == "-" else "counting"
    if isinstance(codes, str) and state == "counting":
        codes = codes.split(",")
    if state == "counting":
        if not isinstance(codes, list) or len(codes) > 8:
            raise ValueError("GFTRACE2 PMU metadata requires at most eight event codes")
        try:
            values = [int(c, 0) if isinstance(c, str) else c for c in codes]
        except ValueError as exc:
            raise ValueError("invalid GFTRACE2 PMU event code") from exc
        if any(type(v) is not int or v < 0 or v > 0xFFFF for v in values) or len(values) != len(set(values)) or 0x11 in values:
            raise ValueError("invalid or duplicate GFTRACE2 PMU event code")
        m["codes"] = ["0x%04x" % v for v in values]
    else:
        m["codes"] = []
    m["pmu_state"] = state
    return m, text, name


def pool_segments(events, where="pool"):
    """Attribute each adjacent pair to the post-event mode and next stage end."""
    segments, pending = [], []
    active, call, mode, sub, wait = False, None, "idle", None, None
    for i, (_tick, kind, code, aux) in enumerate(events):
        if i:
            segments.append([i - 1, i, mode, None])
            if mode != "idle":
                pending.append(len(segments) - 1)
        if kind == 1:
            if active:
                raise ValueError(where + ": nested CALL_B")
            active, call, mode, sub, wait = True, (code, aux), "busy", None, None
        elif not active:
            raise ValueError(where + ": pool event outside a call")
        elif kind == 2:
            if call != (code, aux) or wait is not None or sub is not None:
                raise ValueError(where + ": mismatched CALL_E or open wait/step")
            for j in pending:
                segments[j][3] = "tail"
            pending, active, call, mode = [], False, None, "idle"
        elif kind in (3, 5, 7):
            if wait is not None:
                raise ValueError(where + ": overlapping synchronous waits")
            wait, mode = (kind, code), {3: "bar", 5: "xw", 7: "spin"}[kind]
        elif kind in (4, 6, 8):
            if wait != (kind - 1, code):
                raise ValueError(where + ": wait end without matching begin")
            if kind == 4 and code != NOSTAGE:
                for j in pending:
                    segments[j][3] = code
                pending = []
            wait, mode = None, sub or "busy"
        elif kind == 23:
            if wait is not None:
                raise ValueError(where + ": SUB_B crosses an open wait")
            mode = sub = SITES.get(code, "site.%d" % code)
        elif kind == 24:
            if sub is None or wait is not None or sub != SITES.get(code, "site.%d" % code):
                raise ValueError(where + ": SUB_E without matching step")
            mode, sub = "busy", None
        else:
            raise ValueError(where + ": invalid pool event kind %d" % kind)
    if active or pending:
        raise ValueError(where + ": call not closed at buffer end")
    return segments


def mode_runs(segments):
    result = []
    for i0, i1, mode, stage in segments:
        if result and result[-1][2:] == [mode, stage] and result[-1][1] == i0:
            result[-1][1] = i1
        else:
            result.append([i0, i1, mode, stage])
    return result


def driver_intervals(events, where="driver"):
    stack, result = [], []
    for i, (_t, kind, code, aux) in enumerate(events):
        if kind in DRIVER_PAIRS:
            stack.append((i, kind, code, aux, stack[-1][0] if stack else None))
        elif kind - 1 in DRIVER_PAIRS:
            if not stack or stack[-1][1:4] != (kind - 1, code, aux):
                raise ValueError(where + ": driver end without matching begin")
            j, begin, _code, _aux, parent = stack.pop()
            result.append((j, i, begin, parent))
        else:
            raise ValueError(where + ": invalid driver event kind %d" % kind)
    if stack:
        raise ValueError(where + ": driver interval not closed at buffer end")
    return result


def load_gftrace2(path, metadata=None, mapping=None, windows=True, run_id=None):
    """Import GFTRACE2 directly into a resource v2 session, using no plugins."""
    path = Path(path)
    if metadata is None and path.with_suffix(".txt").is_file():
        metadata = path.with_suffix(".txt")
    raw = path.read_bytes()
    frequency, buffers = _read_gftrace2(raw)
    meta, meta_text, meta_name = _meta(metadata)
    if meta.get("native_clock_id", "CNTVCT_EL0") != "CNTVCT_EL0":
        raise ValueError("GFTRACE2 timestamps are CNTVCT_EL0; metadata cannot rename the native clock")
    declared_freq = meta.get("frequency_hz", meta.get("freq"))
    if declared_freq is not None and (type(declared_freq) is not int or declared_freq != frequency):
        raise ValueError("GFTRACE2 binary and metadata clock frequencies disagree")
    stamps = [e[0] for b in buffers for e in (b["ev"][:1] + b["ev"][-1:])]
    if not stamps:
        raise ValueError("GFTRACE2 contains no events")
    origin = min(stamps)
    ns = lambda t: (t - origin) * 1_000_000_000 // frequency
    resolution = max(1, -(-1_000_000_000 // frequency))
    digest = hashlib.sha256(raw).hexdigest()
    raw_name = "raw/gftrace2/trace.bin.base64"
    raw_files = {raw_name: base64.b64encode(raw).decode("ascii")}
    if meta_text is not None:
        raw_files["raw/gftrace2/metadata.txt"] = meta_text
    else:
        raw_files["raw/gftrace2/metadata.json"] = json.dumps(meta, ensure_ascii=False, sort_keys=True)
    raw_files["raw/gftrace2/encoding.json"] = json.dumps({"path": raw_name, "encoding": "base64", "decoded_sha256": digest,
                                                        "format": "GFTRACE2", "bytes": len(raw)})
    mapping = json.loads(Path(mapping).read_text(encoding="utf-8")) if mapping and not isinstance(mapping, dict) else mapping or {}
    if not isinstance(mapping, dict) or not isinstance(mapping.get("threads", mapping), dict):
        raise ValueError("GFTRACE2 mapping must be an object")
    mapping_threads = mapping.get("threads", mapping)
    resources = [dict(resource_id="gf:node", type="node", parent_id=None, name=meta.get("run") or "GFTRACE2",
                      mapping_source="logical producer slots; no physical CPU mapping inferred")]
    seen, records, aggregates = {"gf:node"}, [], defaultdict(int)
    state = meta["pmu_state"]
    codes = ["0x0011"] + meta["codes"]
    any_nonzero = any(any(row) for b in buffers for row in b["v"] or [])
    if state in ("ticks_only", "disabled") and any_nonzero:
        raise ValueError("GFTRACE2 nonzero PMU readings conflict with ticks/disabled metadata")
    have_counts = any(b["nv"] for b in buffers) and (state == "counting" or state == "unknown" and any_nonzero)
    sources = [dict(source_id="gf.sw", source_layer="software", scope="pool_and_driver_threads", clock_id="cntvct",
                    boundary_semantics="producer software event transitions", lost_records=0,
                    continuity_basis="well-formed complete buffers; producer aborts on overflow")]
    if have_counts:
        sources.append(dict(source_id="gf.pmu", source_layer="hardware", scope="pool_and_driver_thread:user", clock_id="cntvct",
                            native_format="cumulative group readings at events", semantics_status="raw_pmu_user_per_thread",
                            boundary_support="counter_window", completeness="every_event_reading", lost_records=0,
                            codes=codes, pmu_metadata_status=state,
                            timing_basis="producer requires non-multiplexed reads; not independently measured per window"))

    def refs(b, i0, i1):
        return [dict(path=raw_name, encoding="base64", decoded_sha256=digest, buffer_index=b["buffer_index"],
                     event_indices=[i0, i1], event_byte_offsets=[b["event_offset"] + 16 * i for i in (i0, i1)],
                     reading_byte_offsets=[b["value_offset"] + b["nv"] * 8 * i for i in (i0, i1)] if b["nv"] else None)]

    def boundary(b, i0, i1):
        return dict(raw_begin_ticks=b["ev"][i0][0], raw_end_ticks=b["ev"][i1][0], gf_buffer_id=b["buffer_index"],
                    raw_event_indices=[i0, i1], raw_refs=refs(b, i0, i1))

    def stage_name(stage):
        return "tail" if stage == "tail" else str(meta["stages"].get(str(stage), meta["stages"].get(stage, "stage.%d" % stage)))

    def add_counts(b, thread_id, die_id, i0, i1, label):
        readings, events = b["v"], b["ev"]
        if readings is None or not have_counts:
            return
        a, z = ns(events[i0][0]), ns(events[i1][0])
        column_codes = codes + ["unknown.column.%d" % c for c in range(len(codes), 9)]
        for c, code in enumerate(column_codes):
            delta = readings[i1][c] - readings[i0][c]
            if c >= len(codes) and delta == 0:
                continue
            aggregates[(die_id, label, code)] += delta
            if windows and delta:
                records.append(dict(record_id=thread_id + ":w%d:%s" % (i0, code), resource_id=thread_id,
                                    source_id="gf.pmu", category="pmu", layer="hardware", representation="counter_window",
                                    begin_ns=a, end_ns=z, event=code, unit="count", value=delta, raw=delta, scaled=delta,
                                    enabled_ns=z - a, running_ns=z - a, multiplex_ratio=1.0, label=label,
                                    counter_begin=str(readings[i0][c]), counter_end=str(readings[i1][c]), column_index=c,
                                    semantics_status="raw_pmu_user_per_thread" if not code.startswith("unknown") else "counter_code_unknown",
                                    boundary_semantics="cumulative reads at bounding software events; not hardware activity",
                                    uncertainty_ns=resolution, **boundary(b, i0, i1)))

    for b in buffers:
        die, dom, thr = b["die"], b["dom"], b["thr"]
        die_id, dom_id, thread_id = "gf:die:%d" % die, "gf:die:%d:dom:%d" % (die, dom), "gf:thread:%d:%d:%d" % (die, dom, thr)
        slot = "%d:%d:%d" % (die, dom, thr)
        for rid, typ, parent, name in ((die_id, "die", "gf:node", "die %d" % die),
                                       (dom_id, "domain", die_id, "driver" if dom < 0 else "domain %d" % dom),
                                       (thread_id, "thread", dom_id, "driver" if dom < 0 else "thread %d" % thr)):
            if rid not in seen:
                seen.add(rid)
                resources.append(dict(resource_id=rid, type=typ, parent_id=parent, name=name,
                                      mapping_source="gf_trace logical slot", logical_slot=dict(die=die, domain=dom, thread=thr)))
        declared = mapping_threads.get(slot)
        if declared is not None:
            if not isinstance(declared, dict):
                raise ValueError("GFTRACE2 thread mapping must be an object")
            resources[-1]["declared_mapping"] = declared
            resources[-1]["mapping_source"] = "explicit mapping metadata; not observed residency"
        events, readings = b["ev"], b["v"]
        if dom < 0:
            for i0, i1, kind, parent in driver_intervals(events, thread_id):
                base, category = DRIVER_PAIRS[kind]
                code, aux = events[i0][2:]
                args = dict(kind=KINDS[kind - 1][:-2], code=code, aux=aux)
                name = "frame.%d" % aux if kind == 9 else "layer.%d %s" % (code, meta["layers"].get(str(code), meta["layers"].get(code, ""))) if kind == 15 else base
                if kind == 9:
                    frame = meta["frames"].get(str(aux), meta["frames"].get(aux, {}))
                    args.update(frame)
                    args["window_flag"] = code
                    if "counting" in frame:
                        args.update(counting=bool(frame["counting"]), counting_source="frame_metadata")
                record = dict(record_id=thread_id + ":d%d" % i0, resource_id=thread_id, source_id="gf.sw", category=category,
                              layer="software", representation="interval", begin_ns=ns(events[i0][0]), end_ns=ns(events[i1][0]),
                              name=name.strip(), uncertainty_ns=resolution, args=args, **boundary(b, i0, i1))
                if parent is not None:
                    record["parent_id"] = thread_id + ":d%d" % parent
                if category in ("wait", "cx"):
                    record["kind"] = "wait" if category == "wait" else "exchange"
                elif kind == 9:
                    record["kind"] = "frame"
                records.append(record)
            # Attribute each adjacent driver reading exactly once to the active
            # innermost scope. Nested frame/layer pairs must not double counts.
            driver_stack = []
            for index, event in enumerate(events):
                if index:
                    label = "driver|" + DRIVER_PAIRS[driver_stack[-1]][0] if driver_stack else "driver|idle"
                    add_counts(b, thread_id, die_id, index - 1, index, label)
                kind = event[1]
                if kind in DRIVER_PAIRS:
                    driver_stack.append(kind)
                else:
                    driver_stack.pop()
            continue
        for i0, i1, mode, stage in mode_runs(pool_segments(events, thread_id)):
            a, z = ns(events[i0][0]), ns(events[i1][0])
            label = "idle" if stage is None else stage_name(stage) + "|" + mode
            if mode != "idle" and z > a:
                records.append(dict(record_id=thread_id + ":s%d" % i0, resource_id=thread_id, source_id="gf.sw",
                                    category=MODE_CATEGORY.get(mode, "unknown"), kind=MODE_KIND.get(mode, "range"),
                                    layer="software", representation="interval", begin_ns=a, end_ns=z, name=label,
                                    uncertainty_ns=resolution, args=dict(mode=mode, stage=stage_name(stage)), **boundary(b, i0, i1)))
            add_counts(b, thread_id, die_id, i0, i1, label)
    for (die_id, label, code), total in sorted(aggregates.items()):
        records.append(dict(record_id="agg:%s:%s:%s" % (die_id, label, code), resource_id=die_id, source_id="gf.pmu",
                            category="pmu", layer="hardware", representation="aggregate", event=code, unit="count", value=total,
                            label=label, aggregation_rule="sum adjacent cumulative differences over this die's retained threads; driver scopes are exclusive",
                            semantics_status="raw_pmu_user_per_thread" if not code.startswith("unknown") else "counter_code_unknown",
                            uncertainty_ns=None, raw_refs=[dict(path=raw_name, encoding="base64", decoded_sha256=digest)]))
    session_id = "gftrace2-" + digest[:24]
    warnings = ["CNTVCT_EL0 is a native timer, not CLOCK_MONOTONIC; cross-input alignment requires explicit shared counter-domain identity.",
                "Use the ticks run for time; counter-reading runs include instrumentation overhead.",
                "Logical slots are not physical CPU residency or verified hardware activity."]
    if state == "unknown":
        warnings.append("PMU event-code metadata is missing; unnamed columns remain unknown.")
    if any(c.startswith("unknown") for _, _, c in aggregates):
        warnings.append("Nonzero unnamed PMU columns are preserved without inferring event semantics.")
    clock = dict(clock_id="cntvct", unit="ns", raw_unit="ticks", native_clock_id=meta.get("native_clock_id", "CNTVCT_EL0"),
                 frequency_hz=frequency, origin_ticks=origin, last_ticks=max(stamps), conversion="(ticks-origin_ticks)*1000000000//frequency_hz",
                 conversion_rounding="floor after integer origin subtraction", origin_remainder_numerator=(origin * 1_000_000_000) % frequency,
                 resolution_ns=resolution, error_ns=None, calibration_status="native_counter_not_calibrated_to_OS_monotonic")
    if meta.get("counter_domain_id"):
        clock["counter_domain_id"] = meta["counter_domain_id"]
    metadata_text = meta_text if meta_text is not None else raw_files["raw/gftrace2/metadata.json"]
    session = dict(schema_version=SCHEMA, session_id=session_id, run_id=run_id or meta.get("run_id") or meta.get("run") or session_id,
                   data_origin="imported", time_range=[0, max(1, ns(max(stamps)))], raw_time_range_ticks=[origin, max(stamps)],
                   app=dict(name="GFTRACE2 " + str(meta.get("run", "")), build_id=None),
                   resources=resources, allocation=dict(status="unverified", cpu_ids=[]), sources=sources, clocks=[clock], records=records,
                   capability_manifest=[], quality=dict(dropped_records=0, unknown_windows=[], warnings=warnings),
                   provenance=[dict(kind="gftrace2_import", filename=path.name, sha256=digest, metadata=meta_name, format="GFTRACE2",
                                    importer_version="lxtrace.gftrace2.v1", metadata_sha256=hashlib.sha256(metadata_text.encode("utf-8")).hexdigest(),
                                    codes=codes if have_counts else [], pmu_state=state,
                                    windows_emitted=bool(windows), buffer_count=len(buffers), event_count=sum(len(b["ev"]) for b in buffers),
                                    raw_binary_path=raw_name, raw_encoding="base64", cumulative_readings_preserved=True)], raw_files=raw_files)
    for key in ("host_id", "boot_id", "counter_domain_id"):
        if meta.get(key):
            session[key] = meta[key]
    return validate_session(session)
