"""A frame must not cover its operations; concurrent ranges must remain visible."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from lxtrace.workspace import workspace_environment

ROOT = Path(__file__).resolve().parents[1]


class PipelineTrackTests(unittest.TestCase):
    def test_nested_ranges_requests_and_waits_keep_their_boundaries_and_lanes(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node unavailable')
        script = r'''
const assert=require('assert'),T=require('./web/timeline.js');
const event=(id,a,b,kind,category)=>({record_id:id,begin_ns:a,end_ns:b,kind,category,
  representation:'interval',layer:'software'});
const records=[event('frame',0,100,'frame','frame'),event('outer',1,90,'range','mat'),
  event('inner',2,10,'range','mat'),event('vector',11,20,'range','vec'),
  event('exchange',20,30,'exchange','cx'),event('request',21,50,'request','cx'),
  event('wait',30,40,'wait','wait'),event('open',95,null,'range','vec')];
const original=JSON.stringify(records),pipeline=T.softwareTracks(records,'pipeline');
assert.equal(pipeline.flatMap(r=>r.records).length,records.length);
const frame=pipeline.find(r=>r.records.some(x=>x.record_id==='frame'));
assert.equal(frame.records.length,1);
for(const row of pipeline)for(let i=1;i<row.records.length;i++)
  assert(row.records[i-1].end_ns!==null&&row.records[i-1].end_ns<=row.records[i].begin_ns);
const detailed=T.softwareTracks(records,'categories');
assert.equal(detailed.filter(r=>r.label.startsWith('算 · 矩阵')).length,2);
assert.equal(detailed.find(r=>r.label==='通信 · 交换调用').records[0].record_id,'exchange');
assert.equal(detailed.find(r=>r.label==='通信 · 请求生命周期').records[0].record_id,'request');
assert.notEqual(T.colors.exchange,T.colors.mat);
assert.equal(JSON.stringify(records),original);
process.stdout.write(JSON.stringify({pipeline:pipeline.length,categories:detailed.length}));
'''
        result = subprocess.run([node, '-e', script], cwd=ROOT, capture_output=True,
                                text=True, encoding='utf-8', env=workspace_environment(), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(json.loads(result.stdout)['categories'], 5)


class TimelineNavigationTests(unittest.TestCase):
    def run_navigation(self, assertions):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node unavailable')
        script = "const assert=require('assert'),T=require('./web/timeline.js');\n" + assertions
        result = subprocess.run([node, '-e', script], cwd=ROOT, capture_output=True,
                                text=True, encoding='utf-8', env=workspace_environment(), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_selection_zoom_and_scroll_keep_nonzero_range_bounds_and_span(self):
        self.run_navigation(r'''
const range=[1000,11000],selection=[3100,5100],original=JSON.stringify({range,selection});
const view=T.clampView(range,selection);
assert.deepStrictEqual(view,selection,'Zoom must use exactly the selected time range');
assert.notStrictEqual(view,selection,'View changes must not mutate the analysis selection');
assert.deepStrictEqual(T.clampView(range,[-500,1500]),[1000,3000]);
assert.deepStrictEqual(T.clampView(range,[10000,12000]),[9000,11000]);
const geometry=T.scrollGeometry(range,view,600);
assert(geometry.contentWidth>600&&geometry.contentWidth<=1000000);
assert.equal(geometry.maxScroll,geometry.contentWidth-600);
assert(geometry.scrollLeft>0&&geometry.scrollLeft<geometry.maxScroll);
for(const [position,expected] of [[0,[1000,3000]],[geometry.maxScroll,[9000,11000]],
    [-100,[1000,3000]],[geometry.maxScroll+100,[9000,11000]]]){
  const moved=T.viewAtScroll(range,view,position,geometry.maxScroll);
  assert.deepStrictEqual(moved,expected);
  assert.equal(moved[1]-moved[0],2000,'Panning must keep the zoom level');
}
assert.equal(JSON.stringify({range,selection}),original,'Navigation must leave source range and selection unchanged');
''')

    def test_overview_has_no_scroll_and_roundtrips_recover_view(self):
        self.run_navigation(r'''
const range=[150000,4000000000],width=731;
const full=T.scrollGeometry(range,range,width);
assert.equal(full.contentWidth,width);
assert.equal(full.maxScroll,0);
assert.equal(full.scrollLeft,0);
assert.deepStrictEqual(T.viewAtScroll(range,range,10000,0),range);
for(const view of [[150000,1150000],[123456789,223456789],[3999999000,4000000000]]){
  const before=view.slice(),geometry=T.scrollGeometry(range,view,width);
  const recovered=T.viewAtScroll(range,view,geometry.scrollLeft,geometry.maxScroll);
  assert(Math.abs(recovered[0]-view[0])<=1,'Scroll mapping must recover the viewport start within 1 ns');
  assert(Math.abs(recovered[1]-view[1])<=1,'Scroll mapping must recover the viewport end within 1 ns');
  assert.equal(recovered[1]-recovered[0],view[1]-view[0]);
  assert.deepStrictEqual(view,before);
}
''')

    def test_one_nanosecond_zoom_uses_bounded_finite_scroll_geometry(self):
        self.run_navigation(r'''
const range=[1000000000000000,1001000000000000];
const view=[1000500000000000,1000500000000001],width=657;
const geometry=T.scrollGeometry(range,view,width);
for(const value of Object.values(geometry))assert(Number.isFinite(value));
assert(geometry.contentWidth<=1000000&&geometry.contentWidth>width);
assert.deepStrictEqual(T.viewAtScroll(range,view,0,geometry.maxScroll),[range[0],range[0]+1]);
assert.deepStrictEqual(T.viewAtScroll(range,view,geometry.maxScroll,geometry.maxScroll),[range[1]-1,range[1]]);
const recovered=T.viewAtScroll(range,view,geometry.scrollLeft,geometry.maxScroll);
assert(Math.abs(recovered[0]-view[0])<=1);
assert.equal(recovered[1]-recovered[0],1);
''')
