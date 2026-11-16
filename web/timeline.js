/* Stable software tracks shared by every v2 report. No workload-specific code. */
(function(root,factory){
  const api=factory();
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.LXTimeline=api;
})(typeof window!=='undefined'?window:globalThis,function(){
  'use strict';
  const colors={frame:'#7895a4',mat:'#3479b5',vec:'#8470b8',cc:'#b96c8d',
    exchange:'#179b91',request:'#62aaa1',wait:'#a4aeb7',drv:'#647d8d',other:'#9aabb8'};
  const labels={frame:'帧 / 迭代',mat:'算 · 矩阵',vec:'算 · 向量 / 标量',cc:'通信 · 计算内跨核',
    exchange:'通信 · 交换调用',request:'通信 · 请求生命周期',wait:'显式等待',drv:'驱动调用外',other:'其他软件范围'};
  function category(record){
    if(record.kind==='frame'||record.category==='frame')return 'frame';
    if(record.kind==='request'||record.category==='request')return 'request';
    if(record.kind==='wait'||record.category==='wait')return 'wait';
    if(record.kind==='exchange'||['cx','comm_x'].includes(record.category))return 'exchange';
    if(record.kind==='driver_outside'||record.category==='drv')return 'drv';
    return ['mat','vec','cc'].includes(record.category)?record.category:'other';
  }
  function pack(records,label){
    const lanes=[],ends=[];
    for(const record of records.slice().sort((a,b)=>a.begin_ns-b.begin_ns||
        (b.end_ns??Infinity)-(a.end_ns??Infinity)||String(a.record_id).localeCompare(String(b.record_id)))){
      let lane=ends.findIndex(end=>end<=record.begin_ns);
      if(lane<0){lane=lanes.length;lanes.push([]);}
      lanes[lane].push(record);ends[lane]=record.end_ns??Infinity;
    }
    return lanes.map((records,index)=>({type:'interval',layer:'software',
      label:label+(index?' · '+(index+1):''),records}));
  }
  function softwareTracks(records,mode){
    const intervals=records.filter(r=>r.layer==='software'&&r.representation==='interval');
    const rows=[];
    if(mode==='categories'){
      for(const key of Object.keys(labels))rows.push(...pack(intervals.filter(r=>category(r)===key),labels[key]));
    }else{
      rows.push(...pack(intervals.filter(r=>category(r)==='frame'),labels.frame));
      rows.push(...pack(intervals.filter(r=>!['frame','request'].includes(category(r))),'算子流水线'));
      rows.push(...pack(intervals.filter(r=>category(r)==='request'),labels.request));
    }
    return rows;
  }
  function clampView(range,view){
    const [begin,end]=range,span=Math.min(end-begin,Math.max(1,view[1]-view[0]));
    const start=Math.max(begin,Math.min(end-span,view[0]));
    return [start,start+span];
  }
  function scrollGeometry(range,view,viewportWidth){
    const window=clampView(range,view),width=Math.max(1,viewportWidth),total=range[1]-range[0];
    const span=window[1]-window[0],contentWidth=Math.max(width,Math.min(1e6,width*(total/span)));
    const maxScroll=contentWidth-width,travel=total-span;
    return {contentWidth,maxScroll,scrollLeft:travel>0?(window[0]-range[0])/travel*maxScroll:0};
  }
  function viewAtScroll(range,view,scrollLeft,maxScroll){
    const window=clampView(range,view),span=window[1]-window[0];
    const fraction=maxScroll>0?Math.max(0,Math.min(1,scrollLeft/maxScroll)):0;
    const start=range[0]+fraction*(range[1]-range[0]-span);
    return clampView(range,[start,start+span]);
  }
  return {category,colors,labels,softwareTracks,clampView,scrollGeometry,viewAtScroll};
});
