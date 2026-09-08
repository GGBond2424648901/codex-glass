"""Actual native controls on anonymous fixtures; captures never read the desktop."""
import json,sys
from pathlib import Path
from PyQt5.QtCore import Qt,QPoint
from PyQt5.QtGui import QPainter
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from native_dashboard import NativeDashboard
from frosted_desktop import GlassWidget
from glass_host import ScaledSurfaceHost
from tests.native_fixtures import dashboard_fixture
from tests.glass_fixtures import telemetry
from tests.capture_dashboard import run

def save_with_popup(root,popup,path):
    frame=root.grab();p=QPainter(frame);p.drawPixmap(popup.pos()-root.pos(),popup.grab());p.end();frame.save(str(path))

def capture(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    run(out)
    w=NativeDashboard(persist=False,auto_fetch=False);w.setAttribute(Qt.WA_DontShowOnScreen);w.set_motion(False);w.move(0,0);w.show()
    data,context,events=dashboard_fixture();w.context=context;w.can_import=True;w.apply_data(data);w.change_page('history')
    w.pages['history'].apply_history({'events':events,'total':14578});w.pages['history'].choose_models();QTest.qWait(50)
    save_with_popup(w,w.pages['history'].popup,out/'history-filter-open.png');w.pages['history'].popup.hide()
    w.change_page('overview');chart=w.pages['overview'].chart;chart.zoom_at(.55,1);QTest.mouseMove(chart,QPoint(430,130));QTest.qWait(50)
    popup=chart._tooltip if hasattr(chart,'_tooltip') else getattr(chart,'tooltip',None)
    if popup is not None and popup.isVisible():save_with_popup(w,popup,out/'overview-chart-zoom.png');popup.hide()
    else:w.grab().save(str(out/'overview-chart-zoom.png'))
    w.hide();w.deleteLater()
    widget=GlassWidget(url='http://127.0.0.1:1',start_backend=False,persist=False);widget.setAttribute(Qt.WA_DontShowOnScreen);widget.set_motion(False);widget.show();widget.apply_data(telemetry())
    for scope,name in [('today','today'),('last_5_hours','5h'),('all','all')]:
        widget.change_scope(scope);QTest.qWait(100);widget.grab().save(str(out/f'widget-{name}.png'))
    widget.toggle_compact();QTest.qWait(50);widget.grab().save(str(out/'widget-mini.png'));widget.toggle_compact()
    widget.more.click();QTest.qWait(40);widget.menu.grab().save(str(out/'widget-menu.png'));widget.menu.hide()
    widget.show_appearance();QTest.qWait(40);widget.appearance.grab().save(str(out/'widget-appearance.png'));widget.appearance.hide()
    host=ScaledSurfaceHost(widget,scale=.7);host.setAttribute(Qt.WA_DontShowOnScreen);host.show();QTest.qWait(50);host.grab().save(str(out/'widget-70-percent.png'))
    host.hide();widget.tray.hide();host.deleteLater();app.processEvents()
    print('Captured 15 pages and native interactions, widget and miniature.')

if __name__=='__main__':capture(sys.argv[1])
