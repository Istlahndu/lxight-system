import unittest
from lxtrace.schedule import reconstruct_switches


class SchedulerReconstructionTests(unittest.TestCase):
    def test_migration_does_not_extend_old_cpu(self):
        rows = [dict(timestamp_ns=1,cpu=2,event='switch',next_instance='T',next_tid=7),
                dict(timestamp_ns=4,cpu=2,event='switch',prev_instance='T',next_instance='idle2',next_tid=0),
                dict(timestamp_ns=5,cpu=8,event='switch',next_instance='T',next_tid=7),
                dict(timestamp_ns=8,cpu=8,event='switch',prev_instance='T',next_instance='other',next_tid=9)]
        records, _ = reconstruct_switches(rows,{2:'cpu:2',8:'cpu:8'},[0,10],['T'],reliable_stop=True)
        target = [(r['resource_id'],r['begin_ns'],r['end_ns']) for r in records if r['state']=='target']
        self.assertEqual(target,[('cpu:2',1,4),('cpu:8',5,8)])
        self.assertTrue(any(r['state']=='unknown' and r['begin_ns']==0 for r in records))
        self.assertFalse(any(r.get('thread_instance_id')=='other' for r in records))

    def test_loss_and_unproven_tail_stay_unknown_or_open(self):
        rows = [dict(timestamp_ns=1,cpu=2,event='switch',next_instance='T',next_tid=7),
                dict(timestamp_ns=4,cpu=2,event='loss'),
                dict(timestamp_ns=6,cpu=2,event='switch',next_instance='T',next_tid=7)]
        records,q = reconstruct_switches(rows,{2:'cpu:2'},[0,10],['T'])
        self.assertFalse(any(r['state']=='target' and r.get('end_ns') is not None for r in records))
        self.assertIsNone(records[-1]['end_ns'])
        self.assertTrue(q['unknown_windows'])

    def test_mismatched_previous_thread_invalidates_interval(self):
        rows = [dict(timestamp_ns=1,cpu=2,event='switch',next_instance='T',next_tid=7),
                dict(timestamp_ns=4,cpu=2,event='switch',prev_instance='missing-switch',next_instance='T',next_tid=7)]
        records,_ = reconstruct_switches(rows,{2:'cpu:2'},[0,10],['T'],reliable_stop=True)
        self.assertEqual(records[1]['state'],'unknown')

    def test_out_of_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            reconstruct_switches([dict(timestamp_ns=1,cpu=5,event='switch',next_instance='T')],{2:'cpu:2'},[0,10],['T'])

    def test_impossible_same_thread_parallelism_becomes_unknown(self):
        rows=[dict(timestamp_ns=1,cpu=2,event='switch',next_instance='T',next_tid=7),
              dict(timestamp_ns=3,cpu=8,event='switch',next_instance='T',next_tid=7),
              dict(timestamp_ns=5,cpu=2,event='switch',prev_instance='T',next_tid=0),
              dict(timestamp_ns=7,cpu=8,event='switch',prev_instance='T',next_tid=0)]
        _,q=reconstruct_switches(rows,{2:'cpu:2',8:'cpu:8'},[0,10],['T'],reliable_stop=True)
        bad=[w for w in q['unknown_windows'] if w['reason']=='contradictory_simultaneous_thread_instance']
        self.assertEqual({(w['resource_id'],w['begin_ns'],w['end_ns']) for w in bad},
                         {('cpu:2',3,5),('cpu:8',3,5)})
        self.assertTrue(q['protocol_errors'])

    def test_simultaneous_migration_boundary_order_does_not_create_overlap(self):
        rows=[dict(timestamp_ns=1,cpu=2,event='switch',next_instance='T',next_tid=7),
              dict(timestamp_ns=4,cpu=8,event='switch',next_instance='T',next_tid=7),
              dict(timestamp_ns=4,cpu=2,event='switch',prev_instance='T',next_tid=0)]
        _,q=reconstruct_switches(rows,{2:'cpu:2',8:'cpu:8'},[0,10],['T'],reliable_stop=True)
        self.assertFalse(q['protocol_errors'])
