import json,os,sqlite3,tempfile,unittest,shutil
from pathlib import Path
from unittest.mock import patch
from src.upload_credentials import ensure_smb_access,protect_bytes,SMB_SHARE
from test_nas_transfer import fixture


class SMBAuthTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows long paths')
    def test_deep_raw_archive_and_sqlite_survive_upload_restore(self):
        from src.nas_transfer import filesystem_path,readonly_sqlite_uri,upload_subject,restore_subject,SMBStore
        with tempfile.TemporaryDirectory() as folder:
            extended=filesystem_path(folder)
            try:
                project=extended/'project';native=fixture(project)
                relative=Path('wire_archives')/('x'*100)/('y'*100)/('z'*80)
                raw=native/'s001/s001'/relative;raw.mkdir(parents=True)
                (raw/'rx.bin').write_bytes(b'full raw stream')
                db=sqlite3.connect(raw/'eeg.sqlite3')
                db.execute('CREATE TABLE samples(n INTEGER)');db.execute('INSERT INTO samples VALUES (123)');db.commit();db.close()
                store=SMBStore(extended/'nas')
                result=upload_subject(native,'s001',project,store,lambda _:None)
                restored=extended/'restored';restore_subject(result['snapshot_key'],restored,project,store,lambda _:None)
                self.assertEqual((restored/'native/s001/s001'/relative/'rx.bin').read_bytes(),b'full raw stream')
                db=sqlite3.connect(readonly_sqlite_uri(restored/'native/s001/s001'/relative/'eeg.sqlite3'),uri=True)
                try:self.assertEqual(db.execute('SELECT n FROM samples').fetchone(),(123,))
                finally:db.close()
            finally:
                # This entire temporary directory was created by this test.
                shutil.rmtree(extended)

    def test_credentials_never_sent_to_another_share(self):
        with patch('src.upload_credentials.smb_connection') as connect:
            for path in [r'\\other\aim\data',r'\\172.16.4.2\aim-wrong\data']:
                with self.assertRaises(ValueError):ensure_smb_access(path,Path('.'))
            connect.assert_not_called()

    @unittest.skipUnless(os.name=='nt','DPAPI')
    def test_protected_credentials_are_used_for_automatic_login(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'smb.dpapi'
            value=dict(schema_version=1,kind='smb',share=SMB_SHARE,username='fake-user',password='fake-password')
            path.write_bytes(protect_bytes(json.dumps(value).encode()))
            with patch('src.upload_credentials.credential_path',return_value=path),patch('src.upload_credentials.smb_connection') as connect:
                ensure_smb_access(SMB_SHARE+r'\shared\CFIMAGE-20261113\eeg-data',Path(folder))
                connect.assert_called_once_with('fake-user','fake-password')

    def test_upload_picker_finds_existing_project_subjects_without_writing_database(self):
        from src.upload_gui import subjects_in
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);native=fixture(root)
            before=(native/'progress.sqlite').read_bytes()
            dataset,subjects=subjects_in(root)
            self.assertEqual(dataset,native.resolve()) # Windows 8.3 TEMP aliases resolve to the same directory.
            self.assertEqual(subjects,['s001','s002'])
            self.assertEqual(before,(native/'progress.sqlite').read_bytes())
