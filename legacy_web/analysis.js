(function (root) {
  'use strict';
  const valid = x => Array.isArray(x) && Number.isFinite(x[0]) && Number.isFinite(x[1]) && x[1] > x[0];
  function union(items) {
    const sorted = (items || []).filter(valid).map(x => [x[0], x[1]]).sort((a,b) => a[0]-b[0] || a[1]-b[1]);
    const out=[];
    for (const x of sorted) { const p=out[out.length-1]; if (p && x[0]<=p[1]) p[1]=Math.max(p[1],x[1]); else out.push(x); }
    return out;
  }
  function intersection(a,b) {
    a=union(a); b=union(b); const out=[]; let i=0,j=0;
    while(i<a.length && j<b.length) { const x=Math.max(a[i][0],b[j][0]),y=Math.min(a[i][1],b[j][1]); if(y>x)out.push([x,y]); if(a[i][1]<b[j][1])i++;else j++; }
    return out;
  }
  function subtract(a,b) {
    a=union(a); b=union(b); const out=[]; let j=0;
    for(const x of a) { let at=x[0]; while(j<b.length && b[j][1]<=at)j++; let k=j; while(k<b.length && b[k][0]<x[1]) { if(b[k][0]>at)out.push([at,Math.min(x[1],b[k][0])]); at=Math.max(at,b[k][1]); if(at>=x[1])break; k++; } if(at<x[1])out.push([at,x[1]]); }
    return out;
  }
  const duration = a => union(a).reduce((n,x)=>n+x[1]-x[0],0);
  function parallelThreads(threadIntervals) {
    const edges=new Map(),out=[];
    for(const intervals of threadIntervals)for(const [a,b] of union(intervals)){edges.set(a,(edges.get(a)||0)+1);edges.set(b,(edges.get(b)||0)-1);}
    let active=0,previous=null;
    for(const [time,delta] of Array.from(edges.entries()).sort((a,b)=>a[0]-b[0])){if(previous!==null&&time>previous&&active>=2)out.push([previous,time]);active+=delta;previous=time;}
    return union(out);
  }
  const closed = e => e.end_ts!==null && Number.isFinite(e.end_ts) && e.end_ts>=e.begin_ts && e.status!=='incomplete';
  const capturePartial = q => q.capture_partial===true || q.clean_shutdown===false || Boolean(q.late_records) || Boolean(q.reported_incomplete_events);
  const hasLoss = session => [session.quality||{},...(session.sources||[])].some(q=>Number(q.dropped_records||q.lost_events||0)>0 || Number(q.protocol_errors||0)>0 || capturePartial(q));
  function dedup(events) { const seen=new Set(); return events.filter(e=>{const k=String(e.event_id);if(seen.has(k))return false;seen.add(k);return true;}); }
  function frameKeys(e) {
    const args=e&&e.args||{}, out=[];
    if(e&&e.frame_id!==null&&e.frame_id!==undefined)out.push(String(e.frame_id));
    if(e&&e.kind==='frame')out.push(String(e.event_id));
    for(const key of ['application_frame_id','frame_id','frame_key'])if(args[key]!==undefined&&args[key]!==null)out.push(String(args[key]));
    return Array.from(new Set(out));
  }
  function filteredEvents(session,options) {
    options=options||{};
    const selected=options.thread_ids&&options.thread_ids.length?new Set(options.thread_ids.map(String)):null;
    const frames=options.frame_ids&&options.frame_ids.length?new Set(options.frame_ids.map(String)):null;
    const operators=options.operator_ids&&options.operator_ids.length?new Set(options.operator_ids.map(String)):null;
    const frameById=new Map((session.events||[]).filter(e=>e.kind==='frame').map(e=>[String(e.event_id),e]));
    if(frames)for(const [id,e] of frameById){const label=(e.args||{}).application_frame_id;if(label!==null&&label!==undefined&&frames.has(String(label)))frames.add(id);}
    const events=dedup(session.events||[]).filter(e=>{
      if(selected&&!selected.has(String(e.thread_id)))return false;
      if(frames&&!frameKeys(e).some(k=>frames.has(k)))return false;
      if(operators){const args=e.args||{},frameArgs=(frameById.get(String(e.frame_id))||{}).args||{};
        if(!['operator_id','operator','operator_instance_id'].some(k=>[args[k],frameArgs[k]].some(v=>v!==null&&v!==undefined&&operators.has(String(v)))))return false;
      }
      return true;
    });
    return {events,frame_ids:frames?Array.from(frames):[],operator_ids:operators?Array.from(operators):[]};
  }
  function ownWall(session,event,a,b) {
    if(!closed(event))return {inclusive_ns:null,self_wall_ns:null,exact:false};
    const span=intersection([[event.begin_ts,event.end_ts]],[[a,b]]);
    const children=dedup(session.events||[]).filter(e=>e.parent_id===event.event_id && e.thread_id===event.thread_id && !['request','driver_outside','instant','counter'].includes(e.kind));
    const uncertain=children.some(e=>!closed(e)&&e.begin_ts<b)||hasLoss(session);
    return {inclusive_ns:duration(span),self_wall_ns:uncertain?null:duration(subtract(span,children.filter(closed).map(e=>[e.begin_ts,e.end_ts]))),exact:!uncertain};
  }
  function analyze(session,a,b,options) {
    options=options||{};
    if(!Number.isFinite(a)||!Number.isFinite(b)||a<0||b<=a)throw Error('选区必须满足有限的 0 ≤ t0 < t1');
    const filtered=filteredEvents(session,options),events=filtered.events;
    const window=[[a,b]], per=new Map(); const requests=[],waits=[],raw=[],allCompute=[],threadCompute=[];
    const relevant=events.filter(e=>e.begin_ts<b&&(!closed(e)||e.end_ts>a));
    const incomplete=relevant.filter(e=>!closed(e)&&e.kind!=='instant'&&e.kind!=='counter');
    for(const e of relevant.filter(closed)) {
      const p=per.get(String(e.thread_id))||{compute:[],exclude:[]}; per.set(String(e.thread_id),p);
      const span=[e.begin_ts,e.end_ts];
      if(e.kind==='range'&&['mat','vec','cc'].includes(e.category))p.compute.push(span);
      if(e.kind==='wait'||e.kind==='exchange')p.exclude.push(span);
      if(e.kind==='wait')waits.push(span);
      if(e.kind==='request')requests.push(span);
    }
    for(const e of incomplete.filter(e=>e.kind==='wait'||e.kind==='exchange')) {
      const p=per.get(String(e.thread_id))||{compute:[],exclude:[]}; per.set(String(e.thread_id),p);
      p.exclude.push([Math.max(a,e.begin_ts),b]);
    }
    for(const p of per.values()) { const clean=intersection(subtract(p.compute,p.exclude),window); raw.push(...p.compute); allCompute.push(...clean); threadCompute.push(clean); }
    let C=intersection(allCompute,window); const R=intersection(requests,window);
    const q=session.quality||{};
    const loss=hasLoss(session);
    const protocolErrors=Number(q.protocol_errors||0)>0;
    const exact=!loss&&!incomplete.length&&!protocolErrors;
    if(loss||protocolErrors)C=[];
    const O=intersection(C,R),P=loss||protocolErrors?[]:parallelThreads(threadCompute);
    const warnings=[];
    if(incomplete.length)warnings.push('未闭合范围不计入确定时长；当前数值仅为已闭合、已观测子集。');
    if(incomplete.some(e=>e.kind==='wait'||e.kind==='exchange'))warnings.push('未闭合等待/交换之后的同线程计算后缀保持未知，已保守排除。');
    if(loss||protocolErrors)warnings.push('源存在无位置丢失、协议错误或未完整结束，确定计算下界置零；原始标注和闭合请求保留。');
    if(protocolErrors)warnings.push('标注协议存在错误；当前数值仅为已闭合、已观测子集。');
    const exclusion=q.wait_exclusion_coverage||q.exclusion_coverage||'unknown';
    if(exclusion==='unknown')warnings.push('等待/交换标注覆盖未知；仅排除显式标注范围。');
    warnings.push('线程墙钟语义重叠不代表 on-CPU、持续传输或通信隐藏收益。');
    const cn=duration(C),rn=duration(R),on=duration(O);
    return {interval:[a,b],wall_ns:b-a,compute_raw_ns:duration(intersection(raw,window)),raw_overlap_ns:duration(intersection(intersection(raw,window),R)),compute_ns:cn,compute_parallel_ns:duration(P),request_ns:rn,overlap_ns:on,wait_ns:duration(intersection(waits,window)),request_overlap_ratio:exact&&rn>0?on/rn:null,compute_overlap_ratio:exact&&cn>0?on/cn:null,incomplete_count:incomplete.length,capture_partial:capturePartial(q),reported_incomplete_count:q.reported_incomplete_events||0,exclusion_coverage:exclusion,exact,known_subset_only:!exact,warnings,thread_ids:Array.from(new Set(events.map(e=>String(e.thread_id)))),frame_ids:filtered.frame_ids,operator_ids:filtered.operator_ids,event_count:relevant.length,completed_event_count:relevant.filter(closed).length,intervals:{compute:C,requests:R,overlap:O,parallel:P}};
  }
  const api={analyze,union,subtract,intersection,duration,ownWall,closed,dedup,frameKeys,filteredEvents};
  root.LXAnalysis=api; if(typeof module!=='undefined'&&module.exports)module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:this);
