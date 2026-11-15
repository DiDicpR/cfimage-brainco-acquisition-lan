"""TOML-to-engine adapter. Imports never open the cap or start collection."""
from pathlib import Path
from copy import deepcopy
import hashlib, json, sys
from .config import BrainCoConfig
from .project_metadata import load_project_metadata

ROOT=Path(__file__).resolve().parents[1]

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def runtime(config: BrainCoConfig):
    from .runtime_defaults import EXPERIMENT,DEVICES
    e,d=deepcopy(EXPERIMENT),deepcopy(DEVICES)
    e['project']['name']='强脑全场景图片脑电采集 · 自动续采版 20261115 · uv'
    e['paths']=dict(dataset_root=str(config.output_dir),database=str(config.output_dir/'progress.sqlite'),stimulus_manifest=str(config.assets['manifest']))
    e['nas_progress_sync']=True
    e['fixed_schedule'].update(path=str(config.assets['schedule']),sha256=config.assets['schedule_sha256'],manifest_sha256=config.assets['manifest_sha256'])
    e['stimulus'].update(image_duration_ms=round(config.duration_s*1000),isi_ms=round(config.protocol['isi_s']*1000))
    e['operator']['default_participant']=config.protocol['participant']
    e['collection'].update(target_repetitions=config.protocol['repetitions'],max_blocks_per_session=config.protocol['max_cycles_per_session'])
    e['display'].update(config.display)
    e['presentation_break'].update(every_images=config.protocol['break_every'],question_duration_ms=round(config.protocol['math_duration_s']*1000),resume_delay_ms=round(config.protocol['resume_delay_s']*1000))
    d['devices']['brainco'].update({k:v for k,v in config.device_options.items() if k not in {'backend','channels','sample_rate_hz'}})
    d['devices']['brainco'].update(channel_count=config.channels,sampling_rate_hz=config.sample_rate_hz,sdk_python=sys.executable)
    # Single root uv environment, including the acquisition subprocess.
    metadata=load_project_metadata(ROOT/'metadata.toml')
    code_hash=hashlib.sha256()
    source_files = list((ROOT/'src').rglob('*.py'))
    source_files += list((ROOT/'vendor/brainco_standalone').glob('*.py'))
    source_files += list((ROOT/'vendor/brainco_standalone/vendor').rglob('*.py'))
    for p in sorted(source_files):
        if 'tests' not in p.parts:code_hash.update(p.relative_to(ROOT).as_posix().encode()+p.read_bytes())
    e['provenance']=dict(schema_version=2,paradigm_code=metadata.unique_name,source_sha256=code_hash.hexdigest(),uv_lock_sha256=digest(ROOT/'uv.lock'),config_toml=config.source_path.read_text(encoding='utf-8'),config_sha256=digest(config.source_path),stimulus_manifest_sha256=config.assets['manifest_sha256'],schedule_sha256=config.assets['schedule_sha256'],python=sys.version,hardware_sync_verified=False)
    return e,d

def check_assets(config, *, all_images=True):
    from .experiment.scenes import read_scene_catalog
    from .experiment.fixed_schedule import read_schedule
    from PIL import Image
    e,d=runtime(config)
    stimuli,meta=read_scene_catalog(config.assets['manifest'])
    rows=read_schedule(e,stimuli,meta)
    if all_images:
        for item in stimuli:
            path=item.file_path
            if digest(path)!=meta[item.image_id]['sha256']:raise ValueError(f'Image checksum mismatch: {path}')
            with Image.open(path) as image:image.verify()
    return dict(images=len(stimuli),trials=len(rows),scenes=len({r['scene'] for r in meta.values()}),views=sorted({r['view'] for r in meta.values()}),full_image_validation=all_images)

def check_device(config):
    from .operator import preflight_device
    _,devices=runtime(config)
    devices['_preflight_output_root']=str(ROOT/'reports/device_checks')
    devices['_preflight_annotation']=dict(object_id='TEST',object_name='UV connectivity check',semantic_label='test',category_name='test',test_only=True)
    return preflight_device('brainco',devices,200)

def launch(config):
    # Full local image validation precedes any actual acquisition/preflight.
    check_assets(config)
    from .operator import launch_operator_config
    e,d=runtime(config)
    return launch_operator_config(e,d)
