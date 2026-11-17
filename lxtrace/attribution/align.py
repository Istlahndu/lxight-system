"""Align GFTRACE2 counter rates to a timing run without joining run clocks.

The producer's stage-ending barrier / call-end sequence identifies work blocks.
All source counts stay integers. Carried values are estimates over a block-mode
aggregate, never hardware activity intervals. The implementation uses the
product GFTRACE2 reader and state machine; no handoff scripts are imported.
"""
import hashlib
import math
from bisect import bisect_left
from collections import Counter, defaultdict
from pathlib import Path

from ..gftrace import (DRIVER_PAIRS, K, NOSTAGE, _meta, driver_intervals,
                       pool_segments, read_gftrace2)

TAIL = 0xFFFE
MODEL_EVIDENCE = "model_calibrated:cross_run_counter_rate"


def _thread_id(slot):
    return "%d:%d:%d" % slot


def _configuration(meta, text):
    config = dict(meta.get("configuration", {}))
    dies = {}
    for line in (text or "").splitlines():
        words = line.split()
        if not words:
            continue
        if words[0] == "run":
            i = 2
            while i < len(words):
                key = words[i]
                size = 2 if key == "slots" else 1
                if i + size >= len(words):
                    raise ValueError("incomplete production run configuration")
                try:
                    value = [int(x) for x in words[i + 1:i + size + 1]]
                except ValueError as exc:
                    raise ValueError("invalid production run configuration") from exc
                if key != "freq":
                    config[key] = value if size > 1 else value[0]
                i += size + 1
        elif words[0] == "die" and len(words) == 5 and words[2] == "layers":
            dies[str(int(words[1]))] = [int(words[3]), int(words[4])]
    return config, dies


def _segment_buffer(buffer, model=None):
    events = buffer["ev"]
    # Reuse validation and the chained SUB / wait-resume semantics from the
    # existing importer. Work-block identities are additional derived metadata.
    segments = pool_segments(events, "production thread " + _thread_id((buffer["die"], buffer["dom"], buffer["thr"])), model=model)
    closing, labels, layer = [], [], -1
    for index, (_tick, kind, code, _aux) in enumerate(events):
        if kind == K["CALL_B"]:
            layer = code
        if kind == K["CALL_E"] or kind == K["BAR_L"] and code != NOSTAGE:
            closing.append(index)
            labels.append((layer, TAIL if kind == K["CALL_E"] else code))
    labels.append((-1, -1))
    result = []
    grouped = {}
    values = buffer["v"]
    for i0, i1, mode, _stage in segments:
        block = bisect_left(closing, i1)
        key = (block, mode)
        ticks = events[i1][0] - events[i0][0]
        row = grouped.setdefault(key, {"ticks": 0, "values": [0] * 9})
        row["ticks"] += ticks
        if values is not None:
            for column in range(9):
                row["values"][column] += values[i1][column] - values[i0][column]
        result.append((i0, i1, block, mode))
    return {"labels": labels, "groups": grouped, "segments": result}


def _read_run(path, metadata=None, name=None, model=None):
    path = Path(path)
    if metadata is None:
        metadata = path.with_suffix(".txt")
        if not metadata.is_file():
            raise ValueError("production attribution requires GFTRACE2 PMU/configuration metadata: " + str(metadata))
    meta, text, meta_name = _meta(metadata)
    from ..plugins import load_model
    model = load_model(model)
    if 'GFTRACE2' not in model['trace']['format']:
        raise ValueError('selected model plugin does not support GFTRACE2')
    meta['stages'] = {**model['trace'].get('stages', {}), **{str(k): v for k, v in meta['stages'].items()}}
    frequency, buffers = read_gftrace2(path)
    if meta.get("frequency_hz", frequency) != frequency:
        raise ValueError("GFTRACE2 binary and metadata timer frequencies disagree")
    if meta.get("native_clock_id", "CNTVCT_EL0") != "CNTVCT_EL0":
        raise ValueError("production input must use its declared CNTVCT_EL0 clock")
    nonzero = any(any(row) for buffer in buffers for row in buffer["v"] or [])
    if meta["pmu_state"] in ("ticks_only", "disabled") and nonzero:
        raise ValueError("timing-only metadata conflicts with nonzero PMU readings")
    threads, drivers = {}, {}
    for buffer in buffers:
        slot = (buffer["die"], buffer["dom"], buffer["thr"])
        if buffer["dom"] >= 0:
            if not buffer["ev"]:
                raise ValueError("empty production pool thread buffer")
            threads[slot] = dict(buffer, segmentation=_segment_buffer(buffer, model))
        else:
            if buffer["die"] in drivers:
                raise ValueError("multiple driver buffers for one die")
            drivers[buffer["die"]] = dict(buffer, intervals=driver_intervals(buffer["ev"]))
    if not threads:
        raise ValueError("production attribution requires at least one pool thread")
    config, dies = _configuration(meta, text)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata_path = Path(metadata) if isinstance(metadata, (str, Path)) else None
    metadata_bytes = metadata_path.read_bytes() if metadata_path is not None else None
    return dict(name=name or meta.get("run") or path.stem, path=path, frequency=frequency,
                metadata=meta, configuration=config, die_layers=dies,
                threads=threads, drivers=drivers, codes=meta["codes"],
                provenance=dict(run=name or meta.get("run") or path.stem, filename=path.name, path=str(path),
                                sha256=digest, bytes=path.stat().st_size, format="GFTRACE2",
                                metadata=meta_name,
                                metadata_sha256=hashlib.sha256(metadata_bytes).hexdigest() if metadata_bytes is not None else None,
                                reader="lxtrace.gftrace.read_gftrace2", counter_frequency_hz=frequency,
                                pmu_state=meta["pmu_state"], codes=meta["codes"],
                                pool_threads=len(threads), driver_threads=len(drivers)))


def _stage(meta, code):
    if code == TAIL:
        return "tail"
    if code < 0:
        return "(调用外)"
    return str(meta["stages"].get(str(code), meta["stages"].get(code, str(code))))


def _frame_for(run, slot):
    buffer = run["threads"][slot]
    lo, hi = buffer["ev"][0][0], buffer["ev"][-1][0]
    driver = run["drivers"].get(slot[0])
    if driver:
        for i0, i1, kind, _parent in driver["intervals"]:
            if kind == K["FRAME_B"] and driver["ev"][i0][0] <= lo and hi <= driver["ev"][i1][0]:
                return driver["ev"][i0][3]
    return None


def _block_frames(run, slot):
    """Associate active work with enclosing same-run software frames only."""
    buffer = run["threads"][slot]
    driver = run["drivers"].get(slot[0])
    if not driver:
        return {}
    frames = sorted((driver["ev"][i][0], driver["ev"][j][0], driver["ev"][i][3])
                    for i, j, kind, _ in driver["intervals"] if kind == K["FRAME_B"])
    extents = {}
    for i0, i1, block, mode in buffer["segmentation"]["segments"]:
        a, z = buffer["ev"][i0][0], buffer["ev"][i1][0]
        if mode == "idle" or a == z:
            continue
        before = extents.get(block, (a, z))
        extents[block] = (min(a, before[0]), max(z, before[1]))
    result = {}
    for block, (a, z) in extents.items():
        result[block] = next((frame for lo, hi, frame in frames if lo <= a and z <= hi), None)
    return result


def _rate_groups(buffer, codes):
    grouped, labels = buffer["segmentation"]["groups"], buffer["segmentation"]["labels"]
    stage_mode, by_mode = {}, {}
    for (block, mode), row in grouped.items():
        for group, key in ((stage_mode, (labels[block][1], mode)), (by_mode, mode)):
            target = group.setdefault(key, [0] * (1 + len(codes)))
            for column in range(len(target)):
                target[column] += row["values"][column]
    return stage_mode, by_mode


def _windows(base, origin):
    ns = lambda tick: (tick - origin) * 1_000_000_000 // base["frequency"]
    pool_windows, counting_windows, driver_lanes = {}, {}, []
    for die in sorted({slot[0] for slot in base["threads"]}):
        selected = [b for slot, b in base["threads"].items() if slot[0] == die]
        lo = min(b["ev"][0][0] for b in selected)
        hi = max(b["ev"][-1][0] for b in selected)
        enclosing, frame_id = None, None
        driver = base["drivers"].get(die)
        if driver:
            enclosing = next(((driver["ev"][i][0], driver["ev"][j][0], driver["ev"][i][3])
                              for i, j, kind, _ in driver["intervals"] if kind == K["FRAME_B"]
                              and driver["ev"][i][0] <= lo and hi <= driver["ev"][j][0]), None)
        if enclosing:
            lo, hi, frame_id = enclosing
        pool_windows[str(die)] = dict(begin_ns=ns(lo), end_ns=ns(hi), begin_ticks=lo, end_ticks=hi,
                                      frame_id=frame_id, basis="enclosing_driver_frame" if enclosing else "retained_pool_extent")
    for die, driver in sorted(base["drivers"].items()):
        frames = []
        for i, j, kind, parent in driver["intervals"]:
            begin, end = driver["ev"][i], driver["ev"][j]
            name, _category = DRIVER_PAIRS[kind]
            lane = dict(die=die, kind=name, begin_ns=ns(begin[0]), end_ns=ns(end[0]),
                        begin_ticks=begin[0], end_ticks=end[0], code=begin[2], aux=begin[3],
                        layer=begin[2] if kind == K["LAY_B"] else None,
                        raw_event_indices=[i, j], parent_event_index=parent,
                        representation="interval", layer_type="software", evidence="producer_software_boundaries")
            if kind == K["FRAME_B"]:
                lane["frame_id"] = begin[3]
                lane["counting"] = bool(base["metadata"]["frames"].get(str(begin[3]), {}).get("counting", begin[2]))
                frames.append(lane)
            driver_lanes.append(lane)
        frames.sort(key=lambda x: x["begin_ticks"])
        counted = [i for i, frame in enumerate(frames) if frame["counting"]]
        if counted:
            first, last = counted[0], counted[-1]
            a = frames[first - 1]["end_ticks"] if first else frames[first]["begin_ticks"]
            z = frames[last]["end_ticks"]
            counting_windows[str(die)] = dict(begin_ns=ns(a), end_ns=ns(z), begin_ticks=a, end_ticks=z,
                                              frame_ids=[frames[i]["frame_id"] for i in counted],
                                              basis="prior_frame_end_to_last_counting_frame_end")
    return pool_windows, counting_windows, driver_lanes


def align_runs(timing_path, count_paths, core_frequency_hz, *, timing_metadata=None,
               count_metadata=None, strict_alignment=False, expected_pool_threads=None, model=None):
    """Carry first-listed event rates onto tT block-mode aggregates.

    ``count_paths`` is an ordered mapping such as ``{'tA': path, 'tB': path}``.
    Exact block-mode rates take priority, then same-thread stage-mode and mode
    rates. Every fallback and unavailable rate remains explicit. Strict mode
    rejects any non-exact rate on a positive-duration timing aggregate.
    """
    if not isinstance(core_frequency_hz, (int, float)) or isinstance(core_frequency_hz, bool) or not math.isfinite(core_frequency_hz) or core_frequency_hz <= 0:
        raise ValueError("core_frequency_hz must be finite and positive")
    if not isinstance(count_paths, dict) or not count_paths:
        raise ValueError("at least one named counting run is required")
    if expected_pool_threads is not None and (type(expected_pool_threads) is not int or expected_pool_threads <= 0):
        raise ValueError("expected_pool_threads must be a positive integer")
    from ..plugins import load_model
    plugin = load_model(model)
    mode_order = list(dict.fromkeys(['busy'] + plugin['exchange_modes'] + plugin['wait_modes'] + plugin['idle_modes'] + plugin['compute_modes']))
    mode_rank = {mode: i for i, mode in enumerate(mode_order)}
    base = _read_run(timing_path, timing_metadata, "tT", plugin)
    if base["metadata"]["pmu_state"] not in ("ticks_only", "disabled"):
        raise ValueError("the timing run must declare ticks-only or disabled PMU collection")
    counts = []
    for name, path in count_paths.items():
        if not isinstance(name, str) or not name or name == "tT":
            raise ValueError("counting run names must be nonempty and distinct from tT")
        run = _read_run(path, (count_metadata or {}).get(name), name, plugin)
        if run["metadata"]["pmu_state"] != "counting" or not run["codes"]:
            raise ValueError("counting run requires known PMU event codes: " + name)
        if set(run["threads"]) != set(base["threads"]):
            raise ValueError("pool thread sets differ between tT and " + name)
        for field in ("configuration", "die_layers"):
            if run[field] != base[field]:
                raise ValueError("production " + field + " differs between tT and " + name)
        for field in ("stages", "layers", "frames"):
            if run["metadata"][field] != base["metadata"][field]:
                raise ValueError("production " + field + " metadata differs between tT and " + name)
        if any(buffer["nv"] != 9 for buffer in run["threads"].values()):
            raise ValueError("counting run has missing cumulative PMU readings: " + name)
        if any(b[0] == a[0] and any(b[c] != a[c] for c in range(1, 9))
               for buffer in run["threads"].values()
               for a, b in zip(buffer["v"], buffer["v"][1:])):
            raise ValueError("counting run has event increments without elapsed PMU cycles: " + name)
        counts.append(run)
    origin = min(b["ev"][0][0] for b in [*base["threads"].values(), *base["drivers"].values()] if b["ev"])
    ns = lambda tick: (tick - origin) * 1_000_000_000 // base["frequency"]
    cpt = core_frequency_hz / base["frequency"]
    entries, segments, stage_modes, run_counts = [], [], {}, {}
    coverage = Counter()
    coverage_cycles = defaultdict(float)
    run_totals = {run["name"]: dict(matched_count_cycles=0, base_cycles_matched=0.0,
                                     own_pool_cycles=0, unmatched_count_cycles=0,
                                     matched_count_ticks=0, matched_timing_ticks=0,
                                     matched_cycle_ratio_min=None, matched_cycle_ratio_max=None) for run in counts}
    run_of = {}
    for run in counts:
        for code in run["codes"]:
            run_of.setdefault(code, run["name"])
    for slot, buffer in sorted(base["threads"].items()):
        thread_id = _thread_id(slot)
        sb = buffer["segmentation"]
        frame_id = _frame_for(base, slot)
        block_frames = _block_frames(base, slot)
        per_run = {}
        for run in counts:
            name, counted = run["name"], run["threads"][slot]
            sc = counted["segmentation"]
            if sc["labels"] != sb["labels"]:
                raise ValueError("thread %s: %s layer/stage block sequence differs from tT" % (thread_id, name))
            if _frame_for(run, slot) != frame_id:
                raise ValueError("thread %s: %s traced frame differs from tT" % (thread_id, name))
            if _block_frames(run, slot) != block_frames:
                raise ValueError("thread %s: %s per-block traced frames differ from tT" % (thread_id, name))
            sm, modes = _rate_groups(counted, run["codes"])
            per_run[name] = (run, counted, sm, modes)
            for (block, mode), row in sc["groups"].items():
                run_totals[name]["own_pool_cycles"] += row["values"][0]
                if (block, mode) not in sb["groups"]:
                    run_totals[name]["unmatched_count_cycles"] += row["values"][0]
                if mode != "idle":
                    label = _stage(run["metadata"], sc["labels"][block][1]) + "|" + mode
                    target = run_counts.setdefault(label, {}).setdefault(name, {})
                    for code, value in zip(["0x0011"] + run["codes"], row["values"]):
                        target[code] = target.get(code, 0) + value
        ids = {}
        for (block, mode), base_row in sorted(sb["groups"].items(), key=lambda pair: (pair[0][0], mode_rank.get(pair[0][1], len(mode_rank)), pair[0][1])):
            layer, stage_code = sb["labels"][block]
            stage = _stage(base["metadata"], stage_code)
            ticks, cycles = base_row["ticks"], base_row["ticks"] * cpt
            entry_id = thread_id + ":%d:%s" % (block, mode)
            ids[(block, mode)] = entry_id
            entry = dict(entry_id=entry_id, thread_id=thread_id, die=slot[0], domain=slot[1], thread=slot[2],
                         block=block, layer=layer, stage_code=stage_code, stage=stage, mode=mode,
                         frame=block_frames.get(block, frame_id),
                         ticks=ticks, cycles=cycles, codes={}, raw={}, ccyc={}, raw_counts={},
                         counter_cycles={}, rate_evidence={}, representation="aggregate", evidence=MODEL_EVIDENCE)
            for run in counts:
                name = run["name"]
                _run, counted, sm, modes = per_run[name]
                match = counted["segmentation"]["groups"].get((block, mode))
                values = match["values"] if match is not None else [0] * 9
                entry["ccyc"][name] = entry["counter_cycles"][name] = values[0]
                entry["raw_counts"][name] = dict(zip(["0x0011"] + run["codes"], values))
                run_totals[name]["matched_count_cycles"] += values[0]
                if match is not None:
                    run_totals[name]["base_cycles_matched"] += cycles
                    run_totals[name]["matched_count_ticks"] += match["ticks"]
                    run_totals[name]["matched_timing_ticks"] += ticks
                    if cycles > 0:
                        ratio = values[0] / cycles
                        for field, select in (("matched_cycle_ratio_min", min), ("matched_cycle_ratio_max", max)):
                            old = run_totals[name][field]
                            run_totals[name][field] = ratio if old is None else select(old, ratio)
                for column, code in enumerate(run["codes"], 1):
                    if code in entry["rate_evidence"]:
                        continue
                    numerator, denominator = values[column], values[0]
                    basis, source_key = "block_mode", [thread_id, block, mode]
                    if not denominator:
                        fallback = sm.get((stage_code, mode), [])
                        if fallback and fallback[0] > 0:
                            numerator, denominator = fallback[column], fallback[0]
                            basis, source_key = "stage_mode", [thread_id, stage_code, mode]
                        else:
                            fallback = modes.get(mode, [])
                            if fallback and fallback[0] > 0:
                                numerator, denominator = fallback[column], fallback[0]
                                basis, source_key = "mode", [thread_id, mode]
                            else:
                                numerator, denominator = 0, 0
                                basis, source_key = "unavailable", [thread_id, mode]
                    if strict_alignment and ticks > 0 and basis != "block_mode":
                        raise ValueError("strict alignment rejects %s fallback for %s %s" % (basis, entry_id, code))
                    estimate = numerator / denominator * cycles if denominator else None
                    if estimate is not None:
                        entry["codes"][code] = estimate
                    entry["raw"][code] = values[column]
                    evidence = dict(run=name, rate_basis=basis, basis=basis, source_key=source_key,
                                    raw_count=numerator, count_cycles=denominator, matched_raw_count=values[column],
                                    matched_count_cycles=values[0], base_cycles=cycles, estimate=estimate,
                                    matched_count_to_timing_cycle_ratio=values[0]/cycles if match is not None and cycles > 0 else None,
                                    formula="count_run_event / count_run_cycles * timing_run_cycles",
                                    representation="aggregate", evidence=MODEL_EVIDENCE)
                    entry["rate_evidence"][code] = evidence
                    coverage[basis] += 1
                    coverage_cycles[basis] += cycles
            label = stage + "|" + mode
            acc = stage_modes.setdefault(label, {"ticks": 0, "cycles": 0.0})
            acc["ticks"] += ticks
            acc["cycles"] += cycles
            for field, prefix in (("codes", ""), ("raw", "raw_"), ("ccyc", "ccyc_")):
                for key, value in entry[field].items():
                    acc[prefix + key] = acc.get(prefix + key, 0) + value
            entries.append(entry)
        for i0, i1, block, mode in sb["segments"]:
            a, z = buffer["ev"][i0][0], buffer["ev"][i1][0]
            layer, code = sb["labels"][block]
            segments.append(dict(entry_id=ids[(block, mode)], thread_id=thread_id, die=slot[0], domain=slot[1], thread=slot[2],
                                 frame=block_frames.get(block, frame_id), block=block, stage=_stage(base["metadata"], code), mode=mode,
                                 layer=layer, begin_ticks=a, end_ticks=z, begin_ns=ns(a), end_ns=ns(z),
                                 ticks=z-a, cycles=(z-a)*cpt, raw_event_indices=[i0, i1],
                                 allocation_basis="aggregate_rate_within_block_mode"))
    pool_windows, counting_windows, drivers = _windows(base, origin)
    observed = len(base["threads"])
    if expected_pool_threads is not None and observed > expected_pool_threads:
        raise ValueError("retained pool thread count exceeds the declared expected count")
    warnings = ["Cross-run counter rates are model estimates on tT aggregate work blocks, not simultaneous hardware activity.",
                "Uniform allocation within a block-mode is used only to render aggregate bins; internal stall boundaries are unknown."]
    if coverage["stage_mode"] or coverage["mode"] or coverage["unavailable"]:
        warnings.append("Some exact block-mode counter rates are unavailable; fallback basis and source keys are retained per event.")
    full = expected_pool_threads is not None and observed == expected_pool_threads
    if not full:
        warnings.append("Pool coverage is partial or undeclared; whole-machine MFU cannot be inferred from these retained threads.")
    check = {}
    perturbation = {}
    for name, totals in run_totals.items():
        matched = totals["base_cycles_matched"]
        totals["count_to_base_cycles_ratio"] = totals["matched_count_cycles"] / matched if matched else None
        run = next(r for r in counts if r["name"] == name)
        count_time = totals["matched_count_ticks"] * 1_000_000_000 / run["frequency"]
        base_time = totals["matched_timing_ticks"] * 1_000_000_000 / base["frequency"]
        perturbation[name] = dict(matched_counting_time_ns=count_time, matched_timing_time_ns=base_time,
                                  duration_ratio=count_time/base_time if base_time else None,
                                  cycle_ratio=totals["count_to_base_cycles_ratio"],
                                  matched_block_mode_cycle_ratio_range=[totals["matched_cycle_ratio_min"], totals["matched_cycle_ratio_max"]],
                                  interpretation="observed run variation including counter-read disturbance; not an isolated overhead measurement or confidence interval")
        check["count_cycles_" + name] = totals["matched_count_cycles"]
        check["base_cycles_" + name] = matched
    return dict(schema_version="lxtrace.production-alignment.v1", base="tT", counts=list(count_paths),
                counter_frequency_hz=base["frequency"], freq=base["frequency"], core_frequency_hz=core_frequency_hz,
                cycles_per_tick=cpt, cpt=cpt, origin_ticks=origin,
                time_conversion="(ticks-origin_ticks)*1000000000//counter_frequency_hz",
                codes={run["name"]: run["codes"] for run in counts}, run_order=list(count_paths), run_of=run_of,
                metadata=dict(base["metadata"], configuration=base["configuration"], die_layers=base["die_layers"]),
                entries=entries, segments=segments, drivers=drivers, driver_lanes=drivers,
                windows={die: [w["begin_ns"], w["end_ns"]] for die, w in pool_windows.items()},
                pool_windows=pool_windows, counting_windows=counting_windows,
                threads=dict(Counter(str(slot[0]) for slot in base["threads"])),
                expected_pool_threads=expected_pool_threads, observed_pool_threads=observed,
                full_pool_coverage=full, pool_coverage_ratio=observed/expected_pool_threads if expected_pool_threads else None,
                stage_modes=stage_modes, stage_mode_ticks={key: row["ticks"] for key, row in stage_modes.items()},
                run_counts=run_counts, run_totals=run_totals, check=check, perturbation=perturbation,
                alignment=dict(strict=bool(strict_alignment), basis_counts=dict(coverage), basis_cycles=dict(coverage_cycles),
                               coverage_unit="event-code x timing block-mode", missing_rates=coverage["unavailable"],
                               block_sequence_verified=True, absolute_clocks_merged=False),
                provenance=[base["provenance"]] + [run["provenance"] for run in counts], warnings=warnings)
