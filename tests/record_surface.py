"""Record real Qt animation as transparent animated WebP, without desktop pixels."""
import sys,json
from pathlib import Path
from urllib.request import urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from desktop_widget import GlassWidget
from PIL import Image
from io import BytesIO
from PyQt5.QtCore import QBuffer,QIODevice

target=Path(sys.argv[1]);target.parent.mkdir(parents=True,exist_ok=True)
url=sys.argv[2] if len(sys.argv)>2 else 'http://127.0.0.1:8081'
with urlopen(url+'/api/widget',timeout=15) as response:data=json.load(response)
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
w=GlassWidget(url,False,False);w.setAttribute(Qt.WA_DontShowOnScreen,True);w.apply_data(data);w.change_scope('last_5_hours');w.show()
frames=[];first=w.flow_phase
for i in range(80):
    QTest.qWait(250)
    buffer=QBuffer();buffer.open(QIODevice.WriteOnly);w.grab().save(buffer,'PNG')
    frames.append(Image.open(BytesIO(bytes(buffer.data()))).convert('RGBA'))
frames[0].save(target,save_all=True,append_images=frames[1:],duration=250,loop=0,lossless=True)
print('Recorded',len(frames),'frames at actual 250 ms intervals; phase',first,'->',w.flow_phase,'; outer alpha',frames[-1].getpixel((0,0))[3])
w.hide();w.tray.hide();w.detail_popup.hide()
