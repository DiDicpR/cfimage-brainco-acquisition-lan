"""32-channel BrainCo full-stream SDK; native float64 archive, estimated event mapping."""
from __future__ import annotations
import importlib.util
import json
import math
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
import numpy as np
from .base import EEGDevice, DeviceHealth, RecordingArtifact, SyncPoint, PendingSync

KIT = Path(__file__).resolve().parents[2] / 'vendor/brainco_standalone'
MODULES = {}

def kit_module(name):
    if name not in MODULES:
        spec = importlib.util.spec_from_file_location('_scene_brainco_' + name, KIT / (name + '.py'))
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        MODULES[name] = module
    return MODULES[name]


def source_config(config):
    return kit_module('brainco_adapter').validate_config(dict(
        address=config.get('host', ''), port=int(config.get('port', 0)),
        auto_discover=config.get('auto_discover', True), sample_rate=int(config.get('sampling_rate_hz',250)),
        channels=int(config.get('channel_count',32)), gain=int(config.get('gain',6)),
        scan_timeout=int(config.get('scan_timeout',6)), ready_timeout=int(config.get('ready_timeout',20)),
        device_id=config.get('device_id','eeg-cap')))


class NativeAudit:
    """Verify closed, saved samples and marker rows; never use mere file existence."""
    def __init__(self, path):
        self.path = Path(path)
        from ..nas_transfer import readonly_sqlite_uri
        with closing(sqlite3.connect(readonly_sqlite_uri(self.path),uri=True)) as db:
            state = db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()
            if not state or json.loads(state[0]).get('status') != 'closed':
                raise ValueError('BrainCo 连续文件未正常关闭')
            chunks = db.execute('SELECT row_start,n,channels,length(eeg),length(timestamps) FROM chunks ORDER BY row_start').fetchall()
            self.samples=0
            for start,n,c,size,times in chunks:
                if start!=self.samples or c!=32 or n<1 or size!=n*32*8 or times!=n*8:
                    raise ValueError('BrainCo 保存的通道、样本计数或数据长度不一致')
                self.samples+=n
            if not self.samples or self.samples!=json.loads(state[0]).get('samples'):
                raise ValueError('BrainCo 已收与已存样本数不一致或文件为空')
            self.markers=set();self.annotations=[]
            for kind,data in db.execute("SELECT kind,data FROM events WHERE kind IN ('aligned_marker','experiment_event')"):
                value=json.loads(data)
                if kind=='aligned_marker' and value.get('quality')=='software_estimated':
                    row=value['sample_index']
                    if not 1<=row<=self.samples:raise ValueError('BrainCo 标记超出保存的样本范围')
                    self.markers.add((value['code'],row))
                elif kind=='experiment_event':self.annotations.append(value)

    def verify_marker(self, code, index):
        if (code,index) not in self.markers:
            raise ValueError('BrainCo 软件标记未在连续文件中回读到')


class StandaloneBrainCoDevice(EEGDevice):
    def __init__(self, config, source_factory=None):
        self.config=config;self.settings=source_config(config);self.rate=self.settings['sample_rate']
        self.source_factory=source_factory;self.source=None;self.cap=None;self.archive=None
        self.connected=False;self.recording=False;self.error='';self.artifact=None;self.lease=None
        self.markers={};self.marker_seq=0;self.last_marker_time=None;self.closed_count=0

    def connect(self):
        if self.connected:return
        self.error="";self.artifact=None;self.markers={};self.last_marker_time=None
        if self.source_factory:
            self.source=self.source_factory()
        else:
            import sys
            exe=Path(self.config.get('sdk_python',sys.executable))
            if not exe.is_absolute():exe=(Path(__file__).resolve().parents[2]/exe).resolve()
            if not exe.is_file():raise RuntimeError('未找到项目 uv Python 3.12 环境，请先执行 uv sync --locked。')
            self.source=kit_module('brainco_adapter').BrainCoSource(config=self.settings, python_executable=str(exe))
        meta=self.source.describe()
        if len(meta['channels'])!=32 or meta['sample_rate']!=self.rate:
            raise ValueError('BrainCo 必须为 32 EEG 通道，且实际采样率须与配置一致')
        self.connected=True

    def start_recording(self, output_path=None):
        if not self.connected or self.recording:raise RuntimeError('BrainCo 尚未准备好或正在采集')
        if output_path is None:raise ValueError('BrainCo 需要保存路径')
        self.folder=Path(output_path).with_suffix('.brainco');self.folder.mkdir(parents=True,exist_ok=False)
        self.started_ns=time.monotonic_ns()
        # Preserve both clocks. SDK samples use perf_counter; existing display
        # events use monotonic_ns. This mapping remains SOFTWARE ESTIMATED.
        self.clock_offset=time.perf_counter()-time.monotonic_ns()/1e9
        self.lease=kit_module('device_lease').DeviceLease();self.lease.__enter__()
        try:
            self.archive=kit_module('continuous_eeg').ContinuousEEG(self.folder,dict(
                subject=Path(output_path).stem,session_id=self.folder.name))
            self.source.archive_root=self.folder/'wire_archives'
            self.segment=self.archive.segment(self.source.describe())
            self.archive.event('clock_mapping',offset_perf_minus_monotonic=self.clock_offset,
                hardware_sync_verified=False,channels=32,units='sdk_native_unverified')
            self.cap=kit_module('eeg').Capture(self.source,self.archive,self.segment,buffer_seconds=15)
            original=self.cap.on_sample
            self.previous_counter=None;self.previous_time=None
            def receive(t, values, counter):
                original(t,values,counter)
                if self.previous_time is not None and (t-self.previous_time>1.8/self.rate or
                    (counter is not None and self.previous_counter is not None and counter!=self.previous_counter+1)):
                    self.cap.on_error('BrainCo 样本存在缺口或计数器跳变；已保存原始数据，不将本轮视为连续有效采集')
                self.previous_counter=counter;self.previous_time=t
            self.cap.on_sample=receive
            self.recording=True;self.cap.start();self.cap.ready(threading.Event())
            clock=getattr(self.source,'sample_clock',None)
            if clock is not None:
                deadline=time.perf_counter()+5
                while True:
                    self.cap.check()
                    result=clock.map_event(time.perf_counter()-.5)
                    if result['status']=='host_arrival_estimate':break
                    if time.perf_counter()>deadline:raise RuntimeError('BrainCo packet clock warmup failed: '+result['status'])
                    time.sleep(.02)
        except BaseException:
            try:self.stop_recording()
            except Exception:pass
            raise

    def health(self):
        if self.recording and self.cap:
            try:self.cap.check()
            except Exception as error:self.error=str(error)
        count=self.archive.accepted if self.archive else self.closed_count
        return DeviceHealth(self.connected and not self.error,'brainco-ecap-full-stream',self.error,
            self.rate,32,recording=self.recording,state='error' if self.error else 'recording' if self.recording else 'ready',
            sample_count=count,last_sample_index=count or None,last_block_timestamp_ns=(int((self.cap.last_arrival-self.clock_offset)*1e9)
                if self.cap and self.cap.last_arrival else None))

    def send_trigger(self, code):
        raise RuntimeError('BrainCo 无硬件 Trigger；请使用带事件时间的软件标记接口')

    def begin_event(self, code, display_timestamp_ns):
        if not self.recording:raise RuntimeError('BrainCo 尚未采集')
        self.cap.check();self.marker_seq+=1
        target=display_timestamp_ns/1e9+self.clock_offset
        with self.cap.lock:latest=self.cap.rows[-1][0] if self.cap.rows else target
        self.markers[self.marker_seq]=dict(code=int(code),target=target,display_timestamp_ns=display_timestamp_ns,
            deadline=time.perf_counter()+3)
        self.archive.event('marker_requested',host_time=target,code=int(code),display_timestamp_ns=display_timestamp_ns)
        self.last_marker_time=max(target,self.last_marker_time or target)
        return PendingSync(self.marker_seq)

    def poll_event(self,pending):
        event=self.markers.get(pending.handle)
        if event is None:raise ValueError('未知 BrainCo 标记')
        try:self.cap.check()
        except Exception as error:return self._failed(pending,str(error))
        with self.cap.lock:
            rows=list(self.cap.rows);count=self.archive.accepted
        clock = getattr(self.source, 'sample_clock', None)
        if clock is not None:
            mapped = clock.map_event(event['target'])
            if mapped['status'] == 'host_arrival_estimate':
                index = next((i for i, row in enumerate(rows) if row[2] == mapped['sample']), None)
                if index is not None:
                    sample_index = count-len(rows)+index+1
                    self.archive.event('aligned_marker', host_time=event['target'], code=event['code'],
                        sample_index=sample_index, device_counter=rows[index][2],
                        display_timestamp_ns=event['display_timestamp_ns'], quality='software_estimated',
                        hardware_sync_verified=False, segment=self.segment, clock_fit=mapped,
                        nominal_sample_timestamp=rows[index][0])
                    del self.markers[pending.handle]
                    return SyncPoint(sample_index,'brainco_rolling_packet_clock','software_estimated')
            if time.perf_counter()>event['deadline']:
                return self._failed(pending,'Packet clock unavailable: '+mapped['status'])
            return None
        if rows and rows[-1][0]>=event['target']:
            index=min(range(len(rows)),key=lambda i:abs(rows[i][0]-event['target']))
            sample=rows[index];error=abs(sample[0]-event['target'])
            if error>1.5/self.rate:return self._failed(pending,'事件超出样本覆盖范围或位于样本缺口')
            sample_index=count-len(rows)+index+1
            self.archive.event('aligned_marker',host_time=event['target'],code=event['code'],
                sample_index=sample_index,device_counter=sample[2],sample_monotonic=sample[0],
                display_timestamp_ns=event['display_timestamp_ns'],quality='software_estimated',
                hardware_sync_verified=False,segment=self.segment)
            del self.markers[pending.handle]
            return SyncPoint(sample_index,'brainco_counter_to_host_estimated','software_estimated',
                eeg_timestamp_ns=int((sample[0]-self.clock_offset)*1e9),latency_ns=int((sample[0]-event['target'])*1e9))
        if time.perf_counter()>event['deadline']:return self._failed(pending,'等待 BrainCo 样本覆盖事件时间超时')
        return None

    def _failed(self,pending,reason):
        event=self.markers.pop(pending.handle,None)
        if event:self.archive.event('marker_unresolved',host_time=event['target'],reason=reason,**{k:v for k,v in event.items() if k!='target'})
        return SyncPoint(None,'brainco_counter_to_host_estimated','degraded',failure_reason=reason)

    def record_event(self,code,display_timestamp_ns):
        pending=self.begin_event(code,display_timestamp_ns or time.monotonic_ns())
        while True:
            result=self.poll_event(pending)
            if result is not None:return result
            time.sleep(.005)

    def trigger_self_test(self,code):
        result=self.record_event(code,time.monotonic_ns())
        if result.quality!='software_estimated':raise RuntimeError(result.failure_reason)
        return result

    def annotate_event(self,event_type,timestamp_ns,metadata):
        if self.archive and self.recording:
            self.archive.event('experiment_event',host_time=timestamp_ns/1e9+self.clock_offset,
                event_type=event_type,display_timestamp_ns=timestamp_ns,**metadata)

    def acquisition_test(self,code,annotation):
        start=time.perf_counter();first=time.monotonic_ns()
        onset=self.record_event(101,first)
        self.annotate_event('ANNOTATION_TEST_ONSET',first,annotation)
        while time.perf_counter()<start+1:self.cap.check();time.sleep(.01)
        last=time.monotonic_ns();offset=self.record_event(102,last)
        self.annotate_event('ANNOTATION_TEST_OFFSET',last,annotation)
        while time.perf_counter()<start+3:self.cap.check();time.sleep(.01)
        end=start+3
        if not self.cap.wait_until_sample(end,threading.Event()):raise RuntimeError('短测尾部样本不完整')
        _,_,_,_,quality=self.cap.window(start,end)
        if not quality['complete']:raise RuntimeError('BrainCo 三秒采集覆盖/连续性检查未通过: '+json.dumps(quality))
        if any(x.quality!='software_estimated' for x in [onset,offset]):raise RuntimeError('标记时间映射失败')
        self.preflight_markers=[(101,onset.sample_index),(102,offset.sample_index)]
        return dict(window=quality,annotation=annotation,hardware_sync_verified=False)

    def stop_recording(self):
        if not self.recording:return self.artifact
        errors=[]
        try:
            if self.cap:
                if self.last_marker_time and not self.error and not getattr(self.source,'sample_clock',None):
                    try:
                        if not self.cap.wait_until_sample(self.last_marker_time,threading.Event()):errors.append('尾部事件未被样本覆盖')
                    except Exception as e:errors.append(str(e))
                try:self.cap.stop()
                except Exception as e:errors.append(str(e))
                if self.cap.error:errors.append(self.cap.error)
            if self.archive:
                for handle in list(self.markers):self._failed(PendingSync(handle),'recording stopped before marker resolved')
                diagnostics=self.source.diagnostics() if hasattr(self.source,'diagnostics') else {}
                (self.folder/'source_diagnostics.json').write_text(json.dumps(diagnostics,ensure_ascii=False,indent=2),encoding='utf-8')
                try:
                    self.archive.end_segment(self.segment,'error' if errors else 'stopped',diagnostics)
                    self.archive.close('error' if errors else 'stopped')
                except Exception as e:errors.append(str(e))
                if self.archive.accepted!=self.archive.committed:errors.append('收到与提交的样本数不一致')
                self.closed_count=self.archive.committed
            if errors:raise RuntimeError('; '.join(errors))
            audit=NativeAudit(self.archive.path)
            for code,index in getattr(self,'preflight_markers',[]):audit.verify_marker(code,index)
            self.artifact=RecordingArtifact(self.archive.path,'brainco-full-stream-native',audit.samples,self.rate,32,
                tuple(f'Ch{i}' for i in range(1,33)),tuple('sdk_native_unverified' for _ in range(32)),
                recording_start_ns=self.started_ns,recording_end_ns=time.monotonic_ns())
            return self.artifact
        finally:
            self.recording=False;self.connected=False
            if self.lease:self.lease.__exit__(None,None,None);self.lease=None

    def disconnect(self):
        if self.recording:self.stop_recording()
        self.connected=False


def create_device(config):
    return StandaloneBrainCoDevice(config)
