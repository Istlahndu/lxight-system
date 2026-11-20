"""Project normalized software intervals into the built-in pipeline viewer."""
from collections import Counter, defaultdict
from itertools import groupby
import json
from pathlib import Path
import re


TEMPLATE = Path(__file__).resolve().parents[1] / 'web' / 'pipeline.html'
DATA = '<!--LXTRACE_PIPELINE_DATA-->'
DRIVER = re.compile(r'die\.(\d+)\.driver\Z')
WORKER = re.compile(r'die(\d+)\.domain\d+\.cpu\d+\Z')
PIPE_MARKER = {'pipeline.wait_input': '等输入', 'pipeline.wait_slot': '等槽位',
               'pipeline.push': '向下游推送', 'pipeline.embed': 'Embedding', 'pipeline.head': '输出头'}
PREFILL_STAGE_LABELS = {
    'gate_up': '融合 MoE（gate_up）', 'dsa_attn': 'DSA 注意力（含重叠投影）',
    'dsa_oproj': 'DSA 输出投影', 'dsa_q': 'DSA Q 投影', 'dsa_qkv': 'DSA 低秩 Q/K/V',
    'kda_proj': 'KDA 输入投影', 'kda_core': 'KDA 核心', 'kda_oproj': 'KDA 输出投影',
    'kda_fb_conv': 'KDA 卷积与门控', 'hc_wait': 'mHC 交换等待', 'hc_push': 'mHC 跨域推送',
    'router': 'MoE 路由', 'moe_sum': 'MoE 跨域归并',
}


def _order(row):
    return row['begin_ns'], -row['end_ns'], row['record_id']


def _category(row):
    kind, category = row.get('kind'), row['category']
    if kind == 'wait' or category == 'wait':
        return 'wait'
    if kind == 'request' or category == 'request':
        return 'request'
    if kind == 'exchange' or category in ('cx', 'comm_x'):
        return 'transfer'
    stage = (row.get('args') or {}).get('stage')
    if category == 'engine.stage' and isinstance(stage, str):
        if stage == 'hc_wait':
            return 'wait'
        if stage == 'hc_push':
            return 'transfer'
        if stage.startswith(('dsa_', 'kda_')):
            return stage.split('_', 1)[0]
        if stage in ('router', 'group', 'gate_up', 'combine', 'moe_sum'):
            return 'moe'
        if stage in ('dense_gemm', 'swiglu_ag'):
            return 'ffn'
        if stage.startswith('hc_') or stage in ('entry', 'attn_exit', 'ffn_exit'):
            return 'mhc'
    return category if category in ('mat', 'vec', 'cc') else 'other'


def _contains(outer, inner):
    return outer['begin_ns'] <= inner['begin_ns'] <= inner['end_ns'] <= outer['end_ns']


def _same_frame(row, args):
    value = args.get('microbatch')
    if type(value) is int and type(args.get('pp_stage')) is int:
        return f'microbatch:{value}', f'微批 {value}'
    value = args.get('compact_aux')
    if type(value) is int and args.get('scope') == 'die_driver':
        return f'prefill:{value}', f'帧 {value}'
    return row['record_id'], row.get('name') or '帧'


def _operator_group(row):
    args = row.get('args') or {}
    if type(args.get('layer')) is int:
        return f'L{args["layer"]:02d}'
    parts = (row.get('name') or '阶段').split('.')
    return '.'.join(parts[:2]) if len(parts) > 1 and parts[1].isdigit() else parts[0]


def _track(frame, resource):
    name = resource.get('name') or resource['resource_id']
    args = frame.get('args') or {}
    if type(args.get('pp_stage')) is int:
        stage = args['pp_stage']
        rank = args.get('tp_rank')
        label = f'TP {rank}' if type(rank) is int else name
        return f'pp:{stage}', f'PP {stage}', (0, stage), label, rank if type(rank) is int else 0
    match = DRIVER.fullmatch(name)
    if match and args.get('scope') == 'die_driver':
        die = int(match[1])
        return f'die:{die}', f'die {die}', (1, die), 'driver', 0
    return resource['resource_id'], name, (2, name), name, 0


def _compact_layer(row, resource_name, is_driver):
    args = row.get('args') or {}
    match = (DRIVER if is_driver else WORKER).fullmatch(resource_name)
    layer, frame = args.get('layer'), args.get('compact_aux')
    if match and type(frame) is int and type(layer) is int:
        return row['source_id'], int(match[1]), frame, layer
    return None


def _reference(row, intern_ref):
    return intern_ref(', '.join(row.get('raw_refs', ())) or row['record_id'])


def _encoded_group(label, rows, category, intern_name, intern_ref, *, outer=None, source=None):
    begin = outer['begin_ns'] if outer else min(r['begin_ns'] for r in rows)
    end = outer['end_ns'] if outer else max(r['end_ns'] for r in rows)
    packed = []
    for row in rows:
        name = (row.get('args') or {}).get('stage') if row['category'] == 'engine.stage' else None
        name = name or row.get('name') or row['category']
        packed.append([intern_name(name), _category(row), row['begin_ns'] - begin,
                       row['end_ns'] - row['begin_ns'], _reference(row, intern_ref)])
    result = dict(label=label, category=category, begin_ns=begin, end_ns=end, records=packed)
    if outer is not None:
        result['ref'] = _reference(outer, intern_ref)
    if source is not None:
        result['source'] = source
    return result


def _choose_default(frames):
    measured = [f for f in frames if f.get('observed', True)] or frames
    if not measured:
        return {}
    tracks = defaultdict(list)
    for row in measured:
        tracks[row['lane'], row['track']].append(row)

    def service(rows):
        occupied = sum(f['end_ns'] - f['begin_ns'] - sum(
            m['end_ns'] - m['begin_ns'] for m in f['markers'] if m['category'] == 'wait')
            for f in rows)
        return occupied / len(rows)

    selected = max(tracks.values(), key=lambda rows: (service(rows), len(rows)))
    median = sorted(selected, key=lambda row: row['begin_ns'])[(len(selected) - 1) // 2]
    groups = median['groups']
    group = max(range(len(groups)), key=lambda i: groups[i]['end_ns'] - groups[i]['begin_ns']) if groups else None
    return dict(lane=median['lane'], track=median['track'], key=median['key'], group=group)


def pipeline_data(session):
    resources = {r['resource_id']: r for r in session['resources']}
    software = [r for r in session['records'] if r['layer'] == 'software' and r['representation'] == 'interval']
    closed = sorted((r for r in software if r['end_ns'] is not None), key=_order)
    frame_rows = {r['record_id']: r for r in closed if r.get('kind') == 'frame' or r['category'] == 'frame'}
    by_frame, compact_workers, unframed = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in closed:
        if row['record_id'] in frame_rows:
            continue
        frame = frame_rows.get(row.get('frame_id') or row.get('parent_id'))
        if frame and _contains(frame, row):
            by_frame[frame['record_id']].append(row)
            continue
        resource = resources[row['resource_id']]
        key = _compact_layer(row, resource.get('name', ''), False) if row['category'] == 'engine.stage' else None
        (compact_workers[key] if key is not None else unframed[row['resource_id']]).append(row)

    lanes, names, refs, frames = {}, {}, {}, []
    frame_keys = Counter()
    outer_layers = Counter(
        key for frame, rows in by_frame.items() for row in rows
        if row.get('name', '').startswith('prefill.layer.') and
        (key := _compact_layer(row, resources[frame_rows[frame]['resource_id']].get('name', ''), True)) is not None
    )

    def intern_name(name):
        return names.setdefault(name, len(names))

    def intern_ref(ref):
        return refs.setdefault(ref, len(refs))

    def append_frame(raw, resource_id, observed, rows):
        resource = resources[resource_id]
        args = raw.get('args') or {}
        lane_id, label, order, tag, rank = _track(raw, resource)
        lane = lanes.setdefault(lane_id, dict(id=lane_id, label=label, order=order, tracks={}))
        lane['tracks'][resource_id] = dict(id=resource_id, label=tag, order=rank)
        key, frame_label = _same_frame(raw, args) if observed else (raw['record_id'], '无帧标记')
        identity = (lane_id, resource_id, key)
        frame_keys[identity] += 1
        if frame_keys[identity] > 1:
            key = raw['record_id']
        frame = dict(lane=lane_id, track=resource_id, key=key, label=frame_label, observed=observed,
                     begin_ns=raw['begin_ns'], end_ns=raw['end_ns'], groups=[], markers=[])
        if observed:
            frame['ref'] = _reference(raw, intern_ref)
        standalone = []
        for row in rows:
            name = row.get('name', '')
            if name in PIPE_MARKER:
                frame['markers'].append(dict(label=PIPE_MARKER[name], category=_category(row),
                                             begin_ns=row['begin_ns'], end_ns=row['end_ns'],
                                             ref=_reference(row, intern_ref)))
                continue
            key = _compact_layer(row, resource.get('name', ''), True) if name.startswith('prefill.layer.') else None
            if key is None:
                standalone.append(row)
                continue
            # Compact aux is scoped by source, die, frame and layer. Require
            # a unique driver layer and actual containment before joining it.
            candidates = compact_workers.pop(key, []) if outer_layers[key] == 1 else []
            inside = []
            for child in candidates:
                if _contains(row, child):
                    inside.append(child)
                else:
                    unframed[child['resource_id']].append(child)
            source = resources[inside[0]['resource_id']].get('name') if inside else None
            frame['groups'].append(_encoded_group(_operator_group(row), inside, 'layer', intern_name,
                                                  intern_ref, outer=row, source=source))
        for name, segment in groupby(standalone, key=_operator_group):
            rows = list(segment)
            categories = {_category(r) for r in rows}
            category = next(iter(categories)) if len(categories) == 1 else 'layer'
            frame['groups'].append(_encoded_group(name, rows, category, intern_name, intern_ref))
        frame['groups'].sort(key=lambda group: (group['begin_ns'], -group['end_ns']))
        frames.append(frame)

    for raw in frame_rows.values():
        append_frame(raw, raw['resource_id'], True, by_frame[raw['record_id']])
    for rows in compact_workers.values():
        for row in rows:
            unframed[row['resource_id']].append(row)
    for resource_id, rows in sorted(unframed.items()):
        rows.sort(key=_order)
        bounds = dict(record_id=f'unframed:{resource_id}', begin_ns=rows[0]['begin_ns'],
                      end_ns=max(r['end_ns'] for r in rows))
        append_frame(bounds, resource_id, False, rows)

    ordered = sorted(lanes.values(), key=lambda lane: (lane['order'], lane['id']))
    for lane in ordered:
        del lane['order']
        lane['tracks'] = sorted(lane['tracks'].values(), key=lambda track: (track['order'], track['id']))
        for track in lane['tracks']:
            del track['order']
    notices = ['颜色表示软件阶段。耗时来自已记录的区间，阶段可能包含等待。']
    has_worker_layers = any('source' in group for frame in frames for group in frame['groups'])
    if has_worker_layers:
        notices.append('层内阶段来自采到的代表线程。融合 MoE 和重叠 DSA 阶段按采集边界展示。')
    if unframed:
        notices.append('无帧标记的轨道显示软件区间的包络。')
    if len(software) != len(closed):
        notices.append(f'{len(software) - len(closed)} 条区间未闭合，见资源视图。')
    quality = session.get('quality', {})
    if any(quality.get(k) for k in ('dropped_records', 'incomplete_events', 'protocol_errors', 'capture_partial')):
        notices.append('采集质量记录显示数据不完整，详情见资源视图。')
    if session.get('data_origin') == 'synthetic':
        notices.append('输入为合成数据。')

    models = sorted({r['args']['model'] for r in frame_rows.values() if isinstance((r.get('args') or {}).get('model'), str)})
    title = (models[0] if len(models) == 1 else session.get('name') or session['run_id']) or session['session_id']
    if outer_layers:
        title = 'GLM 5F Prefill'
    return dict(title=title, run=session['run_id'], origin=session.get('data_origin'),
                time=session['time_range'], lanes=ordered, frames=frames,
                names=list(names), refs=list(refs), default=_choose_default(frames),
                name_labels=PREFILL_STAGE_LABELS if has_worker_layers else {},
                counts=dict(intervals=len(closed), frames=len(frame_rows)), notices=notices)


def write_report(session, path):
    payload = json.dumps(pipeline_data(session), ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    html = TEMPLATE.read_text(encoding='utf-8')
    if html.count(DATA) != 1:
        raise ValueError('The embedded pipeline data marker must appear exactly once')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        stream.write(html.replace(DATA, payload.replace('<', '\\u003c')))
    return path
