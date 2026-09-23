from cachemonitor.quick_qa import mount, dispose, control
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.ui_details import Details


def test_details_keyboard_toggle_preserves_expansion_and_escapes_data():
    app=QApplication.instance() or QApplication([])
    details=Details('계산 근거')
    details.set_sections([('관측 범위','<unknown-model> · 50개')])
    host=mount(details,500,300)
    app.processEvents()
    assert not details.content.isVisible()
    control(host,details.toggle).forceActiveFocus()
    QTest.keyClick(host.quick,Qt.Key_Space)
    assert details.content.isVisible()
    assert '&lt;unknown-model&gt;' in details.body.text()
    details.set_sections([('관측 범위','<updated> · 51개')])
    assert details.content.isVisible() and '51개' in details.body.text()
    QTest.keyClick(host.quick,Qt.Key_Space)
    assert not details.content.isVisible()
    dispose(host)
