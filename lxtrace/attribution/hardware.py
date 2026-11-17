"""Hardware facts, event meanings and calibrated decomposition algorithms.

No model phase, site, mode or layer rule belongs here. Profiles select a known
algorithm and map semantic event roles to RAW encodings. The compatibility
canonical codes below name the original LX2 event semantics, not fixed input
columns. Count normalization never mutates the preserved input readings.
"""
import copy
import json
import math
from pathlib import Path

PROFILES = Path(__file__).with_name("profiles")
EVIDENCE = "model_calibrated:profile_d_v1"
ALGORITHM = "lx2-sme-v1"
DENSE_CLASSES = ("mat", "work", "far", "fe", "wait")
EVENT_ROLES = {
    "matrix_instruction": "0x7201", "load_stall": "0x7005",
    "load_l1_stall": "0x7007", "load_l2_stall": "0x7008",
    "l2_refill": "0x0017", "sibling_refill": "0x6055",
    "remote_refill": "0x6057", "branch_mispredict": "0x0010",
    "l1i_refill": "0x0001", "l2i_refill": "0x0028",
    "vector_instruction": "0x8006", "vector_load_store": "0x8084",
    "l1d_refill": "0x0003", "instruction": "0x0008",
}
REQUIRED_CODES = {
    "matrix_first": ("0x7201", "0x7005", "0x7008", "0x0017", "0x6055", "0x6057", "0x0010", "0x0001", "0x0028"),
    "issue": ("0x7201", "0x7005", "0x7007", "0x7008", "0x0017", "0x6055", "0x6057", "0x0010", "0x0001", "0x0028"),
    "unit_load": ("0x7201", "0x8006", "0x8084", "0x0003", "0x0017", "0x0008"),
}


def _number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("%s must be a finite number" % label)
    if value < 0 or positive and value <= 0:
        raise ValueError("%s must be %s" % (label, "positive" if positive else "nonnegative"))
    return float(value)


def _load_profile(value, default, schema):
    if value is None or value == default:
        value = PROFILES / (default + ".json")
    if isinstance(value, dict):
        out = copy.deepcopy(value)
    else:
        path = Path(value)
        out = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(out, dict):
        raise ValueError("hardware profile must be a JSON object")
    if out.get("schema_version") != schema:
        raise ValueError("unsupported profile schema: %s" % out.get("schema_version"))
    if not isinstance(out.get("profile_id"), str) or not out["profile_id"]:
        raise ValueError("profile_id is required")
    return out


def _code(value):
    if isinstance(value, str) and value.startswith(("0x", "0X")):
        try:
            number = int(value, 16)
        except ValueError as error:
            raise ValueError("hardware event code must be hexadecimal") from error
    elif isinstance(value, int) and not isinstance(value, bool):
        number = value
    else:
        raise ValueError("hardware event code must be an integer or 0x hexadecimal string")
    if not 0 <= number <= 0xffff:
        raise ValueError("hardware event code must fit 16 bits")
    return "0x%04x" % number


def _validate_events(hardware):
    """Only historical profiles with no dictionary get the original mapping."""
    if "events" not in hardware:
        hardware["events"] = {role: {"code": code} for role, code in EVENT_ROLES.items()}
        hardware["event_mapping_basis"] = "legacy_lx2_semantics"
        return
    events = hardware["events"]
    if not isinstance(events, dict) or set(events) != set(EVENT_ROLES):
        raise ValueError("hardware events must declare exactly the supported semantic roles")
    used = set()
    for role, item in events.items():
        if not isinstance(item, dict) or "code" not in item:
            raise ValueError("hardware event %s requires an object with code" % role)
        code = _code(item["code"])
        if code in used:
            raise ValueError("hardware semantic event roles must have distinct RAW encodings")
        item["code"] = code
        used.add(code)
    hardware.setdefault("event_mapping_basis", "explicit_hardware_dictionary")


def event_code(hardware, canonical_code):
    """Return the selected RAW code for a role or compatibility semantic code.

    The caller can pass a profile already returned by load_hardware. Arbitrary
    event encodings outside the supported algorithm's roles are never guessed.
    """
    hardware = load_hardware(hardware)
    role = canonical_code if canonical_code in EVENT_ROLES else next(
        (name for name, code in EVENT_ROLES.items() if code == canonical_code), None)
    if role is None:
        raise ValueError("unknown hardware event semantic role: %s" % canonical_code)
    return hardware["events"][role]["code"]


def required_codes(hardware=None):
    """Required input encodings for each available decomposition view."""
    hardware = load_hardware(hardware)
    codes = {canonical: hardware["events"][role]["code"] for role, canonical in EVENT_ROLES.items()}
    return {view: tuple(codes[code] for code in required) for view, required in REQUIRED_CODES.items()}


def canonical_counts(counters, hardware=None):
    """Return a non-mutating semantic view; keep original RAW data separately.

    Unknown and missing counters are not manufactured as measured zero. The
    output uses the original LX2 codes as stable internal semantic keys; actual
    encodings may be entirely different, including a permutation of those keys.
    """
    hardware = load_hardware(hardware)
    return {canonical: counters[hardware["events"][role]["code"]]
            for role, canonical in EVENT_ROLES.items()
            if hardware["events"][role]["code"] in counters}


def load_hardware(value=None):
    out = _load_profile(value, "lx2-profiled-v1", "lxtrace.attribution-hardware.v1")
    for key in ("cores", "frequency_hz", "cycles_per_mopa", "flop_per_mopa", "cache_line_bytes",
                "sve_per_cycle", "sve_load_per_cycle", "l2_bytes_per_cycle", "beyond_l2_bytes_per_cycle", "ipc_peak"):
        _number(out.get("facts", {}).get(key), "hardware.facts." + key, True)
    algorithm = out.get("decomposition", ALGORITHM)
    if algorithm != ALGORITHM:
        raise ValueError("unsupported hardware decomposition algorithm: %s" % algorithm)
    out["decomposition"] = algorithm
    cal = out.get("calibration", {})
    for key in ("rho12", "rho2", "branch_penalty", "l1i_penalty", "l2i_penalty", "snoop_latency",
                "cross_package_latency", "local_fill_latency", "mopa_per_pass"):
        _number(cal.get(key), "hardware.calibration." + key, key not in ("rho12", "rho2"))
    for key in ("rho12", "rho2"):
        if cal[key] > 1:
            raise ValueError("hardware calibration %s must not exceed one" % key)
    for key in ("za_input", "za_output", "za_output_last"):
        pair = cal.get(key)
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("hardware calibration %s needs lower and upper values" % key)
        lo, hi = (_number(x, key) for x in pair)
        if lo > hi:
            raise ValueError("hardware calibration bounds are reversed: " + key)
    for key in ("0x0001", "0x0028"):
        _number(cal.get("dense_fe_refill_rates", {}).get(key), "dense_fe_refill_rates." + key)
    _validate_events(out)
    return out


def _bin(entry):
    if not isinstance(entry, dict):
        raise ValueError("hardware aggregate must be an object")
    cycles = entry.get("cycles", 0)
    if isinstance(cycles, list):
        cycles = sum(cycles) / len(cycles) if cycles else 0
    cycles = _number(cycles, "cycles")
    values = entry.get("counters", entry.get("codes", entry))
    if not isinstance(values, dict):
        raise ValueError("hardware counters must be an object")
    counters = {k: _number(v, k) for k, v in values.items() if isinstance(k, str) and k.startswith("0x")}
    return cycles, counters


def _source_shares(counters, cal):
    snoop, remote = counters.get("0x6055", 0), counters.get("0x6057", 0)
    local = max(0, counters.get("0x0017", 0) - snoop - remote)
    weights = (snoop * cal["snoop_latency"], remote * cal["cross_package_latency"], local * cal["local_fill_latency"])
    total = sum(weights)
    return (weights[0] / total, weights[1] / total) if total else (0.0, 0.0)


def _front_end(counters, cal):
    return (counters.get("0x0010", 0) * cal["branch_penalty"]
            + counters.get("0x0001", 0) * cal["l1i_penalty"]
            + counters.get("0x0028", 0) * cal["l2i_penalty"])


def split_compute(entry, hardware=None, rho=None):
    """Matrix-first cycle partition and mathematical overlap/exposed-stall bounds.

    The bounds constrain aggregate amounts, never the absolute position of an
    internal stall. Their source counters are cross-run estimates when carried.
    """
    hardware = load_hardware(hardware)
    cal, facts = hardware["calibration"], hardware["facts"]
    C, raw = _bin(entry)
    e = canonical_counts(raw, hardware)
    r12, r2 = rho or (cal["rho12"], cal["rho2"])
    M = min(C, facts["cycles_per_mopa"] * e.get("0x7201", 0))
    N = C - M
    S = min(C, e.get("0x7005", 0))
    S2 = min(S, e.get("0x7008", 0))
    ov_lo, ov_hi = max(0, M + S - C), min(M, S)
    ov = min(max(min(S - S2, r12 * M) + min(S2, r2 * M), ov_lo), ov_hi)
    exposed = max(0, min(S - ov, N))
    beyond = min(exposed, max(0, S2 - min(S2, r2 * M)))
    sib, remote = _source_shares(e, cal)
    comm = beyond * (sib + remote)
    fe = min(N - exposed, _front_end(e, cal))
    classes = {"mat": M, "mem": exposed - comm, "comm_c": comm, "fe": fe, "vec": N - exposed - fe}
    bounds = {"ov": ov, "ov_lo": ov_lo, "ov_hi": ov_hi, "S": S, "N": N, "M": M,
              "mem_lo": max(0, min(S - ov_hi, N)), "mem_hi": max(0, min(S - ov_lo, N)),
              "bound_semantics": "aggregate_amount_bounds_on_carried_counters",
              "evidence": EVIDENCE, "representation": "aggregate", "hardware_interval": False}
    return classes, bounds


def split_issue(entry, hardware=None):
    """Issuer cycles; no-load stalls and non-MOPA issue remain in other."""
    hardware = load_hardware(hardware)
    C, raw = _bin(entry)
    e = canonical_counts(raw, hardware)
    cal = hardware["calibration"]
    nm, S0 = e.get("0x7201", 0), e.get("0x7005", 0)
    mopa = min(C, nm)
    room = C - mopa
    S = min(S0, room)
    S1 = min(e.get("0x7007", 0), S)
    S2 = min(e.get("0x7008", 0), S1)
    sib, remote = _source_shares(e, cal)
    fe = min(room - S, _front_end(e, cal))
    return {"mopa": mopa, "ld_l1": S - S1, "ld_l2": S1 - S2, "ld_mem": S2 * (1 - sib - remote),
            "ld_sib": S2 * sib, "ld_xpkg": S2 * remote, "fe": fe, "other": room - S - fe}, {
                "clip_mopa": nm - mopa, "clip_ld": S0 - S,
                "other_includes": "non-MOPA issue and zero-issue cycles with no measured pending load"}


def unit_load(entry, hardware=None):
    """Independent unit work/peak ratios, deliberately neither exclusive nor capped."""
    hardware = load_hardware(hardware)
    C, raw = _bin(entry)
    e = canonical_counts(raw, hardware)
    f = hardware["facts"]
    sve, ldst = e.get("0x8006", 0), e.get("0x8084", 0)
    # Whole-count noise of a near-empty bin is tolerated, as in the reference.
    if ldst - sve > 1e-3 * C:
        raise ValueError("SVE load/store count exceeds SVE instruction count")
    per = lambda value, capacity: value / (capacity * C) if C else 0.0
    return {"mat": per(f["cycles_per_mopa"] * e.get("0x7201", 0), 1),
            "sve": per(max(0, sve - ldst), f["sve_per_cycle"]),
            "ldst": per(ldst, f["sve_load_per_cycle"]),
            "l2bw": per(f["cache_line_bytes"] * e.get("0x0003", 0), f["l2_bytes_per_cycle"]),
            "xbw": per(f["cache_line_bytes"] * e.get("0x0017", 0), f["beyond_l2_bytes_per_cycle"]),
            "ipc": per(e.get("0x0008", 0), f["ipc_peak"])}



def split_dense(stage_modes, codes=None, hardware=None, dense_config=None, wait_modes=()):
    """Dense step causes using the step's own raw/ccyc rate and ZA bounds."""
    hardware = load_hardware(hardware)
    cfg = dense_config or {}
    if not isinstance(cfg, dict):
        raise ValueError("hardware detail configuration must be an object")
    stage = cfg.get("stage")
    if stage is None:
        return {"status": "unavailable", "reason": "detail_not_configured"}
    if not isinstance(stage, str) or not stage:
        raise ValueError("hardware detail stage must be a nonempty string")
    matrix_steps = cfg.get("matrix_steps", {})
    if not isinstance(matrix_steps, dict) or any(
            not isinstance(mode, str) or isinstance(n, bool) or not isinstance(n, int) or n < 1
            for mode, n in matrix_steps.items()):
        raise ValueError("hardware detail matrix_steps requires positive integer chunk counts")
    steps_in = {key.split("|", 1)[1]: value for key, value in stage_modes.items() if key.startswith(stage + "|")}
    if not steps_in:
        return {"status": "unavailable", "reason": "dense_stage_not_recorded", "stage": stage}
    cal, facts = hardware["calibration"], hardware["facts"]
    run_of = {}
    for run, events in (codes or {}).items():
        for code in events:
            run_of.setdefault(code, run)
    wait_steps = set(wait_modes)
    total = {k: 0.0 for k in DENSE_CLASSES}
    steps, warnings = {}, []
    counted = set(run_of)
    required = set(required_codes(hardware)["matrix_first"])
    if not required <= counted:
        return {"status": "unavailable", "reason": "dense_required_counters_missing", "stage": stage,
                "missing_codes": sorted(required - counted)}
    missing_by_step = {mode: sorted(required - values.keys()) for mode, values in steps_in.items()
                       if mode not in wait_steps and required - values.keys()}
    if missing_by_step:
        return {"status": "unavailable", "reason": "dense_step_counter_rates_missing", "stage": stage,
                "missing_by_step": missing_by_step,
                "missing_codes": sorted(set().union(*map(set, missing_by_step.values())))}
    for mode, v in sorted(steps_in.items()):
        C = _number(v["cycles"], "dense step cycles")
        det = {k: 0.0 for k in DENSE_CLASSES}
        s = {"cycles": C, "ticks": v.get("ticks", 0), "det": det}
        if mode in wait_steps:
            det["wait"] = C
            s.update(far_lo=0, far_hi=0, far_comm=0, far_parts={"l2": 0, "local": 0, "comm": 0})
        else:
            raw = {code: _number(v.get("raw_" + code, v.get(code, 0)), "dense raw " + code) for code in counted}
            adjusted = {code: _number(v.get(code, 0), "dense carried " + code) for code in counted}
            for code, run in run_of.items():
                ccyc = v.get("ccyc_" + run, 0)
                if "raw_" + code in v and ccyc > 0:
                    adjusted[code] = raw[code] * C / ccyc
            cl, info = split_compute({"cycles": C, "counters": adjusted}, hardware)
            source_run = run_of.get(event_code(hardware, "0x7005"))
            delta = float(v.get("ccyc_" + source_run, C)) - C if source_run else 0.0
            nm = raw.get(event_code(hardware, "0x7201"), 0)
            inst = raw.get(event_code(hardware, "0x0008"))
            s.update(delta=delta, k=inst / nm if inst is not None and nm else None,
                     ipc=inst / C if inst is not None and C else None,
                     mat_busy=facts["cycles_per_mopa"] * nm / C if C else 0,
                     rates={k: x / C if C else 0 for k, x in adjusted.items()}, counts=adjusted,
                     raw_counts=raw, central=cl, rho_central=info["ov"] / info["M"] if info["M"] else None)
            Sa, Sb = raw.get(event_code(hardware, "0x7005"), 0), raw.get(event_code(hardware, "0x7005"), 0) - delta
            stall_cap = max(Sa, Sb)
            fe_hi = cl["fe"]
            rates = cal["dense_fe_refill_rates"]
            fe = min(fe_hi, adjusted.get(event_code(hardware, "0x0010"), 0) * cal["branch_penalty"]
                     + C * (rates["0x0001"] * cal["l1i_penalty"] + rates["0x0028"] * cal["l2i_penalty"]))
            s["fe_hi"] = fe_hi
            if mode in matrix_steps:
                M = min(C, facts["cycles_per_mopa"] * nm)
                if facts["cycles_per_mopa"] * nm > C:
                    warnings.append("%s raw MOPA busy exceeds tT cycles; clipped to physical capacity" % mode)
                N, n, passes = C - M, matrix_steps[mode], nm / cal["mopa_per_pass"]
                zl = passes * ((1 - 1 / n) * cal["za_input"][0] + (1 - 1 / n) * cal["za_output"][0] + cal["za_output_last"][0] / n)
                zh = passes * ((1 - 1 / n) * cal["za_input"][1] + (1 - 1 / n) * cal["za_output"][1] + cal["za_output_last"][1] / n)
                zm = (zl + zh) / 2
                fe = min(fe, N)
                clip = lambda value: min(max(value, 0), N, stall_cap)
                far = clip(N - zm - fe)
                s.update(za={"lo": zl, "mid": zm, "hi": zh, "passes": passes},
                         far_lo=clip(N - zh - fe_hi), far_hi=clip(N - zl), clipped=far != N - zm - fe,
                         rho_eff=(info["S"] - far) / M if M else None)
                det.update(mat=M, fe=fe, far=far, work=N - fe - far)
            else:
                far = cl["mem"] + cl["comm_c"]
                det.update(mat=cl["mat"], fe=fe, far=far, work=C - cl["mat"] - far - fe)
                s.update(far_lo=info["mem_lo"] if cl["mat"] else min(max(min(Sa, Sb), 0), C),
                         far_hi=info["mem_hi"] if cl["mat"] else min(max(stall_cap, 0), C))
            S, S2 = adjusted.get(event_code(hardware, "0x7005"), 0), adjusted.get(event_code(hardware, "0x7008"), 0)
            sib, remote = _source_shares(canonical_counts(adjusted, hardware), cal)
            fb = min(S2, S) / S if S else 0
            far = det["far"]
            s["far_parts"] = {"l2": far * (1 - fb), "local": far * fb * (1 - sib - remote), "comm": far * fb * (sib + remote)}
            s["far_comm"] = s["far_parts"]["comm"]
        s["pct"] = None
        s["evidence"], s["representation"], s["hardware_interval"] = "model_calibrated:dense_za_v1", "aggregate", False
        s["bound_semantics"] = "calibrated_ZA_and_counter_perturbation_range"
        steps[mode] = s
        for k in DENSE_CLASSES:
            total[k] += det[k]
    C = sum(s["cycles"] for s in steps.values())
    comp = [s for mode, s in steps.items() if mode not in wait_steps]
    combined = {code: sum(s.get("counts", {}).get(code, 0) for s in comp) for code in counted}
    cls, bounds = split_compute({"cycles": sum(s["cycles"] for s in comp), "counters": combined}, hardware)
    nm = sum(s.get("raw_counts", {}).get(event_code(hardware, "0x7201"), 0) for s in steps.values())
    for s in steps.values():
        s["pct"] = 100 * s["cycles"] / C if C else 0
        s["cycles_per_mopa"] = s["cycles"] / nm if nm else None
    return {"status": "available", "stage": stage, "cycles": C, "nck": dict(matrix_steps),
            "steps": steps, "total": total, "total_pct": {k: 100 * v / C if C else 0 for k, v in total.items()},
            "mopa_raw": nm, "merged": {"cycles": sum(s["cycles"] for s in comp),
                "central": cls["mem"] + cls["comm_c"], "lo": bounds["mem_lo"], "hi": bounds["mem_hi"], "classes": cls,
                "rho_central": bounds["ov"] / bounds["M"] if bounds["M"] else None},
            "check": {"steps_over_stage": 1 if C else None, "det_over_cycles": sum(total.values()) / C if C else None},
            "evidence": "model_calibrated:dense_za_v1", "representation": "aggregate", "hardware_interval": False,
            "warnings": warnings}
