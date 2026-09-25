"""Public-facing translation and payload checks keep internal records intact."""
import hashlib
import json

import pytest

from cachemonitor.i18n import set_language, tr
from cachemonitor.presentation import Choice, Text
from tools.package_release import payload


def test_language_choice_is_saved_and_used_on_next_window(tmp_path):
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.quick_qa import control, click, render_plot
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    path=str(tmp_path/'language.ini');windows=[]
    set_language('ko')
    try:
        settings=QSettings(path,QSettings.IniFormat)
        window=Dashboard([],start_worker=False,settings=settings);windows.append(window)
        window.open_settings();window.show();QTest.qWait(40)
        choice=window.settings_page.controls['language']
        restart=window.settings_page.controls['restart'];requests=[]
        window.settings_page.restartRequested.connect(lambda:requests.append(True))
        assert not control(window,restart).isEnabled()
        render_plot(window,choice).forceActiveFocus()
        QTest.keyClick(window.quick,Qt.Key_End);QTest.qWait(20)
        settings.sync()
        saved=QSettings(path,QSettings.IniFormat)
        assert saved.value('ui/language')=='en'
        assert control(window,restart).isEnabled()
        QTest.keyClick(window.quick,Qt.Key_Home);QTest.qWait(20)
        assert not control(window,restart).isEnabled()
        QTest.keyClick(window.quick,Qt.Key_End);QTest.qWait(20)
        click(window,control(window,restart))
        assert requests==[True] and not control(window,restart).isEnabled()
        window.quit_app()
        set_language(saved.value('ui/language'))
        reopened=Dashboard([],start_worker=False,settings=saved);windows.append(reopened)
        reopened.open_settings();reopened.show();QTest.qWait(40)
        choice=reopened.settings_page.controls['language']
        assert control(reopened,choice).property('currentText')=='English'
        assert not control(reopened,reopened.settings_page.controls['restart']).isEnabled()
        assert reopened.settings_page.navigation.state['items'][0]['text']=='General'
        assert reopened.grab().save(str(tmp_path/'english-settings.png'))
        render_plot(reopened,choice).forceActiveFocus()
        QTest.keyClick(reopened.quick,Qt.Key_Home);QTest.qWait(20);saved.sync()
        assert QSettings(path,QSettings.IniFormat).value('ui/language')=='ko'
        assert not reopened.qml_errors
    finally:
        for window in windows:window.quit_app()
        set_language('ko');app.setProperty('cachemonitorDisableShellIntegration',previous)


def test_english_token_labels_do_not_change_stored_values():
    set_language('en')
    try:
        label = Text('일반 입력')
        choice = Choice()
        choice.addItem('캐시 읽기', 'cached')
        assert label.text() == '일반 입력'
        assert label.state['text'] == 'Uncached input'
        assert choice.itemData(0) == 'cached'
        assert choice.state['items'][0]['text'] == 'Cached input'
        assert tr('2 / 2호출') == '2 / 2 calls'
        assert tr('모양') == 'Appearance'
        assert tr('알림') == 'Notifications'
    finally:
        set_language('ko')


def test_user_text_is_verbatim_even_when_equal_to_a_translation_key(tmp_path):
    from cachemonitor.i18n import Verbatim
    from cachemonitor.table_model import Table, Cell
    from cachemonitor.quick_qa import mount, dispose, walk
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    set_language('en');host=None
    try:
        for text in ('사용한도 주석 표시 정리','사용 한도','캐시 읽기'):
            assert tr(text)!=text
            assert tr(Verbatim(text))==text
            assert tr(text)!=text
            assert Text(Verbatim(text)).state['text']==text
        choice=Choice();choice.addItem(Verbatim('캐시 읽기'),'project')
        assert choice.state['items'][0]['text']=='캐시 읽기'
        table=Table();table.setColumnCount(1);table.setHorizontalHeaderLabels(['작업'])
        table.setRowCount(1);table.setItem(0,0,Cell(Verbatim('사용한도 주석 표시 정리')))
        host=mount(table)
        labels=[item.property('text') for item in walk(host.quick.rootObject()) if item.objectName()=='cell-label']
        assert '사용한도 주석 표시 정리' in labels
        assert host.grab().save(str(tmp_path/'english-raw-title.png'))
        assert not host.qml_errors
    finally:
        if host:dispose(host)
        set_language('ko')


def test_public_payload_rejects_local_paths_and_unneeded_qt(tmp_path):
    folder = tmp_path/'.codex'/'Codexon';folder.mkdir(parents=True)
    exe = folder/'Codexon.exe';exe.write_bytes(b'synthetic executable')
    recovery=folder/'CodexonRecovery.exe';recovery.write_bytes(b'synthetic recovery')
    (folder/'CodexonHook.exe').write_bytes(b'synthetic hook helper')
    for name in ('LICENSE', 'THIRD-PARTY-NOTICES.md', 'BUNDLED-PYTHON.md', 'BUNDLED-QT.md',
                 'SOURCE-OFFER.md', 'USER-GUIDE.md', 'USER-GUIDE.ko.md'):
        (folder/name).write_text('Synthetic release test', encoding='utf-8')
    manifest = {'product':'Codexon','version':'2026.09.23.6','commit':'a'*40,
                'architecture':'x64','executable':'Codexon.exe',
                'sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),
                'recovery_sha256':hashlib.sha256(recovery.read_bytes()).hexdigest()}
    file = folder/'build-manifest.json'
    file.write_text(json.dumps(manifest), encoding='utf-8')
    assert payload(folder)[2] == manifest
    internal = folder/'_internal';internal.mkdir()
    (internal/'base_library.zip').write_bytes(b'python standard library archive')
    assert payload(folder)[2] == manifest
    (internal/'diagnostics.zip').write_bytes(b'not an app dependency')
    with pytest.raises(ValueError):payload(folder)
    (internal/'diagnostics.zip').unlink()
    file.write_text(json.dumps({**manifest,'source':r'C:\Users\developer\repo'}), encoding='utf-8')
    with pytest.raises(ValueError):payload(folder)
    file.write_text(json.dumps(manifest), encoding='utf-8')
    dll = folder/'_internal'/'PySide6'/'Qt6Graphs.dll';dll.parent.mkdir(parents=True)
    dll.write_bytes(b'not a redistributable module for this product')
    with pytest.raises(ValueError):payload(folder)
