"""Asynchronous request lifetimes must survive standard Trace Event export."""
import copy
import shutil
import subprocess
import unittest
from pathlib import Path

from lxtrace.cli import export_trace
from lxtrace.workspace import workspace_environment


ROOT = Path(__file__).resolve().parents[1]


class CLITraceExportTests(unittest.TestCase):
    def test_crossing_requests_keep_independent_async_lifetimes(self):
        def record(record_id, begin_ns, end_ns, kind, category):
            return dict(record_id=record_id, resource_id='thread:0', source_id='sdk:0',
                        layer='software', representation='interval', name='same-name',
                        kind=kind, category=category, begin_ns=begin_ns, end_ns=end_ns,
                        raw_refs=['raw/sdk.jsonl#'+record_id])

        # Deliberately unsorted, with crossing async requests and a synchronous
        # computation on one thread. A request name is not a unique identifier.
        session = dict(session_id='crossing-async-export', data_origin='synthetic',
                       resources=[dict(resource_id='thread:0', type='thread', name='rank0')],
                       sources=[dict(source_id='sdk:0', source_layer='software')],
                       records=[record('request:1', 10000, 40000, 'request', 'cx'),
                                record('compute:0', 5000, 30000, 'range', 'mat'),
                                record('request:0', 0, 20000, 'request', 'cx'),
                                record('open:0', 15000, None, 'request', 'cx')])
        original = copy.deepcopy(session)
        result = export_trace(session)
        events = result['traceEvents']
        slices = [event for event in events if event['ph'] == 'X']
        self.assertEqual(len(slices), 1)
        self.assertEqual(slices[0]['args']['record_id'], 'compute:0')
        self.assertEqual((slices[0]['ts'], slices[0]['dur']), (5, 25))
        asynchronous = [event for event in events if event['ph'] in ('b', 'e')]
        self.assertEqual(len(asynchronous), 4)
        ids = {event['id'] for event in asynchronous if event['ph'] == 'b'}
        self.assertEqual(len(ids), 2)
        for record_id, endpoints in [('request:0', (0, 20)), ('request:1', (10, 40))]:
            pair = [event for event in asynchronous if event['args']['record_id'] == record_id]
            self.assertEqual([event['ph'] for event in pair], ['b', 'e'])
            self.assertEqual(tuple(event['ts'] for event in pair), endpoints)
            self.assertEqual(pair[0]['id'], pair[1]['id'])
            self.assertEqual(pair[0]['scope'], 'lxtrace.request')
            self.assertEqual(pair[1]['scope'], pair[0]['scope'])
            self.assertEqual(pair[0]['args']['source_id'], 'sdk:0')
            self.assertEqual(pair[0]['args']['raw_refs'], ['raw/sdk.jsonl#'+record_id])
        incomplete = [event for event in events if event['args']['record_id'] == 'open:0']
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(incomplete[0]['ph'], 'i')
        self.assertEqual(incomplete[0]['ts'], 15)
        self.assertIsNone(incomplete[0]['args']['end_ns'])
        self.assertEqual([event['ts'] for event in events], [0, 5, 10, 15, 20, 40])
        self.assertEqual(session, original)
        self.assertEqual(result['lxtrace_native'], original)


class BrowserTraceExportTests(unittest.TestCase):
    def test_crossing_requests_use_async_ids_and_open_requests_stay_incomplete(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node unavailable')
        script = r'''
const assert=require('assert'),fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('web/viewer.js','utf8'),begin=source.indexOf('function traceExport(){'),
  end=source.indexOf('\nasync function offlineHtml()',begin);
const record=(id,start,stop,kind,category)=>({record_id:id,resource_id:'thread:0',source_id:'sdk:0',
  layer:'software',representation:'interval',name:'same-name',kind,category,
  begin_ns:start,end_ns:stop,raw_refs:['raw/sdk.jsonl#'+id]});
const records=[record('request:0',0,20000,'request','cx'),
  record('compute:0',5000,30000,'range','mat'),
  record('request:1',10000,40000,'request','cx'),
  record('open:0',15000,null,'request','cx')];
const session={session_id:'crossing-async-export',data_origin:'synthetic',
  resources:[{resource_id:'thread:0',type:'thread',name:'rank0'}],
  sources:[{source_id:'sdk:0',source_layer:'software'}],records};
const original=JSON.stringify(session),context={S:{session,analysis:{per_cpu:[],counters:[]}},
  selectionExport:()=>({resources:session.resources,records}),origin:()=>session.data_origin,
  viewState:()=>({}),A:{isSchedule:()=>false}};
vm.runInNewContext(source.slice(begin,end)+';this.result=traceExport()',context);
const result=context.result,events=result.traceEvents,
  slices=events.filter(e=>e.ph==='X'),async=events.filter(e=>e.ph==='b'||e.ph==='e');
assert.equal(slices.length,1,'Only the synchronous compute remains a thread-stack slice');
assert.equal(slices[0].args.lxtrace_record.record_id,'compute:0');
assert.equal(async.length,4);
const ids=new Set(async.filter(e=>e.ph==='b').map(e=>e.id));
assert.equal(ids.size,2,'Same business names must not collapse distinct request lifetimes');
for(const id of ids){
  const pair=async.filter(e=>e.id===id);
  assert.equal(pair.length,2);assert.equal(pair[0].ph,'b');assert.equal(pair[1].ph,'e');
  assert.equal(pair[0].scope,'lxtrace.request');assert.equal(pair[1].scope,pair[0].scope);
  assert.equal(pair[0].args.lxtrace_record.source_id,'sdk:0');
  assert.equal(pair[0].args.lxtrace_source.source_id,'sdk:0');
  const r=pair[0].args.lxtrace_record;
  assert.equal(pair[0].ts,r.begin_ns/1000);assert.equal(pair[1].ts,r.end_ns/1000);
}
const open=events.filter(e=>e.args?.lxtrace_record?.record_id==='open:0');
assert.equal(open.length,1);assert.equal(open[0].ph,'i');
assert.equal(open[0].args.end_ns,null);assert.equal(open[0].args.activity_duration_unavailable,true);
const timed=events.filter(e=>e.ph!=='M');
assert(timed.every((e,i)=>i===0||timed[i-1].ts<=e.ts),'Events must be globally timestamp ordered');
assert.equal(JSON.stringify(session),original,'Export must leave the native evidence unchanged');
assert.equal(JSON.stringify(result.lxtrace.native_session),original);
'''
        result = subprocess.run([node, '-e', script], cwd=ROOT, capture_output=True,
                                text=True, encoding='utf-8', env=workspace_environment(), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
