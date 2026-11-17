"""Recover checksummed compact SDK prefixes without inventing missing boundaries."""
import base64
import json
import struct
import zlib
from pathlib import Path

HEADER = struct.Struct('<IHHQIIII')
EVENT = struct.Struct('<QIHH')
MAGIC, TRAILER = 0x3143584c, 0x4543584c
MAX_IMPORT_RECORDS = 1_000_000
MAX_IMPORT_BYTES = 128 * 1024 * 1024


def read_chunks(path, max_records=None, max_bytes=None):
    """Stop at the first invalid chunk. A valid footer is still required for clean."""
    if max_bytes is not None and Path(path).stat().st_size > max_bytes:
        raise ValueError('compact input exceeds in-memory import byte budget; use compact-info and compact-select before report')
    chunks, warnings, valid_bytes, expected, record_budget = [], [], 0, 1, 0
    with Path(path).open('rb') as stream:
        while True:
            offset = stream.tell()
            header = stream.read(32)
            if not header:
                break
            if len(header) != 32:
                warnings.append('truncated chunk header at byte %d' % offset)
                break
            magic, version, kind, sequence, thread, count, size, checksum = HEADER.unpack(header)
            if magic != MAGIC or version != 1 or sequence != expected or size > 64 * 1024 * 1024:
                warnings.append('invalid chunk header/sequence at byte %d' % offset)
                break
            payload, trailer = stream.read(size), stream.read(8)
            crc = zlib.crc32(payload, zlib.crc32(header[:28]))
            if (len(payload) != size or len(trailer) != 8 or
                    struct.unpack('<II', trailer) != (TRAILER, checksum) or crc != checksum):
                warnings.append('truncated or checksum-invalid chunk at byte %d' % offset)
                break
            if kind not in range(1, 9):
                warnings.append('unsupported chunk type at byte %d' % offset)
                break
            # A PMU read expands into a software event, a sample and up to N
            # counter windows. Guard before materializing the normalized rows.
            record_budget += (count if kind == 4 else
                              2 + struct.unpack_from('<I', payload, 16)[0]
                              if kind == 7 and len(payload) >= 20 else 0)
            if max_records is not None and record_budget > max_records:
                raise ValueError('compact input exceeds in-memory import record budget; use compact-info and compact-select before report')
            chunks.append(dict(kind=kind, sequence=sequence, thread=thread,
                               count=count, payload=payload, offset=offset))
            valid_bytes, expected = stream.tell(), expected + 1
    if not chunks or chunks[0]['kind'] != 1:
        raise ValueError('compact input has no valid metadata prefix')
    return chunks, warnings, valid_bytes


def _object(payload):
    value = json.loads(payload.decode('utf-8'))
    if not isinstance(value, dict):
        raise ValueError('compact metadata must be a JSON object')
    return value


def load_compact(path, max_records=MAX_IMPORT_RECORDS, max_bytes=MAX_IMPORT_BYTES):
    from .importers import _load_sdk_rows, _sdk_args_valid
    from .resource import migrate_v1, validate_session
    path = Path(path)
    chunks, warnings, valid_bytes = read_chunks(path, max_records=max_records, max_bytes=max_bytes)
    meta = _object(chunks[0]['payload'])
    if meta.get('schema_version') != 'lxtrace.compact.v1':
        raise ValueError('unsupported compact SDK schema')
    pid = meta['pid']
    sdk_meta = dict(meta, type='meta', schema_version='lxtrace.sdk.v1')
    rows, sites, threads, stacks, losses, snapshots, metadata = [sdk_meta], {}, {}, {}, {}, [], []
    serial, final_footer, malformed = 0, False, []
    first_footer = None
    for chunk in chunks[1:]:
        typ, thread, payload = chunk['kind'], chunk['thread'], chunk['payload']
        try:
            if typ == 2:
                site = _object(payload)
                sid = site['site_id']
                if not isinstance(sid, int) or sid <= 0 or sid in sites:
                    raise ValueError('duplicate/invalid compact site')
                try:
                    args = json.loads(site.get('args_json', '{}'))
                    if site.get('invalid_text') or not _sdk_args_valid(args):
                        raise ValueError('invalid args')
                except (ValueError, UnicodeError):
                    args = {'invalid_args': True}
                    malformed.append('invalid site args at chunk %d' % chunk['sequence'])
                site['args'] = args
                sites[sid] = site
            elif typ == 3:
                t = _object(payload)
                if t.get('invalid_text'):
                    malformed.append('invalid thread label at chunk %d' % chunk['sequence'])
                if thread in threads or t['thread_id'] != thread:
                    raise ValueError('duplicate/conflicting compact thread')
                tid = t['tid']
                threads[thread] = dict(type='thread', thread_id='%s:%s:%s' % (pid, tid, thread),
                                       tid=tid, pid=pid, name=t.get('name', 'worker'), role='worker')
                rows.append(threads[thread])
            elif typ in (4, 7):
                if thread not in threads:
                    raise ValueError('unregistered compact thread')
                if typ == 4:
                    if len(payload) != chunk['count'] * 16:
                        raise ValueError('compact event count mismatch')
                    events = [(*EVENT.unpack_from(payload, i), None) for i in range(0, len(payload), 16)]
                else:
                    if len(payload) < 32:
                        raise ValueError('short compact PMU extension')
                    ts, sid, kind, aux = EVENT.unpack_from(payload)
                    count, reserved, read_end = struct.unpack_from('<IIQ', payload, 16)
                    if not 1 <= count <= 16 or reserved or len(payload) != 32 + count * 24 or read_end < ts:
                        raise ValueError('invalid compact PMU extension')
                    values = [struct.unpack_from('<QQQ', payload, 32 + i*24) for i in range(count)]
                    events = [(ts, sid, kind, aux, dict(read_end=read_end, values=values))]
                for ts, sid, kind, aux, pmu in events:
                    site = sites.get(sid)
                    if site is None or kind not in (1, 2, 3):
                        raise ValueError('invalid compact event site/kind')
                    serial += 1
                    eid, t = '%s:c%d' % (pid, serial), threads[thread]
                    stack = stacks.setdefault(thread, [])
                    event_scope = stack[-1][1] if stack else None
                    event_frame = stack[-1][2] if stack else None
                    if kind == 2:
                        if not stack or stack[-1][0] != sid:
                            malformed.append('non-LIFO end at chunk %d' % chunk['sequence'])
                        else:
                            _, begin_id, _ = stack.pop()
                            rows.append(dict(type='end', event_id=begin_id, ts=ts, tid=t['tid']))
                    else:
                        args = dict(site['args'], compact_site_id=sid, compact_aux=aux,
                                    compact_chunk=chunk['sequence'])
                        semantic = args.pop('kind', 'range')
                        if semantic not in ('frame', 'range', 'wait', 'exchange'):
                            semantic = 'range'
                        if semantic == 'frame':
                            args.setdefault('application_frame_id', '%s:%d' % (site['name'], serial))
                            args.setdefault('driver_calls_complete', False)
                        frame = stack[-1][2] if stack else None
                        rows.append(dict(type='begin' if kind == 1 else 'instant', event_id=eid,
                                         ts=ts, thread_id=t['thread_id'], tid=t['tid'], pid=pid,
                                         kind=semantic, name=site.get('name', 'site:%d' % sid),
                                         category=site.get('category', 'unknown'), args=args,
                                         parent_id=stack[-1][1] if stack else None,
                                         frame_id=eid if semantic == 'frame' else frame))
                        if kind == 1:
                            stack.append((sid, eid, eid if semantic == 'frame' else frame))
                            event_scope, event_frame = eid, stack[-1][2]
                    if pmu:
                        snapshots.append(dict(thread=thread, ts=ts, site=sid, kind=kind,
                                              aux=aux, chunk=chunk['sequence'],
                                              software_scope_id=event_scope,
                                              software_frame_id=event_frame, **pmu))
            elif typ == 5:
                if len(payload) != 64:
                    raise ValueError('invalid compact loss record')
                keys = ('dropped', 'first_seq', 'last_seq', 'first_ts', 'last_ts', 'attempted', 'late', 'published_slots')
                loss = dict(zip(keys, struct.unpack('<8Q', payload)))
                old = losses.get(thread)
                if old and loss['dropped'] < old['dropped']:
                    raise ValueError('nonmonotonic compact loss')
                losses[thread] = loss
            elif typ == 6:
                quality = _object(payload)
                rows.append(dict(quality, type='quality'))
                final_footer = chunk is chunks[-1]
                if first_footer is None:
                    first_footer = chunk['sequence']
            elif typ == 8:
                item = _object(payload)
                if item.get('invalid_text'):
                    raise ValueError('invalid compact metadata text')
                item = _object(item.get('metadata_json', '{}').encode('utf-8'))
                if not _sdk_args_valid(item):
                    raise ValueError('invalid compact metadata values')
                metadata.append(item)
        except (ValueError, UnicodeError, KeyError, TypeError, struct.error) as error:
            malformed.append('chunk %d: %s' % (chunk['sequence'], error))
            break
    warnings.extend(malformed)
    legacy = _load_sdk_rows(path, rows, warnings)
    origin = legacy['clock']['origin_ns']
    quality = legacy['quality']
    pmu_errors = [m for m in metadata if 'engine_pmu_error' in m or
                  isinstance(m.get('engine_pmu_thread'), dict) and m['engine_pmu_thread'].get('status', 0) < 0]
    if pmu_errors:
        quality['warnings'].append('requested engine PMU unavailable or failed; retained software coverage does not certify PMU coverage')
        quality.update(pmu_capture_partial=True, pmu_failures=pmu_errors,
                       clean_shutdown=False, capture_partial=True)
    selections = [m['compact_selection'] for m in metadata if isinstance(m.get('compact_selection'), dict)]
    if selections:
        selected = selections[-1]
        quality.update(compact_selection=selected, capture_scope='selected_threads_only',
                       full_track_coverage='unknown', footer_counters_scope='unfiltered_source_capture')
        quality['warnings'].append('selected thread tracks only; original footer counters describe the unfiltered source capture')
        source_quality = selected.get('source_quality', {})
        if not isinstance(source_quality, dict):
            source_quality = {'capture_partial': True}
            quality['warnings'].append('invalid selection source quality; source coverage is unknown')
        if (selected.get('source_capture_partial') or source_quality.get('capture_partial') or
                source_quality.get('clean_shutdown') is False):
            quality.update(clean_shutdown=False, capture_partial=True, source_continuity='unknown')
            quality['warnings'].append('source capture was partial; selecting surviving tracks does not restore full source coverage')
    if first_footer is not None and any(chunk['sequence'] > first_footer and chunk['kind'] in (4, 7) for chunk in chunks):
        quality['warnings'].append('events follow a shutdown quality footer; capture continuity is unknown')
        final_footer = False
    quality.update(compact_valid_prefix_bytes=valid_bytes, compact_file_bytes=path.stat().st_size,
                   compact_chunk_count=len(chunks), compact_record_bytes=16,
                   loss_position_known=True, loss_bounds=list(dict(thread_id=threads.get(k, {}).get('thread_id'), **v)
                                                               for k, v in losses.items() if v['dropped']),
                   compact_metadata=metadata, recovery='checksummed_prefix',
                   tail_coverage='unknown' if not final_footer or warnings else 'final_quality_snapshot')
    quality['dropped_records'] = max(quality['dropped_records'], sum(v['dropped'] for v in losses.values()))
    # A loss may remove one side of a repeated site. Do not accept a false pair
    # that spans the known loss bounds, even if its site IDs happen to match.
    loss_by_thread = {threads[k]['thread_id']: v for k, v in losses.items() if v['dropped'] and k in threads}
    for event in legacy['events']:
        loss = loss_by_thread.get(event['thread_id'])
        if loss:
            if (event['begin_ts'] <= loss['last_ts'] - origin and
                    (event['end_ts'] is None or event['end_ts'] >= loss['first_ts'] - origin)):
                event.update(end_ts=None, status='incomplete')
                event['args']['loss_intersects_range'] = True
    quality['incomplete_events'] = max(quality.get('incomplete_events', 0),
                                       sum(e['end_ts'] is None for e in legacy['events']))
    if not final_footer or warnings or quality['dropped_records'] or quality['incomplete_events']:
        quality.update(clean_shutdown=False, capture_partial=True, source_continuity='unknown')
    if not final_footer:
        quality['warnings'].append('compact final shutdown proof unavailable; recovered prefix only; unwritten tail is unknown')
    session = migrate_v1(legacy)
    for key in ('host_id', 'boot_id', 'run_id'):
        value = next((m[key] for m in metadata if m.get(key)), meta.get(key))
        if value:
            session[key] = value
    session['clocks'][0]['original_clock']['id'] = meta['clock_id']
    session['quality'] = quality
    raw = path.read_bytes()
    session['raw_files']['compact/capture.lxc.base64'] = base64.b64encode(raw).decode('ascii')
    session['provenance'].append(dict(kind='compact_sdk_import', format='lxtrace.compact.v1',
                                      chunk_count=len(chunks), valid_prefix_bytes=valid_bytes))
    for selected in selections:
        session['provenance'].append(dict(selected, kind='compact_thread_selection'))
    if snapshots:
        _pmu_records(session, snapshots, metadata, threads, sites, origin, losses)
    return validate_session(session)


def _pmu_records(session, snapshots, metadata, threads, sites, origin, losses):
    session['sources'].append(dict(source_id='compact.pmu', source_layer='hardware',
                                   scope='own_thread:user', clock_id='legacy',
                                   native_format='lxtrace.compact.v1',
                                   boundary_support='counter_window', semantics_status='raw_counter_readings'))
    dictionaries = [m['pmu_events'] for m in metadata if isinstance(m.get('pmu_events'), list)]
    configs = dictionaries[0] if dictionaries else []
    if any(items != configs for items in dictionaries) or not all(isinstance(c, dict) for c in configs):
        configs = []
        session['quality'].update(clean_shutdown=False, capture_partial=True)
        session['quality']['warnings'].append('conflicting PMU dictionaries; column meanings left unknown')
    previous = {}
    for index, snapshot in enumerate(snapshots):
        thread, ts = snapshot['thread'], snapshot['ts'] - origin
        rid = 'thread:' + threads[thread]['thread_id']
        values = snapshot['values']
        session['records'].append(dict(record_id='pmu.read:%d' % index, resource_id=rid,
            source_id='compact.pmu', layer='hardware', category='pmu', representation='sample',
            timestamp_ns=ts, name='PMU boundary read', read_end_ns=snapshot['read_end']-origin,
            site_id=snapshot['site'], boundary_kind=snapshot['kind'], aux=snapshot['aux'],
            software_scope_id=snapshot['software_scope_id'], software_frame_id=snapshot['software_frame_id'],
            readings=[dict(value=v, enabled_ns=e, running_ns=r,
                           event=configs[i] if i < len(configs) else {'name': 'unknown:%d' % i})
                      for i, (v, e, r) in enumerate(values)],
            uncertainty_ns=snapshot['read_end']-snapshot['ts']))
        before = previous.get(thread)
        loss = losses.get(thread, {})
        crossing_loss = bool(before and loss.get('dropped') and before['ts'] <= loss['last_ts']
                             and snapshot['ts'] >= loss['first_ts'])
        if crossing_loss:
            session['quality']['warnings'].append('PMU read pair crosses known event loss; cumulative reads retained, derived window omitted')
        if before and not crossing_loss and len(before['values']) == len(values) and before['ts'] <= snapshot['ts']:
            for i, (now, old) in enumerate(zip(values, before['values'])):
                if any(n < o for n, o in zip(now, old)):
                    session['quality'].update(clean_shutdown=False, capture_partial=True)
                    session['quality']['warnings'].append('nonmonotonic PMU readings; delta omitted')
                    continue
                value, enabled, running = (n-o for n, o in zip(now, old))
                config = configs[i] if i < len(configs) else {'name': 'unknown:%d' % i}
                matched_scope = (before['kind'] == 1 and snapshot['kind'] == 2 and
                                 before['software_scope_id'] is not None and
                                 before['software_scope_id'] == snapshot['software_scope_id'])
                matched_frame = (before['software_frame_id'] is not None and
                                 before['software_frame_id'] == snapshot['software_frame_id'])
                session['records'].append(dict(record_id='pmu.window:%d:%d' % (index, i),
                    resource_id=rid, source_id='compact.pmu', layer='hardware', category='pmu',
                    representation='counter_window', begin_ns=before['ts']-origin, end_ns=ts,
                    name=config.get('name', 'raw:%s' % config.get('config', '?')),
                    event=config.get('name', 'unknown:%d' % i), encoding=config,
                    raw_count=value, enabled_ns=enabled, running_ns=running, unit='count',
                    read_uncertainty_ns=[before['read_end']-before['ts'], snapshot['read_end']-snapshot['ts']],
                    boundary_semantics='adjacent cumulative read windows; not hardware activity',
                    software_scope_id=before['software_scope_id'] if matched_scope else None,
                    software_frame_id=before['software_frame_id'] if matched_frame else None,
                    attribution_scope=('matching_software_range' if matched_scope else
                                       'same_marked_frame' if matched_frame else 'unassigned_cumulative_window'),
                    frame_attribution='same_marked_frame' if matched_frame else 'unproven',
                    prior_site=before['site'], prior_boundary_kind=before['kind'], prior_aux=before['aux'],
                    end_site=snapshot['site'], end_boundary_kind=snapshot['kind'], end_aux=snapshot['aux'],
                    uncertainty_ns=None, raw_refs=['raw/compact/capture.lxc.base64#chunk/%d' % before['chunk'],
                                                   'raw/compact/capture.lxc.base64#chunk/%d' % snapshot['chunk']]))
        previous[thread] = snapshot
