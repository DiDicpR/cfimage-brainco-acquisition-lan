"""Desktop acquisition workbench; the experiment's stimulus window is separate."""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QThread, QUrl, QTimer
from PySide6.QtGui import QDesktopServices, QPainter, QPainterPath, QColor, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)


STYLE = """
QDialog#workbench { background: #f3f5f5; }
QWidget { font-family: 'Microsoft YaHei UI', 'Microsoft YaHei'; font-size: 13px; color: #20343b; }
QLabel { background: transparent; }
QFrame#sidebar { background: #152e38; border: none; }
QLabel#brand { color: #ffffff; font-size: 21px; font-weight: 700; letter-spacing: 2px; }
QLabel#brandSub { color: #a9bdc5; font-size: 11px; letter-spacing: 2px; }
QLabel#sideSection { color: #849fa9; font-size: 11px; letter-spacing: 1px; }
QLabel#navActive { background: #264650; color: #f2faf8; border-radius: 9px; padding: 13px 14px; font-weight: 600; }
QPushButton#archiveButton { background: transparent; color: #b3c7cf; border: none; text-align: left; padding: 13px 14px; }
QPushButton#archiveButton:hover { background: #264650; border-radius: 9px; color: white; }
QLabel#sideNote { color: #c0d0d6; font-size: 12px; }
QLabel#sideFoot { color: #829fa9; font-size: 11px; }
QFrame#sideLine { background: #34515b; max-height: 1px; }
QScrollArea { border: none; background: transparent; }
QWidget#canvas { background: #f3f5f5; }
QLabel#eyebrow { color: #66818a; font-size: 10px; font-weight: 600; letter-spacing: 2px; }
QLabel#pageTitle { font-size: 28px; font-weight: 700; color: #152e38; }
QLabel#muted { color: #72828a; font-size: 12px; }
QLabel#small { color: #6a7e86; font-size: 11px; }
QLabel#formalBadge { color: #187567; background: #e2efea; padding: 7px 12px; border-radius: 7px; font-size: 11px; font-weight: 600; }
QFrame#card { background: #ffffff; border: 1px solid #e1e7e8; border-radius: 12px; }
QLabel#cardTitle { font-size: 16px; font-weight: 700; }
QLabel#number { background: #edf3f3; color: #53767c; border-radius: 6px; padding: 4px 7px; font-size: 10px; font-weight: 700; }
QLabel#field { color: #425c66; font-size: 12px; font-weight: 600; }
QLabel#metric { font-size: 24px; color: #163d46; font-weight: 700; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #f8fafb; border: 1px solid #d9e2e4; border-radius: 7px; min-height: 40px; padding: 0 11px; selection-background-color: #197c70; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { background: white; border: 1px solid #278e81; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled { background: #f0f3f3; color: #94a1a5; }
QComboBox::drop-down { border: none; width: 25px; }
QComboBox QAbstractItemView { background: white; selection-background-color: #deeeea; selection-color: #173e43; }
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 19px; border: none; }
QPushButton { border-radius: 7px; padding: 10px 17px; font-weight: 600; }
QPushButton#startButton { background: #17796b; color: white; border: 1px solid #17796b; min-height: 27px; }
QPushButton#startButton:hover { background: #106455; }
QPushButton#startButton:pressed { background: #0b5045; }
QPushButton#startButton:disabled { background: #b6cdc6; color: #eff5f2; border-color: #b6cdc6; }
QPushButton#checkButton { background: white; color: #245a58; border: 1px solid #cbdcd8; }
QPushButton#checkButton:hover { background: #edf6f3; }
QPushButton#checkButton:disabled { color: #a1afad; background: #f5f7f6; }
QPushButton#folderButton { background: #edf3f2; color: #42645e; border: none; font-size: 11px; }
QPushButton#folderButton:hover { background: #dfece7; }
QLabel#statusBadge { color: #7c6944; background: #f7f0e3; border-radius: 6px; padding: 5px 9px; font-size: 10px; }
QLabel#statusBadge[state='checking'] { color: #236f80; background: #e7f1f5; }
QLabel#statusBadge[state='passed'] { color: #187567; background: #e2efea; }
QLabel#statusBadge[state='failed'] { color: #a04540; background: #faeae8; }
QLabel#checkValue { color: #8a979d; font-size: 11px; }
QLabel#checkValue[state='passed'] { color: #17796b; }
QLabel#checkValue[state='failed'] { color: #a04540; }
QLabel#errorText { color: #a04540; font-size: 11px; }
QFrame#line { background: #e9eeee; max-height: 1px; border: none; }
QProgressBar { background: #e7efed; border: none; border-radius: 2px; max-height: 4px; }
QProgressBar::chunk { background: #389b8c; border-radius: 2px; }
QLabel#path { color: #305860; background: #f2f6f6; border-radius: 7px; padding: 12px; font-size: 11px; }
QLabel#chip { color: #617a81; background: #f1f5f5; border-radius: 4px; padding: 4px 7px; font-size: 10px; }
"""


def label(text: str, name: str = '', wrap: bool = False) -> QLabel:
    item = QLabel(text)
    item.setObjectName(name)
    item.setWordWrap(wrap)
    item.setTextFormat(Qt.TextFormat.PlainText)
    return item


def line(name: str = 'line') -> QFrame:
    item = QFrame()
    item.setObjectName(name)
    item.setFixedHeight(1)
    return item


def state(widget: QWidget, value: str) -> None:
    widget.setProperty('state', value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class SignalMark(QWidget):
    """Static brand mark, deliberately not presented as a live EEG waveform."""
    def __init__(self):
        super().__init__()
        self.setFixedSize(46, 38)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#294b52'))
        painter.drawRoundedRect(0, 0, 44, 36, 9, 9)
        painter.setPen(QPen(QColor('#84d9bf'), 2))
        path = QPainterPath()
        path.moveTo(7, 19)
        for x, y in [(14,19),(18,10),(22,27),(27,14),(30,19),(37,19)]:
            path.lineTo(x, y)
        painter.drawPath(path)


class PreflightWorker(QThread):
    def __init__(self, callback: Callable, mode: str, devices: dict, code: int, parent):
        super().__init__(parent)
        self.callback, self.mode, self.devices, self.code = callback, mode, devices, code
        self.result = None
        self.error = ''

    def run(self):
        try:
            self.result = self.callback(self.mode, self.devices, self.code)
        except Exception as error:
            self.error = str(error) or type(error).__name__


class OperatorDialog(QDialog):
    def __init__(self, experiment: dict, devices: dict, modes: list[str], preflight: Callable):
        super().__init__()
        self.experiment, self.devices, self.preflight = experiment, devices, preflight
        self.output_root = Path(experiment['paths']['dataset_root']).resolve()
        self.dual_mode = bool(experiment.get('dual_image', {}).get('enabled', False))
        self.scene_mode = experiment.get('collection', {}).get('strategy') == 'scenes'
        self.fixed_schedule = bool(experiment.get('fixed_schedule'))
        self.selection: dict[str, object] = {}
        self.worker = None
        self.busy = False
        self.catalog_worker = None
        self._opened = False
        self._sync_identity = None
        self._extension_allowed = False
        self.auto_sync_timer = QTimer(self)
        self.auto_sync_timer.setSingleShot(True)
        self.auto_sync_timer.timeout.connect(self.auto_sync_selected)
        self.setObjectName('workbench')
        self.setWindowTitle('场景图片 EEG · 采集工作台' if self.scene_mode else ('THINGS EEG · 双图采集' if self.dual_mode else 'THINGS EEG · 采集工作台'))
        self.setMinimumSize(900, 630)
        self.resize(1080, 850)
        self.setStyleSheet(STYLE)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(200)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(22, 32, 22, 25)
        side.setSpacing(13)
        side.addWidget(SignalMark())
        side.addWidget(label('场景图片 EEG' if self.scene_mode else 'THINGS EEG', 'brand'))
        side.addWidget(label('ACQUISITION SUITE', 'brandSub'))
        side.addSpacing(42)
        side.addWidget(label('实验管理', 'sideSection'))
        side.addWidget(label('01   采集工作台', 'navActive'))
        archive = QPushButton('02   会话文件夹')
        archive.setObjectName('archiveButton')
        archive.clicked.connect(self.open_folder)
        side.addWidget(archive)
        self.upload_button = QPushButton('上传当前被试全部数据')
        self.upload_button.setObjectName('archiveButton')
        from .transfer_ui import upload_from_operator
        self.upload_button.clicked.connect(lambda: upload_from_operator(self))
        side.addWidget(self.upload_button)
        self.query_button = QPushButton('03   被试进度查询')
        self.query_button.setObjectName('archiveButton')
        self.query_button.clicked.connect(self.open_progress_query)
        self.position_mode = experiment.get('collection', {}).get('strategy') == 'positions'
        self.query_button.setVisible(self.position_mode)
        side.addWidget(self.query_button)
        side.addStretch()
        side.addWidget(line('sideLine'))
        side.addWidget(label('每次呈现，都有迹可循。', 'sideNote'))
        side.addWidget(label('脑电记录  /  事件同步\n场景图片实验' if self.scene_mode else '脑电记录  /  事件同步\nTHINGS-EEG2-Extended', 'sideFoot'))
        outer.addWidget(sidebar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        canvas = QWidget()
        canvas.setObjectName('canvas')
        body = QVBoxLayout(canvas)
        body.setContentsMargins(30, 29, 30, 24)
        body.setSpacing(18)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(6)
        heading.addWidget(label('ACQUISITION / 采集工作台', 'eyebrow'))
        heading.addWidget(label('准备下一次采集', 'pageTitle'))
        heading.addWidget(label('填写实验信息，完成设备检查，即可进入图片呈现。', 'muted'))
        header.addLayout(heading, 1)
        right = QVBoxLayout()
        right.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        right.addWidget(label('●  正式采集', 'formalBadge'))
        right.addWidget(label(datetime.now().strftime('%Y / %m / %d'), 'small'))
        header.addLayout(right)
        body.addLayout(header)

        metrics_card, metrics = self.card()
        metrics.setContentsMargins(20, 16, 20, 16)
        metric_row = QHBoxLayout()
        self.channel_metric = label('', 'metric')
        self.rate_metric = label('', 'metric')
        self.duration_metric = label(f"{int(experiment['stimulus']['image_duration_ms'])} ms", 'metric')
        for caption, value, detail in [
            ('记录通道', self.channel_metric, '含事件标记通道'),
            ('采样率', self.rate_metric, '按当前设备配置'),
            ('图片呈现', self.duration_metric, '单张刺激持续时间'),
        ]:
            column = QVBoxLayout()
            column.setSpacing(4)
            column.addWidget(label(caption, 'small'))
            column.addWidget(value)
            detail_label = label(detail, 'small')
            if value is self.channel_metric:self.channel_detail = detail_label
            column.addWidget(detail_label)
            metric_row.addLayout(column, 1)
        metrics.addLayout(metric_row)
        body.addWidget(metrics_card)

        columns = QHBoxLayout()
        columns.setSpacing(16)
        setup_card, setup = self.card('实验设置', '01')
        setup.addWidget(label('被试编号', 'field'))
        self.participant = QLineEdit()
        self.participant.setObjectName('participantInput')
        self.participant.setPlaceholderText('人员平台 8 位编号，例如 00000001' if self.dual_mode else '例如 sub-112')
        self.participant.setMaxLength(64)
        if self.scene_mode:
            import sqlite3
            self.participant_choice = QComboBox()
            self.participant_choice.addItem('新被试（在下方输入编号）', '')
            database = Path(experiment['paths']['database']).resolve()
            if database.is_file():
                with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
                    for (identity,) in db.execute('SELECT DISTINCT participant_id FROM sessions ORDER BY participant_id'):
                        self.participant_choice.addItem(identity, identity)
            self.participant_choice.currentIndexChanged.connect(
                lambda: self.participant.setText(self.participant_choice.currentData() or ''))
            setup.addWidget(self.participant_choice)
        setup.addWidget(self.participant)
        setup.addWidget(label('使用匿名编号；同一被试可沿用原编号续采。', 'small', True))
        inputs = QHBoxLayout()
        for title, attr, widget in [('实验日次', 'day_index', QSpinBox()), ('采集设备', 'mode', QComboBox())]:
            setattr(self, attr, widget)
            column = QVBoxLayout()
            column.addWidget(label(title, 'field'))
            column.addWidget(widget)
            inputs.addLayout(column, 1)
        self.day_index.setRange(1, 999)
        self.mode.setObjectName('deviceSelect')
        names = {'neuracle':'铂睿康 Neuracle（64 EEG 通道）','brainco':'强脑 BrainCo（32 EEG 通道）'}
        for mode in modes:self.mode.addItem(names.get(mode,mode) if self.scene_mode else mode,mode)
        if 'neuracle' in modes:self.mode.setCurrentIndex(self.mode.findData('neuracle'))
        setup.addSpacing(6)
        setup.addLayout(inputs)
        setup.addWidget(label('图片呈现时间（秒）', 'field'))
        self.image_duration = QDoubleSpinBox()
        self.image_duration.setObjectName('imageDurationSeconds')
        self.image_duration.setDecimals(3)
        duration_min, duration_max = experiment['stimulus']['allowed_duration_ms']
        self.image_duration.setRange(duration_min / 1000, duration_max / 1000)
        self.image_duration.setSingleStep(0.1)
        self.image_duration.setSuffix(' 秒')
        self.image_duration.setValue(int(experiment['stimulus']['image_duration_ms']) / 1000)
        setup.addWidget(self.image_duration)
        if self.dual_mode:
            self.image_duration.setValue(1.2)
            self.image_duration.setEnabled(False)
            setup.addWidget(label('双图时序：提示 0.5 s → fixation 0.5 s → 双图 1.2 s → blank 0.5 s。', 'small', True))
            setup.addWidget(label('公司双图范式代码（正式采集必填）', 'field'))
            self.paradigm_code = QLineEdit(str(experiment['dual_image'].get('paradigm_code', '')))
            self.paradigm_code.setObjectName('pairedParadigmCode')
            self.paradigm_code.setPlaceholderText('填写公司的实际范式代码')
            setup.addWidget(self.paradigm_code)
        setup.addWidget(label(f'可设 {duration_min / 1000:g}–{duration_max / 1000:g} 秒；实际显示按屏幕帧数取整。', 'small', True))
        setup.addStretch()
        setup.addWidget(line())
        setup.addWidget(label('正式入口仅使用真实采集设备。', 'small', True))
        columns.addWidget(setup_card, 5)

        device_card, device_layout = self.card('设备与同步', '02')
        status_row = QHBoxLayout()
        status_row.addWidget(label('本次设备状态', 'field'))
        status_row.addStretch()
        self.status = label('等待检查', 'statusBadge')
        status_row.addWidget(self.status)
        device_layout.addLayout(status_row)
        self.endpoint = label('', 'small', True)
        device_layout.addWidget(self.endpoint)
        self.check_values = []
        for title in ['EEG 数据接收', '事件标注与时间对齐', '文件写入与回读']:
            row = QHBoxLayout()
            row.addWidget(label(title))
            row.addStretch()
            value = label('未检查', 'checkValue')
            self.check_values.append(value)
            row.addWidget(value)
            device_layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.hide()
        device_layout.addWidget(self.progress)
        self.status_detail = label('开启 Jellyfish 转发后检查。启动时会再次验证。', 'small', True)
        device_layout.addWidget(self.status_detail)
        self.check_button = QPushButton('连通性与采集/标注测试' if self.scene_mode else '检查设备')
        self.check_button.setObjectName('checkButton')
        self.check_button.clicked.connect(lambda: self.begin_check(False))
        device_layout.addWidget(self.check_button)
        columns.addWidget(device_card, 5)
        body.addLayout(columns)

        collection_card, collection_layout = self.card('场景、视角与图片选择' if self.scene_mode else ('按图片序号采集' if self.position_mode else '续采与分组'), '03')
        self.collection_enabled = bool(experiment.get('collection', {}).get('enabled', False))
        choices = QHBoxLayout()
        self.group_select = QComboBox()
        self.group_select.setObjectName('groupSelect')
        self.group_select.addItem('自动续采未完成组', 0)
        group_count = int(experiment.get('collection', {}).get('group_count', 10))
        for index in range(1, group_count + 1):
            self.group_select.addItem(f'第 {index} / {group_count} 大组', index)
        self.group_select.setCurrentIndex(int(experiment.get('collection', {}).get('group_index', 0)))
        self.position_input = QLineEdit(','.join(map(str, experiment.get('collection', {}).get('positions', [1]))))
        self.position_input.setObjectName('positionInput')
        self.position_input.setPlaceholderText('例如 1,2,3（范围 1–10）')
        self.target_repeats = QSpinBox()
        self.target_repeats.setObjectName('targetRepetitions')
        self.target_repeats.setRange(1, 100)
        self.target_repeats.setValue(int(experiment.get('collection', {}).get('target_repetitions', 10)))
        self.session_limit = QComboBox()
        self.session_limit.setObjectName('sessionLimit')
        default_limit = int(experiment.get('collection', {}).get('max_blocks_per_session', 0 if self.position_mode else 5))
        if self.position_mode or self.scene_mode:
            self.session_limit.addItem('完整选定范围（所有类别）', 0)
        for count in sorted({2, 5, 10, 20, default_limit} - {0}):
            self.session_limit.addItem(f"最多 {count} 区块 / {count * int(experiment['blocks']['trials_per_block'])} 次", count)
        self.session_limit.setCurrentIndex(self.session_limit.findData(default_limit))
        fields = [('训练图序号（每个类别）', self.position_input), ('每张目标有效次数（累计）', self.target_repeats)] if self.position_mode else [('图片大组', self.group_select), ('本次采集上限（含重复）', self.session_limit)]
        if self.scene_mode:
            fields = [('每张固定呈现次数' if self.fixed_schedule else '每张目标有效次数（累计）', self.target_repeats), ('本次采集上限（含重复）', self.session_limit)]
        for title, widget in fields:
            column = QVBoxLayout()
            column.addWidget(label(title, 'field'))
            column.addWidget(widget)
            choices.addLayout(column, 1)
        collection_layout.addLayout(choices)
        if self.scene_mode:
            from .scene_selector import SceneSelector
            self.scene_selector = SceneSelector(Path(experiment['paths']['stimulus_manifest']),
                experiment['collection'].get('selected_image_ids'))
            collection_layout.addWidget(self.scene_selector)

        self.include_test = QCheckBox('另外采集测试集（200 类，每类 1 张）')
        self.include_test.setChecked(bool(experiment.get('collection', {}).get('include_test', False)))
        self.test_repeats = QSpinBox()
        self.test_repeats.setRange(1, 100)
        self.test_repeats.setValue(int(experiment.get('collection', {}).get('test_repetitions', 10)))
        if self.position_mode:
            collection_layout.addWidget(label('每个序号覆盖全部 1,654 个训练类别；例如 1,2,3 共选 4,962 张。', 'small', True))
            test_row = QHBoxLayout()
            test_row.addWidget(self.include_test, 1)
            test_row.addWidget(label('测试图累计次数', 'small'))
            test_row.addWidget(self.test_repeats)
            collection_layout.addLayout(test_row)
            collection_layout.addWidget(label('本次范围（可选分段；分段并不代表已完成全部类别）', 'field'))
            collection_layout.addWidget(self.session_limit)
        if self.dual_mode:
            dual = experiment['dual_image']
            self.target_repeats.setMinimum(2)
            self.target_repeats.setSingleStep(2)
            self.test_repeats.setMinimum(2)
            self.test_repeats.setSingleStep(2)
            collection_layout.addWidget(label('双图范围与配对：目标左右各半，非目标不计目标次数。', 'small', True))
            self.concept_input = QLineEdit(','.join(dual.get('concept_ids', [])))
            self.concept_input.setObjectName('pairedConceptIds')
            self.concept_input.setPlaceholderText('留空选全部类别；补采输入 8 个完整 concept_id，用逗号分隔')
            collection_layout.addWidget(self.concept_input)
            self.pairing_select = QComboBox()
            self.pairing_select.setObjectName('pairedStrategy')
            self.pairing_select.addItem('随机搭配另一类别', 'random')
            self.pairing_select.addItem('固定类别组内搭配', 'fixed')
            self.pairing_select.setCurrentIndex(self.pairing_select.findData(dual.get('pairing', 'random')))
            collection_layout.addWidget(self.pairing_select)
            self.fixed_pairs_input = QLineEdit(';'.join(','.join(pair) for pair in dual.get('fixed_category_pairs', [])))
            self.fixed_pairs_input.setObjectName('pairedFixedPairs')
            self.fixed_pairs_input.setPlaceholderText('固定组：类别A,类别B;类别C,类别D（完整 concept_id）')
            collection_layout.addWidget(self.fixed_pairs_input)
            collection_layout.addWidget(label('补采全部十张时输入 1,2,3,4,5,6,7,8,9,10；看错按 E，续采补齐对应目标侧。', 'small', True))
        progress_row = QHBoxLayout()
        self.collection_status = label('填写被试编号，读取上次进度；开始前也会自动核对。', 'small', True)
        progress_row.addWidget(self.collection_status, 1)
        self.resume_button = QPushButton('同步 NAS 并查看剩余进度' if experiment.get('nas_progress_sync') else '查看剩余进度')
        self.resume_button.setObjectName('checkButton')
        self.resume_button.clicked.connect(lambda: self.begin_check(False, progress_only=True))
        progress_row.addWidget(self.resume_button)
        collection_layout.addLayout(progress_row)
        collection_layout.addWidget(label('按场景 / 视角 / 实物编号独立计数 · 已有效完成的自动跳过' if self.scene_mode else '按固定文件名序号选图 · 累计达标后自动跳过 · 中断/无效试次补采' if self.position_mode else '固定分组 · 已有效完成的自动跳过 · 中断/无效试次补采 · 每次新建会话', 'small', True))
        collection_card.setVisible(self.collection_enabled)
        if self.scene_mode:
            collection_layout.addWidget(label('每张图片独立续采；少量选图仍保留原重复间隔，可能需多次会话补齐。', 'small', True))
        body.addWidget(collection_card)

        storage_card, storage = self.card('数据保存', '04' if self.collection_enabled else '03')
        row = QHBoxLayout()
        row.addWidget(label('按被试与会话自动归档', 'small'))
        row.addStretch()
        folder = QPushButton('打开保存目录  ↗')
        folder.setObjectName('folderButton')
        folder.clicked.connect(self.open_folder)
        row.addWidget(folder)
        storage.addLayout(row)
        self.path_preview = label('', 'path', True)
        self.path_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        storage.addWidget(self.path_preview)
        chips = QHBoxLayout()
        for text in ['原始脑电 · float32', '兼容导出 · EDF', '事件标签 · TSV', '质量校验 · JSON']:
            chips.addWidget(label(text, 'chip'))
        chips.addStretch()
        storage.addLayout(chips)
        body.addWidget(storage_card)

        self.error = label('', 'errorText', True)
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error.hide()
        # Keep the actual exception beside the device checks, where the operator
        # is looking, rather than below the long collection/storage form.
        device_layout.insertWidget(device_layout.indexOf(self.endpoint) + 1, self.error)
        footer = QHBoxLayout()
        self.footer_note = label('就绪后开始\n设备预检通过后，将打开刺激呈现窗口。', 'muted', True)
        footer.addWidget(self.footer_note, 1)
        self.start_button = QPushButton('预检并继续采集  →' if self.fixed_schedule else '预检并开始实验  →')
        self.start_button.setObjectName('startButton')
        self.start_button.setMinimumWidth(220)
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(lambda: self.begin_check(True))
        footer.addWidget(self.start_button)
        body.addStretch()
        scroll.setWidget(canvas)
        # Keep the start action reachable even on smaller acquisition monitors.
        main = QVBoxLayout()
        main.setSpacing(0)
        main.addWidget(scroll, 1)
        footer_frame = QFrame()
        footer_frame.setLayout(footer)
        footer.setContentsMargins(30, 15, 30, 20)
        main.addWidget(footer_frame)
        outer.addLayout(main, 1)
        for button in self.findChildren(QPushButton):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if button is not self.start_button:
                button.setAutoDefault(False)
        if self.dual_mode:
            self.paradigm_code.textChanged.connect(self.update_identity)
            self.concept_input.textChanged.connect(self.reset_collection_status)
            self.pairing_select.currentIndexChanged.connect(self.reset_collection_status)
            self.fixed_pairs_input.textChanged.connect(self.reset_collection_status)
        self.participant.textChanged.connect(self.update_identity)
        self.mode.currentTextChanged.connect(self.update_device)
        self.group_select.currentIndexChanged.connect(self.reset_collection_status)
        self.session_limit.currentIndexChanged.connect(self.reset_collection_status)
        self.position_input.textChanged.connect(self.update_identity)
        self.target_repeats.valueChanged.connect(self.reset_collection_status)
        self.test_repeats.valueChanged.connect(self.reset_collection_status)
        self.include_test.toggled.connect(self.reset_collection_status)
        self.image_duration.valueChanged.connect(self.update_duration)
        if self.scene_mode:
            self.scene_selector.changed.connect(self.update_identity)
        self.update_identity()
        self.update_device()
        if self.fixed_schedule:
            self.setWindowTitle(experiment['project']['name'])
            self.participant.setText(experiment.get('operator', {}).get('default_participant', 'S001'))
            self.participant.setPlaceholderText('确认本次被试的正式编号')
            self.lock_fixed_controls()
            collection_layout.addWidget(label(
                '强脑32通道：全部10个场景、平视及俯视；2580张，默认每张10次。\n'
                '每 80 张显示加减题 2 秒（无需作答），随后停下等待空格。\n'
                '按空格后注视十字 0.8 秒，再开始下一轮 80 张；Esc 保存退出。\n'
                '每张0.6秒、间隔0.1秒；完成目标后可提高累计次数，继续采新增轮次。短测使用独立测试编号。', 'small', True))

    def lock_fixed_controls(self):
        if self.fixed_schedule:
            for widget in (self.image_duration, self.scene_selector):
                widget.setEnabled(False)
            self.target_repeats.setEnabled(self._extension_allowed and not self.busy)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._opened and self.experiment.get('nas_progress_sync'):
            self._opened = True
            QTimer.singleShot(0, self.sync_catalog)

    def sync_catalog(self):
        if not self.isVisible():return
        if self.busy:
            QTimer.singleShot(600, self.sync_catalog)
            return
        from .subject_sync import subject_catalog
        self.collection_status.setText('正在自动同步 NAS 被试列表；选定编号后自动核对进度…')
        self.catalog_worker = PreflightWorker(lambda *_: subject_catalog(), '', {}, 0, self)
        self.catalog_worker.finished.connect(self.catalog_finished)
        self.catalog_worker.start()

    def catalog_finished(self):
        worker = self.catalog_worker
        if worker.error:
            self.error.setText('NAS 自动同步失败：'+worker.error+'；请恢复网络后重新同步，不能按零进度续采。')
            self.error.show()
        else:
            self.participant_choice.blockSignals(True)
            known = {self.participant_choice.itemData(i) for i in range(self.participant_choice.count())}
            for participant in worker.result:
                if participant not in known:self.participant_choice.addItem(participant, participant)
            self.participant_choice.blockSignals(False)
            if not self.busy:self.collection_status.setText('NAS 被试列表已同步。输入或选择编号后自动核对剩余进度。')
            self.auto_sync_timer.start(600)
        worker.deleteLater()
        self.catalog_worker = None

    def auto_sync_selected(self):
        if not self.isVisible():return
        if self.busy or self.catalog_worker:
            self.auto_sync_timer.start(600)
        elif self.resume_button.isEnabled() and self.participant.text().strip():
            self.begin_check(False, progress_only=True)

    @staticmethod
    def card(title: str = '', number: str = ''):
        frame = QFrame()
        frame.setObjectName('card')
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        if title:
            heading = QHBoxLayout()
            heading.addWidget(label(number, 'number'))
            heading.addWidget(label(title, 'cardTitle'))
            heading.addStretch()
            layout.addLayout(heading)
        return frame, layout

    def update_identity(self):
        identity = self.participant.text().strip()
        if self.experiment.get('nas_progress_sync') and identity != self._sync_identity:
            self._sync_identity = identity
            self._extension_allowed = False
            self.target_repeats.blockSignals(True)
            self.target_repeats.setRange(10, 100)
            self.target_repeats.setValue(10)
            self.target_repeats.blockSignals(False)
            self.lock_fixed_controls()
            self.auto_sync_timer.start(600)
        valid = bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', identity)) and identity != 'sub-'
        if self.position_mode:
            from .experiment.positions import parse_positions
            try:
                parse_positions(self.position_input.text())
            except ValueError:
                valid = False
        if self.dual_mode:
            valid = valid and bool(re.fullmatch(r'(?:sub-)?[0-9]{8}', identity))
        if self.scene_mode:
            valid = valid and bool(self.scene_selector.selected_ids())
        can_start = valid and (not self.dual_mode or bool(self.paradigm_code.text().strip()))
        self.start_button.setEnabled(can_start and not self.busy)
        self.upload_button.setEnabled(valid and not self.busy)
        self.resume_button.setEnabled(valid and not self.busy)
        self.path_preview.setText(str(self.output_root / (identity if valid else '<被试编号>')) + '\n<会话目录> / eeg')
        self.error.hide()
        self.reset_collection_status()

    def reset_collection_status(self):
        self.collection_status.setText('填写被试编号，读取上次进度；开始前也会自动核对。')

    def selected_experiment(self):
        experiment = deepcopy(self.experiment)
        experiment['stimulus']['image_duration_ms'] = round(self.image_duration.value() * 1000)
        if self.collection_enabled:
            if self.scene_mode:
                experiment['collection'].update(selected_image_ids=self.scene_selector.selected_ids(),
                    target_repetitions=self.target_repeats.value(),
                    max_blocks_per_session=self.session_limit.currentData())
            elif self.position_mode:
                from .experiment.positions import parse_positions
                experiment['collection'].update(positions=parse_positions(self.position_input.text()),
                    target_repetitions=self.target_repeats.value(), include_test=self.include_test.isChecked(),
                    test_repetitions=self.test_repeats.value(), max_blocks_per_session=self.session_limit.currentData())
            else:
                experiment['collection'].update(group_index=self.group_select.currentData(),
                                                max_blocks_per_session=self.session_limit.currentData())
        if self.dual_mode:
            dual = experiment['dual_image']
            dual['paradigm_code'] = self.paradigm_code.text().strip()
            dual['pairing'] = self.pairing_select.currentData()
            dual['concept_ids'] = [v.strip() for v in self.concept_input.text().replace('，', ',').split(',') if v.strip()]
            dual['fixed_category_pairs'] = [[v.strip() for v in pair.split(',')]
                for pair in self.fixed_pairs_input.text().replace('；', ';').replace('，', ',').split(';') if pair.strip()]
            if dual['pairing'] == 'fixed' and not dual['concept_ids']:
                dual['concept_ids'] = list(dict.fromkeys(v for pair in dual['fixed_category_pairs'] for v in pair))
        return experiment

    def update_duration(self):
        self.duration_metric.setText(f'{round(self.image_duration.value() * 1000)} ms')
        self.reset_collection_status()

    def open_progress_query(self):
        if self.fixed_schedule and self.experiment.get('nas_progress_sync'):
            self.begin_check(False,progress_only=True)
            return
        from .progress_ui import ProgressDialog
        experiment = self.selected_experiment()
        experiment['collection'].update(target_repetitions=self.target_repeats.value(),
                                        test_repetitions=self.test_repeats.value())
        dialog = ProgressDialog(experiment, [self.mode.itemText(i) for i in range(self.mode.count())],
                                self.participant.text().strip(), self.current_mode(), self)
        dialog.exec()

    def show_collection_summary(self, summary):
        if summary.get('fixed_schedule'):
            self._extension_allowed = bool(summary.get('extension_allowed'))
            self.target_repeats.blockSignals(True)
            self.target_repeats.setMinimum(summary.get('registered_target_repetitions', 10))
            self.target_repeats.setValue(summary['target_repetitions'])
            self.target_repeats.blockSignals(False)
            self.lock_fixed_controls()
            self.collection_status.setText(
                f"已核验呈现 {summary['total_completed']} / {summary['total_target']} 次（有效 EEG 另计）\n"
                f"下一原始试次：{summary['next_schedule_trial_id']}；本次 {summary['trial_limit']} 次，"
                f"纯刺激约 {summary['estimated_minutes']} 分钟。\n"
                f"未核验记录 {summary.get('unverified_trials', 0)} 次，不跳过；保持原始排程顺序。\n" +
                ("当前目标已完成，次数栏已解锁：调高累计次数后继续采集。" if self._extension_allowed else
                 "当前累计次数锁定；完成目标后自动解锁。") +
                "\n点击“预检并继续采集”自动接续剩余试次；每80张展示算式2秒，按空格后等待0.8秒继续。")
            return
        if summary.get('strategy') == 'scenes':
            prefix = f"已选 {summary['selected_images']} 张 · 每张累计 {summary['target_repetitions']} 次"
        elif summary.get('strategy') in {'positions', 'paired'}:
            prefix = f"训练图序号 {','.join(map(str, summary['positions']))} · 每张累计 {summary['target_repetitions']} 次"
        else:
            group = summary['group_index']
            prefix = f"第 {group}/{summary['group_count']} 大组尚余 {summary['group_remaining']} 次" if group else '所有大组已完成'
        text = (f"总体有效 {summary['total_completed']} / {summary['total_target']} 次 · {prefix}\n"
                f"本次最多 {summary['trial_limit']} 次，基础时长约 {summary['estimated_minutes']} 分钟（不含探测题及延长休息）")
        if summary.get('strategy') == 'scenes' and summary.get('limited_by_image_gap'):
            text += '\n小范围保留原图片重复间隔：本次每张至多一次，后续会话继续补齐。'
        if summary.get('strategy') == 'paired':
            text += '\n左右有效次数分别累计，各占目标次数的一半；非目标不累计。'
            if summary.get('limited_by_image_gap'):
                text += '\n小范围受原图片重复间隔限制，本次每个未完成目标至多采一次，后续继续补采。'
        if summary.get('previous_session'):
            text += '\n上次会话：' + summary['previous_session']['session_id']
        if summary.get('unverified_trials'):
            text += f"\n另有 {summary['unverified_trials']} 次未通过保存数据核对，不计入有效进度。"
        self.collection_status.setText(text)

    def current_mode(self):
        return self.mode.currentData() or self.mode.currentText()

    def update_device(self):
        self.reset_collection_status()
        config = self.devices['devices'][self.current_mode()]
        count = config.get('channel_count', '—')
        if self.scene_mode and self.current_mode() == 'neuracle':
            self.channel_metric.setText('64 EEG + 1 TRG')
        else:self.channel_metric.setText(str(count))
        self.channel_detail.setText('32 EEG；事件单独保存' if self.current_mode() == 'brainco' else '64 EEG + 1 个 TRG 标记通道')
        self.rate_metric.setText(f"{config.get('sampling_rate_hz', '—')} Hz")
        if str(config.get('trigger_transport', 'serial')).lower() == 'tcp':
            trigger_endpoint = f"{config.get('trigger_host', '—')}:{config.get('trigger_tcp_port', 4321)}（网络）"
        else:
            trigger_endpoint = str(config.get('trigger_port') or config.get('trigger_transport', '—'))
        if self.current_mode() == 'brainco':
            address = '自动发现脑电帽' if config.get('auto_discover',True) else f"{config.get('host','')}:{config.get('port',0)}"
            self.endpoint.setText(address + ' · 软件时间映射，无硬件 Trigger')
        else:self.endpoint.setText(f"{config.get('host', '本机')}:{config.get('port', '—')}  ·  Trigger {trigger_endpoint}")
        self.status.setText('等待检查')
        state(self.status, 'idle')
        for value in self.check_values:
            value.setText('未检查')
            state(value, 'idle')
        self.status_detail.setText('先关闭 BCIGo / 独立诊断的采集连接。短测会收数、保存并回读标注。' if self.current_mode() == 'brainco' else '开启数据转发后检查。启动时会再次验证。')

    def open_folder(self):
        if not self.output_root.is_dir():
            self.error.setText('保存目录将在首次采集时创建：' + str(self.output_root))
            self.error.show()
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_root))):
            self.error.setText('无法打开文件夹，请复制上方路径到资源管理器。')
            self.error.show()

    def begin_check(self, launch: bool, progress_only: bool = False):
        if self.catalog_worker:
            self.collection_status.setText('正在自动同步 NAS 被试列表，请稍候…')
            return
        if self.busy or ((launch or progress_only) and not self.resume_button.isEnabled()) or (launch and not self.start_button.isEnabled()):
            return
        self.auto_sync_timer.stop()
        try:
            experiment = self.selected_experiment()
            from .experiment.config import validate_config
            validate_config(experiment, self.devices)
        except ValueError as error:
            self.error.setText(str(error))
            self.error.show()
            return
        self.checked_experiment = deepcopy(experiment)
        self.busy = True
        self.launch_after_check = launch
        self.progress_only = progress_only
        self.error.hide()
        self.progress.show()
        if progress_only:
            self.collection_status.setText('正在读取有效试次，并核对已保存的 EEG / Trigger…')
        else:
            self.status.setText('正在检查')
            state(self.status, 'checking')
            for value in self.check_values:
                value.setText('检查中')
                state(value, 'checking')
            self.status_detail.setText('正在核对采集计划、EEG 与 Trigger，请稍候。')
        for widget in (self.participant, self.day_index, self.mode, self.start_button, self.check_button,
                       self.group_select, self.session_limit, self.resume_button, self.position_input,
                       self.target_repeats, self.include_test, self.test_repeats, self.query_button, self.image_duration):
            widget.setEnabled(False)
        if self.dual_mode:
            for widget in (self.paradigm_code, self.concept_input, self.pairing_select, self.fixed_pairs_input):
                widget.setEnabled(False)
        if self.scene_mode:
            self.scene_selector.setEnabled(False)
            self.participant_choice.setEnabled(False)
        if not progress_only:
            self.check_button.setText('设备检查中…')
        participant = self.participant.text().strip()
        def check(mode, devices, code):
            summary = None
            synced = None
            if self.collection_enabled and (launch or progress_only):
                if mode=='brainco' and experiment.get('nas_progress_sync'):
                    from .subject_sync import sync_subject_progress
                    synced=sync_subject_progress(experiment['paths']['dataset_root'],participant)
                from .experiment.collection import preview_collection
                summary = preview_collection(experiment, participant, mode)
                if launch and not summary['group_remaining']:
                    raise ValueError('所选范围已完成；请选择其他图片序号/分组，或提高累计目标次数。')
            result = {} if progress_only else self.preflight(mode, devices, code)
            return dict(result, collection=summary, subject_sync=synced, checked_experiment=experiment)
        preflight_devices = deepcopy(self.devices)
        if self.scene_mode:
            ids = self.scene_selector.selected_ids()
            if ids:
                row = self.scene_selector.metadata[ids[0]]
                preflight_devices['_preflight_annotation'] = {k: row.get(k,'') for k in
                    ('image_id','object_id','object_name','semantic_label','category_name','scene','view')}
            preflight_devices['_preflight_output_root'] = str(self.output_root/'device_checks')
        self.worker = PreflightWorker(check, self.current_mode(), preflight_devices,
                                      int(self.devices['trigger']['codes']['TRIGGER_TEST']), self)
        self.worker.finished.connect(self.check_finished)
        self.worker.start()

    def check_finished(self):
        worker = self.worker
        result, error = worker.result, worker.error
        worker.deleteLater()
        self.worker = None
        self.busy = False
        self.progress.hide()
        for widget in (self.participant, self.day_index, self.mode, self.check_button,
                       self.group_select, self.session_limit, self.resume_button, self.position_input,
                       self.target_repeats, self.include_test, self.test_repeats, self.query_button, self.image_duration):
            widget.setEnabled(True)
        if self.scene_mode:
            self.scene_selector.setEnabled(True)
            self.participant_choice.setEnabled(True)
        self.check_button.setText('重新检查设备')
        self.lock_fixed_controls()
        if self.dual_mode:
            self.image_duration.setEnabled(False)
            for widget in (self.paradigm_code, self.concept_input, self.pairing_select, self.fixed_pairs_input):
                widget.setEnabled(True)
        self.update_identity()
        if result and result.get('checked_experiment'):
            self.checked_experiment = result['checked_experiment']
        if error:
            self._extension_allowed = False
            self.lock_fixed_controls()
        if self.progress_only:
            if error:
                self.collection_status.setText('进度读取失败，请检查图片清单和数据库路径。')
                self.error.setText(error)
                self.error.show()
            elif result.get('collection'):
                self.show_collection_summary(result['collection'])
                if result.get('subject_sync'):
                    sync=result['subject_sync']
                    self.collection_status.setText(self.collection_status.text()+f"\n已同步 NAS {sync['remote_machines']} 份电脑进度；今天本机 {sync['local_today_sessions']} 段、其他电脑 {sync['remote_today_sessions']} 段。")
            return
        if result and result.get('collection'):
            self.show_collection_summary(result['collection'])
        if error:
            self.status.setText('检查未通过')
            state(self.status, 'failed')
            for value in self.check_values:
                value.setText('未确认')
                state(value, 'failed')
            self.status_detail.setText('检查中途停止，其余项目尚未确认。具体错误见上方红字，可选中复制。')
            self.error.setText(error)
            self.error.show()
            return
        self.status.setText('检查通过')
        state(self.status, 'passed')
        hardware = result['self_test'] == 'hardware_verified'
        for value, text in zip(self.check_values, ['已收到', '硬件回读' if hardware else '软件映射（未硬同步）', '已写入并回读' if result.get('saved_annotation_verified') else '已验证' if hardware else '已写入 / 未回读']):
            value.setText(text)
            state(value, 'passed')
        self.status_detail.setText('最近检查 ' + datetime.now().strftime('%H:%M:%S') + ' · 启动时会重新预检。' +
            ('\n样本、类别与实物编号已回读。报告：' + result['report_path'] if result.get('report_path') else ''))
        if self.launch_after_check:
            sync=result.get('subject_sync')
            if sync and (sync['local_today_sessions'] or sync['remote_today_sessions'] or sync['pending_upload']):
                answer=QMessageBox.question(self,'该被试已有采集记录',
                    f"被试 {self.participant.text().strip()}：今天本机已采 {sync['local_today_sessions']} 段，其他电脑已上传 {sync['remote_today_sessions']} 段。\n"
                    '本次将跳过已完成的固定试次，从剩余位置续采。结束后请上传，再换电脑。是否继续？')
                if answer!=QMessageBox.StandardButton.Yes:return
            if not hardware:
                QMessageBox.warning(self, '正在使用软件时间对齐',
                    '本设备仅支持软件对齐，未验证硬件 Trigger 回读。结果将标记为 software_estimated。')
            self.selection = dict(participant_id=self.participant.text().strip(),
                                  day_index=self.day_index.value(), mode=self.current_mode(),
                                  image_duration_ms=round(self.image_duration.value() * 1000))
            if self.collection_enabled:
                self.selection['collection'] = deepcopy(self.checked_experiment['collection'])
            if self.dual_mode:
                self.selection['dual_image'] = deepcopy(self.checked_experiment['dual_image'])
            self.accept()

    def reject(self):
        if not self.busy and not self.catalog_worker:
            super().reject()

    def closeEvent(self, event):
        if self.busy or self.catalog_worker:
            event.ignore()
        else:
            super().closeEvent(event)
