"""Build the standard report, including audited same-run software merging.

Only software sessions may be combined. A shared host, boot and OS monotonic
clock are required; an explicit run_id is a grouping declaration, never clock
calibration. A single session of any supported evidence type can be reported.
"""
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath

from . import resource
from .importers import load_sdk


def _json(text):
    return json.loads(text.lstrip("\ufeff"))


def _dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _raw_name(name):
    name = str(name).replace("\\", "/")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(p in (".", "..") for p in path.parts) or ":" in name:
        raise ValueError("unsafe raw evidence path: " + name)
    return name if name.startswith("raw/") else "raw/" + name


def _content(value):
    if isinstance(value, dict) and set(value) == {"content"}:
        value = value["content"]
    if not isinstance(value, str):
        raise ValueError("raw evidence must contain UTF-8 text")
    return value


def _kind(path):
    if path.is_dir():
        return 0 if (path / "manifest.json").is_file() else None
    if not path.is_file() or path.suffix.lower() not in (".json", ".jsonl", ".lxtrace"):
        return None
    try:
        with path.open(encoding="utf-8-sig") as stream:
            first = stream.readline()
            if path.suffix.lower() == ".jsonl":
                value = _json(first)
            else:
                value = _json(first + stream.read())
        schema = value.get("schema_version") if isinstance(value, dict) else None
        return {resource.SCHEMA: 1, "lxtrace.session.v1": 2, "lxtrace.sdk.v1": 3}.get(schema)
    except (ValueError, OSError):
        return None


def _paths(paths):
    values = [paths] if isinstance(paths, (str, os.PathLike)) else list(paths)
    if not values:
        raise ValueError("report requires at least one session input")
    found = []
    for value in values:
        path = Path(value).resolve()
        if not path.exists():
            raise ValueError("session input does not exist: " + str(path))
        kind = _kind(path)
        if kind is not None:
            found.append((path, False, kind))
        elif path.is_dir():
            children = [(p.resolve(), True, _kind(p)) for p in path.iterdir()]
            children = [item for item in children if item[2] is not None]
            if not children:
                raise ValueError("input directory contains no direct child sessions: " + str(path))
            found.extend(sorted(children, key=lambda item: (item[2], str(item[0]))))
        else:
            raise ValueError("unsupported session input: " + str(path))
    return found


def _digests(value):
    """Collect declared evidence hashes, including those inside old migrations."""
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("sha256", "evidence_sha256") and isinstance(child, str) and len(child) == 64:
                found.add(child)
            elif key in ("files", "source_hashes", "raw_file_sha256") and isinstance(child, dict):
                found.update(v for v in child.values() if isinstance(v, str) and len(v) == 64)
            elif key not in ("raw_files",):
                found.update(_digests(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_digests(child))
    return found


def _load(path, kind):
    legacy = None
    exact = None
    if kind == 0:
        session = resource.load_session(path)
        exact = (path / "manifest.json").read_bytes()
    else:
        exact = path.read_bytes()
        if kind == 3:
            meta = next((_json(line) for line in exact.decode("utf-8-sig").splitlines()
                         if line.strip() and _json(line).get("type") == "meta"), None)
            if not meta:
                raise ValueError("raw SDK input requires a supported SDK metadata record")
            legacy = load_sdk(path)
            legacy.update({k: meta[k] for k in ("host_id", "boot_id", "run_id") if meta.get(k)})
            legacy["raw_files"] = {"capture/sdk.jsonl": exact.decode("utf-8")}
            session = resource.migrate_v1(legacy)
        else:
            value = _json(exact.decode("utf-8"))
            if kind == 2:
                legacy = value
                session = resource.migrate_v1(value)
            else:
                session = resource.validate_session(value)
    raw = {_raw_name(k): _content(v) for k, v in session.get("raw_files", {}).items()}
    if legacy is not None:
        raw.update({_raw_name(k): _content(v) for k, v in legacy.get("raw_files", {}).items()})
    if legacy is None:
        for name, text in raw.items():
            if name.endswith("legacy/session-v1.json"):
                candidate = _json(text)
                if candidate.get("schema_version") == "lxtrace.session.v1":
                    legacy = candidate
                    break
    input_name = "manifest.json" if kind == 0 else ("sdk.jsonl" if kind == 3 else "session.json")
    raw["raw/input/" + input_name] = exact.decode("utf-8")
    for key in ("host_id", "boot_id", "run_id", "job_id"):
        value = (legacy or {}).get(key)
        if key in ("host_id", "boot_id") and session.get(key) and value and session[key] != value:
            raise ValueError("session metadata conflicts with its original " + key)
        if not session.get(key) and value:
            session[key] = value
    evidence = _digests(session.get("provenance", [])) | _digests((legacy or {}).get("provenance", []))
    # Original SDK and migration provenance identify v1/v2/raw aliases. Capture
    # metadata files shared by ranks are deliberately not used as identities.
    evidence.update(_sha(text.encode("utf-8")) for name, text in raw.items()
                    if name.endswith(".jsonl") and "/input/" not in name)
    session["raw_files"] = raw
    return dict(path=path, session=session, legacy=legacy, sha256=_sha(exact),
                evidence=evidence, raw_hashes={name: _sha(text.encode("utf-8")) for name, text in raw.items()})


def _clock(item):
    session, legacy = item["session"], item["legacy"]
    descriptors = []
    for clock in session["clocks"]:
        original = clock.get("original_clock", clock)
        native = clock.get("native_clock_id", original.get("native_clock_id", original.get("clock_id", original.get("id"))))
        if native != "CLOCK_MONOTONIC":
            raise ValueError("multi-input report requires known CLOCK_MONOTONIC clocks")
        origin = clock.get("origin_ns", original.get("origin_ns"))
        if not isinstance(origin, int) or isinstance(origin, bool) or origin < 0:
            raise ValueError("multi-input report requires an absolute monotonic origin_ns")
        transform = clock.get("transform")
        if transform and (transform.get("scale", 1) != 1 or transform.get("offset_ns", -origin) != -origin):
            raise ValueError("unsupported cross-source time transform")
        descriptors.append((clock, original, origin))
    if not descriptors and legacy:
        original = legacy.get("clock", {})
        descriptors = [(dict(clock_id="legacy", original_clock=original), original, original.get("origin_ns"))]
    if not descriptors or len({value[2] for value in descriptors}) != 1:
        raise ValueError("sources must share one normalized monotonic origin per session")
    return descriptors[0][2], descriptors


def _record_keys(item):
    """Detect repeated observations even if a wrapper changes its session ID."""
    try:
        origin, _ = _clock(item)
    except ValueError:
        origin = 0
    result = set()
    for r in item["session"]["records"]:
        begin = r.get("timestamp_ns", r.get("begin_ns"))
        end = r.get("end_ns")
        result.add((r["record_id"], r.get("thread_instance_id"),
                    None if begin is None else origin + begin,
                    None if end is None else origin + end, r.get("category"), r.get("name")))
    return result


def _inputs(paths):
    selected = []
    for path, discovered, kind in _paths(paths):
        if any(path == item["path"] for item in selected):
            raise ValueError("duplicate session input: " + str(path))
        if discovered and kind == 3 and any(_sha(path.read_bytes()) in item["evidence"] for item in selected):
            continue
        item = _load(path, kind)
        item["record_keys"] = _record_keys(item)
        duplicate = next((old for old in selected if item["sha256"] == old["sha256"]
                          or item["evidence"] and item["evidence"] == old["evidence"]
                          or item["record_keys"] and item["record_keys"] == old["record_keys"]), None)
        if duplicate:
            if discovered:
                continue
            raise ValueError("duplicate capture/session inputs: " + str(path))
        if any(item["session"]["session_id"] == old["session"]["session_id"] for old in selected):
            raise ValueError("conflicting session_id reused by different session inputs")
        if any(item["evidence"] & old["evidence"] or item["record_keys"] & old["record_keys"] for old in selected):
            raise ValueError("overlapping evidence or repeated capture; choose non-overlapping session inputs")
        selected.append(item)
    return selected


def _namespace(value, prefix):
    return prefix + str(value) if value is not None else None


def _references(value, prefix, resources, sources, clocks):
    """Map declared internal references, retaining PID/TID and user correlations."""
    if isinstance(value, list):
        return [_references(v, prefix, resources, sources, clocks) for v in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = {}
    identifiers = {"record_id", "event_id", "parent_id", "frame_id", "request_id",
                   "thread_id", "thread_instance_id", "application_frame_id",
                   "parent_application_frame_id", "target_event_id"}
    for key, child in value.items():
        if key in identifiers and child is not None and isinstance(child, (str, int)):
            result[key] = _namespace(child, prefix)
        elif key in ("resource_id", "target_id") and isinstance(child, str):
            result[key] = resources.get(child, prefix + child)
        elif key == "source_id" and isinstance(child, str):
            result[key] = sources.get(child, prefix + child)
        elif key == "clock_id" and isinstance(child, str):
            result[key] = clocks.get(child, prefix + child)
        elif key == "correlation_ids":
            # Correlation labels are application data and can intentionally be
            # shared between processes. Map only labels naming an actual ID.
            result[key] = _references(child, prefix, resources, sources, clocks)
        else:
            result[key] = _references(child, prefix, resources, sources, clocks)
    return result


def merge_sessions(paths, run_id=None):
    """Return a v2 session from one input or compatible same-run software inputs.

    A directory discovers immediate session children, preferring v2 over v1/raw
    aliases. Explicit duplicates and partially overlapping captures are errors.
    An explicit run_id permits grouping legacy per-process run IDs; provenance
    records that declaration. It never permits different hosts, boots or clocks.
    """
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError("run_id must be a nonempty string")
    items = _inputs(paths)
    if len(items) == 1:
        result = copy.deepcopy(items[0]["session"])
        result.pop("archive", None)
        if run_id is not None:
            result["provenance"].append(dict(kind="user_run_group", original_run_id=result["run_id"], run_id=run_id))
            result["run_id"] = run_id
        return resource.validate_session(result)
    sessions = [item["session"] for item in items]
    if any(r["layer"] != "software" for s in sessions for r in s["records"]) or any(
            source.get("source_layer", source.get("layer")) != "software" for s in sessions for source in s["sources"]):
        raise ValueError("multi-input reports currently support software sources only; separate proc/PMU runs cannot be merged")
    for key in ("host_id", "boot_id"):
        if any(not s.get(key) for s in sessions) or len({s[key] for s in sessions}) != 1:
            raise ValueError("multi-input report requires the same known " + key)
    original_runs = [s.get("run_id") for s in sessions]
    if run_id is None and (any(not value for value in original_runs) or len(set(original_runs)) != 1):
        raise ValueError("different run_id values require an explicit run_id grouping declaration")
    group = run_id or original_runs[0]
    clocks = [_clock(item) for item in items]
    origin = min(value[0] for value in clocks)
    identity = _sha(_dump(dict(inputs=[i["sha256"] for i in items], run_id=group)).encode("utf-8"))[:24]
    result = dict(schema_version=resource.SCHEMA, session_id="unified-" + identity, run_id=group,
                  data_origin="imported", name="Unified software timeline", host_id=sessions[0]["host_id"],
                  boot_id=sessions[0]["boot_id"], resources=[], sources=[], clocks=[], records=[],
                  allocation=dict(status="unverified", cpu_ids=[]), raw_files={}, capability_manifest=[],
                  provenance=[], app=dict(name="Unified software report"),
                  quality=dict(per_input={}, per_source={}, unknown_windows=[], warnings=[],
                               dropped_records=0, protocol_errors=0, incomplete_events=0,
                               clean_shutdown=True, source_continuity="unknown", clock_uncertainty_ns=None))
    result["resources"].append(dict(resource_id="unified:root", type="node", parent_id=None,
                                    name=sessions[0]["host_id"], mapping_source="same_host_and_boot"))
    provenance_inputs, bounds = [], []
    for index, (item, (source_origin, descriptors)) in enumerate(zip(items, clocks)):
        s, prefix = item["session"], "input%d:" % index
        shift = source_origin - origin
        rid = {r["resource_id"]: prefix + r["resource_id"] for r in s["resources"]}
        sid = {r["source_id"]: prefix + r["source_id"] for r in s["sources"]}
        cid = {c.get("clock_id", c.get("id")): prefix + c.get("clock_id", c.get("id")) for c, _, _ in descriptors}
        for old_clock, original_clock, _ in descriptors:
            clock = copy.deepcopy(old_clock)
            old_id = old_clock.get("clock_id", old_clock.get("id"))
            clock.update(clock_id=cid[old_id], native_clock_id="CLOCK_MONOTONIC", origin_ns=origin,
                         original_clock=copy.deepcopy(original_clock), source_origin_ns=source_origin,
                         absolute_to_relative_offset_ns=-origin,
                         relationship="same host and boot CLOCK_MONOTONIC; original error bounds retained")
            clock["error_ns"] = old_clock.get("error_ns", original_clock.get("calibration_error_ns"))
            clock.pop("transform", None)
            result["clocks"].append(clock)
        for old in s["resources"]:
            r = _references(old, prefix, rid, sid, cid)
            r.update(resource_id=rid[old["resource_id"]], parent_id=rid.get(old.get("parent_id"), "unified:root"))
            if "logical_cpu" in r:
                r["original_logical_cpu"] = r.pop("logical_cpu")
                r["original_type"], r["type"] = r["type"], "cpu_reference"
            if "links" in r:
                r["links"] = [rid.get(link, prefix + link) if isinstance(link, str) else link for link in r["links"]]
            result["resources"].append(r)
        for old in s["sources"]:
            source = _references(old, prefix, rid, sid, cid)
            if old.get("clock_id") not in cid:
                raise ValueError("software source references an unknown clock")
            source.update(source_id=sid[old["source_id"]], clock_id=cid[old["clock_id"]], original_source_id=old["source_id"])
            for field in ("last_reliable_ts", "begin_ns", "end_ns"):
                if source.get(field) is not None:
                    source[field] += shift
            result["sources"].append(source)
            result["quality"]["per_source"][source["source_id"]] = dict(quality=copy.deepcopy(s["quality"]), source=copy.deepcopy(old))
        raw_prefix = "raw/inputs/input%d/" % index
        for name, text in s.get("raw_files", {}).items():
            result["raw_files"][raw_prefix + _raw_name(name)[4:]] = _content(text)
        for old in s["records"]:
            r = _references(old, prefix, rid, sid, cid)
            r.update(record_id=prefix + old["record_id"], resource_id=rid[old["resource_id"]], source_id=sid[old["source_id"]])
            if old.get("clock_id") is not None:
                if old["clock_id"] not in cid:
                    raise ValueError("software record references an unknown clock")
                r["clock_id"] = cid[old["clock_id"]]
            for field in ("begin_ns", "end_ns", "timestamp_ns"):
                if r.get(field) is not None:
                    r[field] += shift
            r["raw_refs"] = [raw_prefix + _raw_name(ref.split("#", 1)[0])[4:] + ("#" + ref.split("#", 1)[1] if "#" in ref else "") for ref in r.get("raw_refs", [])]
            r["original_identity"] = dict(session_id=s["session_id"], record_id=old["record_id"],
                                           resource_id=old["resource_id"], source_id=old["source_id"],
                                           clock_origin_ns=source_origin, time_shift_ns=shift)
            result["records"].append(r)
        q = s["quality"]
        result["quality"]["per_input"][prefix[:-1]] = copy.deepcopy(q)
        for key in ("dropped_records", "protocol_errors", "incomplete_events"):
            result["quality"][key] += q.get(key, 0)
        result["quality"]["clean_shutdown"] &= q.get("clean_shutdown") is True
        result["quality"]["warnings"].extend(q.get("warnings", []))
        for old in q.get("unknown_windows", []):
            window = copy.deepcopy(old)
            for field in ("begin_ns", "end_ns"):
                window[field] += shift
            if window.get("source_id") in sid:
                window["source_id"] = sid[window["source_id"]]
            if window.get("resource_id") in rid:
                window["resource_id"] = rid[window["resource_id"]]
            result["quality"]["unknown_windows"].append(window)
        for capability in s["capability_manifest"]:
            result["capability_manifest"].append(dict(copy.deepcopy(capability), input_session_id=s["session_id"]))
        provenance_inputs.append(dict(namespace=prefix, session_id=s["session_id"], run_id=s["run_id"],
                                      path=str(item["path"]), input_sha256=item["sha256"],
                                      evidence_sha256=sorted(item["evidence"]), raw_file_sha256=item["raw_hashes"],
                                      origin_ns=source_origin, offset_ns=shift, provenance=copy.deepcopy(s["provenance"])))
        bounds.extend([s["time_range"][0] + shift, s["time_range"][1] + shift])
    result["time_range"] = [min(bounds), max(bounds)]
    result["records"].sort(key=lambda r: (r.get("timestamp_ns", r.get("begin_ns", 0)), r["record_id"]))
    result["provenance"] = [dict(kind="unified_software_report", run_id=group,
        grouping="explicit_user_run_id" if run_id is not None else "matching_run_id",
        common_origin_ns=origin, formula="normalized_ns = input_relative_ns + input_origin_ns - common_origin_ns",
        clock_relationship="same host and boot CLOCK_MONOTONIC identity; no error bound fabricated",
        inputs=provenance_inputs,
        limitations=["Software intervals only; affinity is not continuous CPU residency.",
                     "No hardware activity, cross-device calibration or cross-run proc/PMU merge."])]
    return resource.validate_session(result)


def write_unified_report(inputs, output, session_output=None, run_id=None):
    """Write the fixed web viewer and optionally a new JSON/native session.

    Inputs and existing outputs are never overwritten. The function does not
    choose an implicit archive destination.
    """
    output = Path(output).resolve()
    session_path = Path(session_output).resolve() if session_output is not None else None
    inputs = inputs if isinstance(inputs, (str, os.PathLike)) else list(inputs)
    input_paths = [item[0] for item in _paths(inputs)]
    for target in (output, session_path):
        if target is None:
            continue
        if any(target == source or source.is_dir() and source in target.parents or target in source.parents for source in input_paths):
            raise ValueError("report output must not overwrite or contain input evidence")
    if output.exists():
        raise ValueError("report output already exists; choose a new path")
    if session_path is not None:
        if session_path.exists():
            raise ValueError("session output already exists; choose a new path")
        if output == session_path or session_path in output.parents or output in session_path.parents:
            raise ValueError("HTML and session output paths must be separate")
    session = merge_sessions(inputs, run_id=run_id)
    if session_path is not None:
        resource.write_session(session, session_path)
    resource.write_report(session, output, overwrite=False)
    return session
