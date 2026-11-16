(function () {
  'use strict';
  const bootstrapDocument=document.documentElement.cloneNode(true);
  const $=id=>document.getElementById(id), A=window.LXAnalysis;
  const S={session:null,events:[],byId:new Map(),frameByKey:new Map(),lanes:[],threadIds:new Set(),selected:null,selection:[0,1],view:[0,1],bounds:[0,1],matches:[],matchIndex:-1,hits:[],pointer:null,raf:0,stats:null,frameFilter:'all'};
  const C={mat:'#3479b5',vec:'#8470b8',cc:'#b96c8d',cx:'#179b91',wait:'#9ba6ae',drv:'#647d8d',unknown:'#b4c2ca',frame:'#5b8196'};
  const kindNames={frame:'帧',range:'算子 / 范围',wait:'显式等待',exchange:'交换 API',request:'请求生命周期',instant:'瞬时标记',counter:'计数标记',driver_outside:'驱动调用外'};
  const canvas=$('timelineCanvas'),ctx=canvas.getContext('2d'),scroller=$('timelineScroll');
  const AXIS_HEIGHT=34, SUMMARY_ROW_HEIGHT=38, SUMMARY_HEIGHT=SUMMARY_ROW_HEIGHT*2, TRACK_TOP=AXIS_HEIGHT+SUMMARY_HEIGHT, ROW_HEIGHT=32;
  let toastTimer=0,dragDepth=0;
  const fmt=(ns,digits)=>{if(!Number.isFinite(ns))return 'N/A';if(Math.abs(ns)>=1e9)return (ns/1e9).toFixed(digits===undefined?3:digits)+' s';if(Math.abs(ns)>=1e6)return (ns/1e6).toFixed(digits===undefined?3:digits)+' ms';if(Math.abs(ns)>=1e3)return (ns/1e3).toFixed(digits===undefined?2:digits)+' µs';return Math.round(ns)+' ns';};
  const pct=x=>x===null||!Number.isFinite(x)?'N/A':(x*100).toFixed(1)+'%';
  const number=x=>new Intl.NumberFormat('zh-CN').format(x);
  const ms=x=>(x/1e6).toFixed(6).replace(/0+$/,'').replace(/\.$/,'');
  function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,4200);}
  function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=String(text);return n;}
  function empty(n){n.replaceChildren();return n;}
  function frameKeys(e){return A.frameKeys(e);}
  function frameOf(e){if(e.kind==='frame')return e;for(const key of frameKeys(e)){const frame=S.frameByKey.get(key);if(frame)return frame;}return null;}
  function frameSelected(e){return S.frameFilter==='all'||frameKeys(e).includes(String(S.frameFilter));}
  function filteredFrameBounds(frame){return [frame.begin_ts,S.events.filter(frameSelected).reduce((end,e)=>A.closed(e)?Math.max(end,e.end_ts):end,Math.max(frame.begin_ts+1,frame.end_ts||frame.begin_ts))];}
  function categoryLabel(e){if(e.kind==='frame')return '帧';if(e.kind==='request')return '通信·显式交换 / 请求';if(e.kind==='exchange')return '通信·显式交换 / API';if(e.kind==='wait')return '等待';if(e.kind==='driver_outside')return '驱动线程调用外';if(e.category==='mat')return '算·矩阵';if(e.category==='vec')return '算·向量/标量';if(e.category==='cc')return '通信·计算内跨核';return e.category==='unknown'?'软件范围':String(e.category||'软件范围');}
  function renderFrameFilter(){
    const select=$('frameFilter');if(!select)return;
    const old=String(S.frameFilter||'all'),frames=S.events.filter(e=>e.kind==='frame');empty(select);select.append(el('option','', '全部帧'));select.options[0].value='all';
    const seen=new Set();for(const e of frames){const id=String(e.event_id);if(seen.has(id))continue;seen.add(id);const opt=el('option','',String(e.name||id)+' · '+fmt(e.begin_ts)+' → '+(A.closed(e)?fmt(e.end_ts):'未闭合'));opt.value=id;select.append(opt);}
    S.frameFilter=Array.from(select.options).some(o=>o.value===old)?old:'all';select.value=S.frameFilter;
  }
  function safeJson(value){return JSON.stringify(value).replace(/</g,'\\u003c').replace(/\u2028/g,'\\u2028').replace(/\u2029/g,'\\u2029');}
  function fileName(suffix){const name=String(S.session.session_id||'lxtrace').replace(/[^\w.-]+/g,'_').slice(0,80);return name+suffix;}
  function download(name,body,type){const url=URL.createObjectURL(new Blob([body],{type:type||'application/json;charset=utf-8'}));const a=el('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);}
  function setText(id,text){$(id).textContent=text;}
  function cleanSession(session){
    if(!session||session.schema_version!=='lxtrace.session.v1'||!Array.isArray(session.events))throw Error('不支持的会话格式。需要 lxtrace.session.v1 JSON 会话包。');
    if(session.events.length>2000000)throw Error('D0 查看器最多接受 2,000,000 个事件。请先裁剪会话。');
    const seen=new Map();for(const e of session.events){if(!e.event_id||!Number.isFinite(e.begin_ts)||e.begin_ts<0)throw Error('事件缺少 ID，或开始时间不是有效的非负 ns。');if(e.end_ts!==null&&e.end_ts!==undefined&&(!Number.isFinite(e.end_ts)||e.end_ts<e.begin_ts))throw Error('事件结束时间无效。');if(e.end_ts===undefined)e.end_ts=null;const id=String(e.event_id);if(seen.has(id)&&JSON.stringify(seen.get(id))!==JSON.stringify(e))throw Error('相同 event_id 包含冲突内容：'+id);seen.set(id,e);}
    return session;
  }
  function load(session){
    cleanSession(session);S.session=session;S.events=A.dedup(session.events);S.byId=new Map(S.events.map(e=>[String(e.event_id),e]));S.frameByKey=new Map();for(const e of S.events.filter(e=>e.kind==='frame'))for(const key of frameKeys(e))S.frameByKey.set(key,e);S.frameFilter='all';
    if(!Array.isArray(session.threads))session.threads=[];
    const known=new Set(session.threads.map(t=>String(t.thread_id)));
    for(const e of S.events){const id=String(e.thread_id||'unknown');e.thread_id=id;if(!known.has(id)){known.add(id);session.threads.push({thread_id:id,tid:e.tid,pid:e.pid,name:'线程 '+id,role:'unknown'});}}
    S.threadIds=new Set(session.threads.map(t=>String(t.thread_id)));
    let end=1;for(const e of S.events)end=Math.max(end,e.end_ts===null?e.begin_ts:e.end_ts);
    const tr=session.time_range;S.bounds=[Math.max(0,Array.isArray(tr)&&Number.isFinite(tr[0])?tr[0]:0),Math.max(end,Array.isArray(tr)&&Number.isFinite(tr[1])?tr[1]:end)];
    if(S.bounds[1]<=S.bounds[0])S.bounds[1]=S.bounds[0]+1;
    S.view=S.bounds.slice();S.selection=S.bounds.slice();S.selected=null;scroller.scrollTop=0;
    $('emptyState').hidden=true;$('workspace').hidden=false;$('downloadSession').disabled=false;
    const app=session.app||{};setText('sessionName',app.name||session.session_id||'未命名会话');setText('sessionCrumb',session.session_id||'当前会话');
    setText('sessionMeta',(app.build_id?'build '+app.build_id+'  ·  ':'')+session.threads.length+' 线程  ·  '+number(S.events.length)+' 事件  ·  '+fmt(S.bounds[1]-S.bounds[0])+'  ·  '+(session.clock&&session.clock.id||'unknown clock'));
    const origin=session.data_origin||'unknown';setText('originTag',origin);$('originTag').className='origin-tag '+origin;
    const workload=app.workload_config||{},binding=workload.binding||workload.thread_binding;
    $('bindingNote').hidden=!binding;
    if(binding)setText('bindingNote','会话包含线程绑核配置；本图按线程显示软件范围，未采集连续调度，不能据此确认逐核驻留。');
    setText('navCount',number(S.events.length));setText('eventCount',number(S.events.length)+' 事件 · '+session.threads.length+' 线程');
    renderThreads();renderFrameFilter();buildLanes();renderCapabilities();updateSearch(false);updateStats();renderDetails();scheduleDraw();
    try{localStorage.setItem('lxtrace.lastSession',session.session_id||'');}catch(_){}
  }
  function renderThreads(){
    const list=empty($('threadList')),query=$('threadSearch').value.trim().toLowerCase();
    for(const t of S.session.threads){const id=String(t.thread_id);if(query&&!JSON.stringify(t).toLowerCase().includes(query))continue;
      const label=el('label','thread-option'),checkbox=el('input');checkbox.type='checkbox';checkbox.checked=S.threadIds.has(id);checkbox.setAttribute('aria-label','显示线程 '+(t.name||id));
      checkbox.addEventListener('change',()=>{if(checkbox.checked)S.threadIds.add(id);else S.threadIds.delete(id);buildLanes();updateSearch(false);updateStats();scheduleDraw();});
      const info=el('span','thread-info');info.append(el('strong','',t.name||'线程 '+id),el('small','',t.role==='frame_driver'?'帧驱动 · TID '+(t.tid??id):'TID '+(t.tid??id)+' · PID '+(t.pid??'unknown')));
      label.append(checkbox,info);list.append(label);
    }
  }
  function depthOf(e){let depth=0,p=e.parent_id,guard=0;while(p&&guard++<32){const parent=S.byId.get(String(p));if(!parent||parent.thread_id!==e.thread_id||parent.kind==='frame')break;if(parent.kind==='range'||parent.kind==='wait'||parent.kind==='exchange')depth++;p=parent.parent_id;}return Math.min(depth,5);}
  function buildLanes(){
    const display=$('trackDisplay').value,computeOnly=display==='compute',frameMode=display==='frame';
    const grouped=new Map();
    for(const e of S.events){
      if(!S.threadIds.has(String(e.thread_id))||!frameSelected(e))continue;
      if(computeOnly&&!(e.kind==='range'&&['mat','vec','cc'].includes(e.category)))continue;
      const rows=grouped.get(e.thread_id)||new Map();grouped.set(e.thread_id,rows);
      let key,label;
      if(frameMode){
        const frame=frameOf(e),fid=frame?String(frame.event_id):'unframed';
        const rank=e.kind==='frame'?0:e.kind==='range'?({mat:1,vec:2,cc:3}[e.category]||8):({exchange:4,request:5,wait:6,driver_outside:7,instant:9,counter:9}[e.kind]||8);
        key='2:'+String(frame?frame.begin_ts:0).padStart(18,'0')+':'+fid+':'+rank+':'+categoryLabel(e);
        label=categoryLabel(e)+(S.frameFilter==='all'?' · '+(frame?frame.name||fid:'未归属帧'):'');
      }else{
        key=e.kind==='frame'?'0:frame':e.kind==='request'?'8:request':e.kind==='driver_outside'?'9:driver':e.kind==='exchange'?'4:'+depthOf(e):e.kind==='wait'?'5:'+depthOf(e):e.kind==='instant'||e.kind==='counter'?'7:markers':'3:'+depthOf(e);
        const rangeLabel=([ 'mat','vec','cc' ].includes(e.category))?'计算 / 算子':'软件范围 / 阶段';
        label=key.includes('frame')?'帧':key.includes('request')?'请求生命周期':key.includes('driver')?'驱动调用外':key.startsWith('4:')?'交换 API':key.startsWith('5:')?'显式等待':key.includes('markers')?'瞬时 / 计数标记':key==='3:0'?rangeLabel:'↳ '+rangeLabel+' '+key.slice(2);
      }
      const list=rows.get(key)||[];rows.set(key,list);list.push(e);
      if(frameMode)rows.get(key).laneLabel=label;
    }
    S.lanes=[];for(const t of S.session.threads){const id=String(t.thread_id),rows=grouped.get(id);if(!rows)continue;let first=true;for(const [key,events] of Array.from(rows.entries()).sort((a,b)=>a[0].localeCompare(b[0]))){events.sort((a,b)=>a.begin_ts-b.begin_ts);const rangeLabel=events.every(e=>['mat','vec','cc'].includes(e.category))?'计算 / 算子':'软件范围 / 阶段';const label=frameMode?(events.laneLabel||'帧内类别'):(key.includes('frame')?'帧':key.includes('request')?'请求生命周期':key.includes('driver')?'驱动调用外':key.startsWith('4:')?'交换 API':key.startsWith('5:')?'显式等待':key.includes('markers')?'瞬时 / 计数标记':key==='3:0'?rangeLabel:'↳ '+rangeLabel+' '+key.slice(2));S.lanes.push({thread:t,key,events,first,label});first=false;}}
    $('canvasSpace').style.height=Math.max(scroller.clientHeight,TRACK_TOP+4+S.lanes.length*ROW_HEIGHT+18)+'px';
  }
  function selectedThreads(){return S.threadIds.size===S.session.threads.length?undefined:Array.from(S.threadIds);}
  function updateStats(){
    if(!S.session)return;
    const ids=selectedThreads();let stats;
    if(ids&&ids.length===0)stats={interval:S.selection.slice(),wall_ns:S.selection[1]-S.selection[0],compute_raw_ns:0,compute_ns:0,compute_parallel_ns:0,request_ns:0,overlap_ns:0,wait_ns:0,incomplete_count:0,exact:true,known_subset_only:false,request_overlap_ratio:null,compute_overlap_ratio:null,warnings:['当前未选择任何逻辑线程。','线程墙钟语义重叠不代表持续传输或硬件 busy。'],thread_ids:[],exclusion_coverage:'unknown',intervals:{compute:[],requests:[],overlap:[],parallel:[]}};
    else stats=A.analyze(S.session,S.selection[0],S.selection[1],{thread_ids:ids,frame_ids:S.frameFilter==='all'?undefined:[S.frameFilter]});
    S.stats=stats;
    const metric=(id,value)=>{const text=fmt(value),parts=text.split(' '),n=empty($(id));n.append(document.createTextNode(parts[0]),el('span','unit',parts[1]||''));};
    const hasRequests=stats.request_ns>0;
    metric('metricWall',stats.wall_ns);metric('metricCompute',stats.compute_ns);metric('metricOverlap',hasRequests?stats.overlap_ns:NaN);
    setText('metricRatio',hasRequests?'请求生命周期覆盖率 '+pct(stats.request_overlap_ratio):'无已闭合请求');
    setText('metricQuality',stats.known_subset_only?'已观测子集':'闭合范围');setText('metricQualityNote',stats.incomplete_count?stats.incomplete_count+' 个未闭合范围 · 不补结束点':'等待覆盖 '+stats.exclusion_coverage+' · 保留原始边界');
    setText('qualitySummary',stats.known_subset_only?'有缺失 · 子集统计':'已闭合软件范围');
    setText('selectionCaption','['+ms(S.selection[0])+', '+ms(S.selection[1])+') ms');
    $('selectionFrom').value=ms(S.selection[0]);$('selectionTo').value=ms(S.selection[1]);
    for(const [bar,key] of [['computeBar','compute_ns'],['requestBar','request_ns'],['overlapBar','overlap_ns']])$(bar).style.width=Math.min(100,Math.max(0,stats[key]/stats.wall_ns*100))+'%';
    setText('analysisCompute',fmt(stats.compute_ns));setText('analysisRequest',fmt(stats.request_ns));setText('analysisOverlap',fmt(stats.overlap_ns));setText('requestRatio',pct(stats.request_overlap_ratio));setText('computeRatio',hasRequests?pct(stats.compute_overlap_ratio):'N/A · 无请求');setText('waitValue',fmt(stats.wait_ns));
    const notes=empty($('analysisWarnings'));if(stats.known_subset_only)notes.append(el('p','','仅显示已证实的闭合范围子集 / 保守下界；比例暂不可确定。'));
    for(const warning of stats.warnings)notes.append(el('p','',warning));
    renderFrameIntervals();
  }
  function renderFrameIntervals(){
    const body=empty($('frameIntervalRows')),limit=300;
    const events=S.events.filter(e=>S.threadIds.has(String(e.thread_id))&&frameSelected(e)&&['range','exchange','request','wait','driver_outside'].includes(e.kind)).sort((a,b)=>a.begin_ts-b.begin_ts||String(a.event_id).localeCompare(String(b.event_id)));
    const frame=S.byId.get(String(S.frameFilter)),scope=frame?'当前帧 '+(frame.name||frame.event_id):'全部帧';
    setText('frameIntervalCaption',scope+' · '+number(events.length)+' 条区间'+(events.length>limit?' · 先显示 '+limit+' 条，请筛选帧':'')+' · 跟随帧与线程筛选');
    if(!events.length){const row=el('tr'),cell=el('td','','当前筛选没有已观测区间。');cell.colSpan=8;row.append(cell);body.append(row);return;}
    for(const e of events.slice(0,limit)){
      const row=el('tr'),eventCell=el('td'),button=el('button','',e.name||e.event_id);button.title='定位 '+(e.name||e.event_id);button.addEventListener('click',()=>choose(e,true));eventCell.append(button,el('small','',categoryLabel(e)));
      const owner=frameOf(e),thread=S.session.threads.find(t=>String(t.thread_id)===String(e.thread_id)),identity=el('td','',owner?owner.name||owner.event_id:'未归属帧');identity.append(el('small','',thread?thread.name||e.thread_id:e.thread_id));
      const complete=A.closed(e),origin=owner?owner.begin_ts:null;
      row.append(eventCell,identity,el('td','',ms(e.begin_ts)),el('td','',complete?ms(e.end_ts):'未闭合'),el('td','',origin===null?'N/A':ms(e.begin_ts-origin)),el('td','',origin===null||!complete?'N/A':ms(e.end_ts-origin)),el('td','',complete?ms(e.end_ts-e.begin_ts):'N/A'),el('td','',(e.source_id||'unknown')+' / '+(e.evidence||'observed')));body.append(row);
    }
  }
  function renderCapabilities(){
    const session=S.session,clock=session.clock||{};setText('clockSummary',(clock.id||'unknown')+' · ns · 分辨率 '+(clock.resolution_ns??'unknown')+' ns');
    const scoped=S.events.filter(frameSelected),scope=S.frameFilter==='all'?'全会话':'当前帧';
    setText('capabilityScopeNote','以下按'+scope+'实际记录显示八类证据状态；软件范围可重叠，不能把这些卡片相加成互斥的八类耗时。');
    const ranges=category=>scoped.filter(e=>e.kind==='range'&&e.category===category);
    const recorded=(events,evidence)=>events.length?'已记录 · '+evidence+' · '+number(events.length)+' 条':scope+'未记录';
    const counts=events=>{const open=events.filter(e=>!A.closed(e)).length;return scope+'记录数'+(open?'；其中 '+number(open)+' 条未闭合，不补结束点':'；闭合边界可供选区分析');};
    const matrix=ranges('mat'),vector=ranges('vec'),crossCore=ranges('cc'),exchanges=scoped.filter(e=>e.kind==='exchange'),requests=scoped.filter(e=>e.kind==='request'),waits=scoped.filter(e=>e.kind==='wait'),driver=scoped.filter(e=>e.kind==='driver_outside');
    const communication=exchanges.concat(requests),exchangeStatus=communication.length?'已记录 · API '+number(exchanges.length)+' / 请求 '+number(requests.length):'本会话未记录';
    const diag=[
      ['算·矩阵',recorded(matrix,'软件范围'),'矩阵计算的线程墙钟范围，不代表矩阵单元 busy。'+counts(matrix)],
      ['算·向量 / 标量',recorded(vector,'软件范围'),'向量/标量运算标注，不是硬件指令归因。'+counts(vector)],
      ['访存（暴露）','unavailable · 未采集','D0 未采集用于归因的硬件证据；不输出暴露访存耗时或比例，也不把 memcpy 耗时当成暴露访存。'],
      ['通信·计算内跨核',recorded(crossCore,'显式协作范围'),'显式协作软件范围，非硬件跨核通信成本；不推断对端核或停顿。'+counts(crossCore)],
      ['通信·显式交换',exchangeStatus,'API 条是调用墙钟时间；请求条是提交至完成的生命周期，包含排队/等待，不等于持续传输。'+counts(communication)],
      ['其他·前端 / 分支','unavailable · 未采集','D0 未采集前端/分支硬件归因；不把未分类时间补到“其他”。'],
      ['等待',recorded(waits,'显式范围'),'应用显式等待范围；不区分 OS 阻塞与运行中自旋。'+counts(waits)],
      ['驱动线程调用外',recorded(driver,'软件补集'),'仅在完整帧、完整调用边界、帧驱动角色与完整数据满足条件时派生；不是驱动线程空闲。'+counts(driver)]
    ];
    const grid=empty($('diagnosticGrid'));for(const [name,status,note] of diag){const card=el('div','diagnostic-card');card.append(el('b','',name),el('span','',status),el('p','',note));grid.append(card);}
    const table=empty($('capabilityTable'));let caps=session.capability_manifest||[];if(!Array.isArray(caps))caps=Object.entries(caps).map(([k,v])=>({feature_id:k,...(typeof v==='object'?v:{availability:v})}));
    for(const c of caps){const tr=el('tr');tr.append(el('td','',c.feature_id||c.name||'unknown'),el('td','',c.availability||'unverified'),el('td','',[c.reason_code,c.failure_stage,c.reason,c.tested_scope,c.tested_filter].filter(Boolean).map(x=>typeof x==='object'?JSON.stringify(x):x).join(' · ')));table.append(tr);}
    if(!caps.length){const tr=el('tr');tr.append(el('td','','manifest'),el('td','','unverified'),el('td','','会话未包含现场能力快照；未验证能力不视为可用。'));table.append(tr);}
    setText('qualityData',JSON.stringify(session.quality||{},null,2));setText('provenanceData',JSON.stringify({clock:session.clock,sources:session.sources,provenance:session.provenance},null,2));setText('topologyData',JSON.stringify({execution_mode:session.execution_mode,verified_allocation:session.verified_allocation,topology:session.topology},null,2));
  }
  function canvasSize(){const width=scroller.clientWidth,height=scroller.clientHeight,dpr=Math.min(window.devicePixelRatio||1,2);if(canvas.width!==Math.round(width*dpr)||canvas.height!==Math.round(height*dpr)){canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);canvas.style.width=width+'px';canvas.style.height=height+'px';}ctx.setTransform(dpr,0,0,dpr,0,0);return{width,height,left:width<600?146:196,plot:Math.max(20,width-(width<600?146:196)-16)};}
  function scheduleDraw(){if(S.raf)return;S.raf=requestAnimationFrame(()=>{S.raf=0;draw();});}
  function draw(){
    if(!S.session||$('workspace').hidden)return;
    const {width,height,left,plot}=canvasSize(),v0=S.view[0],span=S.view[1]-v0,scroll=scroller.scrollTop;
    ctx.clearRect(0,0,width,height);ctx.fillStyle='#fff';ctx.fillRect(0,0,width,height);S.hits=[];
    const tx=t=>left+(t-v0)/span*plot;
    const ideal=span/7,power=Math.pow(10,Math.floor(Math.log10(ideal))),fraction=ideal/power,step=(fraction<=1?1:fraction<=2?2:fraction<=5?5:10)*power;
    ctx.font='9px Consolas, monospace';ctx.textAlign='center';ctx.fillStyle='#8198a7';
    for(let t=Math.ceil(v0/step)*step;t<=S.view[1];t+=step){const x=tx(t);ctx.strokeStyle='#edf2f5';ctx.beginPath();ctx.moveTo(Math.round(x)+.5,TRACK_TOP);ctx.lineTo(Math.round(x)+.5,height);ctx.stroke();ctx.fillText(fmt(t,2),x,20);}
    const activeQuery=$('search').value.trim().toLowerCase(),matches=activeQuery?new Set(S.matches.map(e=>String(e.event_id))):null;
    const rowHeight=ROW_HEIGHT;
    for(let i=0;i<S.lanes.length;i++){
      const y=TRACK_TOP+4+i*rowHeight-scroll;if(y+rowHeight<TRACK_TOP||y>height)continue;const lane=S.lanes[i];
      ctx.fillStyle=lane.first?'#fbfcfd':'#fff';ctx.fillRect(0,Math.max(TRACK_TOP,y),width,rowHeight);if(lane.first){ctx.strokeStyle='#e5edf1';ctx.beginPath();ctx.moveTo(0,y-.5);ctx.lineTo(width,y-.5);ctx.stroke();}
      ctx.save();ctx.beginPath();ctx.rect(left,TRACK_TOP,plot,height-TRACK_TOP);ctx.clip();
      for(const e of lane.events){if(e.begin_ts>S.view[1])break;if(e.end_ts!==null&&e.end_ts<v0)continue;if(e.end_ts===null&&(e.begin_ts<v0||e.begin_ts>S.view[1]))continue;
        const isPoint=e.kind==='instant'||e.kind==='counter',open=!A.closed(e)&&!isPoint;
        let x=tx(e.begin_ts),end=A.closed(e)?tx(e.end_ts):x+(open?14:1),w=Math.max(isPoint?2:1.5,end-x),h=lane.key.includes('frame')?17:18,barY=y+6;
        if(barY<TRACK_TOP)continue;
        const picked=S.selected&&String(e.event_id)===String(S.selected.event_id),dim=matches&&!matches.has(String(e.event_id));ctx.globalAlpha=dim?.18:1;
        const color=C[e.kind==='frame'?'frame':e.category]||C.unknown;
        if(isPoint){ctx.fillStyle=color;ctx.beginPath();ctx.moveTo(x,barY);ctx.lineTo(x+4,barY+5);ctx.lineTo(x,barY+10);ctx.lineTo(x-4,barY+5);ctx.closePath();ctx.fill();w=8;x-=4;}
        else if(e.kind==='request'){ctx.fillStyle='#e2f3ef';ctx.fillRect(x,barY,w,h);ctx.strokeStyle=picked?'#075c68':'#5aafa7';ctx.setLineDash([4,2]);ctx.strokeRect(x+.5,barY+.5,Math.max(0,w-1),h-1);ctx.setLineDash([]);}
        else if(open){ctx.fillStyle='#fbf3df';ctx.fillRect(x,barY,w,h);ctx.save();ctx.beginPath();ctx.rect(x,barY,w,h);ctx.clip();ctx.strokeStyle='#d8bc76';for(let k=x-h;k<x+w+h;k+=6){ctx.beginPath();ctx.moveTo(k,barY+h);ctx.lineTo(k+h,barY);ctx.stroke();}ctx.restore();ctx.strokeStyle='#baa366';ctx.strokeRect(x+.5,barY+.5,w-1,h-1);}
        else {ctx.fillStyle=color;ctx.fillRect(x,barY,w,h);}
        if(picked){ctx.strokeStyle='#153e54';ctx.lineWidth=1.5;ctx.strokeRect(x-.5,barY-.5,w+1,h+1);ctx.lineWidth=1;}
        if(!isPoint&&!open&&w>33){ctx.save();ctx.beginPath();ctx.rect(x+4,barY,w-8,h);ctx.clip();ctx.font='9px "Segoe UI","Microsoft YaHei",sans-serif';ctx.textAlign='left';ctx.fillStyle=e.kind==='request'?'#36897f':'#fff';ctx.fillText(String(e.name||e.event_id),Math.max(x+5,left+4),barY+12);ctx.restore();}
        S.hits.push({x:Math.max(left,x),y:barY,w:Math.min(width,x+w)-Math.max(left,x),h,event:e,lane:i});ctx.globalAlpha=1;
      }
      ctx.restore();
      ctx.fillStyle=lane.first?'#f7fafb':'#fbfcfd';ctx.fillRect(0,Math.max(TRACK_TOP,y),left-1,rowHeight);ctx.save();ctx.beginPath();ctx.rect(12,Math.max(TRACK_TOP,y),left-22,rowHeight);ctx.clip();
      ctx.textAlign='left';if(lane.first){ctx.fillStyle='#426478';ctx.font='10px "Segoe UI","Microsoft YaHei",sans-serif';ctx.fillText(lane.thread.name||'线程 '+lane.thread.thread_id,13,y+13);ctx.fillStyle='#9aaeba';ctx.font='8px Consolas,monospace';ctx.fillText(lane.label+' · '+(lane.thread.tid??lane.thread.thread_id),13,y+25);}else{ctx.fillStyle='#92a7b5';ctx.font='9px "Segoe UI","Microsoft YaHei",sans-serif';ctx.fillText(lane.label,23,y+20);}ctx.restore();
    }
    if(S.selection){const a=Math.max(left,tx(S.selection[0])),b=Math.min(left+plot,tx(S.selection[1]));if(b>a){ctx.fillStyle='#0c89950b';ctx.fillRect(a,TRACK_TOP,b-a,height-TRACK_TOP);ctx.strokeStyle='#12849188';ctx.setLineDash([3,3]);for(const x of [a,b]){ctx.beginPath();ctx.moveTo(x,TRACK_TOP);ctx.lineTo(x,height);ctx.stroke();}ctx.setLineDash([]);}}
    drawFlow(tx,left,height,scroll);
    drawOverlapSummary(tx,left,plot,width);
    ctx.fillStyle='#f6f9fb';ctx.fillRect(0,0,left-1,AXIS_HEIGHT);ctx.strokeStyle='#dde7ed';ctx.beginPath();ctx.moveTo(0,AXIS_HEIGHT-.5);ctx.lineTo(width,AXIS_HEIGHT-.5);ctx.moveTo(left-.5,0);ctx.lineTo(left-.5,height);ctx.stroke();ctx.font='9px "Segoe UI","Microsoft YaHei",sans-serif';ctx.textAlign='left';ctx.fillStyle='#7d94a4';ctx.fillText('进程 / 逻辑线程',13,20);
    setText('viewportCaption','视窗 '+fmt(S.view[0],2)+' — '+fmt(S.view[1],2));
  }
  function drawOverlapSummary(tx,left,plot,width){
    const stats=S.stats,intervals=stats&&stats.intervals||{compute:[],requests:[],overlap:[],parallel:[]};
    const rows=[{title:'计算 ∩ 请求',items:intervals.overlap||[],total:stats?stats.overlap_ns:0,color:'#087f8c',background:'#edf8f5',labelBackground:'#e7f3ef',note:'软件语义重叠',empty:(intervals.requests||[]).length?'当前选区无已证实重叠':'当前选区没有已闭合请求'},
      {title:'计算线程并行',items:intervals.parallel||[],total:stats?stats.compute_parallel_ns:0,color:'#526fba',background:'#f1f4fb',labelBackground:'#e9eef9',note:'至少两个不同线程',empty:'当前选区无已证实的计算线程并行'}];
    rows.forEach((row,index)=>{const top=AXIS_HEIGHT+index*SUMMARY_ROW_HEIGHT;
      ctx.fillStyle=row.background;ctx.fillRect(0,top,width,SUMMARY_ROW_HEIGHT);ctx.strokeStyle='#d8e3e9';ctx.beginPath();ctx.moveTo(0,top+SUMMARY_ROW_HEIGHT-.5);ctx.lineTo(width,top+SUMMARY_ROW_HEIGHT-.5);ctx.stroke();
      ctx.save();ctx.beginPath();ctx.rect(left,top,plot,SUMMARY_ROW_HEIGHT);ctx.clip();
      for(const [a,b] of row.items){if(b<S.view[0]||a>S.view[1])continue;const x=tx(a),w=Math.max(1.5,tx(b)-x);ctx.fillStyle=row.color;ctx.fillRect(x,top+9,w,20);if(w>64){ctx.font='9px Consolas,"Microsoft YaHei",sans-serif';ctx.textAlign='left';ctx.fillStyle='#fff';ctx.fillText(fmt(b-a),Math.max(left+4,x+5),top+23);}}
      if(!row.items.length){ctx.font='10px "Segoe UI","Microsoft YaHei",sans-serif';ctx.textAlign='left';ctx.fillStyle='#7b929f';ctx.fillText(row.empty,left+10,top+23);}
      ctx.restore();ctx.fillStyle=row.labelBackground;ctx.fillRect(0,top,left-1,SUMMARY_ROW_HEIGHT);ctx.textAlign='left';ctx.fillStyle=row.color;ctx.font='10px "Segoe UI","Microsoft YaHei",sans-serif';ctx.fillText(row.title+' · '+fmt(row.total),13,top+15);ctx.fillStyle='#768e9f';ctx.font='8px "Segoe UI","Microsoft YaHei",sans-serif';ctx.fillText(stats&&stats.known_subset_only?'选区内 · 已知下界':row.note,13,top+29);
    });
  }
  function drawFlow(tx,left,height,scroll){
    if(!S.selected)return;let request=S.selected.kind==='request'?S.selected:null;const args=S.selected.args||{};
    if(!request&&args.request_id)request=S.events.find(e=>e.kind==='request'&&(String((e.args||{}).request_id)===String(args.request_id)||String(e.event_id)===String(args.request_id)));
    if(!request||!A.closed(request))return;
    const lane=S.lanes.findIndex(l=>l.events.includes(request));if(lane<0)return;const y=TRACK_TOP+4+lane*ROW_HEIGHT-scroll+5,x0=tx(request.begin_ts),x1=tx(request.end_ts);
    if(y<TRACK_TOP+2||y>height)return;ctx.save();ctx.beginPath();ctx.rect(left,TRACK_TOP,Math.max(0,canvas.clientWidth-left),height-TRACK_TOP);ctx.clip();ctx.strokeStyle='#0c786f';ctx.fillStyle='#0c786f';ctx.lineWidth=1;ctx.setLineDash([3,2]);ctx.beginPath();ctx.moveTo(x0,y-3);ctx.lineTo(x1,y-3);ctx.stroke();ctx.setLineDash([]);ctx.beginPath();ctx.moveTo(x1-4,y-6);ctx.lineTo(x1,y-3);ctx.lineTo(x1-4,y);ctx.stroke();ctx.beginPath();ctx.arc(x0,y-3,2,0,Math.PI*2);ctx.fill();ctx.restore();
  }
  function position(event){const r=canvas.getBoundingClientRect();return{x:event.clientX-r.left,y:event.clientY-r.top};}
  function timeAt(x){const g=canvasSize();return Math.max(S.bounds[0],Math.min(S.bounds[1],S.view[0]+(x-g.left)/g.plot*(S.view[1]-S.view[0])));}
  function hitAt(p){for(let i=S.hits.length-1;i>=0;i--){const h=S.hits[i];if(p.x>=h.x-2&&p.x<=h.x+h.w+2&&p.y>=h.y&&p.y<=h.y+h.h)return h;}return null;}
  function setView(a,b){const span=Math.min(S.bounds[1]-S.bounds[0],Math.max(1,b-a));a=Math.max(S.bounds[0],Math.min(a,S.bounds[1]-span));S.view=[a,a+span];scheduleDraw();}
  function zoom(factor,center){const c=center??(S.view[0]+S.view[1])/2,span=(S.view[1]-S.view[0])*factor,ratio=(c-S.view[0])/(S.view[1]-S.view[0]);setView(c-span*ratio,c+span*(1-ratio));}
  function choose(e,jump){S.selected=e;renderDetails();if(jump){const end=A.closed(e)?e.end_ts:e.begin_ts+(S.bounds[1]-S.bounds[0])*.01,span=Math.max(end-e.begin_ts,1),pad=Math.max(span*.3,(S.bounds[1]-S.bounds[0])*.002);setView(e.begin_ts-pad,end+pad);const lane=S.lanes.findIndex(l=>l.events.includes(e));if(lane>=0)scroller.scrollTop=Math.max(0,lane*ROW_HEIGHT-ROW_HEIGHT);}scheduleDraw();}
  function renderDetails(){
    const detail=empty($('eventDetail')),e=S.selected;$('eventEvidence').hidden=!e;
    if(!e){detail.className='detail-empty';detail.append(el('span','','↖'));const p=el('p');p.append(document.createTextNode('点击时间线上的范围'),document.createElement('br'),document.createTextNode('查看属性、层级与关联请求'));detail.append(p);return;}
    detail.className='detail-content';setText('eventEvidence',e.evidence||'observed');detail.append(el('div','detail-title',e.name||e.event_id));
    const chips=el('div','detail-chips');chips.append(el('span','detail-chip',kindNames[e.kind]||e.kind),el('span','detail-chip',e.category||'unknown'),el('span','detail-chip'+(!A.closed(e)?' warn':''),A.closed(e)?'closed / observed':'incomplete · end=null'));detail.append(chips);
    const props=el('dl','property-grid');const prop=(name,value,target)=>{props.append(el('dt','',name));const dd=el('dd');if(target){const button=el('button','',value);button.addEventListener('click',()=>choose(target,true));dd.append(button);}else dd.textContent=String(value??'unknown');props.append(dd);};
    prop('Event ID',e.event_id);prop('线程 / TID',e.thread_id+' / '+(e.tid??'unknown'));prop('开始时间',ms(e.begin_ts)+' ms · '+e.begin_ts+' ns');prop('结束时间',A.closed(e)?ms(e.end_ts)+' ms · '+e.end_ts+' ns':'null · 未观测到结束');prop('墙钟包含时间',A.closed(e)?fmt(e.end_ts-e.begin_ts):'N/A · 不补结束点');
    const owner=frameOf(e);if(owner&&owner!==e){prop('所属帧',owner.name||owner.event_id,owner);prop('帧内起止',ms(e.begin_ts-owner.begin_ts)+' → '+(A.closed(e)?ms(e.end_ts-owner.begin_ts):'unknown')+' ms');}
    if(A.closed(e)){const own=A.ownWall(S.session,e,S.selection[0],S.selection[1]);prop('选区包含 / 自身',fmt(own.inclusive_ns)+' / '+fmt(own.self_wall_ns));}
    if(e.parent_id)prop('父范围',e.parent_id,S.byId.get(String(e.parent_id)));if(e.frame_id)prop('Frame ID',e.frame_id);
    prop('来源',e.source_id||'unknown');
    const args=e.args||{};if(e.kind==='request'){prop('提交 → 完成观测',fmt(e.begin_ts)+' → '+(A.closed(e)?fmt(e.end_ts):'unknown'));prop('逻辑字节',args.bytes===undefined?'unknown':number(args.bytes)+' B');prop('完成线程',args.completion_tid??'unknown');}
    if(e.kind==='wait')prop('等待对象',args.wait_object||args.object||args.request_id||'unknown');
    const children=S.events.filter(x=>String(x.parent_id)===String(e.event_id));if(children.length){const dt=el('dt','','子范围'),dd=el('dd');for(const child of children.slice(0,12)){const button=el('button','',(child.name||child.event_id)+' ↗');button.style.display='block';button.addEventListener('click',()=>choose(child,true));dd.append(button);}if(children.length>12)dd.append(document.createTextNode('… '+children.length+' 个'));props.append(dt,dd);}
    const requestId=args.request_id||args.wait_object;const related=requestId?S.events.find(x=>x.kind==='request'&&(String((x.args||{}).request_id)===String(requestId)||String(x.event_id)===String(requestId))):null;if(related&&related!==e)prop('关联请求',related.name||related.event_id,related);
    detail.append(props);if(Object.keys(args).length)detail.append(el('pre','detail-args',JSON.stringify(args,null,2)));
    const controls=el('div','detail-controls');if(A.closed(e)&&e.end_ts>e.begin_ts){const button=el('button','button compact','设为选区');button.addEventListener('click',()=>{S.selection=[e.begin_ts,e.end_ts];updateStats();scheduleDraw();});controls.append(button);}const locate=el('button','button compact','定位此范围');locate.addEventListener('click',()=>choose(e,true));controls.append(locate);detail.append(controls);
  }
  function updateSearch(jump){const q=$('search').value.trim().toLowerCase();S.matches=q?S.events.filter(e=>S.threadIds.has(String(e.thread_id))&&frameSelected(e)&&[e.name,e.event_id,e.frame_id,JSON.stringify(e.args||{})].join(' ').toLowerCase().includes(q)):[];S.matchIndex=S.matches.length?0:-1;setText('searchCount',q?S.matches.length?'1/'+S.matches.length:'0/0':'');if(jump&&S.matches.length)choose(S.matches[0],true);scheduleDraw();}
  function nextMatch(delta){if(!S.matches.length)return;S.matchIndex=(S.matchIndex+delta+S.matches.length)%S.matches.length;setText('searchCount',(S.matchIndex+1)+'/'+S.matches.length);choose(S.matches[S.matchIndex],true);}
  function selectionEvents(){const [a,b]=S.selection,ids=new Set(),direct=S.events.filter(e=>S.threadIds.has(String(e.thread_id))&&frameSelected(e)&&e.begin_ts<b&&(!A.closed(e)||e.end_ts>a)),directIds=new Set(direct.map(e=>String(e.event_id)));for(const e of direct){ids.add(String(e.event_id));let p=e.parent_id,guard=0;while(p&&guard++<50){ids.add(String(p));const parent=S.byId.get(String(p));p=parent&&parent.parent_id;}}return S.events.filter(e=>ids.has(String(e.event_id))).map(e=>({...e,selection_context:!directIds.has(String(e.event_id))}));}
  function selectionPayload(){return{schema_version:'lxtrace.selection.v1',session_id:S.session.session_id,data_origin:S.session.data_origin,interval:S.selection.slice(),frame_ids:S.frameFilter==='all'?[]:[S.frameFilter],statistics:S.stats,events:selectionEvents(),clock:S.session.clock,capability_manifest:S.session.capability_manifest,quality:S.session.quality,provenance:S.session.provenance,note:'原始事件边界保留；selection_context 为必要父级上下文。'};}
  function exportCsv(){const escape=v=>{let s=v===undefined||v===null?'':typeof v==='object'?JSON.stringify(v):String(v);if(/^[=+@\-\t\r]/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"';};const header=['event_id','thread_id','kind','category','name','begin_ts','end_ts','status','parent_id','selection_context','clipped_begin_ns','clipped_end_ns'];const rows=selectionEvents().map(e=>{const c={...e,clipped_begin_ns:Math.max(e.begin_ts,S.selection[0]),clipped_end_ns:A.closed(e)?Math.min(e.end_ts,S.selection[1]):null};return header.map(k=>escape(c[k])).join(',');});const meta=[['# session_id',S.session.session_id],['# interval_ns',JSON.stringify(S.selection)],['# statistics',JSON.stringify(S.stats)],['# quality',JSON.stringify(S.session.quality)],['# capability_manifest',JSON.stringify(S.session.capability_manifest)],['# provenance',JSON.stringify(S.session.provenance)]].map(row=>row.map(escape).join(','));download(fileName('-selection.csv'),'\ufeff'+meta.concat([header.join(',')],rows).join('\r\n'),'text/csv;charset=utf-8');}
  function exportTrace(){const trace=[];for(const e of selectionEvents()){const base={name:e.name||e.event_id,cat:e.category,pid:e.pid||0,tid:e.thread_id,ts:e.begin_ts/1000,args:{...e.args,event_id:e.event_id,source_id:e.source_id,evidence:e.evidence,status:e.status,end_ts:e.end_ts,selection_context:e.selection_context}};if(A.closed(e)&&e.end_ts>e.begin_ts){trace.push({...base,ph:'X',dur:(e.end_ts-e.begin_ts)/1000});if(e.kind==='request'){const completionThread=S.session.threads.find(t=>t.tid===(e.args||{}).completion_tid&&(t.pid===e.pid||t.pid===undefined));trace.push({...base,ph:'s',id:e.event_id});trace.push({...base,ts:e.end_ts/1000,tid:completionThread?completionThread.thread_id:e.thread_id,ph:'f',bp:'e',id:e.event_id});}}else trace.push({...base,ph:'i',s:'t',args:{...base.args,incomplete:!A.closed(e)}});}download(fileName('-trace.json'),JSON.stringify({traceEvents:trace,displayTimeUnit:'ms',lxtrace:{session_id:S.session.session_id,interval:S.selection,statistics:S.stats,clock:S.session.clock,quality:S.session.quality,capability_manifest:S.session.capability_manifest,provenance:S.session.provenance}},null,2));}
  async function exportHtml(){
    try{const clone=bootstrapDocument.cloneNode(true);clone.querySelectorAll('#toast,#tooltip').forEach(n=>{n.hidden=true;n.replaceChildren();});clone.querySelectorAll('script[data-lxtrace-session]').forEach(n=>n.remove());
      for(const link of Array.from(clone.querySelectorAll('link[rel="stylesheet"]'))){const style=el('style');let text='';for(const sheet of document.styleSheets){try{if(sheet.href&&sheet.href===new URL(link.getAttribute('href'),location.href).href){text=Array.from(sheet.cssRules).map(r=>r.cssText).join('\n');break;}}catch(_){}}if(!text){const response=await fetch(link.href);if(!response.ok)throw Error('无法读取样式文件');text=await response.text();}style.textContent=text;link.replaceWith(style);}
      for(const script of Array.from(clone.querySelectorAll('script[src]'))){const original=script.getAttribute('src');const response=await fetch(new URL(original,location.href));if(!response.ok)throw Error('无法读取 '+original);script.textContent=await response.text();script.removeAttribute('src');}
      const data=el('script');data.setAttribute('data-lxtrace-session','');data.textContent='window.LXTRACE_SESSION='+safeJson(S.session)+';';const first=clone.querySelector('script');if(first)first.before(data);else clone.querySelector('body').append(data);
      clone.querySelector('#helpDialog').removeAttribute('open');download(fileName('-offline.html'),'<!doctype html>\n'+clone.outerHTML,'text/html;charset=utf-8');toast('已导出完整会话离线 HTML；选区统计请用 JSON / CSV 保存。');
    }catch(error){toast('离线 HTML 导出失败：'+error.message+'。请使用 CLI export --format html 生成。');}
  }
  async function importFile(file){if(!file)return;try{const start=performance.now();const session=JSON.parse(await file.text());load(session);toast('已载入 '+number(S.events.length)+' 个事件 · '+Math.round(performance.now()-start)+' ms');}catch(error){toast('导入失败：'+error.message);}}
  $('importButton').addEventListener('click',()=>$('fileInput').click());$('emptyImport').addEventListener('click',()=>$('fileInput').click());$('fileInput').addEventListener('change',event=>{importFile(event.target.files[0]);event.target.value='';});
  $('threadSearch').addEventListener('input',()=>S.session&&renderThreads());$('allThreads').addEventListener('click',()=>{if(!S.session)return;S.threadIds=new Set(S.session.threads.map(t=>String(t.thread_id)));renderThreads();buildLanes();updateSearch(false);updateStats();scheduleDraw();});
  $('trackDisplay').addEventListener('change',()=>{if(!S.session)return;scroller.scrollTop=0;buildLanes();scheduleDraw();});
  $('frameFilter').addEventListener('change',()=>{if(!S.session)return;S.frameFilter=$('frameFilter').value;const frame=S.byId.get(String(S.frameFilter));if(frame){$('trackDisplay').value='frame';const bounds=filteredFrameBounds(frame);S.selection=A.closed(frame)&&frame.end_ts>frame.begin_ts?[frame.begin_ts,frame.end_ts]:bounds;setView(bounds[0],bounds[1]);}else{$('trackDisplay').value='all';S.selection=S.bounds.slice();S.view=S.bounds.slice();}S.selected=frame||null;scroller.scrollTop=0;buildLanes();updateSearch(false);updateStats();renderCapabilities();renderDetails();scheduleDraw();});
  $('applySelection').addEventListener('click',()=>{if(!S.session)return;const a=Number($('selectionFrom').value)*1e6,b=Number($('selectionTo').value)*1e6;if(!Number.isFinite(a)||!Number.isFinite(b)||a<0||b<=a){toast('选区需要满足 0 ≤ 起点 < 终点。');return;}S.selection=[a,b];updateStats();scheduleDraw();});
  $('clearSelection').addEventListener('click',()=>{S.selection=S.bounds.slice();updateStats();scheduleDraw();});for(const id of ['selectionFrom','selectionTo'])$(id).addEventListener('keydown',e=>{if(e.key==='Enter')$('applySelection').click();});
  $('zoomIn').addEventListener('click',()=>zoom(.6));$('zoomOut').addEventListener('click',()=>zoom(1.7));$('fitView').addEventListener('click',()=>{const frame=S.byId.get(String(S.frameFilter));S.view=frame?filteredFrameBounds(frame):S.bounds.slice();scheduleDraw();});$('panLeft').addEventListener('click',()=>{const d=(S.view[1]-S.view[0])*.3;setView(S.view[0]-d,S.view[1]-d);});$('panRight').addEventListener('click',()=>{const d=(S.view[1]-S.view[0])*.3;setView(S.view[0]+d,S.view[1]+d);});
  let searchTimer=0;$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>updateSearch(true),160);});$('search').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();nextMatch(e.shiftKey?-1:1);}});$('nextMatch').addEventListener('click',()=>nextMatch(1));$('prevMatch').addEventListener('click',()=>nextMatch(-1));
  canvas.addEventListener('pointerdown',e=>{if(!S.session)return;const p=position(e);if(p.x<canvasSize().left||p.y<AXIS_HEIGHT)return;canvas.setPointerCapture(e.pointerId);S.pointer={x:p.x,y:p.y,startTime:timeAt(p.x),view:S.view.slice(),shift:e.shiftKey,moved:false,hit:hitAt(p)};$('tooltip').hidden=true;});
  canvas.addEventListener('pointermove',e=>{if(!S.session)return;const p=position(e);if(S.pointer){const q=S.pointer;if(Math.abs(p.x-q.x)>3)q.moved=true;if(q.shift){const delta=(p.x-q.x)/canvasSize().plot*(q.view[1]-q.view[0]);setView(q.view[0]-delta,q.view[1]-delta);}else if(q.moved){const t=timeAt(p.x);S.selection=[Math.min(t,q.startTime),Math.max(t,q.startTime)];if(S.selection[1]<=S.selection[0])S.selection[1]=S.selection[0]+1;if(!q.lastStats||performance.now()-q.lastStats>120){updateStats();q.lastStats=performance.now();}scheduleDraw();}return;}const hit=hitAt(p);canvas.style.cursor=hit?'pointer':'crosshair';if(hit){const event=hit.event;$('tooltip').textContent=(event.name||event.event_id)+'\n'+(kindNames[event.kind]||event.kind)+' · '+(A.closed(event)?fmt(event.end_ts-event.begin_ts):'未闭合 · 耗时 N/A')+'\n'+fmt(event.begin_ts)+' → '+(A.closed(event)?fmt(event.end_ts):'end_ts = null');$('tooltip').hidden=false;const tw=$('tooltip').offsetWidth,th=$('tooltip').offsetHeight;$('tooltip').style.left=Math.min(window.innerWidth-tw-10,e.clientX+14)+'px';$('tooltip').style.top=Math.min(window.innerHeight-th-10,e.clientY+14)+'px';}else $('tooltip').hidden=true;});
  canvas.addEventListener('pointerup',e=>{if(!S.pointer)return;const p=S.pointer;S.pointer=null;if(!p.moved&&p.hit)choose(p.hit.event,false);else if(p.moved&&!p.shift)updateStats();try{canvas.releasePointerCapture(e.pointerId);}catch(_){}scheduleDraw();});canvas.addEventListener('pointercancel',()=>{S.pointer=null;});canvas.addEventListener('pointerleave',()=>{$('tooltip').hidden=true;});
  canvas.addEventListener('wheel',e=>{if(!S.session||position(e).x<canvasSize().left)return;if(e.shiftKey){e.preventDefault();const d=(e.deltaY||e.deltaX)/1000*(S.view[1]-S.view[0]);setView(S.view[0]+d,S.view[1]+d);}else if(Math.abs(e.deltaY)>0){e.preventDefault();zoom(e.deltaY>0?1.18:1/1.18,timeAt(position(e).x));}},{passive:false});scroller.addEventListener('scroll',scheduleDraw,{passive:true});
  $('exportJson').addEventListener('click',()=>download(fileName('-selection.json'),JSON.stringify(selectionPayload(),null,2)));$('exportCsv').addEventListener('click',exportCsv);$('exportTrace').addEventListener('click',exportTrace);$('exportHtml').addEventListener('click',exportHtml);$('downloadSession').addEventListener('click',()=>download(fileName('.lxtrace'),JSON.stringify(S.session)));
  $('toggleCapabilities').addEventListener('click',()=>{const details=$('capabilityDetails');details.hidden=!details.hidden;setText('toggleCapabilities',details.hidden?'展开详情':'收起详情');});
  $('qualityNav').addEventListener('click',()=>{if(!S.session){toast('请先导入会话。');return;}$('capabilityDetails').hidden=false;setText('toggleCapabilities','收起详情');const top=$('capabilityPanel').getBoundingClientRect().top+window.scrollY-84;window.scrollTo({top,behavior:'smooth'});});$('timelineNav').addEventListener('click',()=>window.scrollTo({top:0,behavior:'smooth'}));
  $('helpButton').addEventListener('click',()=>$('helpDialog').showModal());$('closeHelp').addEventListener('click',()=>$('helpDialog').close());$('helpDialog').addEventListener('click',e=>{if(e.target===$('helpDialog'))$('helpDialog').close();});
  document.addEventListener('keydown',e=>{if(['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)||!S.session)return;if(e.key.toLowerCase()==='f'){e.preventDefault();$('fitView').click();}if(e.key==='Escape'){S.selected=null;renderDetails();scheduleDraw();}if(e.key==='+'||e.key==='=')zoom(.7);if(e.key==='-')zoom(1.5);});
  document.addEventListener('dragenter',e=>{if(e.dataTransfer.types.includes('Files')){e.preventDefault();dragDepth++;document.body.classList.add('drop-hover');}});document.addEventListener('dragleave',e=>{e.preventDefault();if(--dragDepth<=0)document.body.classList.remove('drop-hover');});document.addEventListener('dragover',e=>{if(e.dataTransfer.types.includes('Files'))e.preventDefault();});document.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;document.body.classList.remove('drop-hover');importFile(e.dataTransfer.files[0]);});
  if(window.ResizeObserver)new ResizeObserver(()=>{if(S.session){buildLanes();scheduleDraw();}}).observe(scroller);window.addEventListener('resize',scheduleDraw);
  if(window.LXTRACE_SESSION){try{load(window.LXTRACE_SESSION);}catch(error){toast('会话载入失败：'+error.message);}}
  else if(location.protocol==='http:'||location.protocol==='https:')fetch('./demo-session.json').then(r=>{if(!r.ok)throw Error('未提供默认会话');return r.json();}).then(load).catch(()=>{});
  window.LXViewer={load,analyze:()=>S.stats,getState:()=>({session:S.session,selection:S.selection.slice(),view:S.view.slice(),lane_count:S.lanes.length,event_count:S.events.length,frame_filter:S.frameFilter,lanes:S.lanes.map(l=>({label:l.label,event_ids:l.events.map(e=>e.event_id)}))})};
})();
