"""App-only transparent PNG evidence. Never capture an external desktop background."""
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtGui import QImage,QPainter
from desktop_widget import GlassWidget
from tests.glass_fixtures import telemetry

out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
w=GlassWidget('http://127.0.0.1:1',False,False);w.set_motion(False);w.timer.stop();w.apply_data(telemetry());w.show()
QTest.qWait(100)
frames=[]
for scope in ('today','last_5_hours','all'):
    w.change_scope(scope);QTest.qWait(30);pix=w.grab();pix.save(str(out/(scope+'.png')));frames.append(pix)
w.toggle_compact();QTest.qWait(30);w.grab().save(str(out/'mini.png'));w.toggle_compact()
w.appearance.show();QTest.qWait(30);w.appearance.grab().save(str(out/'appearance.png'));w.appearance.hide()
sheet=QImage(1320,686,QImage.Format_ARGB32);sheet.fill(Qt.transparent);p=QPainter(sheet)
for i,pix in enumerate(frames):p.drawPixmap(i*440,0,pix)
p.end();sheet.save(str(out/'three-pages.png'))
w.change_scope('last_5_hours')
flow=out/'flow';flow.mkdir(exist_ok=True)
for i in range(48):
    # Eight seconds apart in simulated rendering time; no fabricated data changes.
    w.flow_phase=i*.25;w.update();QTest.qWait(1);w.grab().save(str(flow/f'{i:03d}.png'))
w.hide();w.tray.hide();w.detail_popup.hide()
print('Captured transparent app-only surfaces and 48 procedural material frames.')
