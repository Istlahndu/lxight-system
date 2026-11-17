"""Expose calibrated totals as aggregates to the existing evidence model.

The timing projection is intentionally absent: a model's aggregate value must
never become a verified hardware interval or an overlap measurement.
"""
import copy
import hashlib
import json

from ..resource import SCHEMA, validate_session


def to_resource_session(analysis):
    identity_fields = {key: analysis.get(key) for key in
                       ("provenance", "hardware", "model", "config", "analysis_rules_version")}
    encoded = json.dumps(identity_fields, sort_keys=True, ensure_ascii=False).encode("utf-8")
    identity = hashlib.sha256(encoded).hexdigest()[:20]
    timeline = analysis.get("timeline", {})
    bounds = timeline.get("time_range", [0, 1])
    if bounds[1] <= bounds[0]:
        bounds = [0, 1]
    source = "production.model"
    semantics = "model_calibrated:production_attribution_v1"
    records = []
    category_map = {"comm_c": "cc", "comm_x": "cx", "idle": "drv"}
    for stage, values in analysis.get("stages", {}).items():
        for category, value in values.get("matrix_first", {}).items():
            records.append({
                "record_id": "attribution:%d" % len(records), "resource_id": "node:attribution",
                "source_id": source, "layer": "hardware", "representation": "aggregate",
                "category": category_map.get(category, category), "value": value, "unit": "cycles",
                "label": stage, "name": stage + " / " + category,
                "semantics_status": semantics, "hardware_interval": False,
                "aggregation_rule": "sum of calibrated block-mode estimates over observed pool threads",
                "bounds": copy.deepcopy(values.get("bounds", {})),
                "args": {"production_category": category, "scope": "observed_pool_threads",
                         "evidence_type": "cross_run_model_estimate", "time_projection_is_not_activity": True},
                "raw_refs": copy.deepcopy(analysis.get("provenance", [])),
            })
    summary = {key: copy.deepcopy(analysis.get(key)) for key in
               ("schema_version", "analysis_rules_version", "config", "coverage", "alignment", "hardware",
                "model", "provenance", "totals", "stages", "dense", "mfu", "warnings", "view_status")}
    raw_name = "raw/attribution/summary.json"
    raw = json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    session = {
        "schema_version": SCHEMA, "session_id": "production-model-" + identity,
        "run_id": "cross-run-attribution-" + identity, "data_origin": "imported",
        "time_range": bounds, "app": {"name": "Production attribution estimates"},
        "resources": [{"resource_id": "node:attribution", "parent_id": None, "type": "node",
                       "name": "Observed pool threads", "mapping_source": "input trace metadata"}],
        "sources": [{"source_id": source, "source_layer": "hardware", "scope": "observed_pool_threads",
                     "clock_id": "unknown", "boundary_support": False, "semantics_status": semantics}],
        "clocks": [], "records": records, "allocation": {"status": "unverified", "cpu_ids": []},
        "quality": {"warnings": list(analysis.get("warnings", [])),
                    "attribution_evidence": "aggregate_estimates_only", "cross_run_clocks_merged": False,
                    "coverage": copy.deepcopy(analysis.get("coverage", {}))},
        "capability_manifest": [{"category": category, "availability": "aggregate_only",
                                 "boundary_support": False, "implementation_status": "model_calibrated"}
                                for category in ("mat", "vec", "mem", "cc", "cx", "fe", "wait", "drv")],
        "provenance": copy.deepcopy(analysis.get("provenance", [])),
        "raw_files": {raw_name: raw},
        "source_inputs": "Original binary inputs remain external and are identified by source paths and SHA256; summary is derived evidence.",
    }
    return validate_session(session)
