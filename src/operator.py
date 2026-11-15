from __future__ import annotations

import tempfile
import time
from pathlib import Path

from .acquisition_devices.acquisition_lock import acquisition_lock
from .acquisition_devices.loader import available_device_modes, create_configured_device
from .experiment.config import load_config, validate_config


def formal_device_modes(devices: dict) -> list[str]:
    if devices.get('devices', {}).get('realtime_simulation', {}).get('enabled'):
        raise ValueError('正式启动入口禁止实时模拟设备，请先关闭 realtime_simulation。')
    if devices.get('devices', {}).get('neuracle', {}).get('allow_software_trigger_fallback'):
        raise ValueError('正式 Neuracle 采集必须启用硬件 Trigger 回读，禁止软件降级。')
    return available_device_modes(devices, allow_simulation=False)


def preflight_device(mode: str, devices: dict, trigger_code: int) -> dict[str, object]:
    import json
    import uuid
    from contextlib import nullcontext
    if mode not in formal_device_modes(devices):
        raise ValueError('请选择已启用的真实采集设备；模拟测试请使用单独的测试入口。')
    annotation = devices.get('_preflight_annotation')
    output_root = devices.get('_preflight_output_root')
    directory = None
    if output_root:
        directory = Path(output_root) / (time.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8])
        directory.mkdir(parents=True, exist_ok=False)
    with acquisition_lock(mode, devices):
        device = create_configured_device(mode, devices)
        try:
            device.connect()
            health = device.health()
            if not health.connected:raise RuntimeError(health.message or 'Device is not connected')
            context = nullcontext(str(directory)) if directory else tempfile.TemporaryDirectory(prefix='scene-eeg-preflight-')
            with context as folder:
                output = Path(folder)/'preflight.edf'
                details = {};markers=[]
                try:
                    device.start_recording(output)
                    deadline = time.monotonic()+5
                    while device.health().sample_count < 1:
                        health=device.health()
                        if not health.connected or time.monotonic()>=deadline:
                            raise RuntimeError(health.message or '预检未收到真实 EEG 样本')
                        time.sleep(.01)
                    sync=device.trigger_self_test(trigger_code)
                    native = getattr(device,'acquisition_test',None)
                    if native:
                        annotation = annotation or dict(object_id='003',object_name='卷尺',
                            semantic_label='tapemeasure',category_name='卷尺',test_only=True)
                        details = native(trigger_code,dict(annotation,test_only=True))
                    elif annotation:
                        started=time.monotonic()
                        for code in (101,102):
                            stamp=time.monotonic_ns();point=device.record_event(code,stamp)
                            if point.quality!='hardware_verified' and mode=='neuracle':
                                raise RuntimeError('标注短测 Trigger 未通过硬件回读')
                            markers.append(dict(code=code,sample_index=point.sample_index,display_timestamp_ns=stamp,
                                annotation=annotation,test_only=True))
                            time.sleep(1)
                        while time.monotonic()-started<3:time.sleep(.01)
                    health=device.health()
                    if not health.connected:raise RuntimeError(health.message or '采集短测期间断连')
                    artifact=device.stop_recording()
                except BaseException:
                    disconnect=getattr(device,'disconnect',None)
                    try:
                        if disconnect:disconnect()
                        elif device.health().recording:device.stop_recording()
                    except Exception:pass
                    raise
                if artifact is None or not artifact.sample_count:raise RuntimeError('预检未保存 EEG 数据')
                if mode=='neuracle':
                    from .qc.saved_trigger import verify_saved_trigger
                    verify_saved_trigger(output,trigger_code,sync.sample_index)
                    for marker in markers:verify_saved_trigger(output,marker['code'],marker['sample_index'])
                    if markers:
                        marker_path=Path(folder)/'annotation_test.json'
                        marker_path.write_text(json.dumps(markers,ensure_ascii=False,indent=2),encoding='utf-8')
                        if json.loads(marker_path.read_text(encoding='utf-8'))!=markers:raise RuntimeError('物体标注回读不一致')
                elif artifact.source_transport=='brainco-full-stream-native':
                    from .acquisition_devices.brainco_native import NativeAudit
                    audit=NativeAudit(artifact.path);audit.verify_marker(trigger_code,sync.sample_index)
                    for code,index in device.preflight_markers:audit.verify_marker(code,index)
                    if not any(x.get('object_id')==annotation.get('object_id') and
                               x.get('semantic_label')==annotation.get('semantic_label') for x in audit.annotations):
                        raise RuntimeError('BrainCo 物品类别/实物编号未在已保存数据中回读到')
                expected='hardware_verified' if mode=='neuracle' else 'software_estimated'
                if sync.quality!=expected:raise RuntimeError(f'{mode} 自检返回 {sync.quality}，预期 {expected}')
                result=dict(mode=mode,connected=True,self_test=sync.quality,
                    sampling_rate_hz=health.sampling_rate_hz,channel_count=health.channel_count,
                    eeg_channels=32 if mode=='brainco' else 64,
                    saved_trigger_verified=mode=='neuracle',saved_annotation_verified=bool(annotation),
                    sample_count=artifact.sample_count,details=details,
                    test_kind='acquisition_and_annotation_roundtrip_not_display_latency',
                    recording_path=str(artifact.path),hardware_sync_verified=mode=='neuracle')
                if directory:
                    result['report_path']=str(directory/'test_report.json')
                    Path(result['report_path']).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                return result
        except BaseException as error:
            if directory:(directory/'failed.json').write_text(json.dumps(dict(error=str(error),mode=mode,passed=False),ensure_ascii=False,indent=2),encoding='utf-8')
            raise
        finally:
            disconnect=getattr(device,'disconnect',None)
            if disconnect:disconnect()


def launch_operator(config_path: Path, devices_path: Path, *, continue_collection: bool = False) -> int:
    return launch_operator_config(load_config(config_path), load_config(devices_path), continue_collection=continue_collection)


def launch_operator_config(experiment: dict, devices: dict, *, continue_collection: bool = False) -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    from .operator_ui import OperatorDialog

    from .experiment.session_runner import run_session

    validate_config(experiment, devices)
    modes = formal_device_modes(devices)
    if not modes:
        raise RuntimeError("No locally enabled acquisition device is available")

    app = QApplication.instance() or QApplication([])
    dialog = OperatorDialog(experiment, devices, modes, preflight_device)
    if continue_collection:
        QTimer.singleShot(0, lambda: dialog.begin_check(True))
    elif experiment.get('fixed_schedule') and not experiment.get('nas_progress_sync') and dialog.participant.text().strip():
        QTimer.singleShot(0, lambda: dialog.begin_check(False, progress_only=True))
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return 1
    selection = dialog.selection
    experiment['stimulus']['image_duration_ms'] = int(selection['image_duration_ms'])
    if selection.get('dual_image'):
        experiment['dual_image'] = selection['dual_image']
    if selection.get('collection'):
        experiment['collection'] = selection['collection']
    validate_config(experiment, devices)
    workspace = Path.cwd()
    manifest = Path(experiment["paths"]["stimulus_manifest"])
    if not manifest.is_absolute():
        manifest = (workspace / manifest).resolve()
    for key in ("dataset_root", "database"):
        value = Path(experiment["paths"][key])
        experiment["paths"][key] = str(value if value.is_absolute() else (workspace / value).resolve())
    selected_mode = str(selection["mode"])
    try:
        return run_session(
            experiment,
            devices,
            participant_id=str(selection["participant_id"]),
            day_index=int(selection["day_index"]),
            manifest_path=manifest,
            windowed=not bool(experiment['display'].get('fullscreen', True)),
            run_mode=selected_mode,
            device=create_configured_device(selected_mode, devices),
        )
    except (OSError, ValueError, RuntimeError) as error:
        QMessageBox.critical(None, '无法开始本次采集', str(error))
        return 1
