"""Bounded compact capture inspection and complete-thread selection.

This path never constructs event/session lists. Memory is one <=64 MiB chunk,
bounded diagnostics, site IDs (at most 1,000,000), and 1024 thread summaries with
128-entry LIFO stacks and <=16 previous PMU readings each. Full HTML import is a
separate operation; select whole threads first when a capture is too large.
"""
import hashlib
import json
import os
import struct
import zlib
from pathlib import Path

from .compact import EVENT, HEADER, MAGIC, TRAILER

MAX_CHUNK_BYTES = 64 * 1024 * 1024
MAX_SITES = 1000000
MAX_THREADS = 1024
MAX_DEPTH = 128
MAX_DIAGNOSTICS = 100
LOSS_KEYS = ('dropped', 'first_seq', 'last_seq', 'first_ts', 'last_ts',
             'attempted', 'late', 'published_slots')


def _integer(value, minimum=0, maximum=None):
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= minimum and (maximum is None or value <= maximum))


def _object(payload):
    def invalid_constant(value):
        raise ValueError('nonfinite JSON constant: ' + value)
    value = json.loads(payload.decode('utf-8'), parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('JSON object required')
    from .importers import _sdk_args_valid
    if not _sdk_args_valid(value):
        raise ValueError('invalid JSON values/depth/UTF-8')
    return value


def _warning(state, text):
    state['warning_count'] += 1
    if len(state['warnings']) < MAX_DIAGNOSTICS:
        state['warnings'].append(str(text))


def iter_compact_chunks(path, diagnostics=None):
    """Yield complete validated envelopes; stop at the first invalid prefix.

    The mutable diagnostics object receives valid_bytes, chunk_count, warnings,
    warning_count. Envelope validity does not certify semantic event validity.
    Payloads are bounded by MAX_CHUNK_BYTES; callers must not collect this iterator.
    """
    state = diagnostics if diagnostics is not None else {}
    state.update(valid_bytes=0, chunk_count=0, warnings=[], warning_count=0)
    expected = 1
    with Path(path).open('rb') as stream:
        while True:
            offset = stream.tell()
            header = stream.read(HEADER.size)
            if not header:
                return
            if len(header) != HEADER.size:
                _warning(state, 'truncated header at byte %d' % offset)
                return
            magic, version, kind, seq, thread, count, size, checksum = HEADER.unpack(header)
            if (magic != MAGIC or version != 1 or seq != expected
                    or kind not in range(1, 9) or size > MAX_CHUNK_BYTES):
                _warning(state, 'invalid header/type/sequence/size at byte %d' % offset)
                return
            payload, trailer = stream.read(size), stream.read(8)
            if (len(payload) != size or len(trailer) != 8
                    or struct.unpack('<II', trailer) != (TRAILER, checksum)
                    or zlib.crc32(payload, zlib.crc32(header[:28])) != checksum):
                _warning(state, 'truncated or checksum-invalid chunk at byte %d' % offset)
                return
            state['valid_bytes'] = stream.tell()
            state['chunk_count'] += 1
            yield dict(kind=kind, thread=thread, count=count, payload=payload,
                       sequence=seq, offset=offset, end_offset=stream.tell())
            expected += 1


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _signature(path):
    stat = Path(path).stat()
    return dict(size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)


def inspect_compact(path):
    """Return a JSON-safe bounded summary of the entire checksummed prefix.

    Invalid initial metadata raises ValueError. Later format/semantic anomalies
    retain the inspectable prefix and make quality partial. Counts describe
    surviving records, never unrecorded hardware work. Thread IDs are ring IDs.
    """
    path = Path(path).resolve()
    signature = _signature(path)
    wire = {}
    result = dict(format='lxtrace.compact-inspection.v1', path=str(path),
                  file_bytes=signature['size_bytes'], source_signature=signature,
                  metadata={}, chunks={}, site_count=0, thread_count=0,
                  event_count=0, pmu_read_count=0, begin_count=0, end_count=0,
                  instant_count=0, first_timestamp_ns=None, last_timestamp_ns=None,
                  pmu_events=None, warnings=[], warning_count=0)
    sites, threads, stacks, losses, last_pmu = set(), {}, {}, {}, {}
    footer, footer_count, first_footer = None, 0, None
    footer_bad, protocol_errors, last_kind = False, 0, None
    selection, semantic_valid_bytes, semantic_stopped = None, 0, False

    def fail(text):
        nonlocal protocol_errors
        protocol_errors += 1
        _warning(result, text)

    def event(thread, ts, site, kind, aux, pmu=None, read_end=None):
        if thread not in threads or site not in sites or kind not in (1, 2, 3):
            raise ValueError('unregistered thread/site or invalid event kind')
        item = threads[thread]
        if item['last_timestamp_ns'] is not None and ts < item['last_timestamp_ns']:
            fail('timestamp regression in thread %d at %d' % (thread, ts))
        if first_footer is not None:
            fail('event follows a shutdown footer in thread %d' % thread)
        item['records'] += 1
        item['published_slots'] += 1 if pmu is None else 1 + (32 + len(pmu) * 24 + 4 + 15) // 16
        key = {1: 'begin', 2: 'end', 3: 'instant'}[kind]
        item[key] += 1
        result[key + '_count'] += 1
        result['event_count'] += 1
        if item['first_timestamp_ns'] is None:
            item['first_timestamp_ns'] = ts
        item['last_timestamp_ns'] = ts
        result['first_timestamp_ns'] = ts if result['first_timestamp_ns'] is None else min(result['first_timestamp_ns'], ts)
        result['last_timestamp_ns'] = ts if result['last_timestamp_ns'] is None else max(result['last_timestamp_ns'], ts)
        stack = stacks[thread]
        if kind == 1:
            if len(stack) >= MAX_DEPTH:
                raise ValueError('scope depth exceeds bounded SDK limit')
            stack.append(site)
        elif kind == 2:
            if not stack or stack[-1] != site:
                fail('non-LIFO end in thread %d at %d' % (thread, ts))
            else:
                stack.pop()
        if pmu is not None:
            result['pmu_read_count'] += 1
            item['pmu_records'] += 1
            previous = last_pmu.get(thread)
            if previous and (len(previous) != len(pmu) or any(
                    any(now < old for now, old in zip(n, o)) for n, o in zip(pmu, previous))):
                fail('PMU column count/reset/regression in thread %d' % thread)
            if any(running > enabled for _, enabled, running in pmu):
                fail('PMU running time exceeds enabled time in thread %d' % thread)
            last_pmu[thread] = pmu
            reading = dict(timestamp_ns=ts, read_end_ns=read_end, site_id=site, kind=kind, aux=aux,
                           values=[dict(value=v, enabled_ns=e, running_ns=r) for v, e, r in pmu])
            if item['first_pmu_read'] is None:
                item['first_pmu_read'] = reading
            item['last_pmu_read'] = reading

    for chunk in iter_compact_chunks(path, wire):
        typ, thread, count, payload = chunk['kind'], chunk['thread'], chunk['count'], chunk['payload']
        result['chunks'][str(typ)] = result['chunks'].get(str(typ), 0) + 1
        last_kind = typ
        if semantic_stopped:
            continue
        try:
            if chunk['sequence'] == 1:
                if typ != 1 or thread or count != 1 or len(payload) > 65535:
                    raise ValueError('initial compact metadata envelope required')
                meta = _object(payload)
                if (meta.get('schema_version') != 'lxtrace.compact.v1'
                        or not _integer(meta.get('pid'), 1)
                        or not isinstance(meta.get('clock_id'), str) or not meta['clock_id']
                        or meta.get('record_bytes', 16) != 16):
                    raise ValueError('unsupported initial compact metadata')
                result['metadata'] = meta
            elif typ == 1:
                raise ValueError('duplicate initial metadata')
            elif typ in (2, 3, 6, 8):
                if count != 1 or (typ != 3 and thread) or len(payload) > 65535:
                    raise ValueError('invalid JSON chunk count/thread')
                value = _object(payload)
                if typ == 2:
                    sid = value.get('site_id')
                    if not _integer(sid, 1, 0xfffffffe) or sid in sites or len(sites) >= MAX_SITES:
                        raise ValueError('invalid/duplicate site or bounded site limit exceeded')
                    sites.add(sid)
                    if (value.get('invalid_text') or not isinstance(value.get('name'), str)
                            or not isinstance(value.get('category'), str)):
                        fail('invalid site label at chunk %d' % chunk['sequence'])
                    try:
                        _object(value.get('args_json', '{}').encode('utf-8'))
                    except (ValueError, UnicodeError, AttributeError, RecursionError):
                        fail('invalid site args at chunk %d' % chunk['sequence'])
                elif typ == 3:
                    if (not thread or thread in threads or len(threads) >= MAX_THREADS
                            or value.get('thread_id') != thread or not _integer(value.get('tid'), 1)):
                        raise ValueError('invalid/duplicate thread or SDK thread limit exceeded')
                    if value.get('invalid_text') or not isinstance(value.get('name'), str):
                        fail('invalid thread label at chunk %d' % chunk['sequence'])
                    threads[thread] = dict(thread_id=thread, tid=value['tid'], name=value.get('name', ''),
                                           records=0, begin=0, end=0, instant=0, pmu_records=0,
                                           published_slots=0,
                                           first_timestamp_ns=None, last_timestamp_ns=None,
                                           first_pmu_read=None, last_pmu_read=None)
                    stacks[thread] = []
                elif typ == 6:
                    if value.get('quality_semantics') != 'cumulative_snapshot':
                        raise ValueError('cumulative footer required')
                    for key in ('quality_revision', 'dropped_records', 'protocol_errors', 'late_records',
                                'incomplete_events', 'attempted_records', 'published_slots'):
                        if key in value and not _integer(value[key]):
                            raise ValueError('invalid footer integer ' + key)
                        if footer and value.get(key, 0) < footer.get(key, 0):
                            fail('nonmonotonic footer ' + key)
                    if footer and value.get('quality_revision', 0) <= footer.get('quality_revision', 0):
                        fail('nonincreasing footer revision')
                    footer_bad |= value.get('clean_shutdown') is not True or any(value.get(k, 0) for k in (
                        'dropped_records', 'protocol_errors', 'late_records', 'incomplete_events',
                        'unsupported_fork_count', 'io_error'))
                    footer_count += 1
                    footer = value
                    if first_footer is None:
                        first_footer = chunk['sequence']
                else:
                    if value.get('invalid_text'):
                        raise ValueError('invalid metadata text')
                    item = _object(value.get('metadata_json', '{}').encode('utf-8'))
                    if ('engine_pmu_error' in item or isinstance(item.get('engine_pmu_thread'), dict)
                            and item['engine_pmu_thread'].get('status', 0) < 0):
                        fail('requested engine PMU unavailable or failed; software coverage does not certify PMU coverage')
                    if 'pmu_events' in item:
                        events = item['pmu_events']
                        if (not isinstance(events, list) or len(events) > 16
                                or not all(isinstance(e, dict) for e in events)):
                            fail('invalid PMU event dictionary')
                        elif result['pmu_events'] is None:
                            result['pmu_events'] = events
                        elif result['pmu_events'] != events:
                            fail('conflicting PMU event dictionaries')
                    if 'compact_selection' in item:
                        if not isinstance(item['compact_selection'], dict):
                            raise ValueError('invalid selection metadata')
                        selection = item['compact_selection']
            elif typ == 4:
                if thread not in threads or len(payload) != count * EVENT.size:
                    raise ValueError('invalid event chunk count/thread')
                for ts, site, kind, aux in EVENT.iter_unpack(payload):
                    event(thread, ts, site, kind, aux)
            elif typ == 7:
                if count != 1 or len(payload) < 32:
                    raise ValueError('invalid PMU chunk count/header')
                ts, site, kind, aux = EVENT.unpack_from(payload)
                n, reserved, read_end = struct.unpack_from('<IIQ', payload, 16)
                if not 1 <= n <= 16 or reserved or len(payload) != 32 + n * 24 or read_end < ts:
                    raise ValueError('invalid PMU column count/read window')
                values = tuple(struct.unpack_from('<QQQ', payload, 32 + i * 24) for i in range(n))
                event(thread, ts, site, kind, aux, values, read_end)
            elif typ == 5:
                if thread not in threads or count != 1 or len(payload) != 64:
                    raise ValueError('invalid loss chunk count/thread/payload')
                value = dict(zip(LOSS_KEYS, struct.unpack('<8Q', payload)))
                old = losses.get(thread)
                if (value['late'] > value['dropped'] or value['dropped'] > value['attempted']
                        or (value['dropped'] and not 1 <= value['first_seq'] <= value['last_seq'] <= value['attempted'])
                        or (value['dropped'] and (value['first_ts'] > value['last_ts']
                            or value['last_seq'] - value['first_seq'] + 1 < value['dropped']))
                        or (not value['dropped'] and any(value[k] for k in ('first_seq', 'last_seq', 'first_ts', 'last_ts')))):
                    raise ValueError('inconsistent loss bounds/counts')
                if old and any(value[k] < old[k] for k in ('dropped', 'late', 'attempted', 'published_slots')):
                    raise ValueError('nonmonotonic loss counters')
                losses[thread] = value
            semantic_valid_bytes = chunk['end_offset']
        except (ValueError, UnicodeError, TypeError, KeyError, AttributeError, struct.error, RecursionError) as error:
            if chunk['sequence'] == 1:
                raise ValueError('invalid initial compact metadata: %s' % error) from error
            fail('chunk %d: %s' % (chunk['sequence'], error))
            # Keep validating later envelopes but never decode past a semantic
            # failure into a supposedly continuous source.
            semantic_stopped = True
    if not result['metadata']:
        raise ValueError('compact input has no valid metadata prefix')
    for text in wire['warnings']:
        _warning(result, text)
    open_scopes = sum(map(len, stacks.values()))
    if open_scopes:
        fail('%d surviving scopes have no end' % open_scopes)
    if last_kind != 6 or footer is None or semantic_stopped:
        _warning(result, 'final quality footer missing; unflushed tail coverage unknown')
    dropped = max(sum(v['dropped'] for v in losses.values()), (footer or {}).get('dropped_records', 0))
    late = max(sum(v['late'] for v in losses.values()), (footer or {}).get('late_records', 0))
    incomplete = max(open_scopes, (footer or {}).get('incomplete_events', 0))
    source_partial = bool(selection and selection.get('source_capture_partial'))
    if selection and isinstance(selection.get('source_quality'), dict):
        source_partial |= bool(selection['source_quality'].get('capture_partial')
                               or selection['source_quality'].get('clean_shutdown') is False)
    if footer and not selection and not semantic_stopped and not wire['warnings']:
        observed_slots = sum(item['published_slots'] for item in threads.values())
        if 'published_slots' in footer and footer['published_slots'] != observed_slots:
            fail('footer published_slots disagrees with surviving records')
        if len(losses) == len(threads):
            for key, field in (('attempted_records', 'attempted'), ('published_slots', 'published_slots')):
                if key in footer and footer[key] != sum(item[field] for item in losses.values()):
                    fail('footer %s disagrees with cumulative per-thread counters' % key)
    digest = _sha256(path)
    source_changed = signature != _signature(path)
    if source_changed:
        _warning(result, 'source file changed during inspection')
    result.update(sha256=digest, valid_bytes=wire['valid_bytes'],
                  semantic_valid_bytes=semantic_valid_bytes, chunk_count=wire['chunk_count'],
                  site_count=len(sites), thread_count=len(threads),
                  threads=[dict(item, open_scopes=len(stacks[tid]), loss=losses.get(tid))
                           for tid, item in sorted(threads.items())],
                  footer=footer, footer_count=footer_count, selection=selection,
                  source_changed=source_changed)
    clean = (last_kind == 6 and footer is not None and not footer_bad and not result['warning_count']
             and not dropped and not late and not incomplete and not source_partial and not source_changed)
    result['quality'] = dict(clean_shutdown=clean, capture_partial=not clean,
                             dropped_records=dropped, late_records=late,
                             protocol_errors=protocol_errors + (footer or {}).get('protocol_errors', 0),
                             incomplete_events=incomplete, loss_bounds=[dict(thread_id=k, **v)
                                                                        for k, v in sorted(losses.items()) if v['dropped']],
                             footer_counters_scope='unfiltered_source_capture' if selection else 'this_capture',
                             full_track_coverage='unknown' if selection else 'not_certified_by_software_events')
    return result


def _emit(stream, sequence, kind, thread, count, payload):
    header = HEADER.pack(MAGIC, 1, kind, sequence, thread, count, len(payload), 0)
    crc = zlib.crc32(payload, zlib.crc32(header[:28]))
    stream.write(header[:28] + struct.pack('<I', crc))
    stream.write(payload)
    stream.write(struct.pack('<II', TRAILER, crc))


def select_compact(input_path, output_path, thread_ids):
    """Exclusively write complete selected ring tracks and return their summary.

    Sites, initial/cold metadata and original source footer snapshots survive.
    New selection metadata states source SHA, selected IDs, source partial state,
    and that original footer counters describe the unfiltered source. No time
    clipping, synthetic scope closure or claim of full-machine coverage occurs.
    """
    source, target = Path(input_path).resolve(), Path(output_path).resolve()
    if source == target:
        raise ValueError('selection output must differ from its input')
    if target.exists():
        raise FileExistsError('selection output already exists: ' + str(target))
    ids = list(thread_ids)
    if not ids or not all(_integer(value, 1, 0xffffffff) for value in ids) or len(ids) != len(set(ids)):
        raise ValueError('thread_ids must contain unique positive ring IDs')
    ids = sorted(ids)
    summary = inspect_compact(source)
    if summary['source_changed']:
        raise ValueError('source changed during inspection; retry after capture stops')
    available = {item['thread_id'] for item in summary['threads']}
    if not set(ids) <= available:
        raise ValueError('unknown compact thread IDs: ' + ','.join(map(str, sorted(set(ids)-available))))
    selection = dict(source_sha256=summary['sha256'], source_file_bytes=summary['file_bytes'],
                     source_valid_bytes=summary['valid_bytes'], selected_thread_ids=ids,
                     source_thread_count=summary['thread_count'], selected_thread_count=len(ids),
                     source_capture_partial=summary['quality']['capture_partial'],
                     source_quality={key: value for key, value in summary['quality'].items()
                                     if key in ('clean_shutdown', 'capture_partial', 'dropped_records',
                                                'late_records', 'protocol_errors', 'incomplete_events')},
                     footer_counter_scope='original_source',
                     policy='complete_selected_thread_chunks_no_time_clipping',
                     source_footer_counters_scope='unfiltered_source_capture',
                     source_scope=dict(coverage='selected_threads_only', full_track_coverage='unknown'),
                     source_footer=summary['footer'])
    metadata = json.dumps({'metadata_json': json.dumps({'compact_selection': selection},
                          ensure_ascii=False, allow_nan=False)}, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(metadata) > 65535:
        raise ValueError('selection provenance exceeds bounded metadata size')
    sequence, emitted_selection = 0, False
    created = False
    try:
        descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            created = True
            diagnostics = {}
            for chunk in iter_compact_chunks(source, diagnostics):
                if chunk['end_offset'] > summary['semantic_valid_bytes']:
                    break
                kind, thread = chunk['kind'], chunk['thread']
                if kind in (3, 4, 5, 7) and thread not in ids:
                    continue
                if kind == 6 and not emitted_selection:
                    sequence += 1
                    _emit(stream, sequence, 8, 0, 1, metadata)
                    emitted_selection = True
                sequence += 1
                _emit(stream, sequence, kind, thread, chunk['count'], chunk['payload'])
            if not emitted_selection:
                sequence += 1
                _emit(stream, sequence, 8, 0, 1, metadata)
            stream.flush()
            os.fsync(stream.fileno())
        if summary['source_signature'] != _signature(source) or summary['sha256'] != _sha256(source):
            raise ValueError('source changed while selecting; output discarded')
        selected = inspect_compact(target)
        selected['selection'] = selection
        return selected
    except BaseException:
        if created:
            target.unlink(missing_ok=True)
        raise
