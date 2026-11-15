import json,sqlite3,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from src.storage.database import connect_database
from src.nas_transfer import SMBStore,upload_subject,sha256,canonical,safe_path
from src.subject_sync import (sync_subject_progress,machine_id,reserve_subject,check_reservation,
    upload_overview,checkpoint_for_snapshot,remote_completed,begin_synced_acquisition,end_synced_acquisition)
from src.experiment.fixed_schedule import resume_progress

DIGEST='a'*64


def add_session(root,sid,positions,statuses=None,start='2026-11-14T16:30:00+00:00',end='2026-11-14T16:40:00+00:00'):
    native=root/'data/native';native.mkdir(parents=True,exist_ok=True)
    db=connect_database(native/'progress.sqlite')
    statuses=statuses or ['valid']*len(positions)
    folder=native/'12345678'/sid/'eeg';folder.mkdir(parents=True)
    recording=folder/'recording.brainco/continuous_eeg.sqlite3';recording.parent.mkdir()
    raw=sqlite3.connect(recording)
    raw.executescript('CREATE TABLE metadata(key TEXT,value TEXT); CREATE TABLE chunks(row_start INTEGER,n INTEGER,channels INTEGER,eeg BLOB,timestamps BLOB); CREATE TABLE events(kind TEXT,data TEXT);')
    n=max(10,len(positions)*10)
    raw.execute('INSERT INTO metadata VALUES (?,?)',('state',json.dumps(dict(status='closed',samples=n))))
    raw.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',(0,n,32,bytes(n*32*8),bytes(n*8)))
    db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)',(sid,'12345678',1,start,end,'h',1,f'12345678/{sid}/plan.csv','brainco','brainco','completed'))
    block=sid+'-block'
    db.execute('INSERT INTO blocks VALUES (?,?,?,?,?)',(block,sid,1,len(positions),'completed'))
    db.execute('''INSERT INTO eeg_recordings(recording_id,session_id,device_type,source_transport,edf_path,sha256,file_size_bytes,status)
                  VALUES (?,?,?,?,?,?,?,?)''',(sid,sid,'brainco','brainco-full-stream-native',recording.relative_to(native).as_posix(),'hash',1,'complete'))
    for i,(position,status) in enumerate(zip(positions,statuses)):
        trial=sid+'-trial-'+str(i);image='image-'+str(position);on=1_000_000_000+i*1_000_000_000;off=on+600_000_000
        db.execute('''INSERT INTO trials(trial_id,session_id,block_id,image_id,repetition_index,global_index,status,actual_onset_ns,actual_offset_ns,planned_frame_count,trigger_status,run_mode)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',(trial,sid,block,image,1,i,status,on,off,45,'verified','brainco'))
        for j,(kind,code,stamp,index) in enumerate([('IMAGE_ONSET',101,on,i*10+1),('IMAGE_OFFSET',102,off,i*10+8)]):
            meta=dict(schedule_sha256=DIGEST,schedule_trial_id=position,image_id=image)
            event=db.execute('INSERT INTO events(event_seq,session_id,trial_id,event_type,timestamp_ns,trigger_code,metadata_json) VALUES (?,?,?,?,?,?,?)',
                             (i*2+j,sid,trial,kind,stamp,code,json.dumps(meta))).lastrowid
            db.execute('INSERT INTO eeg_sync(event_id,eeg_sample_index,alignment_method,sync_quality) VALUES (?,?,?,?)',(event,index,'test','software_estimated'))
            raw.execute('INSERT INTO events VALUES (?,?)',('aligned_marker',json.dumps(dict(code=code,sample_index=index,quality='software_estimated'))))
            raw.execute('INSERT INTO events VALUES (?,?)',('experiment_event',json.dumps(dict(event_type=kind,trial_id=trial,image_id=image,display_timestamp_ns=stamp))))
    db.commit();db.close();raw.commit();raw.close()
    return native


class SubjectSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        self.a=self.folder/'pc-a';self.b=self.folder/'pc-b';self.store=SMBStore(self.folder/'nas')
        self.exp={'fixed_schedule':{'sha256':DIGEST},'nas_progress_sync':True}
    def tearDown(self):self.tmp.cleanup()
    def upload(self,root,native):return upload_subject(native,'12345678',root,self.store,lambda _:None)
    def sync(self,root,native):return sync_subject_progress(native,'12345678',root,self.store,lambda _:None)
    def progress(self,native):
        db=connect_database(native/'progress.sqlite')
        try:return resume_progress(db,'12345678','brainco',native,self.exp)[0]
        finally:db.close()

    def test_two_pc_resume_incremental_upload_dedup_and_rollback(self):
        native_a=add_session(self.a,'session-a',[1,2,3],['valid','invalid','aborted'])
        first=self.upload(self.a,native_a)
        native_b=self.b/'data/native';self.sync(self.b,native_b)
        self.assertEqual({k[1] for k in self.progress(native_b)},{1,2})
        self.assertFalse((native_b/'12345678/session-a').exists()) # No EEG copy or DB import.
        add_session(self.b,'session-b',[1,3])
        self.assertEqual({k[1] for k in self.progress(native_b)},{1,2,3})
        self.upload(self.b,native_b)
        again=self.upload(self.a,native_a)
        self.assertEqual(first['snapshot_key'],again['snapshot_key'])
        self.assertEqual(again['uploaded_files'],0)
        self.assertEqual(again['uploaded_bytes'],0)
        self.assertEqual(again['reused_files'],again['files'])
        # Roll back a contaminated tail on its authoritative source computer.
        db=sqlite3.connect(native_a/'progress.sqlite')
        db.execute("UPDATE trials SET status='aborted' WHERE image_id='image-2'");db.commit();db.close()
        changed=self.upload(self.a,native_a)
        self.assertEqual(changed['uploaded_files'],2) # progress DB + small checkpoint, no old EEG
        self.sync(self.b,native_b)
        self.assertEqual({k[1] for k in self.progress(native_b)},{1,3})
        self.sync(self.a,native_a)
        self.assertEqual({k[1] for k in self.progress(native_a)},{1,3})

    def test_reservation_blocks_other_pc_until_verified_upload(self):
        native=add_session(self.a,'session-a',[1])
        a=machine_id(self.a);b=machine_id(self.b)
        reserve_subject(self.store,'12345678',a)
        with self.assertRaisesRegex(RuntimeError,'另一台电脑'):
            reserve_subject(self.store,'12345678',b)
        with self.assertRaisesRegex(RuntimeError,'另一台电脑'):self.sync(self.b,self.b/'data/native')
        self.upload(self.a,native)
        self.assertIsNone(check_reservation(self.store,'12345678',b))
        self.sync(self.b,self.b/'data/native')

    def test_failed_upload_does_not_release_subject(self):
        native=add_session(self.a,'session-a',[1]);owner=machine_id(self.a)
        reserve_subject(self.store,'12345678',owner)
        with patch.object(self.store,'write',side_effect=OSError('offline')):
            with self.assertRaises(OSError):self.upload(self.a,native)
        self.assertIsNotNone(check_reservation(self.store,'12345678',owner))

    def test_failed_preparation_without_new_data_releases_new_reservation(self):
        native=add_session(self.a,'session-a',[1]);self.upload(self.a,native)
        e=dict(self.exp,paths={'dataset_root':str(self.b/'data/native')})
        reservation=begin_synced_acquisition(e,'12345678',self.b,self.store)
        end_synced_acquisition(reservation)
        self.assertIsNone(check_reservation(self.store,'12345678',machine_id(self.a)))

    def test_same_day_uses_beijing_date_and_retry_receipt(self):
        native=add_session(self.a,'session-a',[1])
        now=datetime(2026,11,14,17,0,tzinfo=timezone.utc) # 11/15 Beijing
        info=upload_overview(native,'12345678',self.a,self.store,now)
        self.assertEqual(info['local_today_sessions'],1)
        self.assertFalse(info['already_uploaded_today'])
        self.upload(self.a,native)
        self.assertEqual(upload_overview(native,'12345678',self.a,self.store,now)['local_today_sessions'],1)

    def test_schedule_mismatch_refused(self):
        native=add_session(self.a,'session-a',[1]);self.upload(self.a,native)
        other=self.b/'data/native';self.sync(self.b,other)
        with self.assertRaisesRegex(ValueError,'另一固定排程'):remote_completed(other,'12345678',None,'b'*64)

    def test_corrupt_manifest_or_missing_raw_eeg_never_accepted(self):
        native=add_session(self.a,'session-a',[1]);result=self.upload(self.a,native)
        manifest_path=safe_path(self.store.root,result['snapshot_key'])
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        recording=next(x for x in manifest['files'] if x['path']=='resume/verified-progress-v1.json')
        safe_path(self.store.root,recording['key']).write_bytes(b'damaged')
        with self.assertRaises(IOError):self.sync(self.b,self.b/'data/native')
        self.assertFalse((self.b/'data/native/.nas-progress/sub-12345678.json').exists())

    def test_local_rollback_overrides_old_uploaded_copy(self):
        native=add_session(self.a,'session-a',[1,2]);self.upload(self.a,native);self.sync(self.a,native)
        db=sqlite3.connect(native/'progress.sqlite');db.execute("UPDATE trials SET status='aborted' WHERE image_id='image-2'");db.commit();db.close()
        self.assertEqual({k[1] for k in self.progress(native)},{1})

    def test_missing_sync_is_not_silently_treated_as_new_subject(self):
        with self.assertRaisesRegex(RuntimeError,'尚未同步'):remote_completed(self.b/'data/native','12345678',None,DIGEST)

    def test_other_subject_is_not_imported(self):
        native=add_session(self.a,'session-a',[1]);self.upload(self.a,native)
        other=self.b/'data/native'
        sync_subject_progress(other,'87654321',self.b,self.store,lambda _:None)
        self.assertEqual(remote_completed(other,'87654321',None,DIGEST),set())

    def test_compact_checkpoint_needs_no_remote_eeg_audit_but_missing_eeg_blocks(self):
        native=add_session(self.a,'session-a',[1]);result=self.upload(self.a,native)
        with patch('src.acquisition_devices.brainco_native.NativeAudit',side_effect=AssertionError('must use compact checkpoint')):
            self.sync(self.b,self.b/'data/native')
        manifest=json.loads(safe_path(self.store.root,result['snapshot_key']).read_text(encoding='utf-8'))
        item=next(x for x in manifest['files'] if 'continuous_eeg.sqlite3' in x['path'])
        safe_path(self.store.root,item['key']).unlink()
        with self.assertRaises(OSError):self.sync(self.b,self.b/'data/native')

    def test_new_receipt_can_select_a_previously_existing_snapshot(self):
        from src.subject_sync import latest_snapshots,atomic_json
        import time
        native=add_session(self.a,'session-a',[1]);first=self.upload(self.a,native)
        prefix='/'.join(first['snapshot_key'].split('/')[:2])
        second_key=prefix+'/snapshots/'+'b'*64+'.json'
        atomic_json(safe_path(self.store.root,second_key),{'placeholder':True})
        time.sleep(.02)
        atomic_json(safe_path(self.store.root,prefix+'/latest-upload.json'),dict(snapshot_key=first['snapshot_key']))
        self.assertEqual(latest_snapshots(self.store,'12345678')[0][0],first['snapshot_key'])

    def test_upload_refuses_an_acquisition_database_lock(self):
        from src.nas_transfer import stopped_database
        native=add_session(self.a,'session-a',[1])
        with stopped_database(native):
            with self.assertRaisesRegex(RuntimeError,'准备或执行采集'):self.upload(self.a,native)
