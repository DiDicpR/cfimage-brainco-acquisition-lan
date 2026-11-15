"""Standalone uploader; never opens EEG hardware or changes acquisition progress."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from PySide6.QtWidgets import QApplication,QDialog,QVBoxLayout,QLabel,QLineEdit,QPushButton,QComboBox,QFileDialog,QMessageBox
from .nas_transfer import ROOT,upload_subject,check_upload_access,readonly_sqlite_uri
from .transfer_ui import TransferDialog


def find_dataset(folder):
    folder=Path(folder).resolve()
    for candidate in (folder,folder/'native',folder/'data/native'):
        if (candidate/'progress.sqlite').is_file():return candidate
    raise ValueError('找不到 progress.sqlite，请选择原采集程序的 data/native 文件夹。')


def subjects_in(folder):
    dataset=find_dataset(folder)
    with closing(sqlite3.connect(readonly_sqlite_uri(dataset/'progress.sqlite'),uri=True)) as db:
        rows=db.execute('SELECT DISTINCT participant_id FROM sessions ORDER BY participant_id').fetchall()
    return dataset,[str(row[0]) for row in rows if (dataset/str(row[0])).is_dir()]


class UploadDialog(QDialog):
    def __init__(self):
        super().__init__();self.setWindowTitle('CFIMAGE · 上传被试全部数据到 NAS 公区');self.resize(720,330)
        layout=QVBoxLayout(self)
        hint=QLabel('结束采集并保存后使用。上传所选被试在本机的全部会话，包括整段 EEG、原始网络时序、标签和进度。\n逐文件读回校验；本地数据保留。')
        hint.setWordWrap(True);layout.addWidget(hint)
        self.folder=QLineEdit(str(ROOT/'data/native'));layout.addWidget(self.folder)
        browse=QPushButton('选择已有数据文件夹');browse.clicked.connect(self.browse);layout.addWidget(browse)
        self.subject=QComboBox();layout.addWidget(self.subject)
        refresh=QPushButton('刷新被试列表');refresh.clicked.connect(self.refresh);layout.addWidget(refresh)
        check=QPushButton('检查公区上传权限（自动登录）');check.clicked.connect(self.check);layout.addWidget(check)
        upload=QPushButton('上传所选被试全部数据');upload.clicked.connect(self.upload);layout.addWidget(upload)
        self.status=QLabel('');self.status.setWordWrap(True);layout.addWidget(self.status);self.refresh()
    def browse(self):
        folder=QFileDialog.getExistingDirectory(self,'选择原采集程序的 data/native',self.folder.text())
        if folder:self.folder.setText(folder);self.refresh()
    def refresh(self):
        self.subject.clear()
        try:
            folder,subjects=subjects_in(self.folder.text());self.folder.setText(str(folder));self.subject.addItems(subjects)
            self.status.setText(f'找到 {len(subjects)} 个被试；请确认编号后上传。')
        except Exception as e:self.status.setText(str(e))
    def check(self):
        TransferDialog('检查公共目录上传',lambda progress:check_upload_access(progress=progress),self).exec()
    def upload(self):
        participant=self.subject.currentText()
        if not participant:
            QMessageBox.information(self,'没有被试','请先选择有采集数据的文件夹。');return
        try:dataset=find_dataset(self.folder.text())
        except ValueError as e:QMessageBox.warning(self,'数据目录',str(e));return
        from .transfer_ui import upload_with_notice
        upload_with_notice(dataset,participant,self)


def main():
    app=QApplication.instance() or QApplication(sys.argv);dialog=UploadDialog();dialog.show();return app.exec()


if __name__=='__main__':raise SystemExit(main())
