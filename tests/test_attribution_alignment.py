"""Cross-run work identity, rate provenance and timing-origin regressions."""
import copy
import struct
import unittest
from pathlib import Path

from lxtrace.attribution.align import align_runs
from lxtrace.workspace import temporary_directory


FREQ = 100_000_000
ORIGIN = 2**60 + 17
POOL = [(0, 1, 0, 8), (10, 23, 3, 0), (30, 23, 4, 0), (70, 24, 4, 0),
        (80, 3, 2, 0), (90, 4, 2, 0), (100, 2, 0, 8)]
DRIVER = [(0, 9, 1, 0), (100, 10, 1, 0)]


class AttributionAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name, *, events=None, origin=ORIGIN, counting=False, codes=None,
              cycle_step=100, event_step=10, configuration="T 8 B 1 dies 1 domains 1",
              driver=None, count_values=True, pool_slot=(0, 0, 0), metadata_extra=""):
        events = list(events or POOL)
        driver = list(DRIVER if driver is None else driver)
        codes = codes or ["0x7201", "0x0008"]
        path = self.root / ("trace-" + name + ".bin")
        buffers = [(0, -1, -1, driver, None)] if driver else []
        values = [(i*cycle_step, i*event_step, i*event_step*2, 0, 0, 0, 0, 0, 0)
                  for i in range(len(events))] if counting and count_values else None
        buffers.append((*pool_slot, events, values))
        raw = bytearray(struct.pack("<8sQii", b"GFTRACE2", FREQ, len(buffers), 0))
        for die, dom, thr, ev, readings in buffers:
            raw.extend(struct.pack("<6i", die, dom, thr, len(ev), 9 if readings else 0, 0))
            raw.extend(b"".join(struct.pack("<QHHi", origin+t, kind, code, aux) for t, kind, code, aux in ev))
            if readings:
                raw.extend(b"".join(struct.pack("<9Q", *row) for row in readings))
        path.write_bytes(raw)
        path.with_suffix(".txt").write_text(
            "run %s %s freq %d\nstage 2 dense_gemm\nstage 3 gate_up\nlayer 0 dense\n"
            "frame 0 counting 1 parts 1 tokens 8\npmu %s\n%s" %
            (name, configuration, FREQ, ",".join(codes) if counting else "ticks", metadata_extra), encoding="utf-8")
        return path

    def align(self, timing=None, counts=None, **options):
        timing = timing or self.write("tT")
        counts = counts or {"tA": self.write("tA", counting=True, origin=ORIGIN+1_000_000)}
        return align_runs(timing, counts, 1_550_000_000, **options)

    def test_only_timing_run_supplies_time_with_large_integer_origins(self):
        timing = self.write("tT")
        counted_events = [(t*3, k, c, a) for t, k, c, a in POOL]
        counted_driver = [(t*3, k, c, a) for t, k, c, a in DRIVER]
        counted = self.write("tA", counting=True, events=counted_events, driver=counted_driver,
                             origin=ORIGIN+10**14)
        aligned = self.align(timing, {"tA": counted}, expected_pool_threads=1)
        self.assertEqual(aligned["origin_ticks"], ORIGIN)
        self.assertEqual(aligned["windows"], {"0": [0, 1000]})
        self.assertEqual(sum(s["ticks"] for s in aligned["segments"]), 100)
        self.assertEqual(sum(e["cycles"] for e in aligned["entries"]), 1550)
        self.assertAlmostEqual(sum(e["codes"]["0x7201"] for e in aligned["entries"]), 155)
        self.assertTrue(aligned["full_pool_coverage"])
        self.assertFalse(aligned["alignment"]["absolute_clocks_merged"])
        self.assertTrue(all(s["begin_ns"] <= s["end_ns"] <= 1000 for s in aligned["segments"]))
        self.assertEqual(aligned["perturbation"]["tA"]["duration_ratio"], 3)

    def test_raw_counts_and_first_run_event_precedence_remain_distinct(self):
        a = self.write("tA", counting=True, event_step=10)
        v = self.write("tV", counting=True, event_step=30)
        aligned = self.align(counts={"tA": a, "tV": v})
        self.assertEqual(aligned["run_of"]["0x7201"], "tA")
        entry = next(e for e in aligned["entries"] if e["mode"] == "mmg")
        self.assertEqual(entry["raw"]["0x7201"], 10)
        self.assertEqual(entry["raw_counts"]["tV"]["0x7201"], 30)
        self.assertEqual(entry["rate_evidence"]["0x7201"]["run"], "tA")
        self.assertEqual(entry["rate_evidence"]["0x7201"]["rate_basis"], "block_mode")
        self.assertEqual(entry["ccyc"]["tA"], 100)
        self.assertEqual(aligned["run_counts"]["dense_gemm|mmg"]["tV"]["0x7201"], 30)
        self.assertIsInstance(aligned["run_counts"]["dense_gemm|mmg"]["tA"]["0x0011"], int)

    def _fallback(self, second_stage=2):
        base = POOL + [(t+200, k, second_stage if c == 2 else c, a) for t,k,c,a in POOL]
        first = [r for r in POOL if r[1] != 23 or r[2] != 4]
        first = [(t,k,3 if k == 24 else c,a) for t,k,c,a in first]
        counted = first + [(t+200,k,second_stage if c == 2 else c,a) for t,k,c,a in POOL]
        driver = [(0,9,1,0),(300,10,1,0)]
        timing = self.write("tT", events=base, driver=driver)
        count = self.write("tA", counting=True, events=counted, driver=driver)
        return timing, {"tA": count}

    def test_stage_mode_fallback_is_labeled_and_strict_mode_rejects(self):
        timing, counts = self._fallback()
        aligned = self.align(timing, counts)
        entry = next(e for e in aligned["entries"] if e["mode"] == "mmg" and e["block"] == 0)
        source = entry["rate_evidence"]["0x7201"]
        self.assertEqual(source["rate_basis"], "stage_mode")
        self.assertEqual(source["matched_raw_count"], 0)
        self.assertEqual(source["raw_count"], 10)
        self.assertEqual(source["source_key"], ["0:0:0",2,"mmg"])
        with self.assertRaisesRegex(ValueError, "strict alignment rejects"):
            self.align(timing, counts, strict_alignment=True)

    def test_mode_fallback_is_labeled_instead_of_claiming_exact(self):
        timing, counts = self._fallback(3)
        aligned = self.align(timing, counts)
        entry = next(e for e in aligned["entries"] if e["mode"] == "mmg" and e["block"] == 0)
        self.assertEqual(entry["rate_evidence"]["0x7201"]["rate_basis"], "mode")
        self.assertGreater(aligned["alignment"]["basis_counts"]["mode"], 0)

    def test_unavailable_rate_stays_null_and_not_fake_zero_measurement(self):
        events = [r for r in POOL if r[1] != 23 or r[2] != 4]
        events = [(t,k,3 if k == 24 else c,a) for t,k,c,a in events]
        aligned = self.align(counts={"tA": self.write("tA", counting=True, events=events)})
        entry = next(e for e in aligned["entries"] if e["mode"] == "mmg")
        self.assertNotIn("0x7201", entry["codes"])
        self.assertIsNone(entry["rate_evidence"]["0x7201"]["estimate"])
        self.assertGreater(aligned["alignment"]["missing_rates"], 0)

    def test_layer_stage_and_workload_configuration_mismatches_fail(self):
        for mutation in ("layer", "stage", "B", "tokens", "slot"):
            with self.subTest(mutation=mutation):
                timing = self.write("tT")
                events = copy.deepcopy(POOL)
                config = "T 8 B 1 dies 1 domains 1"
                if mutation == "layer":
                    events = [(t,k,1 if k in (1,2) else c,a) for t,k,c,a in events]
                elif mutation == "stage":
                    events = [(t,k,3 if k in (3,4) else c,a) for t,k,c,a in events]
                elif mutation == "B":
                    config = "T 8 B 2 dies 1 domains 1"
                counted = self.write("tA", counting=True, events=events, configuration=config,
                                     pool_slot=(0,0,1) if mutation == "slot" else (0,0,0))
                if mutation == "tokens":
                    m = counted.with_suffix(".txt")
                    m.write_text(m.read_text(encoding="utf-8").replace("tokens 8", "tokens 9"), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.align(timing, {"tA": counted})

    def test_missing_pmu_readings_and_zero_cycle_nonzero_events_fail(self):
        for values, step in ((False,100),(True,0)):
            counted = self.write("tA", counting=True, count_values=values, cycle_step=step)
            with self.subTest(values=values, step=step), self.assertRaises(ValueError):
                self.align(counts={"tA":counted})

    def test_counting_run_cannot_be_used_as_the_timing_base(self):
        counted = self.write("tT", counting=True)
        with self.assertRaisesRegex(ValueError, "timing run must declare"):
            self.align(timing=counted)

    def test_equal_layer_sequence_in_a_different_sampled_frame_is_rejected(self):
        driver = [(0,9,1,0),(100,10,1,0),(200,9,1,1),(300,10,1,1)]
        extra = "frame 1 counting 1 parts 1 tokens 8\n"
        timing = self.write("tT", driver=driver, metadata_extra=extra)
        count = self.write("tA", counting=True, events=[(t+200,k,c,a) for t,k,c,a in POOL],
                           driver=driver, metadata_extra=extra)
        with self.assertRaisesRegex(ValueError, "traced frame differs"):
            self.align(timing,{"tA":count})

    def test_unknown_coverage_cannot_certify_whole_machine(self):
        aligned = self.align()
        self.assertFalse(aligned["full_pool_coverage"])
        self.assertIsNone(aligned["expected_pool_threads"])
        self.assertIsNone(aligned["pool_coverage_ratio"])
        partial = self.align(expected_pool_threads=592)
        self.assertEqual(partial["pool_coverage_ratio"], 1/592)
        self.assertFalse(partial["full_pool_coverage"])

    def test_multiple_frames_are_not_counted_as_extra_parallel_threads(self):
        pool = POOL + [(t+200,k,c,a) for t,k,c,a in POOL]
        driver = [(0,9,1,0),(100,10,1,0),(200,9,1,1),(300,10,1,1)]
        extra = "frame 1 counting 1 parts 1 tokens 8\n"
        timing = self.write("tT",events=pool,driver=driver,metadata_extra=extra)
        counted = self.write("tA",counting=True,events=pool,driver=driver,metadata_extra=extra)
        aligned = self.align(timing,{"tA":counted},expected_pool_threads=1)
        self.assertEqual(aligned["threads"], {"0":1})
        self.assertEqual({e["frame"] for e in aligned["entries"] if e["mode"]!="idle"},{0,1})
        self.assertEqual(aligned["counting_windows"]["0"]["frame_ids"],[0,1])

    def test_source_hash_and_event_indices_trace_estimates_to_inputs(self):
        aligned = self.align()
        self.assertEqual({p["reader"] for p in aligned["provenance"]},{"lxtrace.gftrace.read_gftrace2"})
        self.assertTrue(all(len(p["sha256"]) == 64 for p in aligned["provenance"]))
        self.assertTrue(all(e["representation"]=="aggregate" for e in aligned["entries"]))
        self.assertTrue(all(s["raw_event_indices"][1] == s["raw_event_indices"][0]+1 for s in aligned["segments"]))


if __name__ == "__main__":
    unittest.main()
