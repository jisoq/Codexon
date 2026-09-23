from PySide6.QtWidgets import QApplication
from cachemonitor.overlay import SessionOverlay
from test_overlay_presentation import summary


def test_pre_dashboard_overlay_design_dimensions():
    app=QApplication.instance() or QApplication([]);card=SessionOverlay()
    try:
        card.set_content(summary());model=card.content_model
        assert (card.width(),card.height())==(380,578)
        model.set_layout(detail=True)
        assert model.panel_width()==620 and model.monitor_x==240
        assert not card.qml_errors
    finally:card.close();app.processEvents()
