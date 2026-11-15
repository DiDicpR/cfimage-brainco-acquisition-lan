"""NAS actions run outside the Qt GUI thread."""
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QProgressBar, QPushButton, QMessageBox, QInputDialog, QLineEdit


class TransferWorker(QThread):
    message = Signal(str)
    def __init__(self, callback, parent):
        super().__init__(parent);self.callback=callback;self.result=None;self.error=''
    def run(self):
        try:self.result=self.callback(self.message.emit)
        except Exception as e:self.error=f'{type(e).__name__}: {e}'


class TransferDialog(QDialog):
    def __init__(self, title, callback, parent=None, *, auto_close=False):
        super().__init__(parent);self.auto_close=auto_close;self.setWindowTitle(title);self.resize(650,230)
        layout=QVBoxLayout(self);self.status=QLabel('正在准备…');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.bar=QProgressBar();self.bar.setRange(0,0);layout.addWidget(self.bar)
        self.button=QPushButton('关闭');self.button.setEnabled(False);self.button.clicked.connect(self.accept);layout.addWidget(self.button)
        self.worker=TransferWorker(callback,self);self.worker.message.connect(self.status.setText);self.worker.finished.connect(self.done_transfer);self.worker.start()
    def done_transfer(self):
        self.bar.setRange(0,1);self.bar.setValue(1);self.button.setEnabled(True)
        if self.worker.error:self.status.setText('未完成：'+self.worker.error)
        else:
            self.status.setText('完成，已核验保存内容。\n'+str(self.worker.result))
            if self.auto_close:self.accept()
    def reject(self):
        if not self.worker.isRunning():super().reject()
    def closeEvent(self,event):
        if self.worker.isRunning():event.ignore()
        else:super().closeEvent(event)


def connect_smb(parent=None):
    """Credentials stay in Windows' current logon session, never in repo files."""
    import os,ctypes
    from ctypes import wintypes
    if os.name!='nt':raise RuntimeError('SMB login helper requires Windows')
    username,ok=QInputDialog.getText(parent,'连接 NAS','SMB 用户名（或 DOMAIN\\username）')
    if not ok:return
    password,ok=QInputDialog.getText(parent,'连接 NAS','SMB 密码（仅用于当前 Windows 会话）',QLineEdit.EchoMode.Password)
    if not ok:return
    class Resource(ctypes.Structure):
        _fields_=[('scope',wintypes.DWORD),('type',wintypes.DWORD),('display',wintypes.DWORD),('usage',wintypes.DWORD),('local',wintypes.LPWSTR),('remote',wintypes.LPWSTR),('comment',wintypes.LPWSTR),('provider',wintypes.LPWSTR)]
    resource=Resource();resource.type=1;resource.remote=r'\\172.16.4.2\aim'
    function=ctypes.WinDLL('mpr').WNetAddConnection2W
    function.argtypes=[ctypes.POINTER(Resource),wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD];function.restype=wintypes.DWORD
    code=function(ctypes.byref(resource),password,username,0);password=''
    if code:raise RuntimeError(f'Windows SMB 登录失败（{code}）。1219 表示本机已有其他账号连接此 NAS，请先在 Windows 中处理连接。')
    QMessageBox.information(parent,'NAS','已连接；密码未写入项目。')


def upload_from_operator(dialog):
    from .nas_transfer import upload_subject
    participant=dialog.participant.text().strip()
    if dialog.busy or not participant:return
    upload_with_notice(dialog.output_root,participant,dialog)


def auto_upload_after_session(experiment,participant):
    """Also runs after Esc or a handled device stop, once saving has finished."""
    from .nas_transfer import upload_subject
    from .subject_sync import sync_subject_progress
    dataset=experiment['paths']['dataset_root']
    def transfer(progress):
        result=upload_subject(dataset,participant,progress=progress)
        progress('上传及读回核验完成，正在更新跨电脑续采进度…')
        sync_subject_progress(dataset,participant,progress=progress)
        return (f"被试 {participant}：数据已上传、读回核验，NAS 续采进度已更新。\n"
                f"新增/变更 {result['uploaded_files']} 个文件；复用 {result['reused_files']} 个文件。\n本地原始 EEG 保留。")
    dialog=TransferDialog('采集结束：自动上传并同步进度',transfer)
    dialog.exec()
    if dialog.worker.error:
        QMessageBox.warning(None,'自动上传未完成，本地数据保留',
            '本次未能完成全部上传/读回/进度同步，请恢复 NAS 网络后点击“上传当前被试全部数据”重试。\n'
            '在成功上传前不要换电脑继续同一被试。\n'+dialog.worker.error)
    return not bool(dialog.worker.error)


def upload_with_notice(dataset,participant,parent=None):
    from .nas_transfer import upload_subject
    from .subject_sync import upload_overview
    check=TransferDialog('核对被试当天采集和上传记录',lambda _:upload_overview(dataset,participant),parent,auto_close=True)
    check.exec()
    if check.worker.error:return
    info=check.worker.result
    notice=(f"被试 {participant} · {info['date']}\n今天本机已采集 {info['local_today_sessions']} 段，本机累计 {info['local_total_sessions']} 段。\n"
            +('今天已经上传过；再次上传会复用一致文件。\n' if info['already_uploaded_today'] else '今天尚未发现本机上传记录。\n')
            +'只上传新增或变化的文件；进度库有变化时会重新上传该小文件。旧 EEG 只校验，不重复传送。\n上传完成后，其他电脑才能接续最新进度。继续上传？')
    if QMessageBox.question(parent,'确认被试与增量上传',notice)!=QMessageBox.StandardButton.Yes:return
    TransferDialog('上传当前被试全部数据：'+participant,
        lambda progress:upload_subject(dataset,participant,progress=progress),parent).exec()
