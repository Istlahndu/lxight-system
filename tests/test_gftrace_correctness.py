"""GFTRACE1 compatibility and production substep/counting metadata semantics."""
import struct
import unittest
from pathlib import Path

from lxtrace.core import analyze
from lxtrace.importers import load_gftrace
from lxtrace.resource import migrate_v1
from lxtrace.workspace import temporary_directory


class GFTraceCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def trace(self, records, metadata=None, driver=False):
        path = self.root / "trace.bin"
        raw = struct.pack("<8sQii", b"GFTRACE1", 100_000_000, 1, 0)
        raw += struct.pack("<iiii", 0, -1 if driver else 0, 0, len(records))
        raw += b"".join(struct.pack("<QHHi", 120_000_000_000_000 + t, k, c, a)
                        for t, k, c, a in records)
        path.write_bytes(raw)
        if isinstance(metadata, str):
            path.with_suffix(".txt").write_text(metadata, encoding="utf-8")
            metadata = None
        return load_gftrace(path, metadata=metadata)

    @staticmethod
    def steps(session):
        return [e for e in session["events"] if e["args"].get("legacy_kind") == 23]

    def test_chained_steps_close_at_next_begin_without_losing_call(self):
        session = self.trace([(0, 1, 3, -1), (50, 23, 3, 0), (60, 23, 4, 0),
                              (80, 24, 4, 0), (90, 3, 10, 0), (100, 4, 10, 0), (100, 2, 3, -1)])
        self.assertEqual(session["quality"]["protocol_errors"], 0)
        self.assertEqual(session["quality"]["incomplete_events"], 0)
        first, second = self.steps(session)
        self.assertEqual((first["begin_ts"], first["end_ts"]), (500, 600))
        self.assertEqual(first["args"]["end_boundary"], "next_sub_begin")
        self.assertEqual(first["args"]["end_boundary_event_index"], 2)
        self.assertEqual(first["args"]["raw_end_ticks"], second["args"]["raw_begin_ticks"])
        self.assertEqual(first["parent_id"], second["parent_id"])
        self.assertEqual(session["events"][0]["end_ts"], 1000)
        self.assertEqual(analyze(session, 0, 1000)["compute_ns"], 300)

    def test_repeated_site_and_equal_timestamp_are_valid_mode_switches(self):
        session = self.trace([(0, 1, 3, -1), (10, 23, 4, 0), (10, 23, 4, 0),
                              (20, 24, 4, 0), (30, 2, 3, -1)])
        self.assertEqual(session["quality"]["protocol_errors"], 0)
        self.assertEqual([e["end_ts"] - e["begin_ts"] for e in self.steps(session)], [0, 100])

    def test_step_table_keeps_exchange_sites_and_distinguishes_compute(self):
        records = [(0, 1, 3, -1)]
        for site in range(1, 10):
            records += [(site * 10, 23, site, 0), (site * 10 + 5, 24, site, 0)]
        records += [(100, 2, 3, -1)]
        session = self.trace(records)
        self.assertEqual(session["quality"]["protocol_errors"], 0)
        expected = [("exchange", "cx"), ("exchange", "cx"), ("range", "vec"),
                    ("range", "mat"), ("range", "vec"), ("range", "mat"),
                    ("range", "vec"), ("range", "vec"), ("range", "vec")]
        self.assertEqual([(e["kind"], e["category"]) for e in self.steps(session)], expected)
        self.assertFalse(any(e["kind"] == "request" for e in session["events"]))
        self.assertTrue(all(e["name"] == "subexchange.api" for e in self.steps(session)[:2]))

    def test_unknown_site_is_not_fabricated_as_communication(self):
        session = self.trace([(0, 23, 99, 0), (10, 24, 99, 0)])
        step = self.steps(session)[0]
        self.assertEqual((step["kind"], step["category"]), ("range", "unknown"))
        self.assertEqual(step["args"]["category_source"], "unknown_site")

    def test_explicit_old_category_extension_still_takes_precedence(self):
        for categories in ({23: "cx"}, {"23": "cx"}):
            with self.subTest(categories=categories):
                session = self.trace([(0, 23, 4, 0), (10, 24, 4, 0)], {"categories": categories})
                step = self.steps(session)[0]
                self.assertEqual((step["kind"], step["category"]), ("exchange", "cx"))
                self.assertEqual(step["args"]["category_source"], "metadata")

    def test_next_sub_does_not_close_an_open_wait(self):
        session = self.trace([(0, 1, 3, -1), (10, 23, 3, 0), (20, 7, 1, 0),
                              (30, 23, 4, 0), (40, 24, 4, 0), (50, 8, 1, 0), (60, 2, 3, -1)])
        self.assertGreater(session["quality"]["protocol_errors"], 0)
        self.assertIsNone(self.steps(session)[0]["end_ts"])
        self.assertNotIn("end_boundary", self.steps(session)[0]["args"])

    def test_unknown_duplicate_sub_does_not_trigger_known_mode_switch_rule(self):
        session = self.trace([(0, 23, 99, 0), (10, 23, 4, 0), (20, 24, 4, 0)])
        self.assertGreater(session["quality"]["protocol_errors"], 0)
        self.assertIsNone(self.steps(session)[0]["end_ts"])

    def test_missing_last_step_end_is_not_filled_at_call_or_capture_end(self):
        session = self.trace([(0, 1, 3, -1), (10, 23, 3, 0), (20, 23, 4, 0), (30, 2, 3, -1)])
        self.assertEqual(self.steps(session)[0]["end_ts"], 200)
        self.assertIsNone(self.steps(session)[1]["end_ts"])
        self.assertIsNone(session["events"][0]["end_ts"])
        self.assertGreater(session["quality"]["protocol_errors"], 0)

    def test_wrong_explicit_sub_end_is_not_repaired(self):
        session = self.trace([(0, 23, 3, 0), (10, 24, 4, 0)])
        self.assertIsNone(self.steps(session)[0]["end_ts"])
        self.assertEqual(session["quality"]["protocol_errors"], 1)

    def test_frame_counting_true_false_and_tokens_survive_v2_migration(self):
        session = self.trace([(0, 9, 1, 0), (10, 10, 1, 0), (20, 9, 0, 1), (30, 10, 0, 1)],
                             "frame 0 counting 1 parts 2 tokens 100\nframe 1 counting 0 parts 1 tokens 80\n", driver=True)
        frames = session["events"]
        self.assertIs(frames[0]["args"]["counting"], True)
        self.assertIs(frames[1]["args"]["counting"], False)
        self.assertEqual(frames[0]["args"]["counting_source"], "frame_metadata")
        self.assertEqual((frames[0]["args"]["parts"], frames[0]["args"]["tokens"]), (2, 100))
        self.assertEqual([r["args"]["counting"] for r in migrate_v1(session)["records"]], [True, False])

    def test_missing_frame_counting_is_unknown_not_false_or_frame_code(self):
        # FRAME_B.code describes the timed window, not a substitute for metadata.
        session = self.trace([(0, 9, 1, 0), (10, 10, 1, 0)], driver=True)
        self.assertNotIn("counting", session["events"][0]["args"])
        self.assertNotIn("counting", migrate_v1(session)["records"][0]["args"])

    def test_json_frame_metadata_supports_integer_and_string_keys(self):
        for key, counting in ((0, True), ("0", 0)):
            with self.subTest(key=key):
                session = self.trace([(0, 9, 1, 0), (10, 10, 1, 0)],
                                     {"frames": {key: {"counting": counting}}}, driver=True)
                self.assertIs(session["events"][0]["args"]["counting"], bool(counting))

    def test_json_frame_without_counting_preserves_unknown(self):
        session = self.trace([(0, 9, 1, 0), (10, 10, 1, 0)],
                             {"frames": {"0": {"parts": 2, "tokens": 100}}}, driver=True)
        self.assertNotIn("counting", session["events"][0]["args"])
        self.assertEqual(session["events"][0]["args"]["tokens"], 100)

    def test_json_frame_metadata_shapes_are_validated_even_without_matching_events(self):
        for metadata in ({"frames": []}, {"frames": None}, {"frames": {"0": []}},
                         {"frames": {"0": None}}, {"frames": {"99": {"counting": "false"}}},
                         {"frames": {"99": {"counting": None}}}, {"frames": {"99": {"parts": -1}}}):
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    self.trace([(0, 9, 1, 0), (10, 10, 1, 0)], metadata, driver=True)

    def test_nonobject_json_metadata_file_raises_value_error(self):
        metadata = self.root / "metadata.json"
        metadata.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "metadata must be an object"):
            self.trace([(0, 9, 1, 0), (10, 10, 1, 0)], metadata, driver=True)

    def test_malformed_or_conflicting_frame_metadata_is_rejected(self):
        for metadata in ("frame 0 counting 2\n", "frame 0 counting 1 parts\n",
                         "frame\n", "frame 0\n", "frame 0 counting\n", "frame 0 parts 1\n",
                         "frame 0 counting 1 typo 7\n",
                         "frame 0 counting 1\nframe 0 counting 0\n",
                         {"frames": {"0": {"counting": "false"}}}):
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    self.trace([(0, 9, 1, 0), (10, 10, 1, 0)], metadata, driver=True)


if __name__ == "__main__":
    unittest.main()
