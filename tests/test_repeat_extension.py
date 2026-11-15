import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import pytest
from src.acquisition import runtime
from src.config import load_config
from src.experiment.fixed_schedule import read_schedule,build_fixed_plan,preview_fixed,EXTENSION_VERSION
from src.experiment.scenes import read_scene_catalog
from src.subject_sync import (atomic_json,sync_subject_progress,cached_target,target_path,
    begin_synced_acquisition,end_synced_acquisition)
from src.nas_transfer import SMBStore

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def tmp_path():
    with tempfile.TemporaryDirectory() as folder:
        yield Path(folder)

@pytest.fixture
def experiment(tmp_path):
    e,d=runtime(load_config(ROOT/'config/brainco.toml'))
    e['paths'].update(dataset_root=str(tmp_path/'native'),database=str(tmp_path/'native/progress.sqlite'))
    return e,d


@pytest.mark.skipif(not (ROOT/'assets/stimuli').is_dir(),reason='Requires local stimuli')
def test_extended_order_stable_and_resume_only_new_cycles(experiment):
    e,_=experiment
    stimuli,metadata=read_scene_catalog(Path(e['paths']['stimulus_manifest']))
    base=read_schedule(e,stimuli,metadata)
    e['collection']['target_repetitions']=11
    eleven=read_schedule(e,stimuli,metadata)
    e['collection']['target_repetitions']=12
    twelve=read_schedule(e,stimuli,metadata)
    assert eleven[:25800]==base and twelve[:len(eleven)]==eleven
    assert len(twelve)==30960
    for cycle in (11,12):
        rows=twelve[(cycle-1)*2580:cycle*2580]
        assert Counter(r['image_id'] for r in rows)==Counter(r['image_id'] for r in base[:2580])
        assert all(int(r['cycle'])==cycle for r in rows)
        boundary=(cycle-1)*2580
        last={r['image_id']:i for i,r in enumerate(twelve[:boundary])}
        for i,row in enumerate(rows,boundary):
            assert i-last.get(row['image_id'],-1000)>30
            last[row['image_id']]=i
    digest=e['fixed_schedule']['sha256']
    done={(digest,int(r['trial_id']),r['image_id']):1 for r in eleven}
    plan,summary=build_fixed_plan(stimuli,metadata,done,e)
    assert len(plan.trials)==2580 and plan.trials[0].schedule_trial_id==28381
    assert summary['extension_version']==EXTENSION_VERSION
    # A hole must be resumed even when later positions already exist.
    done.pop(next(key for key in done if key[1]==25804))
    assert build_fixed_plan(stimuli,metadata,done,e)[0].trials[0].schedule_trial_id==25804


@pytest.mark.skipif(not (ROOT/'assets/stimuli').is_dir(),reason='Requires local stimuli')
def test_goal_cannot_extend_before_completion_and_adopts_remote_goal(experiment,tmp_path):
    e,_=experiment;store=SMBStore(tmp_path/'nas');root=tmp_path/'client'
    sync_subject_progress(e['paths']['dataset_root'],'TEST',root,store,lambda _:None)
    e['collection']['target_repetitions']=11
    with patch('src.experiment.fixed_schedule.resume_progress',return_value=({},0)):
        with pytest.raises(ValueError,match='先完成'):preview_fixed(e,'TEST','brainco')
    stimuli,meta=read_scene_catalog(Path(e['paths']['stimulus_manifest']))
    baseline=deepcopy(e);baseline['collection']['target_repetitions']=10
    done={(e['fixed_schedule']['sha256'],int(r['trial_id']),r['image_id']):1 for r in read_schedule(baseline,stimuli,meta)}
    with patch('src.experiment.fixed_schedule.resume_progress',return_value=(done,0)):
        assert preview_fixed(e,'TEST','brainco')['extension_allowed']
        reservation=begin_synced_acquisition(e,'TEST',root,store)
        end_synced_acquisition(reservation)
    other=deepcopy(baseline);other['paths'].update(dataset_root=str(tmp_path/'other-native'),database=str(tmp_path/'other-native/progress.sqlite'))
    sync_subject_progress(other['paths']['dataset_root'],'TEST',tmp_path/'other',store,lambda _:None)
    assert cached_target(other,'TEST')==11
    with patch('src.experiment.fixed_schedule.resume_progress',return_value=(done,0)):
        summary=preview_fixed(other,'TEST','brainco')
        assert summary['target_repetitions']==11 and not summary['extension_allowed']
        assert summary['next_schedule_trial_id']==25801
    # Another subject stays at the default.
    sync_subject_progress(other['paths']['dataset_root'],'OTHER',tmp_path/'other',store,lambda _:None)
    assert cached_target(other,'OTHER')==10


def test_bad_target_registry_refused(experiment,tmp_path):
    e,_=experiment;store=SMBStore(tmp_path/'nas')
    atomic_json(target_path(store,'TEST'),dict(participant='TEST',schema_version=1,
        extension_version='different',target_repetitions=12))
    with pytest.raises(ValueError,match='目标记录'):sync_subject_progress(e['paths']['dataset_root'],'TEST',tmp_path/'client',store)


@pytest.mark.skipif(not (ROOT/'assets/stimuli').is_dir(),reason='Requires local stimuli')
def test_workbench_auto_sync_and_unlock_do_not_leak_between_subjects(experiment):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from src.operator_ui import OperatorDialog
    from src.operator import formal_device_modes
    app=QApplication.instance() or QApplication([])
    e,d=experiment
    with patch('src.subject_sync.subject_catalog',return_value=['TEST','OTHER']) as catalog:
        dialog=OperatorDialog(e,d,formal_device_modes(d),lambda *_:None)
        with patch.object(dialog,'begin_check') as check:
            dialog.show()
            for _ in range(50):
                app.processEvents();QTest.qWait(20)
                if catalog.called and not dialog.catalog_worker:break
            assert catalog.called
            assert any(dialog.participant_choice.itemData(i)=='TEST' for i in range(dialog.participant_choice.count()))
            dialog.participant.setText('TEST');QTest.qWait(750)
            check.assert_called_with(False,progress_only=True)
            assert not dialog.target_repeats.isEnabled()
            summary=dict(fixed_schedule=True,total_completed=25800,total_target=25800,next_schedule_trial_id=None,
                trial_limit=0,estimated_minutes=0,registered_target_repetitions=10,target_repetitions=10,extension_allowed=True)
            dialog.show_collection_summary(summary)
            assert dialog.target_repeats.isEnabled()
            dialog.target_repeats.setValue(12)
            assert dialog.selected_experiment()['collection']['target_repetitions']==12
            dialog.participant.setText('OTHER')
            assert not dialog.target_repeats.isEnabled() and dialog.target_repeats.value()==10
            dialog.close();dialog.deleteLater();app.processEvents()


@pytest.mark.parametrize('exit_code',[0,1])
def test_finished_and_interrupted_sessions_upload_after_locks_released(experiment,exit_code):
    from contextlib import contextmanager
    from src.experiment.session_runner import run_session
    from src.nas_transfer import stopped_database
    e,d=experiment;held=[];uploaded=[]
    @contextmanager
    def lease(*_):
        held.append(True)
        try:yield
        finally:held.clear()
    def upload(*_):
        assert not held
        with stopped_database(Path(e['paths']['dataset_root'])):uploaded.append(True)
    with patch('src.subject_sync.begin_synced_acquisition',return_value={'test':True}), \
         patch('src.subject_sync.end_synced_acquisition',return_value=True), \
         patch('src.experiment.session_runner.acquisition_lock',side_effect=lease), \
         patch('src.experiment.session_runner._run_session_locked',return_value=exit_code), \
         patch('src.transfer_ui.auto_upload_after_session',side_effect=upload):
        assert run_session(e,d,participant_id='TEST',run_mode='brainco')==exit_code
        assert uploaded==[True]
