"""Validated model semantics and FLOP numerators, independent of rendering.

JSON plugins select declared, built-in mathematical adapters. They never execute
Python, shell commands or expressions. New trace formats still need a reader.
"""
import copy
import hashlib
import json
import math
import re
from pathlib import Path

PROFILE_DIR = Path(__file__).resolve().parents[1] / 'attribution' / 'profiles'
SCHEMA = 'lxtrace.attribution-model.v1'
ADAPTERS = {'none', 'external_summary', 'glm5f_prefill.v1', 'matmul_components.v1'}
CATEGORIES = {'mat', 'vec', 'mem', 'cc', 'cx', 'fe', 'wait', 'unknown', 'drv'}
MODE_GROUPS = ('compute_modes', 'exchange_modes', 'wait_modes', 'idle_modes')
COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')


def _json_safe(value):
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError('model plugin must contain finite JSON values') from exc


def _read(value):
    if isinstance(value, dict):
        result = copy.deepcopy(value)
    else:
        path = Path(value)
        if not path.is_file() and str(value) in ('glm5f', 'generic-transformer'):
            path = (Path(__file__).with_name('generic-transformer.json') if str(value) == 'generic-transformer'
                    else PROFILE_DIR / (str(value) + '.json'))
        result = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(result, dict):
        raise ValueError('model plugin must be an object')
    _json_safe(result)
    return result


def _strings(value, label):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v or '|' in v for v in value):
        raise ValueError(label + ' must be a list of nonempty names without |')
    if len(value) != len(set(value)):
        raise ValueError(label + ' contains duplicate values')
    return value


def _color(value, label):
    if not isinstance(value, str) or not COLOR.fullmatch(value):
        raise ValueError(label + ' must be a #RRGGBB color')
    return value


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + ' must be an object')
    return value


def load_model(value=None):
    """Load the shipped GLM default or a validated declarative model plugin.

    Old v1 profile dictionaries remain supported. Protocol modes and legacy
    GLM sites are supplied only by the shipped compatibility profile, never by
    the presentation or numerical hardware backend.
    """
    out = _read('glm5f' if value is None else value)
    if out.get('schema_version') not in (SCHEMA, 'lxtrace.model-plugin.v1'):
        raise ValueError('unsupported model plugin schema: ' + str(out.get('schema_version')))
    if not isinstance(out.get('profile_id'), str) or not out['profile_id']:
        raise ValueError('model profile_id is required')
    legacy_without_trace = out['schema_version'] == SCHEMA and ('trace' not in out or
        isinstance(out['trace'], dict) and not out['trace'].get('sites') and not out['trace'].get('stages'))
    legacy_glm = legacy_without_trace and (out['profile_id'] == 'glm5f' or out.get('model') == 'GLM-5.3-Flash')
    if legacy_glm:
        out['trace'] = copy.deepcopy(_read('glm5f')['trace'])
        out['trace_compatibility'] = {'basis': 'shipped_glm5f_legacy_v1', 'profile_id': 'glm5f'}
    for key in MODE_GROUPS:
        _strings(out.get(key), 'model ' + key)
    groups = [set(out[k]) for k in MODE_GROUPS]
    if sum(map(len, groups)) != len(set.union(*groups)):
        raise ValueError('model mode groups must be disjoint')
    for group, fixed in (('compute_modes', {'busy'}), ('idle_modes', {'idle'}),
                         ('wait_modes', {'bar', 'xw', 'spin'})):
        if (not legacy_without_trace or legacy_glm) and not fixed <= set(out[group]):
            raise ValueError('GFTRACE protocol modes missing from ' + group + ': ' + ','.join(sorted(fixed - set(out[group]))))
    known_modes = set.union(*groups)
    out.setdefault('exchange_stages', [])
    _strings(out['exchange_stages'], 'model exchange_stages')
    if 'expected_pool_threads' in out and (type(out['expected_pool_threads']) is not int or out['expected_pool_threads'] < 1):
        raise ValueError('expected_pool_threads must be a positive integer')
    trace = _object(out.setdefault('trace', {}), 'model trace')
    formats = trace.setdefault('format', ['GFTRACE1', 'GFTRACE2'])
    if isinstance(formats, str):
        formats = [formats]
    _strings(formats, 'model trace.format')
    if not formats or not set(formats) <= {'GFTRACE1', 'GFTRACE2'}:
        raise ValueError('unsupported model trace adapter; supported formats are GFTRACE1/GFTRACE2')
    trace['format'] = formats
    stages = _object(trace.setdefault('stages', {}), 'model trace.stages')
    for key, name in list(stages.items()):
        if not str(key).isdigit() or not 0 <= int(key) < 65535 or not isinstance(name, str) or not name or '|' in name:
            raise ValueError('trace.stages requires 16-bit stage IDs and nonempty names')
    if len({int(k) for k in stages}) != len(stages):
        raise ValueError('trace.stages has duplicate numeric IDs')
    trace['stages'] = {str(int(k)): v for k, v in stages.items()}
    sites = _object(trace.setdefault('sites', {}), 'model trace.sites')
    if len({int(k) for k in sites if str(k).isdigit()}) != len(sites):
        raise ValueError('trace.sites requires unique numeric IDs')
    normalized = {}
    for key, spec in sites.items():
        spec = _object(spec, 'model trace site')
        if not str(key).isdigit() or not 0 <= int(key) < 65535:
            raise ValueError('trace site ID must fit 16 bits')
        mode, category = spec.get('mode'), spec.get('category', 'unknown')
        if (not isinstance(mode, str) or not isinstance(category, str) or
                mode not in known_modes or category not in CATEGORIES):
            raise ValueError('trace site must reference a declared mode and a supported category')
        normalized[str(int(key))] = dict(spec, category=category)
    trace['sites'] = normalized
    categories = _object(trace.setdefault('mode_categories', {}), 'model trace.mode_categories')
    for key, category in categories.items():
        if not isinstance(category, str) or key not in known_modes or category not in CATEGORIES:
            raise ValueError('mode_categories contains an undeclared mode or unsupported category')
    for mode in out['wait_modes']:
        categories.setdefault(mode, 'wait')
    for mode in out['exchange_modes']:
        categories.setdefault(mode, 'cx')
    for mode in out['idle_modes']:
        categories.setdefault(mode, 'drv')
    for spec in normalized.values():
        if spec['mode'] in categories and categories[spec['mode']] != spec['category']:
            raise ValueError('site category conflicts with its mode category')
        categories[spec['mode']] = spec['category']
    for mode in known_modes:
        categories.setdefault(mode, 'unknown')
    allowed_categories = (('compute_modes', {'mat', 'vec', 'mem', 'cc', 'fe', 'unknown'}),
                          ('exchange_modes', {'cx'}), ('wait_modes', {'wait'}), ('idle_modes', {'drv'}))
    for group, allowed in allowed_categories:
        if any(categories[mode] not in allowed for mode in out[group]):
            raise ValueError('mode category conflicts with its declared ' + group)
    out['mode_categories'] = copy.deepcopy(categories)
    labels = _object(out.setdefault('mode_labels', {}), 'model mode_labels')
    if any(mode not in known_modes or not isinstance(label, str) or not label for mode, label in labels.items()):
        raise ValueError('mode_labels must name declared modes')
    dense = _object(out.setdefault('dense', {}), 'model dense')
    for mode, chunks in _object(dense.get('matrix_steps', {}), 'dense matrix_steps').items():
        if mode not in known_modes or type(chunks) is not int or chunks < 1:
            raise ValueError('dense chunk count must reference a mode and be a positive integer')
    if dense.get('stage') is not None and (not isinstance(dense['stage'], str) or not dense['stage'] or '|' in dense['stage']):
        raise ValueError('dense stage must be nonempty')
    if dense.get('backend', 'dense_za_v1') not in ('dense_za_v1', 'none'):
        raise ValueError('unsupported dense analysis backend')
    families = _object(out.setdefault('stage_families', {}), 'model stage_families')
    family_membership = set()
    for key, spec in families.items():
        if not isinstance(key, str) or not key:
            raise ValueError('stage family ID must be nonempty')
        if isinstance(spec, list):
            _strings(spec, 'stage family')
        else:
            _object(spec, 'stage family')
            _strings(spec.get('stages', []), 'stage family stages')
            if 'label' in spec and (not isinstance(spec['label'], str) or not spec['label']):
                raise ValueError('stage family label must be nonempty')
            if 'color' in spec:
                _color(spec['color'], 'stage family color')
        members = set(spec if isinstance(spec, list) else spec.get('stages', []))
        if family_membership.intersection(members):
            raise ValueError('a stage must not belong to multiple stage families')
        family_membership.update(members)
    view = _object(out.setdefault('presentation', {}), 'model presentation')
    for field in ('stage_labels', 'labels', 'metric_labels'):
        values = _object(view.get(field, {}), 'presentation ' + field)
        if any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in values.items()):
            raise ValueError('presentation ' + field + ' must contain nonempty names and labels')
    views = _object(view.get('views', {}), 'presentation views')
    if 'dense_enabled' in views and type(views['dense_enabled']) is not bool:
        raise ValueError('presentation dense_enabled must be boolean')
    if any(k != 'dense_enabled' and not isinstance(v, str) for k, v in views.items()):
        raise ValueError('presentation view titles and descriptions must be strings')
    for palette in _object(view.get('palettes', {}), 'presentation palettes').values():
        for key, spec in _object(palette, 'presentation palette').items():
            if (not isinstance(key, str) or not key or not isinstance(spec, list) or len(spec) != 2 or
                    not isinstance(spec[0], str) or not spec[0]):
                raise ValueError('presentation palette entries require [label, color]')
            _color(spec[1], 'presentation palette color')
    for name, color in _object(view.get('stage_colors', {}), 'presentation stage_colors').items():
        if not isinstance(name, str):
            raise ValueError('stage color keys must be names')
        _color(color, 'stage color')
    flop = out.get('flop', {'adapter': 'none'})
    if isinstance(flop, str):
        flop = {'adapter': flop}
    _object(flop, 'model flop')
    if not isinstance(flop.get('adapter', 'none'), str) or flop.get('adapter', 'none') not in ADAPTERS:
        raise ValueError('unsupported FLOP adapter')
    flop.setdefault('adapter', 'none')
    if flop['adapter'] == 'matmul_components.v1':
        if not isinstance(flop.get('components'), list) or not flop['components']:
            raise ValueError('matmul_components adapter requires components')
        for component in flop['components']:
            _object(component, 'FLOP component')
            if component.get('kind', 'matmul') not in ('matmul', 'elementwise'):
                raise ValueError('unsupported FLOP component kind')
            if not isinstance(component.get('stage'), str) or not component['stage'] or '|' in component['stage']:
                raise ValueError('FLOP component stage is required')
    out['flop'] = flop
    return out


resolve_model = load_model


def list_models():
    """Describe shipped plugins without scanning user files or executing code."""
    result = []
    for name in ('glm5f', 'generic-transformer'):
        model = load_model(name)
        result.append(dict(profile_id=model['profile_id'], model=model.get('model'), engine=model.get('engine'),
                           trace_formats=model['trace']['format'], flop_adapter=model['flop']['adapter']))
    return result


def presentation(model, hardware=None):
    """Build pure display metadata; no phase name decides a numerical rule."""
    model = load_model(model)
    hp = (hardware or {}).get('presentation', {})
    display = copy.deepcopy(model.get('presentation', {}))
    families, stage_family = {}, {}
    colors = ('#5587ad', '#9274ab', '#b79055', '#4f9992', '#ac8e64', '#738ea4')
    for index, (key, value) in enumerate(model['stage_families'].items()):
        spec = {'stages': value} if isinstance(value, list) else value
        families[key] = dict(label=spec.get('label', key), color=spec.get('color', colors[index % len(colors)]),
                             stages=copy.deepcopy(spec.get('stages', [])))
        for stage in families[key]['stages']:
            stage_family.setdefault(stage, key)
    stages = {}
    names = set(model['trace']['stages'].values()) | set(stage_family) | set(model['exchange_stages'])
    for name in sorted(names):
        family = stage_family.get(name)
        stages[name] = dict(name=name, label=display.get('stage_labels', {}).get(name, name), family=family,
                            color=display.get('stage_colors', {}).get(name, families.get(family, {}).get('color', '#8196a5')))
    modes = {mode: {'label': model['mode_labels'].get(mode, mode), 'category': category}
             for mode, category in model['trace']['mode_categories'].items()}
    palettes = copy.deepcopy(hp.get('palettes', {}))
    if 'unit_load' in palettes:
        palettes.setdefault('units', copy.deepcopy(palettes['unit_load']))
    views = dict(hp.get('views', {}), **display.get('views', {}))
    views.setdefault('dense_enabled', bool(model['dense'].get('stage')) and model['dense'].get('backend') != 'none')
    views.setdefault('dense_title', display.get('dense_title', '子步骤归因'))
    labels = dict(hp.get('labels', {}), **display.get('labels', {}))
    hardware_display = (hardware or {}).get('display', {})
    labels.setdefault('matrix_instruction', hardware_display.get('instruction_label', 'Matrix instruction'))
    labels.setdefault('vector_unit', hardware_display.get('vector_label', 'Vector unit'))
    if isinstance(hardware_display.get('unit_labels'), dict):
        units = palettes.setdefault('units', {})
        for key, label in hardware_display['unit_labels'].items():
            if isinstance(label, str):
                previous = units.get(key, [key, '#7893a5'])
                units[key] = [label, previous[1]]
    # Hardware terms are defaults; explicit model presentation wins.
    for key, values in display.get('palettes', {}).items():
        palettes.setdefault('units' if key == 'unit_load' else key, {}).update(copy.deepcopy(values))
    return dict(schema_version='lxtrace.presentation.v1', model_label=model.get('model', model['profile_id']),
                stages=stages, stage_ids=copy.deepcopy(model['trace']['stages']), families=families, modes=modes,
                palettes=palettes, views=views,
                labels=labels, metric_labels=copy.deepcopy(display.get('metric_labels', {})),
                dense=dict(enabled=bool(model['dense'].get('stage')) and model['dense'].get('backend') != 'none',
                           title=display.get('dense_title', '子步骤归因'), stage=model['dense'].get('stage')))


def _positive_integer(value, label, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError(label + ' must be a ' + ('nonnegative' if zero else 'positive') + ' integer')
    return value


def _configuration(config):
    if isinstance(config, (str, Path)):
        path = Path(config)
        if path.is_dir():
            path = path / 'config.json'
        config = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('model FLOP configuration must be an object')
    _json_safe(config)
    return copy.deepcopy(config)


def _workload(config, workload):
    workload = dict(_object(workload, 'FLOP workload'))
    text = _object(config.get('text_config', config), 'model text_config')
    n = _positive_integer(text.get('num_hidden_layers', 1), 'model num_hidden_layers')
    result = {key: _positive_integer(workload.get(key, default), 'workload.' + key, key == 'T_c')
              for key, default in (('T', None), ('B', 1), ('T_c', 0))}
    layers = workload.get('layers', [0, n - 1])
    if (not isinstance(layers, (list, tuple)) or len(layers) != 2 or
            any(type(x) is not int for x in layers) or not 0 <= layers[0] <= layers[1] < n):
        raise ValueError('workload.layers must be an inclusive [first,last] model layer range')
    result['layers'] = list(layers)
    return result


def _factor(value, config, workload, depth=0):
    if isinstance(value, list):
        if not value or len(value) > 16 or depth > 8:
            raise ValueError('FLOP factor product requires 1..16 factors and bounded nesting')
        return math.prod(_factor(v, config, workload, depth + 1) for v in value)
    if type(value) is int and value >= 0:
        return value
    if not isinstance(value, str):
        raise ValueError('FLOP dimensions must be nonnegative integers or declared field references')
    if value == 'layers':
        return workload['layers'][1] - workload['layers'][0] + 1
    if value in workload and value != 'layers':
        return workload[value]
    if value.startswith('workload.'):
        key = value[9:]
        if key in workload and key != 'layers':
            return workload[key]
    if value.startswith('config.'):
        current = config.get('text_config', config)
        for key in value[7:].split('.'):
            if not key or not isinstance(current, dict) or key not in current:
                raise ValueError('missing FLOP dimension ' + value)
            current = current[key]
        return _positive_integer(current, value, zero=True)
    raise ValueError('unsupported FLOP field reference ' + value)


def calculate_flops(model, config=None, workload=None):
    """Return a numerator only. Window correspondence is verified by workflow.

    ``layers=[first,last]`` is inclusive. ``B`` counts independent requests;
    the GLM output head is counted once per request when the last layer is in
    the selected range. Hardware core count, clock and throughput never enter.
    """
    model = load_model(model)
    adapter = model['flop']['adapter']
    if adapter in ('none', 'external_summary'):
        return dict(status='unavailable', reason='matching_model_flop_not_supplied', total=None, phases={},
                    definition=model['flop'].get('definition', 'No built-in model FLOP counter declared'),
                    scope='application_supplied_matching_window_required', configuration=None)
    config = _configuration(config)
    work = _workload(config, workload)
    phases = {}
    if adapter == 'glm5f_prefill.v1':
        from .glm5f_flops import load_shape, count, by_stage
        shape = load_shape(config)
        lines = count(shape, work['T'], work['T_c'], range(work['layers'][0], work['layers'][1] + 1))
        phases = {key: value * work['B'] for key, value in by_stage(lines).items()}
    else:
        for component in model['flop']['components']:
            if component.get('kind', 'matmul') == 'matmul':
                value = 2 * math.prod(_factor(component.get(key), config, work) for key in ('m', 'n', 'k'))
            else:
                value = _factor(component.get('elements'), config, work) * _factor(component.get('flop_per_element'), config, work)
            value *= _factor(component.get('repeat', 1), config, work)
            stage = component['stage']
            phases[stage] = phases.get(stage, 0) + value
    total = sum(phases.values())
    if total <= 0 or total > 1e308 or not math.isfinite(total):
        raise ValueError('model FLOP total must be finite and positive')
    canonical = json.dumps(config, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return dict(status='available', total=total, phases=phases,
                definition=model['flop'].get('definition', adapter), adapter=adapter,
                scope=dict(kind='declared_prefill_workload', **work), configuration=work,
                model_config_sha256=hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
                profile_id=model['profile_id'], window_correspondence='not_proven_by_counter_alone')
