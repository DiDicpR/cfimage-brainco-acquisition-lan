from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PySide6.QtCore import QObject, QTimer, Signal, Qt

from ..acquisition_devices.acquisition_lock import acquisition_lock
from ..acquisition_devices.base import EEGDevice, PendingSync, RecordingArtifact, SyncPoint
from ..acquisition_devices.simulation import SimulationDevice
from ..acquisition_devices.trigger_router import TriggerError, TriggerRouter
from ..display.frame_clock import FrameClock
from ..display.stimulus_widget import PresentedFrame, StimulusWidget, RasterStimulusWidget
from ..storage.database import SessionStore, connect_database, recover_running_sessions, sha256_file
from .config import config_digest
from .planner import PlannedTrial, SessionPlan, build_session_plan
from .stimulus_manifest import read_manifest
from .collection import saved_progress, group_mapping, build_collection_plan


@dataclass(frozen=True)
class _PendingEventSync:
    event_type: str
    trial: PlannedTrial | None
    pending: PendingSync


class SessionRunner(QObject):
    finished = Signal(int)

    def __init__(
        self,
        *,
        widget: StimulusWidget,
        experiment: dict[str, Any],
        devices: dict[str, Any],
        participant_id: str,
        day_index: int,
        manifest_path: Path,
        windowed: bool,
        run_mode: str = "simulation",
        device: EEGDevice | None = None,
        skippable_waits: bool = True,
    ) -> None:
        super().__init__(widget)
        self.widget = widget
        self.experiment = experiment
        self.devices = devices
        self.participant_id = participant_id
        self.day_index = day_index
        self.manifest_path = manifest_path
        self.windowed = windowed
        self.run_mode = run_mode
        self._dual = bool(experiment.get('dual_image', {}).get('enabled', False))
        self._target_errors: set[int] = set()
        self.skippable_waits = skippable_waits
        self.database_path = Path(experiment["paths"]["database"]).resolve()
        self.dataset_root = Path(experiment["paths"]["dataset_root"]).resolve()
        self.database = connect_database(self.database_path)
        recover_running_sessions(self.database)
        if device is None and run_mode != 'simulation':
            raise ValueError('真实采集模式缺少设备适配器，不能回退到模拟设备。')
        self.device = device or SimulationDevice()
        self.router = TriggerRouter(self.device, devices["trigger"]["codes"])
        refresh_rate = self.widget.screen().refreshRate() if self.widget.screen() else 60.0
        self.clock = FrameClock(refresh_rate if refresh_rate > 1 else 60.0)
        self.plan: SessionPlan | None = None
        self.store: SessionStore | None = None
        self.current_block = 1
        self.current_trial_offset = 0
        self.current_trial: PlannedTrial | None = None
        self._awaited_token: int | None = None
        self._after_presented: Callable[[PresentedFrame], None] | None = None
        self._active_token: int | None = None
        self._frames_remaining = 0
        self._phase_frame_count = 0
        self._image_actual_frames = 0
        self._after_frames: Callable[[], None] | None = None
        self._image_onset: PresentedFrame | None = None
        self._image_offset: PresentedFrame | None = None
        self._phase = "instructions"
        self._probe_started_ns = 0
        self._rest_ready = False
        self._presentations_since_pause = 0
        self._passive_break_number = 0
        self._manual_wait_visible = False
        self._passive_math_expression = ''
        self._recording_id: str | None = None
        self._failed_trial_sync_ids: set[int] = set()
        self._pending_syncs: dict[int, _PendingEventSync] = {}
        self._pending_sync_count_by_trial: dict[int, int] = {}
        self._pending_trial_finalization: tuple[PlannedTrial, int, int] | None = None
        self._completion_waiting = False
        self._consecutive_trigger_failures = 0
        self._pending_device_failure_reason: str | None = None
        self._last_health_state: str | None = None
        self._phase_timer = QTimer(self)
        self._phase_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._phase_timer.setSingleShot(True)
        self._phase_timer.timeout.connect(self._on_phase_timeout)
        self._frame_timer = QTimer(self)
        self._frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._frame_timer.setInterval(max(1, self.clock.duration_for_frames(1)))
        self._frame_timer.timeout.connect(self.widget.update)
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(1000)
        self._health_timer.timeout.connect(self._check_device_health)
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(
            max(1, int(self.devices.get("devices", {}).get("neuracle", {}).get("trigger_poll_interval_ms", 5)))
        )
        self._sync_timer.timeout.connect(self._poll_pending_syncs)
        self._export_worker = None
        self._export_exit_code = None
        self._export_reason = ""
        self.widget.scene_presented.connect(self._on_scene_presented)
        self.widget.key_pressed.connect(self._on_key)
        self.widget.close_requested.connect(lambda: self._on_key("escape"))

    def prepare(self) -> None:
        stimuli = read_manifest(self.manifest_path)
        manifest_sha256 = sha256_file(self.manifest_path)
        from ..qc.integrity import validate_participant_manifest

        validate_participant_manifest(
            self.database,
            participant_id=self.participant_id,
            dataset_root=self.dataset_root,
            current_sha256=manifest_sha256,
            current_stimuli=stimuli,
            allow_identity_preserving_change=bool(
                self.experiment.get("robustness", {}).get("allow_identity_preserving_manifest_change", False)
            ),
        )
        if self.experiment.get('fixed_schedule'):
            from .fixed_schedule import resume_progress
            progress, unverified_trials = resume_progress(self.database, self.participant_id,
                self.run_mode, self.dataset_root, self.experiment)
        else:
            progress, unverified_trials = saved_progress(
                self.database, self.participant_id, self.run_mode, self.dataset_root, by_target_side=self._dual)
        randomization = self.experiment["randomization"]
        seed = randomization.get("seed")
        if seed is None:
            seed = time.time_ns() % (2**31)
        blocks = self.experiment["blocks"]
        collection = self.experiment.get('collection', {})
        collection_context = None
        self.scene_metadata = {}
        if self._dual:
            from .positions import read_catalog
            from .paired import build_paired_plan
            _, metadata = read_catalog(self.manifest_path)
            self.plan, collection_context = build_paired_plan(
                stimuli, metadata, progress, self.experiment, int(seed))
            collection_context.update(unverified_trials=unverified_trials,
                resume_policy='verified_paired_targets_per_side')
        elif collection.get('enabled', False) and collection.get('strategy') == 'scenes':
            from .scenes import read_scene_catalog, build_scene_plan
            _, metadata = read_scene_catalog(self.manifest_path)
            self.scene_metadata = metadata
            self.plan, collection_context = build_scene_plan(
                stimuli, metadata, progress, self.experiment, int(seed))
            collection_context.update(unverified_trials=unverified_trials,
                resume_policy=('verified_fixed_schedule_ids' if self.experiment.get('fixed_schedule')
                               else 'saved_valid_trials_same_mode'))
        elif collection.get('enabled', False) and collection.get('strategy') == 'positions':
            from .positions import read_catalog, build_position_plan
            _, metadata = read_catalog(self.manifest_path)
            self.plan, collection_context = build_position_plan(
                stimuli, metadata, progress, self.experiment, int(seed))
            previous = self.database.execute(
                'SELECT session_id,status FROM sessions WHERE participant_id=? AND run_mode=? ORDER BY start_time DESC LIMIT 1',
                (self.participant_id, self.run_mode)).fetchone()
            collection_context.update(previous_session=dict(previous) if previous else None,
                unverified_trials=unverified_trials, resume_policy='saved_valid_trials_same_mode')
        elif collection.get('enabled', False):
            mapping = group_mapping(self.database, self.participant_id, stimuli,
                                    int(collection.get('group_count', 10)))
            self.plan, collection_context = build_collection_plan(
                stimuli, progress, mapping, self.experiment, seed=int(seed))
            group_mapping(self.database, self.participant_id, stimuli,
                          int(collection.get('group_count', 10)), persist=True)
            previous = self.database.execute(
                'SELECT session_id,status FROM sessions WHERE participant_id=? AND run_mode=? ORDER BY start_time DESC LIMIT 1',
                (self.participant_id, self.run_mode)).fetchone()
            collection_context.update(previous_session=dict(previous) if previous else None,
                                      unverified_trials=unverified_trials,
                                      resume_policy='saved_valid_trials_same_mode',
                                      image_groups=mapping)
        else:
            self.plan = build_session_plan(
                stimuli,
                progress=progress,
                repetitions=int(self.experiment["repetition"]["minimum_valid_repetitions"]),
                blocks=int(blocks["session_blocks"]),
                trials_per_block=int(blocks["trials_per_block"]),
                minimum_image_gap=int(self.experiment["repetition"]["minimum_image_gap"]),
                attention=self.experiment["attention_probe"],
                seed=int(seed),
                plan_version=str(randomization["plan_version"]),
                auto_size_to_remaining=bool(blocks.get("auto_size_to_remaining", False)),
            )
        if not self.plan.trials:
            raise ValueError('本方案已完成，没有剩余固定试次。')
        if bool(self.experiment["stimulus"].get("preload_images", False)):
            # A session plan contains at most one session's trials. Never
            # preload the full THINGS library into the GUI process.
            missing = self.widget.preload([path for trial in self.plan.trials[:int(self.experiment["stimulus"].get("preload_batch_size", len(self.plan.trials)))] for path in ((trial.left_file_path, trial.right_file_path) if self._dual else (trial.file_path,))])
            if missing:
                preview = ", ".join(missing[:3])
                raise FileNotFoundError(f"Stimulus images could not be loaded: {preview}")
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        session_id = f"ses-{stamp}"
        if self._dual:
            import uuid
            session_id = 'ses-' + uuid.uuid4().hex
        self.store = SessionStore(
            self.database,
            dataset_root=self.dataset_root,
            participant_id=self.participant_id,
            session_id=session_id,
            day_index=self.day_index,
            config_hash=config_digest({"experiment": self.experiment, "devices": self.devices}),
            random_seed=self.plan.seed,
            run_mode=self.run_mode,
            locked_config={
                "experiment": self.experiment,
                "devices": self.devices,
                "stimulus_manifest_sha256": manifest_sha256,
                "collection": collection_context,
            },
            manifest_path=self.manifest_path,
        )
        self.store.create(self.plan, device_name=self.device.health().device_name)
        if self.experiment.get('fixed_schedule'):
            import shutil
            shutil.copyfile(self.experiment['fixed_schedule']['path'],
                            self.store.session_dir / 'fixed_schedule.csv')
            if self.run_mode == 'brainco' and self.experiment['collection']['target_repetitions'] > 10:
                import csv, json, hashlib
                from .fixed_schedule import read_schedule, EXTENSION_VERSION
                effective = self.store.session_dir / 'fixed_schedule.csv'
                shutil.copyfile(effective, self.store.session_dir / 'fixed_schedule_base.csv')
                rows = read_schedule(self.experiment, stimuli, self.scene_metadata)
                with effective.open('w', encoding='utf-8-sig', newline='') as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                    writer.writeheader(); writer.writerows(rows)
                (self.store.session_dir / 'schedule-provenance.json').write_text(json.dumps(dict(
                    base_schedule_sha256=self.experiment['fixed_schedule']['sha256'],
                    effective_schedule_sha256=hashlib.sha256(effective.read_bytes()).hexdigest(),
                    extension_version=EXTENSION_VERSION,
                    target_repetitions=self.experiment['collection']['target_repetitions']), indent=2), encoding='utf-8')
        if collection_context:
            import json
            (self.store.session_dir / 'collection.json').write_text(
                json.dumps(collection_context, ensure_ascii=False, indent=2), encoding='utf-8')
        summary = (
            f"训练图序号 {','.join(map(str, collection_context['positions']))} · "
            f"每张累计目标 {collection_context['target_repetitions']} 次\n"
            f"本次 {len(self.plan.trials)} 次呈现 / {self.plan.block_count} 个区块；已完成试次自动跳过。\n"
        ) if collection_context and collection_context.get('strategy') in {'positions', 'paired'} else (
            f"第 {collection_context['group_index']} / {collection_context['group_count']} 大组 · "
            f"本次 {len(self.plan.trials)} 次图片呈现 / {self.plan.block_count} 个区块。\n"
            "自动延续同一被试的有效进度；未完成或无效试次会补采。\n"
        ) if collection_context and collection_context.get('strategy') != 'scenes' else ''
        if collection_context and collection_context.get('strategy') == 'scenes':
            summary = (f"已选 {collection_context['selected_images']} 张场景图片 · "
                       f"每张累计目标 {collection_context['target_repetitions']} 次\n"
                       f"本次 {len(self.plan.trials)} 次呈现 / {self.plan.block_count} 个区块；已完成试次自动跳过。\n")
        if self.experiment.get('fixed_schedule'):
            summary = (f"强脑32通道 · 2580张全部场景图片 × 10次\n"
                f"固定顺序，从原始试次 {self.plan.trials[0].schedule_trial_id} 继续；本次 {len(self.plan.trials)} 次。\n"
                "呈现 0.6 秒 / 间隔 0.1 秒；Esc 结束并保存，下次按原序续采。\n"
                "呈现次数与有效 EEG 次数分开统计。\n")
            if self.experiment.get('presentation_break', {}).get('enabled'):
                summary += "每 80 张显示加减题 2 秒（无需作答），随后按空格，0.8 秒后继续。\n"
        self.widget.show_instruction(
            "准备开始",
            summary + ("按提示注视左侧或右侧目标图片；看错请按 E，本次不计有效次数。"
                       if self._dual else "实验过程中请始终注视中央十字。"),
            ("按空格开始；题目自动显示 2 秒，休息时再按空格继续；Esc 保存退出"
             if self.experiment.get('presentation_break', {}).get('enabled')
             else "按空格键开始；图片前阶段和组间休息也可按空格跳过"),
        )

    def start(self) -> None:
        if self.plan is None or self.store is None:
            raise RuntimeError("prepare() must be called before start()")
        try:
            self.device.connect()
            health = self.device.health()
            if not health.connected:
                raise RuntimeError(health.message or "Device is not connected.")
            self.store.record_device_log(
                health.device_name,
                "healthy",
                f"sampling_rate_hz={health.sampling_rate_hz}; channel_count={health.channel_count}",
            )
            output_path = None
            if self.run_mode != "simulation":
                output_path = (
                    self.store.session_dir
                    / "eeg"
                    / f"{self.participant_id}_{self.store.session_id}_task-things_eeg.edf"
                )
                output_path.parent.mkdir(exist_ok=True)
            self.device.start_recording(output_path)
            trigger_code = int(self.devices["trigger"]["codes"]["TRIGGER_TEST"])
            self_test_sync = self.device.trigger_self_test(trigger_code)
            if self.run_mode == 'neuracle' and self_test_sync.quality != 'hardware_verified':
                raise RuntimeError('正式采集 Trigger 未通过硬件回读，已阻止实验开始。')
            if self.run_mode == 'brainco' and self_test_sync.quality != 'software_estimated':
                raise RuntimeError('BrainCo 软件事件映射未通过自检，已阻止实验开始。')
            self_test_event = self.store.record_event(
                "TRIGGER_TEST",
                trigger_code=trigger_code,
                metadata={
                    "self_test": "passed",
                    "alignment_method": self_test_sync.alignment_method,
                    "sync_quality": self_test_sync.quality,
                    "eeg_sample_index": self_test_sync.sample_index,
                },
            )
            self.store.record_sync(self_test_event, self_test_sync)
            anchor_ns = time.monotonic_ns()
            self._event("SESSION_START", metadata={
                "wall_clock_anchor": datetime.now(UTC).isoformat(),
                "monotonic_anchor_ns": anchor_ns,
            })
            self._health_timer.start()
        except (RuntimeError, TriggerError, OSError) as error:
            self._device_failure(f"device_start_failed: {error}")
            return
        if self.experiment.get('fixed_schedule'):
            self._start_block()
            return
        timezone_name = str(self.experiment["project"]["timezone"])
        baseline_session_id = self.store.completed_baseline_session_for_day(
            local_day=datetime.now(ZoneInfo(timezone_name)).date(),
            timezone_name=timezone_name,
        )
        if baseline_session_id is not None:
            self._event(
                "BASELINE_SKIPPED",
                metadata={
                    "reason": "completed_same_day_baseline",
                    "source_session_id": baseline_session_id,
                    "timezone": timezone_name,
                },
            )
            self._start_block()
            return
        self._baseline("eyes_open")

    def _baseline(self, name: str) -> None:
        messages = {
            "transition": "准备闭眼。\n\n按空格键可跳过当前阶段。",
            "eyes_closed": "请闭眼并保持放松。\n\n按空格键可跳过当前阶段。",
        }
        events = {
            "eyes_open": ("BASELINE_EYES_OPEN_START", "BASELINE_EYES_OPEN_END"),
            "eyes_closed": ("BASELINE_EYES_CLOSED_START", "BASELINE_EYES_CLOSED_END"),
            "transition": ("BASELINE_TRANSITION_START", "BASELINE_TRANSITION_END"),
        }
        self._phase = f"baseline_{name}"
        # The eyes-open baseline is a clean fixation screen. Instructions must
        # never occupy the centre or cover the fixation mark during recording.
        token = self.widget.show_fixation() if name == "eyes_open" else self.widget.show_question(messages[name])

        def after(_: PresentedFrame) -> None:
            self._event(events[name][0])
            duration = int(self.experiment["baseline"][f"{name}_sec"])
            self._phase_timer.start(duration * 1000)

        self._await(token, after)

    def _start_block(self) -> None:
        assert self.plan is not None and self.store is not None
        if self.current_block > self.plan.block_count:
            self._complete()
            return
        self.current_trial_offset = 0
        self.store.set_block_status(self.current_block, "running")
        self._event("BLOCK_START")
        if self.experiment.get('fixed_schedule'):
            self._start_next_trial()
            return
        self._phase = "block_fixation"
        token = self.widget.show_fixation()

        def after(_: PresentedFrame) -> None:
            self._event("BLOCK_FIXATION_ONSET")
            self._run_frames(
                self.clock.frames_for(int(self.experiment["stimulus"]["block_fixation_duration_ms"])),
                self._start_next_trial,
            )

        self._await(token, after)

    def _start_next_trial(self) -> None:
        if self._pending_device_failure_reason is not None:
            return
        assert self.plan is not None and self.store is not None
        block_trials = self.plan.by_block(self.current_block)
        if self.current_trial_offset >= len(block_trials):
            self._event("BLOCK_END")
            self.store.set_block_status(self.current_block, "complete")
            self.current_block += 1
            if self.current_block > self.plan.block_count:
                self._complete()
            else:
                self._start_rest()
            return
        self.current_trial = block_trials[self.current_trial_offset]
        self.current_trial_offset += 1
        self._image_onset = None
        self._image_offset = None
        self._image_actual_frames = 0
        if self._dual:
            self._start_target_cue()
        else:
            self._show_trial_images()

    def _start_target_cue(self) -> None:
        assert self.current_trial is not None
        self._phase = 'target_cue'
        left = self.current_trial.target_side == 'left'
        token = self.widget.show_question('本次注视左侧图片 ←' if left else '本次注视右侧图片 →')
        def after(frame: PresentedFrame) -> None:
            self._event('TARGET_CUE_ONSET', self.current_trial, frame=frame)
            self._run_frames(self.clock.frames_for(int(self.experiment['dual_image']['cue_duration_ms'])),
                self._start_target_fixation)
        self._await(token, after)

    def _start_target_fixation(self) -> None:
        self._phase = 'target_fixation'
        token = self.widget.show_fixation()
        def after(frame: PresentedFrame) -> None:
            self._event('TARGET_CUE_OFFSET', self.current_trial, frame=frame)
            self._event('FIXATION_ONSET', self.current_trial, frame=frame)
            self._run_frames(self.clock.frames_for(int(self.experiment['dual_image']['fixation_duration_ms'])),
                self._show_trial_images)
        self._await(token, after)

    def _show_trial_images(self) -> None:
        assert self.current_trial is not None
        self._phase = 'image'
        try:
            token = (self.widget.show_pair(self.current_trial.left_file_path,
                        self.current_trial.right_file_path) if self._dual
                     else self.widget.show_image(self.current_trial.file_path))
        except (OSError, ValueError, RuntimeError) as error:
            self._device_failure(f'stimulus_image_failed: {error}')
            return
        self._await(token, self._image_presented)

    def _image_presented(self, frame: PresentedFrame) -> None:
        assert self.current_trial is not None and self.store is not None
        self._image_onset = frame
        if self._dual:
            self._event('FIXATION_OFFSET', self.current_trial, frame=frame)
        planned_frames = self.clock.frames_for(int(self.experiment["stimulus"]["image_duration_ms"]))
        self.store.mark_presented(self.current_trial, onset_ns=frame.timestamp_ns, planned_frames=planned_frames)
        # Start frame accounting before DCP acknowledgement/database work. The
        # trigger itself is still sent from this first presented-frame callback.
        self._run_frames(planned_frames, self._start_isi)
        self._event("IMAGE_ONSET", self.current_trial, frame=frame, metadata={"planned_frames": planned_frames})

    def _start_isi(self) -> None:
        assert self.current_trial is not None
        self._phase = "isi"
        self._image_actual_frames = self._phase_frame_count
        token = self.widget.show_blank() if self._dual else self.widget.show_fixation()
        self._await(token, self._isi_presented)

    def _isi_presented(self, frame: PresentedFrame) -> None:
        assert self.current_trial is not None
        self._image_offset = frame
        # IMAGE_OFFSET is aligned to the first actually presented fixation
        # frame, not to the earlier request to change the scene.
        self._event("IMAGE_OFFSET", self.current_trial, frame=frame)
        self._event("ISI_ONSET", self.current_trial, frame=frame)
        self._run_frames(self.clock.frames_for(int(self.experiment["stimulus"]["isi_ms"])), self._finish_isi)

    def _finish_isi(self) -> None:
        assert (
            self.current_trial is not None
            and self.store is not None
            and self._image_onset is not None
            and self._image_offset is not None
        )
        now_ns = time.monotonic_ns()
        self._event("ISI_OFFSET", self.current_trial, timestamp_ns=now_ns)
        planned_frames = self.clock.frames_for(int(self.experiment["stimulus"]["image_duration_ms"]))
        actual_offset_ns = self._image_offset.timestamp_ns
        if self._pending_sync_count_by_trial.get(self.current_trial.global_index, 0):
            # Do not block the display thread. Keep showing fixation until the
            # already-sent onset/offset markers have been checked.
            self._phase = "trigger_sync_wait"
            self._pending_trial_finalization = (
                self.current_trial,
                actual_offset_ns,
                planned_frames,
            )
            return
        self._finalize_trial(self.current_trial, actual_offset_ns, planned_frames)

    def _finalize_trial(
        self,
        trial: PlannedTrial,
        actual_offset_ns: int,
        planned_frames: int,
    ) -> None:
        if self._pending_device_failure_reason is not None:
            return
        assert self.store is not None
        sync_ok = trial.global_index not in self._failed_trial_sync_ids
        valid = (abs(self._image_actual_frames - planned_frames) <= 1 and sync_ok
                 and trial.global_index not in self._target_errors)
        reason = None
        if abs(self._image_actual_frames - planned_frames) > 1:
            reason = "image_frame_count_mismatch"
        elif not sync_ok:
            reason = "event_alignment_degraded"
        elif trial.global_index in self._target_errors:
            reason = 'target_error_reported' 
        self.store.finalize_trial(
            trial,
            valid=valid,
            actual_offset_ns=actual_offset_ns,
            actual_frames=self._image_actual_frames,
            reason=reason,
        )
        self._presentations_since_pause += 1
        pause = self.experiment.get('presentation_break', {})
        if (pause.get('enabled') and self.plan is not None
                and self._presentations_since_pause >= int(pause['every_images'])
                and trial.global_index < len(self.plan.trials)):
            self._start_passive_math()
        elif trial.probe:
            self._start_probe()
        else:
            self._start_next_trial()

    def _break_metadata(self) -> dict:
        return dict(break_number=self._passive_break_number,
            after_schedule_trial_id=self.current_trial.schedule_trial_id if self.current_trial else None,
            images_since_last_break=self._presentations_since_pause,
            expression=self._passive_math_expression, requires_response=False)

    def _start_passive_math(self) -> None:
        import random
        self._passive_break_number += 1
        rng = random.Random(f'{self.participant_id}:{self.plan.seed}:{self.current_trial.schedule_trial_id}:{self._passive_break_number}')
        left, right = rng.randint(1, 20), rng.randint(1, 20)
        operation = rng.choice(('+', '-'))
        if operation == '-':
            left, right = max(left, right), min(left, right)
        self._passive_math_expression = f'{left} {operation} {right} = ?'
        self._phase = 'passive_math'
        token = self.widget.show_question(self._passive_math_expression + '\n\n无需作答')
        def after(frame: PresentedFrame) -> None:
            self._phase_timer.start(int(self.experiment['presentation_break']['question_duration_ms']))
            self._event('PASSIVE_MATH_ONSET', frame=frame, metadata=self._break_metadata())
        self._await(token, after)

    def _show_manual_break(self) -> None:
        self._phase = 'manual_break'
        self._manual_wait_visible = False
        token = self.widget.show_instruction('可以休息', '准备好后按空格，0.8 秒后继续下一轮。', '按空格继续')
        def after(frame: PresentedFrame) -> None:
            self._event('PASSIVE_MATH_OFFSET', frame=frame, metadata=self._break_metadata())
            self._event('MANUAL_BREAK_START', frame=frame, metadata=self._break_metadata())
            self._preload_next_batch()
            self._manual_wait_visible = self._pending_device_failure_reason is None
        self._await(token, after)

    def _preload_next_batch(self) -> None:
        size = int(self.experiment.get('stimulus', {}).get('preload_batch_size', 0))
        if not size:
            return
        # This callback runs after the rest screen has painted. Never decode
        # thousands of images during stimulus timing or keep them all in RAM.
        start = self.current_trial.global_index
        try:
            missing = self.widget.preload([t.file_path for t in self.plan.trials[start:start + size]])
        except (OSError, ValueError, RuntimeError) as error:
            self._schedule_device_failure('stimulus_preload_failed: ' + str(error))
            return
        if missing:
            self._schedule_device_failure('stimulus_preload_failed: ' + str(missing[:3]))

    def _resume_after_manual_break(self) -> None:
        self._manual_wait_visible = False
        self._phase = 'resume_delay'
        self._event('MANUAL_BREAK_END', metadata=self._break_metadata())
        token = self.widget.show_fixation()
        def after(frame: PresentedFrame) -> None:
            self._phase_timer.start(int(self.experiment['presentation_break']['resume_delay_ms']))
            self._event('RESUME_DELAY_ONSET', frame=frame, metadata=self._break_metadata())
        self._await(token, after)

    def _start_probe(self) -> None:
        assert self.current_trial is not None
        self._phase = "probe"
        expression = self.current_trial.arithmetic_expression
        if expression is None or self.current_trial.arithmetic_is_correct is None:
            raise RuntimeError("Attention probe is missing its arithmetic question")
        token = self.widget.show_question(f"判断计算是否正确：\n\n{expression}\n\nF = 错误    J = 正确")

        def after(frame: PresentedFrame) -> None:
            self._probe_started_ns = frame.timestamp_ns
            self._event("ATTENTION_PROBE_ONSET", self.current_trial, frame=frame)
            self._phase_timer.start(int(self.experiment["attention_probe"]["response_timeout_ms"]))

        self._await(token, after)

    def _answer_probe(self, response: str | None, *, timeout: bool) -> None:
        assert self.current_trial is not None and self.store is not None
        self._phase_timer.stop()
        reaction_time_ms = None if timeout else round((time.monotonic_ns() - self._probe_started_ns) / 1_000_000)
        expected = (
            self.experiment["attention_probe"]["key_correct"]
            if self.current_trial.arithmetic_is_correct
            else self.experiment["attention_probe"]["key_incorrect"]
        )
        correct = response == expected and not timeout
        self.store.record_attention(
            self.current_trial,
            response=response,
            reaction_time_ms=reaction_time_ms,
            correct=correct,
            timeout=timeout,
        )
        self._event(
            "ATTENTION_PROBE_TIMEOUT" if timeout else "ATTENTION_PROBE_RESPONSE",
            self.current_trial,
            metadata={
                "question_type": "arithmetic_verification",
                "expression": self.current_trial.arithmetic_expression,
                "statement_is_correct": self.current_trial.arithmetic_is_correct,
                "response": response,
                "correct": correct,
                "reaction_time_ms": reaction_time_ms,
            },
        )
        self._phase = "post_probe"
        token = self.widget.show_fixation()
        self._await(
            token,
            lambda _: self._run_frames(
                self.clock.frames_for(int(self.experiment["attention_probe"]["post_probe_fixation_ms"])),
                self._start_next_trial,
            ),
        )

    def _start_rest(self) -> None:
        if self.experiment.get('fixed_schedule'):
            self._start_block()
            return
        self._phase = "rest"
        self._rest_ready = False
        self._event("REST_START")
        self.widget.show_instruction(
            "休息中",
            "请放松并保持就位。",
            "按空格键可跳过休息并开始下一组",
        )
        self._phase_timer.start(int(self.experiment["blocks"]["minimum_rest_sec"]) * 1000)

    def _on_phase_timeout(self, *, skipped: bool = False) -> None:
        metadata = {"skipped_by_space": True} if skipped else None
        if self._phase == 'passive_math':
            self._show_manual_break()
        elif self._phase == 'resume_delay':
            self._event('RESUME_DELAY_END', metadata=self._break_metadata())
            self._presentations_since_pause = 0
            self._start_next_trial()
        elif self._phase == "baseline_eyes_open":
            self._event("BASELINE_EYES_OPEN_END", metadata=metadata)
            self._baseline("transition")
        elif self._phase == "baseline_transition":
            self._event("BASELINE_TRANSITION_END", metadata=metadata)
            self._baseline("eyes_closed")
        elif self._phase == "baseline_eyes_closed":
            self._event("BASELINE_EYES_CLOSED_END", metadata=metadata)
            self._start_block()
        elif self._phase == "probe":
            self._answer_probe(None, timeout=True)
        elif self._phase == "rest":
            self._rest_ready = True
            self.widget.show_instruction(
                "休息结束",
                "下一组已准备好。",
                "按空格键开始下一组",
            )

    def _on_key(self, key: str) -> None:
        if self._phase in {"exporting", "stopping"}:
            return
        if self._phase in {"device_error", "export_error"}:
            if key in {"space", "escape"}:
                self.finished.emit(1)
            return
        if key == "escape":
            self.abort()
            return
        if self._phase in {'passive_math', 'manual_break', 'resume_delay'}:
            if self._phase == 'manual_break' and self._manual_wait_visible and key == 'space':
                self._resume_after_manual_break()
            return
        if (self._dual and key == self.experiment['dual_image']['report_error_key']
                and self._phase in {'image', 'isi'} and self.current_trial is not None
                and self._image_onset is not None):
            trial = self.current_trial
            if trial.global_index not in self._target_errors:
                self._target_errors.add(trial.global_index)
                self._event('TARGET_ERROR', trial, metadata={
                    'response': key, 'error_reported': True,
                    'reaction_time_ms': (time.monotonic_ns() - self._image_onset.timestamp_ns) / 1_000_000,
                })
            return
        if self._phase == "instructions" and key == "space":
            self.start()
        elif (
            self._phase == "rest"
            and key == "space"
            and (self._rest_ready or (self.skippable_waits and self._phase_timer.isActive()))
        ):
            skipped = self._phase_timer.isActive()
            self._phase_timer.stop()
            self._event("REST_END", metadata={"skipped_by_space": skipped})
            self._start_block()
        elif self._phase == "probe" and key in {
            self.experiment["attention_probe"]["key_correct"],
            self.experiment["attention_probe"]["key_incorrect"],
        }:
            self._answer_probe(key, timeout=False)
        elif (
            self.skippable_waits
            and key == "space"
            and self._phase
            in {"baseline_eyes_open", "baseline_transition", "baseline_eyes_closed"}
            and self._phase_timer.isActive()
        ):
            # Advance a pre-image phase exactly as if its timer had expired.
            # isActive() also guards the one-frame window before the phase's
            # START event has been logged.
            self._phase_timer.stop()
            self._on_phase_timeout(skipped=True)

    def _await(self, token: int, callback: Callable[[PresentedFrame], None]) -> None:
        self._awaited_token = token
        self._after_presented = callback

    def _on_scene_presented(self, frame: PresentedFrame) -> None:
        if self._pending_device_failure_reason is not None:
            return
        if self._awaited_token == frame.token and self._after_presented is not None:
            callback = self._after_presented
            self._awaited_token = None
            self._after_presented = None
            self._active_token = frame.token
            callback(frame)
            return
        if self._active_token == frame.token and self._frames_remaining:
            self._phase_frame_count += 1
            self._frames_remaining -= 1
            if self._frames_remaining == 0:
                self._frame_timer.stop()
                callback = self._after_frames
                self._after_frames = None
                if callback:
                    callback()

    def _run_frames(self, frames: int, callback: Callable[[], None]) -> None:
        self._phase_frame_count = 1
        self._frames_remaining = max(0, frames - 1)
        self._after_frames = callback
        if self._frames_remaining == 0:
            QTimer.singleShot(0, callback)
        else:
            self._frame_timer.start()

    def _event(
        self,
        event_type: str,
        trial: PlannedTrial | None = None,
        *,
        frame: PresentedFrame | None = None,
        metadata: dict[str, Any] | None = None,
        timestamp_ns: int | None = None,
    ) -> None:
        assert self.store is not None
        code = self.devices["trigger"]["codes"].get(event_type)
        event_timestamp_ns = timestamp_ns or (frame.timestamp_ns if frame else time.monotonic_ns())
        payload = {"run_mode": self.run_mode, "config_hash": self.store.config_hash}
        if trial:
            payload.update(
                {
                    "block_index": trial.block_index,
                    "trial_index": trial.trial_index,
                    "image_id": trial.image_id,
                    "repetition_index": trial.repetition_index,
                }
            )
        if trial and trial.image_id in getattr(self, 'scene_metadata', {}):
            row = self.scene_metadata[trial.image_id]
            payload.update({k: row[k] for k in ('scene','view','object_id','object_name','semantic_label')})
            payload['category_name'] = row.get('category_name',row['object_name'])
            for key in ('scene_id', 'group', 'original_image_id'):
                if key in row:
                    payload[key] = row[key]
        if trial and trial.schedule_trial_id is not None:
            for key in ('schedule_trial_id', 'schedule_cycle', 'schedule_cycle_position', 'schedule_sha256'):
                payload[key] = getattr(trial, key)
        if trial and self._dual:
            for name in ('pair_id', 'left_object_id', 'right_object_id', 'target_object_id',
                         'target_side', 'left_image_id', 'right_image_id',
                         'left_file_path', 'right_file_path'):
                payload[name] = getattr(trial, name)
        if metadata:
            payload.update(metadata)
        sync: SyncPoint | None = None
        pending: PendingSync | None = None
        trigger_error = None
        if code is not None:
            try:
                code, result = self.router.emit_deferred(
                    event_type,
                    display_timestamp_ns=event_timestamp_ns,
                )
                if isinstance(result, PendingSync):
                    pending = result
                    payload["sync_status"] = "pending_software_alignment" if self.run_mode == "brainco" else "pending_hardware_verification"
                else:
                    sync = result
                    payload.update(self._sync_metadata(sync))
            except TriggerError as error:
                trigger_error = str(error)
                payload["trigger_error"] = trigger_error
        event_id = self.store.record_event(
            event_type,
            trial=trial,
            frame_index=frame.frame_index if frame else None,
            trigger_code=code,
            metadata=payload,
            timestamp_ns=event_timestamp_ns,
        )
        annotate = getattr(self.device, 'annotate_event', None)
        if annotate is not None:
            try:
                annotate(event_type, event_timestamp_ns, dict(payload, event_id=event_id,
                    trial_id=self.store.trial_id(trial) if trial is not None else None, trigger_code=code))
            except Exception as error:
                self._schedule_device_failure('标注写入失败: ' + str(error))
        if sync is not None:
            self._record_sync_result(event_id, event_type, trial, sync)
        if pending is not None:
            self._pending_syncs[event_id] = _PendingEventSync(event_type, trial, pending)
            if trial is not None:
                trial_key = trial.global_index
                self._pending_sync_count_by_trial[trial_key] = (
                    self._pending_sync_count_by_trial.get(trial_key, 0) + 1
                )
            if not self._sync_timer.isActive():
                self._sync_timer.start()
        if trigger_error is not None:
            self.store.record_device_log(self.device.health().device_name, "trigger_error", trigger_error)
            self._schedule_device_failure(trigger_error)

    @staticmethod
    def _sync_metadata(sync: SyncPoint) -> dict[str, Any]:
        return {
            "eeg_sample_index": sync.sample_index,
            "eeg_timestamp_ns": sync.eeg_timestamp_ns,
            "alignment_method": sync.alignment_method,
            "sync_quality": sync.quality,
            "latency_ns": sync.latency_ns,
            "failure_reason": sync.failure_reason,
        }

    def _poll_pending_syncs(self) -> None:
        if self.store is None:
            return
        for event_id, state in tuple(self._pending_syncs.items()):
            try:
                sync = self.router.poll(state.event_type, state.pending)
            except TriggerError as error:
                sync = SyncPoint(
                    None,
                    "deferred_trigger_verification",
                    "degraded",
                    failure_reason=str(error),
                )
                self._schedule_device_failure(str(error))
            if sync is None:
                # Preserve physical send order when repeated 101/102 codes are pending.
                break
            del self._pending_syncs[event_id]
            if state.trial is not None:
                key = state.trial.global_index
                remaining = self._pending_sync_count_by_trial.get(key, 1) - 1
                if remaining > 0:
                    self._pending_sync_count_by_trial[key] = remaining
                else:
                    self._pending_sync_count_by_trial.pop(key, None)
            self._record_sync_result(event_id, state.event_type, state.trial, sync)
            if self._pending_device_failure_reason is not None:
                break
        if not self._pending_syncs:
            self._sync_timer.stop()
        if self._pending_device_failure_reason is not None:
            return
        self._finish_trial_after_sync_if_ready()
        if self._completion_waiting and not self._pending_syncs:
            self._finish_completion()

    def _record_sync_result(
        self,
        event_id: int,
        event_type: str,
        trial: PlannedTrial | None,
        sync: SyncPoint,
    ) -> None:
        assert self.store is not None
        self.store.record_sync(event_id, sync, recording_id=self._recording_id)
        aligned = sync.quality in {"hardware_verified", "software_estimated"}
        if aligned:
            self._consecutive_trigger_failures = 0
            if self.run_mode == 'neuracle' and sync.latency_ns is not None and sync.latency_ns >= 1_000_000_000:
                self.store.record_device_log(self.device.health().device_name, 'trigger_delayed',
                    f'{event_type} event_id={event_id} verified after {sync.latency_ns / 1e6:.0f} ms; sample={sync.sample_index}')
            return
        if trial is not None:
            self._failed_trial_sync_ids.add(trial.global_index)
        reason = sync.failure_reason or f"{event_type}_alignment_degraded"
        self.store.record_device_log(self.device.health().device_name, "trigger_error", reason)
        if self.run_mode == "neuracle":
            self._consecutive_trigger_failures += 1
            # The adapter quarantines a timed-out code immediately. Continuing
            # any trial after that point can only fail or misattribute a late
            # marker. Stop on the first degraded result, regardless of legacy
            # consecutive-failure settings.
            self._schedule_device_failure(f'{event_type} hardware verification failed: {reason}')

    def _finish_trial_after_sync_if_ready(self) -> None:
        if self._pending_device_failure_reason is not None:
            return
        waiting = self._pending_trial_finalization
        if waiting is None:
            return
        trial, actual_offset_ns, planned_frames = waiting
        if self._pending_sync_count_by_trial.get(trial.global_index, 0):
            return
        self._pending_trial_finalization = None
        self._phase = "isi"
        self._finalize_trial(trial, actual_offset_ns, planned_frames)

    def _schedule_device_failure(self, reason: str) -> None:
        if self._pending_device_failure_reason is not None:
            return
        self._pending_device_failure_reason = reason
        self._phase_timer.stop()
        self._frame_timer.stop()
        self._awaited_token = None
        self._after_presented = None
        self._after_frames = None
        self._frames_remaining = 0
        self.widget.show_fixation()
        QTimer.singleShot(0, lambda: self._device_failure(reason))

    def _cancel_pending_syncs(self, reason: str) -> None:
        if self.store is None:
            return
        for event_id, state in tuple(self._pending_syncs.items()):
            # A failed earlier marker must not discard a later marker that is
            # already present in the received EEG. Poll once, without waiting,
            # emitting a trigger, advancing a trial, or clearing a failure.
            try:
                received = self.router.poll(state.event_type, state.pending)
            except (TriggerError, OSError, RuntimeError, ValueError):
                received = None
            if received is not None and received.quality in {'hardware_verified','software_estimated'}:
                self.store.record_sync(event_id, received, recording_id=self._recording_id)
                continue
            self.store.record_sync(
                event_id,
                SyncPoint(
                    None,
                    "deferred_trigger_verification",
                    "degraded",
                    failure_reason=reason,
                ),
                recording_id=self._recording_id,
            )
            if state.trial is not None:
                self._failed_trial_sync_ids.add(state.trial.global_index)
        self._pending_syncs.clear()
        self._pending_sync_count_by_trial.clear()
        self._sync_timer.stop()

    def _close_recording(self) -> RecordingArtifact | None:
        self._health_timer.stop()
        artifact = self.device.stop_recording()
        if artifact is not None:
            self._recording_id = self.store.register_recording(self.device.health().device_name, artifact)
        return artifact

    def _check_device_health(self) -> None:
        if self.run_mode == "simulation" or self.store is None:
            return
        health = self.device.health()
        if health.state != self._last_health_state:
            self.store.record_device_log(health.device_name, health.state, health.message)
            self._last_health_state = health.state
        if not health.connected or health.state in {"disconnected", "error"}:
            self._device_failure(health.message or f"device_{health.state}")

    def _device_failure(self, reason: str) -> None:
        if self._phase in {"complete", "aborted", "device_error", "exporting", "export_error", "stopping"}:
            return
        self._phase = "device_error"
        self._pending_device_failure_reason = None
        self._phase_timer.stop()
        self._frame_timer.stop()
        self._cancel_pending_syncs(f"verification_cancelled_after_device_error: {reason}")
        if self.store is not None:
            self.store.record_device_log(self.device.health().device_name, "error", reason)
            self.store.record_event("DEVICE_ERROR", metadata={"reason": reason, "run_mode": self.run_mode})
            try:
                self._close_recording()
            except (OSError, RuntimeError, ValueError) as error:
                self.store.record_device_log(self.device.health().device_name, "stop_error", str(error))
            self.store.abort_unfinished(export=False)
            self._begin_export(exit_code=None, reason=reason)

    def _complete(self) -> None:
        assert self.store is not None
        if self._phase == "finishing_sync":
            return
        self._phase = "finishing_sync"
        try:
            self._event("SESSION_END")
        except (OSError, RuntimeError, TriggerError, ValueError) as error:
            self._device_failure(f"device_stop_failed: {error}")
            return
        if self._pending_device_failure_reason is not None:
            return
        self._completion_waiting = True
        if self._pending_syncs:
            return
        self._finish_completion()

    def _finish_completion(self) -> None:
        assert self.store is not None
        if self._phase != "finishing_sync":
            return
        self._completion_waiting = False
        try:
            self._close_recording()
        except (OSError, RuntimeError, TriggerError, ValueError) as error:
            self._device_failure(f"device_stop_failed: {error}")
            return
        self.store.finish("complete", export=False)
        self._begin_export(exit_code=0)

    def _begin_export(self, *, exit_code: int | None, reason: str = "") -> None:
        from ..storage.export_worker import SessionExportWorker

        self._phase = "exporting"
        self._phase_timer.stop()
        self._frame_timer.stop()
        self._awaited_token = None
        self._after_presented = None
        self._frames_remaining = 0
        self._export_exit_code = exit_code
        self._export_reason = reason
        self.widget.show_question(
            "采集已停止，正在导出、校验并打包数据。\n\n"
            "请等待保存完成，不要关闭终端或断电。"
            + (f"\n\n停止原因：{reason}" if reason else "")
        )
        self._export_worker = SessionExportWorker(self.database_path, self.store, self)
        self._export_worker.finished.connect(self._export_finished)
        self._export_worker.start()

    def _export_finished(self) -> None:
        error = self._export_worker.error
        if error:
            self._phase = "export_error"
            self.widget.show_question(
                "采集已停止，但导出或校验未完成。\n\n"
                "请保留本次数据目录和数据库，不要删除或覆盖。\n"
                "错误详情见会话目录 export_error.log。\n\n"
                "按空格或 Esc 退出后联系实验员检查。"
            )
            return
        if self._export_exit_code is None:
            self._phase = "device_error"
            self.widget.show_question(
                "设备异常，本次采集已停止，数据导出和校验已完成。\n\n"
                f"原因：{self._export_reason}\n\n"
                "按空格或 Esc 退出；排除连接问题后用同一被试编号续采。"
            )
        else:
            self._phase = "complete" if self._export_exit_code == 0 else "aborted"
            self.finished.emit(self._export_exit_code)

    def abort(self) -> None:
        if self._phase in {"complete", "aborted", "exporting", "stopping", "export_error"}:
            return
        self._phase = "aborted"
        self._phase_timer.stop()
        self._frame_timer.stop()
        if self.store is not None:
            try:
                self._event("SESSION_ABORT")
                self._cancel_pending_syncs("verification_cancelled_after_operator_abort")
                self._close_recording()
            except (OSError, RuntimeError, TriggerError, ValueError) as error:
                self.store.record_device_log(self.device.health().device_name, "stop_error", str(error))
            self.store.abort_unfinished(export=False)
            self._begin_export(exit_code=1)
        else:
            self.finished.emit(1)


def run_session(experiment: dict[str, Any], devices: dict[str, Any], **kwargs: Any) -> int:
    # Prevent a second process from marking a currently running session as a
    # recovered interruption. QLockFile also detects locks left by dead PIDs.
    from PySide6.QtCore import QLockFile
    database_path = Path(experiment['paths']['database']).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(database_path) + '.acquisition.lock')
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise RuntimeError('已有实验正在使用该数据库，请先结束并保存原实验，不能同时续采。')
    nas_reservation=None
    try:
        if kwargs.get('run_mode')=='brainco' and experiment.get('nas_progress_sync'):
            from ..subject_sync import begin_synced_acquisition
            nas_reservation=begin_synced_acquisition(experiment,str(kwargs['participant_id']))
        with acquisition_lock(str(kwargs.get("run_mode", "simulation")), devices):
            try:
                return _run_session_locked(experiment, devices, **kwargs)
            finally:
                disconnect = getattr(kwargs.get("device"), "disconnect", None)
                if disconnect is not None:
                    disconnect()
    finally:
        needs_upload=False
        try:
            if nas_reservation:
                from ..subject_sync import end_synced_acquisition
                needs_upload=end_synced_acquisition(nas_reservation)
        finally:lock.unlock()
        if needs_upload:
            # The stream, SQLite writer, device lease and local acquisition lock
            # are all closed before taking a consistent upload snapshot.
            from ..transfer_ui import auto_upload_after_session
            auto_upload_after_session(experiment,str(kwargs['participant_id']))


def _run_session_locked(
    experiment: dict[str, Any],
    devices: dict[str, Any],
    *,
    participant_id: str,
    day_index: int,
    manifest_path: Path,
    windowed: bool,
    run_mode: str = "simulation",
    software_rendering: bool = True,
    device: EEGDevice | None = None,
    skippable_waits: bool = True,
) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    # Validate the session plan before creating a visible window. This keeps
    # configuration/manifest errors in the terminal and avoids an empty GUI.
    read_manifest(manifest_path)
    if experiment.get('dual_image', {}).get('enabled', False) and run_mode != 'simulation':
        import re
        if not re.fullmatch(r'(?:sub-)?[0-9]{8}', participant_id):
            raise ValueError('双图正式采集必须使用人员平台的 8 位被试编号，保留前导零。')
        if not str(experiment['dual_image'].get('paradigm_code', '')).strip():
            raise ValueError('请先填写公司的双图范式代码 paradigm_code，不能复用旧 RSVP 标识。')

    # This must happen before QApplication is created. Software OpenGL makes
    # the MVP usable on machines with an unreliable graphics-driver path.
    if software_rendering and QApplication.instance() is None:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL)
    application = QApplication.instance() or QApplication([])
    fixed = bool(experiment.get('fixed_schedule'))
    widget_class = RasterStimulusWidget if fixed else StimulusWidget
    if fixed:
        experiment['display']['presentation_backend'] = 'raster_paint_completion'
    widget = widget_class(
        background_rgb=experiment["display"]["background_rgb"],
        max_image_fraction=float(experiment["display"]["max_image_fraction"]),
        screen_index=int(experiment["display"].get("screen_index", 0)),
        image_cache_size=int(experiment["stimulus"].get("preload_batch_size", 272)) if fixed else 64,
        protocol_image_mode=bool(experiment.get('fixed_schedule')),
    )
    # Bind the window to the configured acquisition screen before any show*()
    # call; fullscreen otherwise lands on the operator's primary screen.
    widget.place_on_screen()
    if windowed:
        widget.show_windowed()
    else:
        widget.showFullScreen()
    runner = SessionRunner(
        widget=widget,
        experiment=experiment,
        devices=devices,
        participant_id=participant_id,
        day_index=day_index,
        manifest_path=manifest_path,
        windowed=windowed,
        run_mode=run_mode,
        device=device,
        skippable_waits=skippable_waits,
    )
    try:
        runner.prepare()
        runner.finished.connect(application.exit)
        return application.exec()
    finally:
        # Teardown must not start a second asynchronous export on close.
        widget.close_requested.disconnect()
        widget.close()
        runner.database.close()


def run_simulation(experiment: dict[str, Any], devices: dict[str, Any], **kwargs: Any) -> int:
    return run_session(experiment, devices, run_mode="simulation", **kwargs)
