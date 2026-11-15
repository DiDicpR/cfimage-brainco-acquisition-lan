"""Immutable protocol order; resume by audited schedule trial IDs, never counts."""
from __future__ import annotations

from collections import Counter
import csv
import hashlib
import math
from pathlib import Path
import sqlite3

from .planner import PlannedTrial, SessionPlan, PlanError

EXTENSION_VERSION = 'brainco-append-v1'


def append_cycles(rows, repetitions, count=2580):
    """Stable prefix across target increases and computers; base rows untouched."""
    result = list(rows)
    prototypes = {r['image_id']: r for r in rows[:count]}
    for cycle in range(11, repetitions + 1):
        ordered = sorted(prototypes, key=lambda image: hashlib.sha256(
            f'{EXTENSION_VERSION}:{cycle}:{image}'.encode('utf-8')).digest())
        recent = {r['image_id'] for r in result[-30:]}
        first = [image for image in ordered if image not in recent][:30]
        first_set = set(first)
        ordered = first + [image for image in ordered if image not in first_set]
        for pos, image in enumerate(ordered, 1):
            result.append(dict(prototypes[image], trial_id=str((cycle-1)*count+pos),
                cycle=str(cycle), repeat_index=str(cycle), cycle_position=str(pos)))
    return result


def validate_settings(experiment):
    spec = experiment['fixed_schedule']
    brainco = spec.get('profile') == 'brainco_all_images'
    count = 2580 if brainco else 272
    duration = 600 if brainco else 1200
    repetitions = 10 if brainco else 100
    requested = experiment['collection']['target_repetitions']
    valid_target = (type(requested) is int and 10 <= requested <= 100) if brainco else requested == repetitions
    if (int(experiment['stimulus']['image_duration_ms']) != duration
            or int(experiment['stimulus']['isi_ms']) != 100
            or experiment['attention_probe']['enabled']
            or experiment.get('dual_image', {}).get('enabled', False)
            or not valid_target
            or experiment['collection']['strategy'] != 'scenes'
            or experiment['blocks']['trials_per_block'] != count
            or experiment['blocks']['minimum_rest_sec'] != 0
            or experiment['randomization']['seed'] != 20261112
            or any(experiment['baseline'].values())):
        raise ValueError('固定方案时长、图片范围或次数与锁定配置不符。')
    for path, expected in ((Path(spec['path']), spec['sha256']),
                           (Path(experiment['paths']['stimulus_manifest']), spec['manifest_sha256'])):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'固定方案文件校验失败，禁止更改排程或图片清单：{path}')


def read_schedule(experiment, stimuli, metadata):
    validate_settings(experiment)
    brainco = experiment['fixed_schedule'].get('profile') == 'brainco_all_images'
    count = 2580 if brainco else 272
    seconds = .6 if brainco else 1.2
    repetitions = 10 if brainco else 100
    with Path(experiment['fixed_schedule']['path']).open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    by_id = {s.image_id: s for s in stimuli}
    selected = experiment['collection'].get('selected_image_ids')
    if len(by_id) != count or (selected is not None and set(selected) != set(by_id)):
        raise ValueError(f'固定方案必须使用全部 {count} 张图片，不能增减选图。')
    if len(rows) != count * repetitions or Counter(r['image_id'] for r in rows) != Counter({k: repetitions for k in by_id}):
        raise ValueError(f'固定排程必须为 {count} 张图片 × {repetitions} 次。')
    for i, row in enumerate(rows):
        cycle = i // count + 1
        if (int(row['trial_id']) != i + 1 or int(row['cycle']) != cycle
                or int(row['repeat_index']) != cycle or int(row['cycle_position']) != i % count + 1
                or float(row['image_duration_s']) != seconds or float(row['inter_stimulus_s']) != .1):
            raise ValueError(f'固定排程序号或时序错误：{i + 1}')
        meta = metadata[row['image_id']]
        if (row['object_id'] != meta['object_id'] or row['scene_zh'] != meta['scene']
                or row['scene_id'] != meta['scene_id'] or row['category_en'] != by_id[row['image_id']].category_id
                or (row['view'] != meta['view'] if brainco else (row['view'] != 'front' or meta['view'] != '平视'))):
            raise ValueError(f'固定排程与物体/场景/视角清单不一致：{i + 1}')
    for start in range(0, len(rows), count):
        if {r['image_id'] for r in rows[start:start + count]} != set(by_id):
            raise ValueError(f'每个 cycle 必须恰好包含 {count} 张图片各一次。')
    if brainco:
        rows = append_cycles(rows, experiment['collection']['target_repetitions'], count)
    return rows


def resume_progress(database, participant, mode, dataset_root, experiment):
    from .collection import saved_progress
    # Each key includes the schedule digest, original trial ID and image identity.
    # Finalized presentations with bad timing remain in QC as invalid; they are
    # not silently replaced to turn 100 presentations into 100 valid EEG epochs.
    progress, rejected = saved_progress(database, participant, mode, dataset_root, by_schedule=True)
    digest = experiment['fixed_schedule']['sha256']
    if mode=='brainco' and experiment.get('nas_progress_sync'):
        from ..subject_sync import remote_completed
        remote=remote_completed(dataset_root,participant,database,digest)
        # Completion is a set of exact protocol positions, not a sum of repeats
        # across PCs. Original repeated trials remain unchanged for analysis.
        progress={key:1 for key in set(progress)|remote}
    if any(key[0] != digest for key in progress):
        raise ValueError('该被试已有不同固定排程的记录，请恢复原方案。')
    if any(count != 1 for count in progress.values()):
        raise ValueError('发现同一固定试次的多份已核验记录，请先核对重复边界。')
    return progress, rejected


def build_fixed_plan(stimuli, metadata, progress, experiment):
    rows = read_schedule(experiment, stimuli, metadata)
    digest = experiment['fixed_schedule']['sha256']
    known = {(digest, int(r['trial_id']), r['image_id']) for r in rows}
    if set(progress) - known:
        raise ValueError('保存的试次编号与固定排程不一致。')
    remaining = [r for r in rows if (digest, int(r['trial_id']), r['image_id']) not in progress]
    limit = int(experiment['collection']['max_blocks_per_session'])
    if limit:
        cycles = sorted({int(r['cycle']) for r in remaining})[:limit]
        remaining = [r for r in remaining if int(r['cycle']) in cycles]
    cycle_blocks = {cycle: i + 1 for i, cycle in enumerate(sorted({int(r['cycle']) for r in remaining}))}
    by_id = {s.image_id: s for s in stimuli}
    offsets = Counter()
    trials = []
    for i, row in enumerate(remaining, 1):
        stimulus = by_id[row['image_id']]
        block = cycle_blocks[int(row['cycle'])]
        offsets[block] += 1
        trials.append(PlannedTrial(global_index=i, block_index=block, trial_index=offsets[block],
            image_id=stimulus.image_id, concept_id=stimulus.concept_id, category_id=stimulus.category_id,
            file_path=str(stimulus.file_path), repetition_index=int(row['repeat_index']),
            schedule_trial_id=int(row['trial_id']), schedule_cycle=int(row['cycle']),
            schedule_cycle_position=int(row['cycle_position']), schedule_sha256=digest))
    plan = SessionPlan(20261112, experiment['randomization']['plan_version'], tuple(trials))
    summary = dict(strategy='scenes', fixed_schedule=True, selected_images=len(stimuli),
        target_repetitions=experiment['collection']['target_repetitions'], total_target=len(rows), total_completed=len(progress),
        group_remaining=len(rows) - len(progress),
        trial_limit=len(trials), planned_trials=len(trials), planned_blocks=plan.block_count,
        estimated_minutes=math.ceil(len(trials) * ((experiment['stimulus']['image_duration_ms'] + experiment['stimulus']['isi_ms']) / 1000) / 60), limited_by_image_gap=False,
        schedule_sha256=digest, resume_policy='verified_fixed_schedule_ids',
        next_schedule_trial_id=trials[0].schedule_trial_id if trials else None)
    if experiment['fixed_schedule'].get('profile') == 'brainco_all_images':
        summary['extension_version'] = EXTENSION_VERSION
    return plan, summary


def preview_fixed(experiment, participant, mode):
    from .scenes import read_scene_catalog
    registered = 10
    if mode == 'brainco' and experiment.get('nas_progress_sync'):
        from ..subject_sync import cached_target
        registered = cached_target(experiment, participant)
        experiment['collection']['target_repetitions'] = max(registered, experiment['collection']['target_repetitions'])
    stimuli, metadata = read_scene_catalog(Path(experiment['paths']['stimulus_manifest']).resolve())
    path = Path(experiment['paths']['database']).resolve()
    database = None
    try:
        if path.is_file():
            database = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            database.row_factory = sqlite3.Row
        progress, rejected = resume_progress(database, participant, mode,
            Path(experiment['paths']['dataset_root']).resolve(), experiment)
        _, summary = build_fixed_plan(stimuli, metadata, progress, experiment)
        if mode == 'brainco':
            complete = sum(key[1] <= registered * 2580 for key in progress) == registered * 2580
            if experiment.get('nas_progress_sync') and experiment['collection']['target_repetitions'] > registered and not complete:
                raise ValueError(f'请先完成当前每张 {registered} 次的目标，再增加累计次数。')
            summary.update(registered_target_repetitions=registered, extension_allowed=complete,
                           extension_version=EXTENSION_VERSION)
        summary['unverified_trials'] = rejected
        return summary
    finally:
        if database:
            database.close()
