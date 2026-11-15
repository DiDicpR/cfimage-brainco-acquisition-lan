"""Cross-PC fixed-schedule progress from verified, immutable NAS backups.

Never import another PC's SQLite rows or EEG into the local recording database.
Network work happens before acquisition or after upload, never per stimulus.
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import sqlite3
import tempfile
import uuid

from .nas_transfer import (ROOT, SMBStore, canonical, filesystem_path, readonly_sqlite_uri,
                           safe_path, settings, sha256, store_for)

VERSION = 1
BEIJING = timezone(timedelta(hours=8))


def identity(participant):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', participant):
        raise ValueError('Invalid subject ID')
    return participant.removeprefix('sub-')


def machine_id(root=ROOT):
    local=Path(root)/'.local';local.mkdir(parents=True,exist_ok=True)
    path=local/'machine-id'
    if not path.exists():
        try:
            with path.open('x') as f:f.write(uuid.uuid4().hex)
        except FileExistsError:pass
    value=path.read_text().strip()
    if not re.fullmatch('[0-9a-f]{32}',value):raise ValueError('Invalid local machine ID')
    return value


def atomic_json(path,value):
    path=filesystem_path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as f:f.write(canonical(value));f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()


def smb_store(root=ROOT,store=None):
    store=store or store_for(settings(root),assets=False,write=True,root=root)
    if not isinstance(store,SMBStore):
        raise ValueError('跨电脑续采目前需要配置 SMB 数据公区；不能把 S3 列表误当完整进度。')
    return store


def _reservation(store,participant):
    return safe_path(store.root,'_subject_sync/sub-'+identity(participant)+'/pending-upload')


def check_reservation(store,participant,machine):
    path=_reservation(store,participant)
    if not path.exists():return None
    try:owner=json.loads((path/'owner.json').read_text(encoding='utf-8'))
    except (OSError,ValueError) as exc:
        raise RuntimeError('该被试的 NAS 占用记录尚未完整写入；请稍后重试或由管理员核查，不能自动跳过。') from exc
    if owner['machine_id']!=machine:
        raise RuntimeError(f"该被试在另一台电脑（{owner.get('hostname',owner['machine_id'])}）采集或尚未上传。请先在原电脑结束并上传，再换机续采；不会自动抢占。")
    return owner


def reserve_subject(store,participant,machine):
    path=_reservation(store,participant);path.parent.mkdir(parents=True,exist_ok=True)
    try:path.mkdir()
    except FileExistsError:return check_reservation(store,participant,machine)
    owner=dict(machine_id=machine,hostname=socket.gethostname(),participant=identity(participant),
               created_at=datetime.now(timezone.utc).isoformat(),state='collecting_or_pending_upload')
    atomic_json(path/'owner.json',owner)
    return owner


def release_after_upload(store,participant,machine):
    # The acquisition device lease is still held by the uploader here.
    path=_reservation(store,participant)
    if path.exists():
        owner=json.loads((path/'owner.json').read_text(encoding='utf-8'))
        if owner.get('machine_id')==machine:
            (path/'owner.json').unlink();path.rmdir()


def latest_snapshots(store,participant):
    folder=safe_path(store.root,'sub-'+identity(participant))
    if not folder.exists():return []
    result=[]
    for machine in sorted(folder.iterdir()):
        if not machine.is_dir() or not re.fullmatch('[0-9a-f]{32}',machine.name):continue
        snapshots=machine/'snapshots'
        candidates=[p for p in snapshots.glob('*.json') if re.fullmatch('[0-9a-f]{64}',p.stem)]
        if candidates:
            # Old releases have no head pointer; server file mtime also sees old
            # clients uploading after a new client. Never union old snapshots.
            chosen=max(candidates,key=lambda p:(p.stat().st_mtime_ns,p.name))
            head=machine/'latest-upload.json'
            if head.exists() and head.stat().st_mtime_ns>=chosen.stat().st_mtime_ns:
                receipt=json.loads(head.read_text(encoding='utf-8'))
                current=safe_path(store.root,receipt['snapshot_key'])
                if current.parent!=snapshots or not current.is_file():raise ValueError('Invalid latest upload receipt')
                chosen=current
            result.append((f'sub-{identity(participant)}/{machine.name}/snapshots/{chosen.name}',chosen.stat().st_mtime))
    return result


def subject_catalog(root=ROOT,store=None):
    """Only list subject names at startup; defer EEG audits until selection."""
    store=smb_store(root,store)
    return sorted(p.name[4:] for p in store.root.iterdir()
                  if p.is_dir() and re.fullmatch(r'sub-[A-Za-z0-9][A-Za-z0-9_-]{0,63}',p.name))


def target_path(store,participant):
    return safe_path(store.root,'_subject_sync/sub-'+identity(participant)+'/target.json')


def read_target(store,participant):
    from .experiment.fixed_schedule import EXTENSION_VERSION
    path=target_path(store,participant)
    if not path.exists():return None
    value=json.loads(path.read_text(encoding='utf-8'))
    if (value.get('participant')!=identity(participant) or value.get('schema_version')!=VERSION
        or value.get('extension_version')!=EXTENSION_VERSION
        or type(value.get('target_repetitions')) is not int or not 10<=value['target_repetitions']<=100):
        raise ValueError('NAS 被试目标记录损坏或版本不兼容，不能猜测续采次数。')
    return value


def cached_target(experiment,participant):
    path=Path(experiment['paths']['dataset_root'])/'.nas-progress'/('sub-'+identity(participant)+'.json')
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('participant')!=identity(participant):raise ValueError('Wrong subject target cache')
    target=value.get('target')
    if target and target['base_schedule_sha256']!=experiment['fixed_schedule']['sha256']:
        raise ValueError('NAS 被试目标使用其他排程，禁止混合续采。')
    return target['target_repetitions'] if target else 10


def _cached_object(store,item,cache):
    digest=item['sha256']
    if not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('Bad object hash')
    path=cache/digest
    if not path.exists() or sha256(path)!=digest:
        cache.mkdir(parents=True,exist_ok=True)
        temp=path.with_name(digest+'.'+uuid.uuid4().hex+'.partial')
        try:
            store.read(item['key'],temp)
            if temp.stat().st_size!=item['size'] or sha256(temp)!=digest:raise IOError('Progress object checksum mismatch')
            os.replace(temp,path)
        finally:
            if temp.exists():temp.unlink()
    return path


def audit_progress(database,participant,resolve_recording):
    """Compact exposure checkpoint; invalid-but-finalized trials stay consumed.

    A completed exposure is NOT an EEGQC-approved epoch. Aborted/planned/deleted
    trials never consume a schedule position.
    """
    from .acquisition_devices.brainco_native import NativeAudit
    completed=[];sessions=[];recordings=[];rejected=0
    with closing(sqlite3.connect(readonly_sqlite_uri(database),uri=True)) as db:
        db.row_factory=sqlite3.Row
        for s in db.execute('SELECT * FROM sessions WHERE participant_id=? AND run_mode=? ORDER BY start_time',(participant,'brainco')).fetchall():
            sid=s['session_id']
            presented=db.execute('SELECT count(*) FROM trials WHERE session_id=? AND actual_onset_ns IS NOT NULL',(sid,)).fetchone()[0]
            sessions.append(dict(session_id=sid,start_time=s['start_time'],end_time=s['end_time'],status=s['status'],presented=presented))
            trials=db.execute("SELECT * FROM trials WHERE session_id=? AND run_mode='brainco' AND status IN ('valid','invalid')",(sid,)).fetchall()
            if not trials:continue
            recording=db.execute("SELECT * FROM eeg_recordings WHERE session_id=? AND status='complete' AND source_transport='brainco-full-stream-native'",(sid,)).fetchall()
            if len(recording)!=1:rejected+=len(trials);continue
            audit=NativeAudit(resolve_recording(recording[0]['edf_path']))
            recordings.append(dict(session_id=sid,path=str(recording[0]['edf_path']).replace('\\','/'),samples=audit.samples))
            annotations={(x.get('trial_id'),x.get('event_type')):x for x in audit.annotations}
            events={}
            for e in db.execute('''SELECT e.*,y.eeg_sample_index,y.sync_quality FROM events e JOIN eeg_sync y USING(event_id)
                    WHERE e.session_id=? AND e.event_type IN ('IMAGE_ONSET','IMAGE_OFFSET') ORDER BY e.event_seq''',(sid,)):
                events.setdefault(e['trial_id'],{})[e['event_type']]=dict(e)
            for trial in trials:
                pair=events.get(trial['trial_id'],{})
                if set(pair)!={'IMAGE_ONSET','IMAGE_OFFSET'}:rejected+=1;continue
                on,off=pair['IMAGE_ONSET'],pair['IMAGE_OFFSET']
                if (any(e['sync_quality']!='software_estimated' or e['eeg_sample_index'] is None for e in pair.values())
                    or not 0<on['eeg_sample_index']<off['eeg_sample_index']):rejected+=1;continue
                try:
                    for kind,code in [('IMAGE_ONSET',101),('IMAGE_OFFSET',102)]:
                        event=pair[kind];audit.verify_marker(code,event['eeg_sample_index'])
                        raw=annotations.get((trial['trial_id'],kind),{})
                        if event['trigger_code']!=code or raw.get('image_id')!=trial['image_id'] or raw.get('display_timestamp_ns')!=event['timestamp_ns']:
                            raise ValueError('Saved image annotation does not match progress')
                except ValueError:rejected+=1;continue
                meta=json.loads(on['metadata_json'])
                if not meta.get('schedule_sha256') or type(meta.get('schedule_trial_id')) is not int:
                    raise ValueError('固定排程续采缺少原始编号，不能用次数猜测。')
                completed.append(dict(session_id=sid,trial_id=trial['trial_id'],image_id=trial['image_id'],
                    schedule_sha256=meta['schedule_sha256'],schedule_trial_id=meta['schedule_trial_id'],status=trial['status']))
    return dict(schema_version=VERSION,participant=identity(participant),sessions=sessions,completed=completed,recordings=recordings,
                rejected=rejected,meaning='verified_presentations_not_eeg_quality_approval')


def checkpoint_for_snapshot(store,key,root=ROOT,progress=print):
    prefix=str(PurePosixPath(key).parent.parent)
    expected=PurePosixPath(key).stem
    if not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('Bad snapshot name')
    cache=filesystem_path(Path(root)/'.local/subject-sync-cache');cache.mkdir(parents=True,exist_ok=True)
    manifest_path=cache/(expected+'.manifest.json')
    if not manifest_path.exists() or sha256(manifest_path)!=expected:
        store.read(key,manifest_path)
        if sha256(manifest_path)!=expected:raise IOError('Snapshot checksum mismatch')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if prefix!=f"sub-{identity(manifest['participant_id'])}/{manifest['machine_id']}":raise ValueError('Snapshot identity mismatch')
    entries={}
    for item in manifest['files']:
        safe_path(cache,item['path'])
        if item['key']!=prefix+'/objects/'+item['sha256'] or item['path'] in entries:raise ValueError('Invalid backup object mapping')
        entries[item['path']]=item
    dbitem=entries['progress.sqlite']
    def check_recordings(checkpoint):
        # Cheap existence/size checks on every sync. Full SHA readback was done
        # at upload; do not download multi-GB EEG just to resume the plan.
        records=checkpoint['recordings']
        if {r['session_id'] for r in checkpoint['completed']}-{r['session_id'] for r in records}:
            raise ValueError('Completed positions lack a saved recording reference')
        for record in records:
            item=entries['native/'+record['path']]
            if safe_path(store.root,item['key']).stat().st_size!=item['size']:
                raise IOError('Referenced EEG backup missing or changed size')
    checkpoint_path=cache/(expected+f'.checkpoint-v{VERSION}.json')
    if checkpoint_path.exists():
        envelope=json.loads(checkpoint_path.read_text(encoding='utf-8'))
        checkpoint=envelope['checkpoint']
        if (envelope['sha256']==hashlib.sha256(canonical(checkpoint)).hexdigest()
            and checkpoint.get('schema_version')==VERSION and checkpoint.get('snapshot_key')==key
            and checkpoint.get('participant')==identity(manifest['participant_id'])
            and checkpoint.get('machine_id')==manifest['machine_id']
            and checkpoint.get('progress_sha256')==dbitem['sha256']):
            if 'recordings' in checkpoint:
                check_recordings(checkpoint)
                return manifest,checkpoint
            # Development/legacy cache without recording references is rebuilt.
        else:raise IOError('Local progress cache damaged; remove the cache and synchronize again')
    progress('核对 NAS 进度及已保存标记：'+manifest['machine_id'][:8])
    compact=entries.get('resume/verified-progress-v1.json')
    if compact:
        checkpoint=json.loads(_cached_object(store,compact,cache).read_text(encoding='utf-8'))
        if (checkpoint.get('schema_version')!=VERSION or checkpoint.get('participant')!=identity(manifest['participant_id'])
            or checkpoint.get('progress_sha256')!=dbitem['sha256']):raise ValueError('Compact progress provenance mismatch')
    else:
        dbpath=_cached_object(store,dbitem,cache)
        def resolve(path):
            keypath='native/'+str(path).replace('\\','/')
            item=entries[keypath]
            target=safe_path(store.root,item['key'])
            if target.stat().st_size!=item['size']:raise IOError('EEG backup size changed')
            return target
        checkpoint=audit_progress(dbpath,manifest['participant_id'],resolve)
    checkpoint.update(snapshot_key=key,machine_id=manifest['machine_id'],progress_sha256=dbitem['sha256'])
    check_recordings(checkpoint)
    atomic_json(checkpoint_path,dict(checkpoint=checkpoint,sha256=hashlib.sha256(canonical(checkpoint)).hexdigest()))
    return manifest,checkpoint


def _local_sessions(dataset_root,participant):
    path=Path(dataset_root)/'progress.sqlite'
    if not path.exists():return []
    with closing(sqlite3.connect(readonly_sqlite_uri(path),uri=True)) as db:
        return [dict(session_id=sid,start_time=start,end_time=end,status=status,presented=n) for sid,start,end,status,n in db.execute('''
            SELECT s.session_id,s.start_time,s.end_time,s.status,
                (SELECT count(*) FROM trials t WHERE t.session_id=s.session_id AND t.actual_onset_ns IS NOT NULL)
            FROM sessions s WHERE participant_id=? AND run_mode='brainco' ''',(participant,))]


def _today(session,day):
    # Acquisition dates use UTC ISO times; cross-midnight sessions count on both days.
    start=datetime.fromisoformat(session['start_time']);end=datetime.fromisoformat(session['end_time'] or session['start_time'])
    if start.tzinfo is None or end.tzinfo is None:raise ValueError('Session date lacks timezone')
    return session['presented']>0 and start.astimezone(BEIJING).date()<=day<=end.astimezone(BEIJING).date()


def upload_overview(dataset_root,participant,root=ROOT,store=None,now=None):
    store=smb_store(root,store);machine=machine_id(root);day=(now or datetime.now(BEIJING)).astimezone(BEIJING).date()
    local=_local_sessions(dataset_root,participant)
    same=[(key,stamp) for key,stamp in latest_snapshots(store,participant) if key.split('/')[1]==machine]
    uploaded_today=any(datetime.fromtimestamp(stamp,BEIJING).date()==day for _,stamp in same)
    head=safe_path(store.root,f'sub-{identity(participant)}/{machine}/latest-upload.json')
    if head.exists():
        head_value=json.loads(head.read_text(encoding='utf-8'))
        uploaded_today=uploaded_today or datetime.fromisoformat(head_value['uploaded_at']).astimezone(BEIJING).date()==day
    return dict(participant=participant,date=str(day),local_today_sessions=sum(_today(s,day) for s in local),
                local_total_sessions=len(local),already_uploaded_today=uploaded_today,machine_id=machine)


def sync_subject_progress(dataset_root,participant,root=ROOT,store=None,progress=print):
    store=smb_store(root,store);machine=machine_id(root)
    owner=check_reservation(store,participant,machine)
    checkpoints=[]
    for key,_ in latest_snapshots(store,participant):
        _,checkpoint=checkpoint_for_snapshot(store,key,root,progress)
        if checkpoint['participant']!=identity(participant):raise ValueError('Wrong subject checkpoint')
        checkpoints.append(checkpoint)
    target=read_target(store,participant)
    value=dict(schema_version=VERSION,participant=identity(participant),synced_at=datetime.now(timezone.utc).isoformat(),checkpoints=checkpoints,target=target)
    path=Path(dataset_root)/'.nas-progress'/('sub-'+identity(participant)+'.json')
    atomic_json(path,value)
    local=_local_sessions(dataset_root,participant);day=datetime.now(BEIJING).date()
    local_ids={s['session_id'] for s in local}
    remote_today={s['session_id'] for c in checkpoints for s in c['sessions'] if s['session_id'] not in local_ids and _today(s,day)}
    return dict(remote_machines=len(checkpoints),remote_today_sessions=len(remote_today),
                local_today_sessions=sum(_today(s,day) for s in local),
                pending_upload=bool(owner),date=str(day),checkpoint_path=str(path),target_repetitions=target['target_repetitions'] if target else 10,
                message='NAS 进度已核对；仅合并完成位置，不下载旧 EEG、不覆盖本机数据库。')


def remote_completed(dataset_root,participant,database,digest):
    path=Path(dataset_root)/'.nas-progress'/('sub-'+identity(participant)+'.json')
    if not path.exists():raise RuntimeError('尚未同步 NAS 进度，请先在工作台查看剩余进度或预检续采。')
    value=json.loads(path.read_text(encoding='utf-8'))
    if value.get('schema_version')!=VERSION or value.get('participant')!=identity(participant):raise ValueError('Progress cache identity/version mismatch')
    local_ids={r[0] for r in database.execute('SELECT session_id FROM sessions WHERE participant_id=?',(participant,))} if database else set()
    result=set()
    for checkpoint in value['checkpoints']:
        for row in checkpoint['completed']:
            # Local latest status wins over its previously uploaded snapshot,
            # including rollbacks/removals. Never revive a contaminated local tail.
            if row['session_id'] in local_ids:continue
            if row['schedule_sha256']!=digest:raise ValueError('NAS 上该被试使用另一固定排程，禁止混合续采。')
            result.add((digest,row['schedule_trial_id'],row['image_id']))
    return result


def begin_synced_acquisition(experiment,participant,root=ROOT,store=None):
    store=smb_store(root,store);machine=machine_id(root)
    previous=check_reservation(store,participant,machine)
    reserve_subject(store,participant,machine)
    # Re-read after acquiring the NAS reservation, closing the preview/start race.
    try:
        summary=sync_subject_progress(experiment['paths']['dataset_root'],participant,root,store)
        if experiment.get('collection'):
            from .experiment.fixed_schedule import preview_fixed,EXTENSION_VERSION
            plan=preview_fixed(experiment,participant,'brainco')
            target=dict(schema_version=VERSION,participant=identity(participant),
                target_repetitions=plan['target_repetitions'],base_schedule_sha256=experiment['fixed_schedule']['sha256'],
                extension_version=EXTENSION_VERSION,updated_at=datetime.now(timezone.utc).isoformat(),machine_id=machine)
            atomic_json(target_path(store,participant),target)
            # Keep local runtime and its checkpoint consistent with the reserved goal.
            cache=Path(summary['checkpoint_path']);value=json.loads(cache.read_text(encoding='utf-8'))
            value['target']=target;atomic_json(cache,value)
        return dict(store=store,machine=machine,new_reservation=previous is None,
                    dataset_root=experiment['paths']['dataset_root'],participant=participant,
                    before_presentations=sum(s['presented'] for s in _local_sessions(experiment['paths']['dataset_root'],participant)),
                    summary=summary)
    except BaseException:
        if previous is None:release_after_upload(store,participant,machine)
        raise


def end_synced_acquisition(reservation):
    if reservation:
        after=sum(s['presented'] for s in _local_sessions(reservation['dataset_root'],reservation['participant']))
        if after==reservation['before_presentations'] and reservation['new_reservation']:
            release_after_upload(reservation['store'],reservation['participant'],reservation['machine'])
        return after>reservation['before_presentations']
    return False
