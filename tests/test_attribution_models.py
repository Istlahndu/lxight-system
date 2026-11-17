"""Aggregate model invariants; no production capture or golden is a fixture."""
import copy
import math
import unittest

from lxtrace.attribution.model import (REQUIRED_CODES, analyze_attribution, analyze_mfu_counts,
                                       dense_attribution, load_hardware, load_model,
                                       split_compute, split_issue, unit_load)


ALL_CODES = sorted(set().union(*map(set, REQUIRED_CODES.values())))


def measured(**values):
    out = {code: 0.0 for code in ALL_CODES}
    out.update(values)
    return out


def aligned(entries):
    for n, e in enumerate(entries):
        e.setdefault("entry_id", "e%d" % n)
        e.setdefault("thread_id", "0:0:1")
        e.setdefault("die", 0)
        e.setdefault("ticks", int(e["cycles"]))
    return {"entries": entries, "segments": [], "stage_modes": {},
            "codes": {"tA": ALL_CODES}, "expected_pool_threads": 592, "full_pool_coverage": False}


class AttributionModelTests(unittest.TestCase):
    def test_matrix_first_is_nonnegative_conserved_and_inside_hard_bounds(self):
        for cycles, mopa, stall, beyond in ((100, 30, 80, 40), (100, 60, 200, 150),
                                            (100, 0, 40, 20), (0, 0, 0, 0)):
            with self.subTest(cycles=cycles, mopa=mopa):
                classes, info = split_compute({"cycles": cycles, "counters": measured(**{
                    "0x7201": mopa, "0x7005": stall, "0x7008": beyond})})
                self.assertAlmostEqual(sum(classes.values()), cycles)
                self.assertTrue(all(x >= -1e-12 for x in classes.values()))
                self.assertLessEqual(info["ov_lo"], info["ov"])
                self.assertLessEqual(info["ov"], info["ov_hi"])
                exposed = classes["mem"] + classes["comm_c"]
                self.assertLessEqual(info["mem_lo"], exposed + 1e-12)
                self.assertLessEqual(exposed, info["mem_hi"] + 1e-12)
                self.assertFalse(info["hardware_interval"])
                self.assertEqual(info["representation"], "aggregate")

    def test_fill_source_latency_comes_from_the_selected_hardware_profile(self):
        e = {"cycles": 100, "counters": measured(**{"0x7005": 80, "0x7008": 80,
              "0x6055": 10, "0x0017": 20})}
        normal = split_compute(e)[0]
        hw = load_hardware()
        hw["calibration"]["snoop_latency"] = 20000
        changed = split_compute(e, hw)[0]
        self.assertGreater(changed["comm_c"], normal["comm_c"])
        self.assertAlmostEqual(changed["comm_c"] + changed["mem"], 80)

    def test_issue_partition_retains_unmeasured_zero_issue_other_cycles(self):
        result, quality = split_issue({"cycles": 100, "counters": measured(**{
            "0x7201": 20, "0x7005": 50, "0x7007": 30, "0x7008": 10})})
        self.assertAlmostEqual(sum(result.values()), 100)
        self.assertEqual(result["ld_l1"], 20)
        self.assertEqual(result["ld_l2"], 20)
        self.assertEqual(result["other"], 30)
        self.assertIn("zero-issue", quality["other_includes"])

    def test_unit_utilization_overlaps_and_is_never_forced_to_sum_to_one(self):
        result = unit_load({"cycles": 100, "counters": measured(**{
            "0x7201": 50, "0x8006": 300, "0x8084": 100,
            "0x0003": 100, "0x0017": 20, "0x0008": 340})})
        self.assertEqual(result["mat"], 1)
        self.assertEqual(result["sve"], 1)
        self.assertEqual(result["ldst"], 0.5)
        self.assertGreater(sum(result.values()), 1)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            unit_load({"cycles": 100, "counters": {"0x8006": 1, "0x8084": 5}})

    def test_software_exchange_wait_idle_and_dense_pack_classification(self):
        result = analyze_attribution(aligned([
            {"stage": "gate_up", "mode": "push", "cycles": 10, "codes": measured()},
            {"stage": "combine", "mode": "busy", "cycles": 20, "codes": measured()},
            {"stage": "dense_gemm", "mode": "bar", "cycles": 30, "codes": measured()},
            {"stage": "dense_gemm", "mode": "idle", "cycles": 40, "codes": measured()},
            {"stage": "dense_gemm", "mode": "pkg", "cycles": 50, "codes": measured()}]))
        cls = result["totals"]["matrix_first"]
        self.assertEqual((cls["comm_x"], cls["wait"], cls["idle"], cls["vec"]), (30, 30, 40, 50))
        self.assertAlmostEqual(sum(cls.values()), 150)
        self.assertAlmostEqual(sum(result["totals"]["issue"].values()), 150)
        self.assertEqual(result["totals"]["view_status"]["matrix_first"], "available")

    def test_missing_event_rates_are_unknown_not_measured_zero(self):
        result = analyze_attribution(aligned([
            {"stage": "dense_gemm", "mode": "mmg", "cycles": 100, "codes": {"0x7201": 20}},
            {"stage": "dense_gemm", "mode": "bar", "cycles": 10, "codes": {}}]))
        t = result["totals"]
        self.assertEqual(t["matrix_first"]["unknown"], 100)
        self.assertEqual(t["matrix_first"]["wait"], 10)
        self.assertEqual(t["view_status"]["matrix_first"], "unavailable")
        self.assertIn("0x7005", t["view_missing_codes"]["matrix_first"])
        self.assertIsNone(t["unit_load"]["mat"])
        self.assertEqual(result["dense"]["status"], "unavailable")
        self.assertEqual(result["dense"]["reason"], "dense_block_counter_rates_missing")

    def test_timeline_allocation_preserves_entry_amounts_without_hardware_claim(self):
        data = aligned([{"stage": "gate_up", "mode": "busy", "cycles": 100,
                         "ticks": 10, "codes": measured(**{"0x7201": 25})}])
        data["segments"] = [{"entry_id": "e0", "ticks": 4, "begin_ns": 0, "end_ns": 4},
                            {"entry_id": "e0", "ticks": 6, "begin_ns": 20, "end_ns": 26}]
        out = analyze_attribution(data)
        self.assertEqual([s["matrix_first"]["mat"] for s in out["segments"]], [20, 30])
        self.assertEqual(sum(s["cycles"] for s in out["segments"]), 100)
        for s in out["segments"]:
            self.assertFalse(s["hardware_interval"])
            self.assertEqual(s["allocation_basis"], "uniform_rate_within_block_mode")

    def test_dense_steps_preserve_raw_work_and_calibrated_bounds(self):
        sm = {}
        for mode, C, nm, stall in (("mmg", 10000, 1000, 5000), ("deqg", 1000, 0, 600), ("pkg", 500, 0, 100)):
            values = measured(**{"0x7201": nm, "0x7005": stall, "0x7008": stall / 2, "0x0008": 5000})
            sm["dense_gemm|" + mode] = dict(values, cycles=C, ticks=C, ccyc_tA=C * 1.1,
                                             **{"raw_" + c: v for c, v in values.items()})
        sm["dense_gemm|bar"] = dict(cycles=100, ticks=100)
        result = dense_attribution(sm, {"tA": ALL_CODES})
        self.assertEqual(result["status"], "available")
        self.assertAlmostEqual(sum(result["total"].values()), 11600)
        self.assertEqual(result["steps"]["mmg"]["k"], 5)
        self.assertEqual(result["steps"]["mmg"]["ipc"], 0.5)
        self.assertEqual(result["steps"]["mmg"]["mat_busy"], 0.2)
        for step in result["steps"].values():
            self.assertLessEqual(step["far_lo"], step["far_hi"])
            self.assertAlmostEqual(sum(step["det"].values()), step["cycles"])
            self.assertFalse(step["hardware_interval"])

    def test_mfu_four_factor_chain_matches_direct_model_flops(self):
        hw = load_hardware()
        hw["facts"].update(cores=8, frequency_hz=100)
        counts = {"source": "synthetic-complete-window", "frame": {
            "threads": 4, "seconds": 10, "compute_cycles": 2000,
            "mopa": 500, "inst": 4000, "flop": 409600}}
        result = analyze_mfu_counts(counts, hw)
        fr = result["frame"]
        self.assertEqual((fr["pool"], fr["in_compute"], fr["mat_busy"], fr["eff_mopa"]), (0.5, 0.5, 0.5, 0.8))
        self.assertAlmostEqual(fr["mfu"], 10)
        self.assertAlmostEqual(fr["mfu"], fr["mfu_direct"])
        self.assertEqual(result["source"], counts["source"])
        self.assertEqual(result["waterfall"][-1]["value"], 10)
        self.assertEqual(result["input_counts"], counts)

    def test_partial_pool_does_not_get_whole_machine_mfu_even_with_numerator(self):
        out = analyze_attribution(aligned([{"stage": "gate_up", "mode": "busy", "cycles": 100,
                                           "codes": measured(**{"0x7201": 25})}]), model_flops=10000)
        self.assertEqual(out["mfu"]["status"], "unavailable")
        self.assertEqual(out["mfu"]["reason"], "incomplete_pool_coverage")
        self.assertIsNone(out["mfu"]["frame"]["mfu"])
        with self.assertRaisesRegex(ValueError, "separately"):
            analyze_attribution(aligned([]), mfu_counts={})

    def test_full_thread_count_does_not_prove_full_frame_time_coverage(self):
        data = aligned([{"stage": "gate_up", "mode": "busy", "cycles": 100,
                         "codes": measured(**{"0x7201": 25})}])
        data.update(expected_pool_threads=1, full_pool_coverage=True)
        result = analyze_attribution(data, model_flops=10000)
        self.assertEqual(result["mfu"]["reason"], "full_frame_time_coverage_not_proven")
        self.assertIsNone(result["mfu"]["frame"]["mfu"])

    def test_phase_units_use_all_aggregate_counts_and_keep_local_anomaly_visible(self):
        data = aligned([
            {"stage": "dense_gemm", "mode": "pkg", "cycles": 1000,
             "codes": measured(**{"0x8006": 100, "0x8084": 200})},
            {"stage": "dense_gemm", "mode": "pkg", "cycles": 1000,
             "codes": measured(**{"0x8006": 500, "0x8084": 100})}])
        out = analyze_attribution(data)
        self.assertAlmostEqual(out["stages"]["dense_gemm"]["unit_load"]["sve"], 0.075)
        self.assertAlmostEqual(out["stages"]["dense_gemm"]["unit_load"]["ldst"], 0.075)
        self.assertEqual(out["stages"]["dense_gemm"]["unit_local_unknown_cycles"], 1000)
        self.assertEqual(out["stages"]["dense_gemm"]["view_status"]["unit_load"], "partial")
        self.assertIsNone(out["entries"][0]["unit_load"]["sve"])
        self.assertTrue(any("SVE load/store" in w for w in out["warnings"]))

    def test_invalid_profile_or_nonfinite_counter_is_rejected(self):
        for val in (-1, math.inf, math.nan):
            with self.subTest(value=val), self.assertRaises(ValueError):
                split_compute({"cycles": 100, "counters": {"0x7201": val}})
        hw = load_hardware()
        hw["facts"]["frequency_hz"] = 0
        with self.assertRaises(ValueError):
            load_hardware(hw)
        plugin = load_model()
        plugin["wait_modes"].append("busy")
        with self.assertRaisesRegex(ValueError, "disjoint"):
            load_model(plugin)

    def test_mfu_schema_errors_are_values_errors(self):
        for counts in ([], None, {}, {"frame": []}, {"frame": {}, "phases": "bad"},
                       {"frame": {}, "phases": ["bad"]}):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                analyze_mfu_counts(counts)

    def test_dense_missing_step_rate_is_not_substituted_with_zero(self):
        values = {"cycles": 1000, "ticks": 1000, "0x7201": 100, "0x7005": 400}
        out = dense_attribution({"dense_gemm|mmg": values}, {"tA": ALL_CODES})
        self.assertEqual(out["status"], "unavailable")
        self.assertEqual(out["reason"], "dense_step_counter_rates_missing")
        self.assertIn("0x7008", out["missing_codes"])

    def test_analysis_does_not_mutate_producer_evidence(self):
        data = aligned([{"stage": "gate_up", "mode": "busy", "cycles": 100,
                         "codes": measured(**{"0x7201": 25})}])
        before = copy.deepcopy(data)
        analyze_attribution(data)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
