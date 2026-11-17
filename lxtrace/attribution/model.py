"""Production aggregate attribution, separated from verified hardware intervals.

Formula sources: glm5f-tl3@850d5a0f gf_pmu.py (matrix-first, issuer and
unit views), glm5f-dsub@148a1202 gf_dsub.py (dense), and mfu-kit-tl3@09d3036a
chain.py. The implementation uses only the standard library and the versioned
hardware/model profiles shipped here. It never imports a reference checkout or
uses golden output as an input to a calculation.
"""
import copy
from collections import defaultdict

from .hardware import (EVIDENCE, REQUIRED_CODES, _number, _bin, _source_shares,
                       load_hardware, split_compute, split_issue, unit_load,
                       required_codes, canonical_counts, event_code)
from ..plugins import load_model, presentation


MODEL_VERSION = "lxtrace.production-attribution.v1"
MATRIX_CLASSES = ("mat", "vec", "mem", "comm_c", "fe", "comm_x", "wait", "idle", "unknown")
ISSUE_CLASSES = ("mopa", "ld_l1", "ld_l2", "ld_mem", "ld_sib", "ld_xpkg", "fe", "other")
UNITS = ("mat", "sve", "ldst", "l2bw", "xbw", "ipc")
DENSE_CLASSES = ("mat", "work", "far", "fe", "wait")


def _empty():
    return {"cycles": 0.0, "ticks": 0, "matrix_first": {k: 0.0 for k in MATRIX_CLASSES},
            "issue": {k: 0.0 for k in ISSUE_CLASSES + ("comm_x", "wait", "idle", "unknown")},
            "compute_cycles": 0.0, "counters": {}, "compute_counters": {}, "unit_load": {k: 0.0 for k in UNITS},
            "view_missing_codes": {k: [] for k in REQUIRED_CODES},
            "view_unknown_cycles": {k: 0.0 for k in REQUIRED_CODES},
            "unit_denominator_cycles": 0.0, "bounds": {k: 0.0 for k in ("ov", "ov_lo", "ov_hi", "mem_lo", "mem_hi")}}


def _add(group, entry):
    group["cycles"] += entry["cycles"]
    group["ticks"] += entry.get("ticks", 0)
    for field in ("matrix_first", "issue", "bounds"):
        for k in group[field]:
            group[field][k] += entry[field].get(k, 0)
    group["compute_cycles"] += entry.get("compute_cycles", 0)
    for k, v in entry.get("codes", {}).items():
        group["counters"][k] = group["counters"].get(k, 0) + v
        if entry.get("compute_cycles"):
            group["compute_counters"][k] = group["compute_counters"].get(k, 0) + v
    for field in REQUIRED_CODES:
        group["view_missing_codes"][field] = sorted(set(group["view_missing_codes"][field]) | set(entry.get("view_missing_codes", {}).get(field, [])))
        group["view_unknown_cycles"][field] += entry.get("view_unknown_cycles", {}).get(field, 0)
    denominator = entry.get("unit_denominator_cycles", 0)
    group["unit_denominator_cycles"] += denominator
    for k in UNITS:
        group["unit_load"][k] += (entry.get("unit_load", {}).get(k) or 0) * denominator


def _finish(group, hardware=None):
    C = group["cycles"]
    group["matrix_first_pct"] = {k: 100 * v / C if C else 0 for k, v in group["matrix_first"].items()}
    group["issue_pct"] = {k: 100 * v / C if C else 0 for k, v in group["issue"].items()}
    den = group["unit_denominator_cycles"]
    group["unit_load"] = {k: v / den if den else None for k, v in group["unit_load"].items()}
    # A phase's unit view uses that phase's aggregate compute counts. A tiny
    # block can have inconsistent cross-run SVE rates while the phase total is
    # valid. Preserve the local unknown amount and do not silently exclude its
    # work from the phase's numerator or denominator.
    if group["compute_cycles"] and not group["view_missing_codes"]["unit_load"]:
        group["unit_local_unknown_cycles"] = group["view_unknown_cycles"]["unit_load"]
        try:
            group["unit_load"] = unit_load({"cycles": group["compute_cycles"], "counters": group["compute_counters"]}, hardware)
            group["unit_denominator_cycles"] = group["compute_cycles"]
            group["unit_aggregation_basis"] = "aggregate_compute_counts_over_compute_cycles"
        except ValueError:
            group["unit_load"] = {k: None for k in UNITS}
            group["unit_denominator_cycles"] = 0
            group["view_unknown_cycles"]["unit_load"] = group["compute_cycles"]
    group["unit_denominator"] = "compute_cycles"
    group["unit_load_exclusive"] = False
    group["view_status"] = {key: ("unavailable" if group["view_unknown_cycles"][key] >= group["compute_cycles"] > 0
                                  else "partial" if group["view_unknown_cycles"][key] else "available") for key in REQUIRED_CODES}
    group["evidence"] = EVIDENCE
    group["representation"] = "aggregate"
    group["hardware_interval"] = False
    group["conservation"] = {"matrix_first_sum": sum(group["matrix_first"].values()),
                              "issue_sum": sum(group["issue"].values()), "cycles": C}
    return group


def dense_attribution(stage_modes, codes=None, hardware=None, model=None):
    """Compatibility entry point: model metadata selects hardware detail math."""
    from .hardware import split_dense
    model = load_model(model)
    cfg = model.get("dense", {})
    if cfg.get("backend") == "none":
        return {"status": "unavailable", "reason": "detail_not_configured", "stage": cfg.get("stage")}
    return split_dense(stage_modes, codes, hardware, cfg, model["wait_modes"] + model["idle_modes"])


def analyze_mfu_counts(counts, hardware=None):
    """MFU chain from a declared complete, matching-window model counts object."""
    hardware = load_hardware(hardware)
    if not isinstance(counts, dict):
        raise ValueError("MFU counts must be an object")
    if not isinstance(counts.get("frame"), dict):
        raise ValueError("MFU frame must be an object")
    if not isinstance(counts.get("phases", []), list) or any(not isinstance(p, dict) for p in counts.get("phases", [])):
        raise ValueError("MFU phases must be a list of objects")
    f, fr = hardware["facts"], counts.get("frame", {})
    thr, sec = (_number(fr.get(k), "MFU frame." + k, True) for k in ("threads", "seconds"))
    if thr > f["cores"]:
        raise ValueError("pool threads exceed single-node hardware core count")
    cc, nm, flop = (_number(fr.get(k), "MFU frame." + k, True) for k in ("compute_cycles", "mopa", "flop"))
    inst = None if fr.get("inst") is None else _number(fr["inst"], "MFU frame.inst")
    capacity = thr * sec * f["frequency_hz"]
    factors = {"capacity_cycles": capacity, "pool": thr / f["cores"], "in_compute": cc / capacity,
               "mat_busy": f["cycles_per_mopa"] * nm / cc, "eff_mopa": flop / (nm * f["flop_per_mopa"]),
               "ipc": inst / cc if inst is not None else None, "inst_per_mopa": inst / nm if inst is not None else None}
    peak = f["cores"] * f["frequency_hz"] * f["flop_per_mopa"] / f["cycles_per_mopa"]
    factors["mfu"] = 100 * factors["pool"] * factors["in_compute"] * factors["mat_busy"] * factors["eff_mopa"]
    factors["mfu_direct"] = 100 * flop / (peak * sec)
    levels = [100, 100 * factors["pool"]]
    levels.append(levels[-1] * factors["in_compute"])
    levels.append(levels[-1] * factors["mat_busy"])
    levels.append(factors["mfu"])
    labels = ["全部节点", "池内核", "计算中", "矩阵忙", "有效模型 FLOP"]
    waterfall = [{"label": label, "value": value, "delta": value - levels[i - 1] if i else 0}
                 for i, (label, value) in enumerate(zip(labels, levels))]
    phases = []
    for p in counts.get("phases", []):
        C, comp, pm = (_number(p.get(k), "MFU phase." + k) for k in ("cycles", "compute_cycles", "mopa"))
        pi = None if p.get("inst") is None else _number(p["inst"], "MFU phase.inst")
        pf = None if p.get("flop") is None else _number(p["flop"], "MFU phase.flop")
        phases.append({"name": p.get("name"), "parent": p.get("parent"), "cycles": C, "share": C / capacity,
                       "compute_share": comp / capacity, "inst_per_mopa": pi / pm if pi is not None and pm else None,
                       "ipc": pi / comp if pi is not None and comp else None,
                       "mat_busy": f["cycles_per_mopa"] * pm / comp if comp else None,
                       "mat_pts": 100 * factors["pool"] * f["cycles_per_mopa"] * pm / capacity,
                       "eff_mopa": pf / (pm * f["flop_per_mopa"]) if pf is not None and pm else None,
                       "mfu_pts": 100 * pf / (peak * sec) if pf is not None else None})
    top = [p for p in counts.get("phases", []) if not p.get("parent")]
    warnings = []
    if factors["in_compute"] > 1 + 1e-6 or factors["mat_busy"] > 1 + 1e-6:
        warnings.append("MFU factor exceeds physical capacity; inspect configuration and matching-window evidence")
    return {"status": "available", "evidence": "model_calibrated:mfu_chain_v1", "representation": "aggregate",
            "hardware_interval": False, "coverage": "declared_complete_aggregation", "label": counts.get("label"),
            "source": counts.get("source"), "frame": factors, "waterfall": waterfall, "phases": phases,
            "phases_cover": sum(p["compute_cycles"] for p in top) / cc if top else None,
            "input_counts": copy.deepcopy(counts), "hardware_profile": hardware["profile_id"], "warnings": warnings}


def _sample_mfu(aligned, total, hardware, model, model_flops):
    f = hardware["facts"]
    entries = aligned.get("entries", [])
    observed = len({str(e.get("thread_id")) for e in entries})
    by_die = defaultdict(set)
    for entry in entries:
        by_die[str(entry.get("die"))].add(str(entry.get("thread_id")))
    freq = aligned.get("freq", aligned.get("counter_frequency_hz"))
    windows = aligned.get("pool_windows", {})
    window_seconds = 0.0
    known_window = bool(observed and freq and all(die in windows for die in by_die))
    if known_window:
        for die, threads in by_die.items():
            window = windows[die]
            window_seconds += len(threads) * (window["end_ticks"] - window["begin_ticks"]) / freq
        seconds = window_seconds / observed
    else:
        seconds = total["cycles"] / observed / f["frequency_hz"] if observed else 0
    capacity = observed * seconds * f["frequency_hz"]
    cc = total["compute_cycles"]
    semantic_counts = canonical_counts(total['compute_counters'], hardware)
    nm, inst = semantic_counts.get("0x7201"), semantic_counts.get("0x0008")
    expected = aligned.get("expected_pool_threads", model.get("expected_pool_threads"))
    coverage = observed / expected if expected else None
    sample = {"threads": observed, "expected_threads": expected, "thread_coverage_ratio": coverage,
              "seconds": seconds, "pool": observed / f["cores"],
              "in_compute": cc / capacity if capacity else None,
              "capacity_cycles": capacity, "recorded_cycles": total["cycles"],
              "window_basis": "enclosing_tT_pool_windows" if known_window else "retained_pool_extent_average",
              "recorded_window_coverage_ratio": total["cycles"] / capacity if capacity else None,
              "mat_busy": f["cycles_per_mopa"] * nm / cc if nm is not None and cc else None,
              "ipc": inst / cc if inst is not None and cc else None,
              "inst_per_mopa": inst / nm if inst is not None and nm else None,
              "eff_mopa": None, "mfu": None, "scope": "observed_pool_threads_only"}
    if not aligned.get("full_pool_coverage"):
        return {"status": "unavailable", "reason": "incomplete_pool_coverage", "sample_scope": sample,
                "frame": sample, "waterfall": [], "warnings": ["截取线程只用于局部归因，不计算全机 MFU。"]}
    if model_flops is None:
        return {"status": "unavailable", "reason": "matching_model_flop_not_supplied", "sample_scope": sample,
                "frame": sample, "waterfall": [], "warnings": ["缺少同一完整窗口的模型 FLOP 分子。"]}
    if not known_window or abs(total["cycles"] - capacity) > 1e-9 * max(capacity, 1):
        return {"status": "unavailable", "reason": "full_frame_time_coverage_not_proven", "sample_scope": sample,
                "frame": sample, "waterfall": [], "warnings": ["线程数覆盖不等于完整帧时间覆盖；不推算全机 MFU。"]}
    if total["view_unknown_cycles"]["matrix_first"]:
        return {"status": "unavailable", "reason": "required_counter_rates_missing", "sample_scope": sample,
                "frame": sample, "waterfall": [], "warnings": ["计算周期缺少归因计数，不推算全机 MFU。"]}
    return analyze_mfu_counts({"label": "Aligned full-pool timing window", "source": "aligned_timing_window",
                               "frame": {"threads": observed, "seconds": seconds, "compute_cycles": cc,
                                         "mopa": nm, "inst": inst, "flop": model_flops}}, hardware)


def analyze_attribution(aligned, hardware=None, model=None, model_flops=None, mfu_counts=None):
    """Analyze block/mode aggregates, retaining tT software boundaries separately."""
    hardware, model = load_hardware(hardware), load_model(model)
    if mfu_counts is not None:
        raise ValueError("complete MFU counts must be analyzed separately with analyze_mfu_counts; they cannot override an unrelated aligned trace")
    required_views = required_codes(hardware)
    total, dies, stages, stage_modes = _empty(), {}, {}, {}
    entries, warnings = [], list(aligned.get("warnings", []))
    waits, exchanges, idles = set(model["wait_modes"]), set(model["exchange_modes"]), set(model["idle_modes"])
    known_modes = waits | exchanges | idles | set(model["compute_modes"])
    known_codes = {c for codes in aligned.get("codes", {}).values() for c in codes}
    missing = sorted({event_code(hardware, code) for code in ("0x7201", "0x7005", "0x7007", "0x7008")} - known_codes)
    if missing:
        warnings.append("Attribution is incomplete: missing counters " + ", ".join(missing))
    for raw_entry in aligned.get("entries", []):
        e = copy.deepcopy(raw_entry)
        C, counters = _bin(e)
        e["cycles"], e["codes"] = C, counters
        e["matrix_first"] = {k: 0.0 for k in MATRIX_CLASSES}
        e["issue"] = {k: 0.0 for k in ISSUE_CLASSES + ("comm_x", "wait", "idle", "unknown")}
        e["bounds"] = {k: 0.0 for k in ("ov", "ov_lo", "ov_hi", "mem_lo", "mem_hi")}
        e["compute_cycles"], e["unit_denominator_cycles"] = 0.0, 0.0
        e["unit_load"] = {k: 0.0 for k in UNITS}
        e["view_missing_codes"] = {k: [] for k in REQUIRED_CODES}
        e["view_unknown_cycles"] = {k: 0.0 for k in REQUIRED_CODES}
        mode, stage = e.get("mode"), e.get("stage")
        if mode not in known_modes:
            raise ValueError("unclassified production mode: %r" % mode)
        category = None
        if mode in idles:
            category = "idle"
        elif mode in waits:
            category = "wait"
        elif mode in exchanges or stage in model["exchange_stages"]:
            category = "comm_x"
        if category:
            e["matrix_first"][category] = C
            e["issue"][category] = C
        else:
            e["compute_cycles"] = C
            for view, required in required_views.items():
                e["view_missing_codes"][view] = sorted(set(required) - counters.keys())
                if e["view_missing_codes"][view]:
                    e["view_unknown_cycles"][view] = C
            if e["view_missing_codes"]["matrix_first"]:
                e["matrix_first"]["unknown"] = C
            else:
                mf, bounds = split_compute(e, hardware)
                e["matrix_first"].update(mf)
                e["bounds"].update({k: bounds[k] for k in e["bounds"]})
            if e["view_missing_codes"]["issue"]:
                e["issue"]["unknown"] = C
            else:
                issue, clips = split_issue(e, hardware)
                e["issue"].update(issue)
                e["issue_clipping"] = clips
            if not e["view_missing_codes"]["unit_load"]:
                try:
                    e["unit_load"] = unit_load(e, hardware)
                    e["unit_denominator_cycles"] = C
                except ValueError as error:
                    warnings.append("%s: %s" % (e.get("entry_id"), error))
                    e["view_unknown_cycles"]["unit_load"] = C
                    e["unit_status"] = "invalid_counter_relation"
            if e["view_unknown_cycles"]["unit_load"]:
                e["unit_load"] = {k: None for k in UNITS}
                e["unit_denominator_cycles"] = 0
        e.update(evidence=EVIDENCE, representation="aggregate", hardware_interval=False)
        entries.append(e)
        for group in (total, dies.setdefault(str(e.get("die")), _empty()),
                      stages.setdefault(stage, _empty()), stage_modes.setdefault(stage + "|" + mode, _empty())):
            _add(group, e)
    total = _finish(total, hardware)
    for grouping in (dies, stages, stage_modes):
        for key in grouping:
            grouping[key] = _finish(grouping[key], hardware)
    by_id = {e["entry_id"]: e for e in entries}
    segments = []
    for raw_segment in aligned.get("segments", []):
        s = copy.deepcopy(raw_segment)
        e = by_id.get(s.get("entry_id"))
        if e is None:
            continue
        span = s.get("ticks", s.get("end_ticks", 0) - s.get("begin_ticks", 0))
        share = span / e["ticks"] if e.get("ticks") else 0
        s["cycles"] = e["cycles"] * share
        s["compute_cycles"] = e["compute_cycles"] * share
        s["unit_denominator_cycles"] = e["unit_denominator_cycles"] * share
        s["view_unknown_cycles"] = {k: v * share for k, v in e["view_unknown_cycles"].items()}
        s["view_missing_codes"] = copy.deepcopy(e["view_missing_codes"])
        for field in ("matrix_first", "issue", "bounds"):
            s[field] = {k: v * share for k, v in e[field].items()}
        s["unit_load"] = copy.deepcopy(e["unit_load"])
        s.update(die=e.get("die"), stage=e.get("stage"), mode=e.get("mode"),
                 evidence=EVIDENCE, representation="aggregate", hardware_interval=False,
                 allocation_basis="uniform_rate_within_block_mode")
        segments.append(s)
    dense = dense_attribution(aligned.get("stage_modes", {}), aligned.get("codes", {}), hardware, model)
    dense_stage = model.get("dense", {}).get("stage")
    missing_dense_entries = [e for e in entries if e.get("stage") == dense_stage and e.get("compute_cycles")
                             and e["view_missing_codes"]["matrix_first"]]
    if missing_dense_entries:
        dense = {"status": "unavailable", "reason": "dense_block_counter_rates_missing", "stage": dense_stage,
                 "missing_codes": sorted({code for e in missing_dense_entries for code in e["view_missing_codes"]["matrix_first"]}),
                 "unknown_cycles": sum(e["cycles"] for e in missing_dense_entries),
                 "warnings": ["Dense attribution omitted: some required block rates are unavailable, not measured zero."]}
    mfu = _sample_mfu(aligned, total, hardware, model, model_flops)
    return {"schema_version": "lxtrace.production-analysis.v1", "analysis_rules_version": MODEL_VERSION,
            "evidence": EVIDENCE, "representation": "aggregate", "hardware_interval": False,
            "hardware": hardware, "model": model, "presentation": presentation(model, hardware),
            "totals": total, "dies": dies, "stages": stages,
            "stage_modes": stage_modes, "entries": entries, "segments": segments, "dense": dense, "mfu": mfu,
            "drivers": copy.deepcopy(aligned.get("drivers", [])), "windows": copy.deepcopy(aligned.get("windows", {})),
            "alignment": copy.deepcopy({k: v for k, v in aligned.items() if k not in ("entries", "segments", "drivers")}),
            "warnings": list(dict.fromkeys(warnings + dense.get("warnings", []) + mfu.get("warnings", []))),
            "notes": ["Software tT boundaries are measured; derived class amounts are calibrated aggregates.",
                      "Timeline class placement assumes a uniform rate within each matched block/mode.",
                      "Matrix-first and issuer cycle partitions are exclusive; unit utilization may overlap.",
                      "No derived class is a verified hardware activity interval."]}
