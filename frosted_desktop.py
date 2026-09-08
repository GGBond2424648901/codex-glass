"""Native Frosted Glass presentation: real Qt controls, vector charts and SQLite API."""
import argparse, ctypes, json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from PyQt5.QtCore import Qt,QTimer,QUrl,QPoint,QRectF,QSettings,QVariantAnimation
from PyQt5.QtGui import QPainter,QPen,QColor,QIcon,QPixmap,QFontMetrics
from PyQt5.QtNetwork import QNetworkAccessManager,QNetworkRequest,QLocalServer,QLocalSocket
from PyQt5.QtWidgets import QApplication,QWidget,QLabel,QPushButton,QButtonGroup,QScrollArea,QVBoxLayout,QSystemTrayIcon
from widget_data import compact,window_data,quota_windows
from glass_chart import GlassChart
from glass_ui import INK,MUTED,BLUE,MINT,STYLE,font,label,material,native_blur,pulse_path,IconButton,Appearance,GlassRange
from glass_popups import GlassInfoPopup,GlassMenu

def display_number(value):
    text=compact(value)
    if text and text[-1] in 'KMBT' and '.' in text:return text[:-1].rstrip('0').rstrip('.')+text[-1]
    return text

def app_icon():
    path=Path(__file__).parent/'assets'/'codex-glass.png'
    if path.exists():return QIcon(str(path))
    pix=QPixmap(64,64);pix.fill(Qt.transparent);p=QPainter(pix);p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen);p.setBrush(QColor(BLUE));p.drawRoundedRect(3,3,58,58,16,16)
    p.setPen(QPen(Qt.white,4,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));p.drawPath(pulse_path(QRectF(12,17,40,30)));p.end()
    return QIcon(pix)

class ModelRow(QWidget):
    def __init__(self,name,color,callback):
        super().__init__();self.name=name;self.color=color;self.callback=callback;self.stats={};self.unpriced=False;self.separator=True
        self.chart=GlassChart(self,color,mini=True);self.chart.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedHeight(65);self.setCursor(Qt.PointingHandCursor);self.setFocusPolicy(Qt.StrongFocus);self.setAccessibleName(name+' 模型详情')
    def refresh(self,stats,values,unpriced,animate=True):
        self.stats=stats;self.unpriced=unpriced;self.chart.set_series(values,animate=animate)
        self.setToolTip(self.name+f'\n{stats.get("total_tokens",0):,} Token\n'+('未定价，不计入费用' if unpriced else f'预估 ${stats.get("estimated_cost_usd",0):,.2f}'));self.update()
    def resizeEvent(self,event):
        self.chart.setGeometry(134,max(0,(self.height()-45)//2),max(30,self.width()-224),45)
    def mouseReleaseEvent(self,event):
        if event.button()==Qt.LeftButton:self.callback(self.name)
    def keyPressEvent(self,event):
        if event.key() in (Qt.Key_Return,Qt.Key_Space):self.callback(self.name)
        else:super().keyPressEvent(event)
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.Antialiasing);w=self.width();h=self.height();mid=h/2
        if self.underMouse() or self.hasFocus():
            p.setBrush(QColor(255,255,255,65));p.setPen(Qt.NoPen);p.drawRoundedRect(QRectF(0,1,w,h-2),10,10)
        p.setPen(Qt.NoPen);p.setBrush(QColor(self.color));p.drawEllipse(QRectF(0,mid-6.5,13,13))
        p.setFont(font(17,True));p.setPen(QColor(INK))
        p.drawText(QRectF(25,mid-14,108,28),Qt.AlignVCenter,p.fontMetrics().elidedText('未定价模型' if self.name=='unknown-model' else self.name,Qt.ElideRight,107))
        p.setFont(font(16,True));p.drawText(QRectF(w-93,mid-21,90,23),Qt.AlignRight|Qt.AlignVCenter,display_number(self.stats.get('total_tokens')))
        p.setFont(font(15));p.setPen(QColor(MUTED));cost='未计价' if self.unpriced else f'${self.stats.get("estimated_cost_usd",0):,.2f}'
        p.drawText(QRectF(w-104,mid+1,101,22),Qt.AlignRight|Qt.AlignVCenter,cost)
        if self.separator:p.setPen(QColor(255,255,255,115));p.drawLine(0,h-1,w,h-1)

class GlassWidget(QWidget):
    FULL_SIZE=(440,686)
    def __init__(self,url='http://127.0.0.1:8081',start_backend=True,persist=True):
        super().__init__();self.url=url.rstrip('/');self.start_backend=start_backend;self.persist=persist
        self.settings=QSettings('CodexMonitor','FrostedGlass');self.transparency=65;self.glass_preset='balanced'
        self.scope='today';self.data={};self.rows={};self.selected=None;self.compact_mode=False;self.drag=None
        self.network_busy=False;self.backend_process=None;self.backend_next_attempt=0;self.backend_attempts=0;self.connected=False;self.blur_active=False
        self.motion=True;self.chart_scope=None;self.remaining=None;self.flow_phase=0.0
        self.setWindowFlags(Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint);self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(STYLE);self.setWindowIcon(app_icon());self.setWindowTitle('Codex Glass');self.setFixedSize(*self.FULL_SIZE)
        if persist:
            self.transparency=max(15,min(90,self.settings.value('transparency',65,type=int)))
            self.glass_preset=self.settings.value('preset','balanced',type=str)
            if self.glass_preset not in ('clear','balanced','frosted'):self.glass_preset='balanced'
        self.body=QWidget(self);self.body.setGeometry(0,54,440,632)
        self.brand=label(self,'Codex',22,bold=True);self.brand.setGeometry(82,18,78,32)
        self.dot=label(self,'●',18,MINT);self.dot.setGeometry(155,21,18,23)
        self.pin=IconButton('pin','始终置顶',self);self.pin.move(343,17);self.pin.setCheckable(True);self.pin.setChecked(True)
        self.more=IconButton('more','外观与菜单',self);self.more.move(386,17);self.more.clicked.connect(self.show_menu)
        self.segments=QWidget(self.body);self.segments.setGeometry(28,4,384,42)
        self.segments.setStyleSheet('background:rgba(91,104,159,22);border:1px solid rgba(255,255,255,75);border-radius:20px;')
        self.group=QButtonGroup(self);self.group.setExclusive(True)
        for i,(title,key) in enumerate((('今日','today'),('5小时','last_5_hours'),('累计','all'))):
            b=QPushButton(title,self.body);b.setGeometry(31+127*i,7,124,36);b.setCheckable(True);b.setChecked(i==0);b.setFont(font(16,i==0))
            b.setStyleSheet('QPushButton{border:0;border-radius:18px;background:transparent;} QPushButton:checked{background:qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 rgba(255,255,255,235),stop:1 rgba(240,249,255,215));border:1px solid white;} QPushButton:hover{background:rgba(255,255,255,125);}')
            b.clicked.connect(lambda checked=False,k=key:self.change_scope(k));self.group.addButton(b)
        self.cost_title=label(self,'预估费用',16,MUTED);self.cost_title.setGeometry(32,118,245,24)
        self.cost=label(self,'—',48,bold=True);self.cost.setGeometry(30,136,255,58)
        self.tokens_title=label(self,'Token',16,MUTED);self.tokens_title.setGeometry(284,119,121,24)
        self.tokens=label(self,'—',32,bold=True);self.tokens.setGeometry(283,145,124,43)
        self.cost.setToolTip('按已知模型价格估算，非实际账单；未定价用量不计入费用。')
        self.chart_label=label(self.body,'Token / min',16,MUTED);self.chart_label.setGeometry(32,154,205,25)
        self.rate=label(self.body,'—',25,BLUE,True);self.rate.setAlignment(Qt.AlignRight);self.rate.setGeometry(284,152,123,29)
        self.range_box=GlassRange(self.body);self.range_box.addItems(['近7天','近30天','近90天']);self.range_box.setCurrentIndex(1)
        self.range_box.setGeometry(315,150,90,33);self.range_box.setFont(font(14));self.range_box.currentIndexChanged.connect(lambda:self.render_chart(False));self.range_box.hide()
        self.chart=GlassChart(self.body);self.chart.setGeometry(29,190,384,137)
        self.scroll=QScrollArea(self.body);self.scroll.setGeometry(32,340,378,198);self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.scroll.viewport().setAutoFillBackground(False)
        self.model_host=QWidget();self.model_host.setStyleSheet('background:transparent;');self.model_layout=QVBoxLayout(self.model_host)
        self.model_layout.setContentsMargins(0,0,0,0);self.model_layout.setSpacing(0);self.model_layout.addStretch();self.scroll.setWidget(self.model_host)
        self.quota_label=label(self.body,'额度',16,MUTED);self.quota_label.setGeometry(32,558,220,25)
        self.quota_text=label(self.body,'—',16,INK);self.quota_text.setGeometry(295,563,87,24)
        self.quota_reset=label(self.body,'',13,MUTED);self.quota_reset.setGeometry(296,589,108,21)
        self.collapse=IconButton('down','折叠为迷你模式',self.body);self.collapse.move(385,572);self.collapse.clicked.connect(self.toggle_compact)
        self.expand=IconButton('up','展开完整组件',self);self.expand.move(300,17);self.expand.clicked.connect(self.toggle_compact);self.expand.hide()
        self.mini_chart=GlassChart(self,mini=True);self.mini_chart.setGeometry(27,115,371,53);self.mini_chart.show_endpoint=True;self.mini_chart.hide()
        self.mini_quota=label(self,'—',16,'#087f79',True);self.mini_quota.setGeometry(354,72,61,27);self.mini_quota.hide()
        self.detail_popup=GlassInfoPopup(owner=self);self.detail_popup.setFixedWidth(355)
        self.menu=GlassMenu(owner=self);self.menu.addAction('显示 / 恢复',self.restore);self.menu.addAction('收起到托盘',self.hide_to_tray)
        self.top_action=self.menu.addAction('始终置顶');self.top_action.setCheckable(True);self.top_action.setChecked(True);self.top_action.toggled.connect(self.toggle_top);self.pin.toggled.connect(self.top_action.setChecked)
        self.menu.addAction('迷你 / 展开',self.toggle_compact);self.menu.addAction('外观…',self.show_appearance)
        self.motion_action=self.menu.addAction('动态效果');self.motion_action.setCheckable(True);self.motion_action.setChecked(True);self.motion_action.toggled.connect(self.set_motion)
        self.menu.addAction('立即检查数据',self.retry_backend);self.menu.addAction('打开完整看板',self.open_dashboard);self.menu.addSeparator();self.menu.addAction('退出',self.quit_widget)
        self.appearance=Appearance(self)
        self.tray=QSystemTrayIcon(app_icon(),self);self.tray.setToolTip('Codex Glass');self.tray.setContextMenu(self.menu);self.tray.activated.connect(self.tray_activate);self.tray.show()
        self.manager=QNetworkAccessManager(self);self.manager.finished.connect(self.received)
        self.timer=QTimer(self);self.timer.setInterval(5000);self.timer.timeout.connect(self.fetch);self.timer.start()
        area=QApplication.primaryScreen().availableGeometry();self.move(area.right()-self.width()-20,area.top()+25)
        if persist:
            pos=self.settings.value('position',type=QPoint)
            if pos and any(s.availableGeometry().contains(pos) for s in QApplication.screens()):self.move(pos)
        self.flash=QVariantAnimation(self);self.flash.setDuration(750);self.flash.setStartValue(0.0);self.flash.setEndValue(1.0);self.flash.valueChanged.connect(self.flash_metric)
        self.flow_timer=QTimer(self);self.flow_timer.setInterval(160);self.flow_timer.timeout.connect(self.flow_tick)
        if sys.platform=='win32':
            enabled=ctypes.c_int(1)
            try:
                if ctypes.windll.user32.SystemParametersInfoW(0x1042,0,ctypes.byref(enabled),0) and not enabled.value:self.motion_action.setChecked(False)
            except (OSError,AttributeError):pass
        from glass_tooltips import install_glass_tooltips
        install_glass_tooltips(self)
        QTimer.singleShot(100,self.fetch)
    def showEvent(self,event):
        super().showEvent(event);self.blur_active=native_blur(self)
        if hasattr(self,'timer'):self.timer.start()
        if hasattr(self,'flow_timer') and self.motion:self.flow_timer.start()
    def hideEvent(self,event):
        if hasattr(self,'timer'):self.timer.stop()
        if hasattr(self,'flow_timer'):self.flow_timer.stop()
        super().hideEvent(event)
    def flow_tick(self):
        self.flow_phase+=.008;self.update()
        if self.appearance.isVisible():self.appearance.update()
    def paintEvent(self,event):
        p=QPainter(self);material(p,QRectF(5,5,self.width()-10,self.height()-10),self.transparency,self.glass_preset,self.flow_phase)
        p.setPen(QPen(QColor(BLUE),2.5,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin));p.drawPath(pulse_path(QRectF(36,22,31,27)))
        if self.compact_mode:
            p.setPen(QPen(QColor(107,137,168,100),1));p.drawLine(194,74,194,101)
            ring=QRectF(316,73,27,27);p.setBrush(Qt.NoBrush);p.setPen(QPen(QColor(255,255,255,150),5));p.drawEllipse(ring)
            if self.remaining is not None:
                p.setPen(QPen(QColor(MINT),5,Qt.SolidLine,Qt.RoundCap));p.drawArc(ring,90*16,-round(self.remaining*3.6*16))
            return
        p.setPen(QColor(255,255,255,170));p.drawLine(32,600,408,600)
        track=QRectF(32,643,250,13);p.setPen(Qt.NoPen);p.setBrush(QColor(93,132,175,58));p.drawRoundedRect(track,6.5,6.5)
        if self.remaining is not None:
            p.setBrush(QColor(MINT if self.remaining>20 else '#e9aa65'));p.drawRoundedRect(QRectF(track.x(),track.y(),track.width()*self.remaining/100,track.height()),6.5,6.5)
    def flash_metric(self,t):
        a=QColor(BLUE);b=QColor(INK);c=QColor(round(a.red()*(1-t)+b.red()*t),round(a.green()*(1-t)+b.green()*t),round(a.blue()*(1-t)+b.blue()*t))
        self.tokens.setStyleSheet('color:'+c.name()+';background:transparent;')
    def apply_data(self,data):
        old=self.data;self.data=data;self.connected=True;self.render()
        if self.motion and old and old.get('total')!=data.get('total'):self.flash.start()
    def render(self):
        if not self.data:return
        total,models=window_data(self.data,self.scope);animate=self.chart_scope==self.scope
        incomplete=not self.data.get('by_model') and self.data.get('index',{}).get('status') not in ('ready',None)
        self.cost_title.setText('累计预估' if self.scope=='all' else '预估费用')
        self.cost.setText('—' if incomplete else f'${total.get("estimated_cost_usd",0):,.2f}');self.tokens.setText('—' if incomplete else display_number(total.get('total_tokens')));self.fit_metrics()
        self.tokens.setToolTip(f'{total.get("total_tokens",0):,} Token');self.render_chart(animate)
        unpriced={x.get('model') for x in self.data.get('pricing',{}).get('unpriced_models',[])}
        colors={'gpt-6-astra':'#0ac5e3','gpt-5.6-sol':'#a273fb','gpt-5.6-terra':'#fa9981'}
        ordering=sorted(models,key=lambda n:(n in unpriced,0 if n=='gpt-6-astra' else 1 if n=='gpt-5.6-sol' else 2,-models[n].get('total_tokens',0),n))
        for name in list(self.rows):
            if name not in models:
                row=self.rows.pop(name);self.model_layout.removeWidget(row);row.deleteLater()
        series=self.current_series()
        for i,name in enumerate(ordering):
            if name not in self.rows:self.rows[name]=ModelRow(name,colors.get(name,'#9798b5' if name in unpriced else '#739de2'),self.select_model)
            row=self.rows[name];row.separator=i<len(ordering)-1;self.model_layout.removeWidget(row);self.model_layout.insertWidget(i,row);row.setFixedHeight(49 if self.scope=='all' else 63)
            row.chart.motion=self.motion;row.refresh(models[name],series.get('by_model',{}).get(name,[]),name in unpriced,animate)
        self.chart_scope=self.scope;self.render_quota();self.update_status();self.update()
    def fit_metrics(self):
        chosen=38 if self.compact_mode else (40 if self.scope=='all' else 48)
        while chosen>19 and QFontMetrics(font(chosen,True)).horizontalAdvance(self.cost.text())>self.cost.width()-4:chosen-=1
        self.cost.setFont(font(chosen,True));size=23 if self.compact_mode else 32
        while size>14 and QFontMetrics(font(size,True)).horizontalAdvance(self.tokens.text())>self.tokens.width():size-=1
        self.tokens.setFont(font(size,True))
    def current_series(self):
        chart=self.data.get('charts',{}).get(self.scope,{})
        if self.scope!='all':return chart
        count=[7,30,90][self.range_box.currentIndex()]
        return {**chart,'labels':chart.get('labels',[])[-count:],'values':chart.get('values',[])[-count:],'by_model':{n:v[-count:] for n,v in chart.get('by_model',{}).items()}}
    def render_chart(self,animate=True):
        chart=self.current_series();values=chart.get('values',[]);labels=chart.get('labels',[]);daily=self.scope=='all'
        self.range_box.setVisible(daily);self.rate.setVisible(not daily);self.chart_label.setText('每日 Token' if daily else 'Token / min')
        self.chart.set_series(values,labels,animate,'每日 Token' if daily else 'Token / min');self.rate.setText(display_number(values[-1]) if values else '—')
        self.mini_chart.set_series(values,labels,animate,'每日 Token' if daily else 'Token / min')
        if daily:
            for name,row in self.rows.items():row.chart.set_series(chart.get('by_model',{}).get(name,[]),animate=animate)
    def render_quota(self):
        quotas=quota_windows(self.data);self.remaining=None
        if not quotas:
            self.quota_label.setText('额度 · 暂无有效快照');self.quota_text.setText('—');self.quota_reset.setText('');self.mini_quota.setText('—');return
        title,row=quotas[0];self.remaining=max(0,min(100,100-float(row['used_percent'])));plan=str((self.data.get('rate_limits') or {}).get('plan_type') or '').upper()
        self.quota_label.setText((plan+' · ' if plan else '')+title);self.quota_text.setText(f'剩余 <b style="color:#087f79">{self.remaining:g}%</b>');self.mini_quota.setText(f'{self.remaining:g}%');reset=''
        if row.get('resets_at'):
            try:
                date=datetime.fromisoformat(row['resets_at']);seconds=max(0,(date-datetime.now(date.tzinfo)).total_seconds())
                reset=f'{int(seconds//86400)}天后重置' if seconds>=86400 else f'{int(seconds//3600)}时{int(seconds%3600//60)}分后重置'
            except (ValueError,TypeError):pass
        self.quota_reset.setText(reset)
        tip='\n'.join(f'{name}：剩余 {max(0,100-float(item["used_percent"])):g}%\n快照：{item.get("observed_at","未知")}' for name,item in quotas)
        self.quota_label.setToolTip(tip);self.quota_text.setToolTip(tip)
        if len(quotas)>1:
            hour=next((r for name,r in quotas if r.get('window_minutes')==300),None)
            if hour:self.quota_reset.setText(f'5h 剩余 {100-float(hour["used_percent"]):g}%')
    def update_status(self):
        stamp=self.data.get('generated_at','')
        try:
            moment=datetime.fromisoformat(stamp);age=(datetime.now(moment.tzinfo)-moment).total_seconds()
        except (ValueError,TypeError):age=999
        fresh=self.connected and age<180;self.dot.setStyleSheet('color:'+(MINT if fresh else '#d79c62')+';')
        self.dot.setToolTip(('已同步' if fresh else '离线或数据较旧')+f'\n{stamp}\n每5秒检查；索引汇总可能延迟');self.chart.set_live(fresh)
        self.tray.setToolTip(f'Codex Glass · {self.cost.text()} · {self.tokens.text()} Token')
    def change_scope(self,scope):
        self.scope=scope
        for b,key in zip(self.group.buttons(),('today','last_5_hours','all')):b.setChecked(scope==key);b.setFont(font(16,scope==key))
        self.render()
    def set_motion(self,value):
        self.motion=value
        if hasattr(self,'flow_timer'):
            if value and self.isVisible():self.flow_timer.start()
            else:self.flow_timer.stop()
        for chart in [self.chart,self.mini_chart]+[r.chart for r in self.rows.values()]:
            chart.motion=value
            if not value:chart.animation.stop();chart.display_values=chart.target_values[:];chart.pulse.stop();chart.update()
        if value:self.update_status()
    def set_transparency(self,value):
        self.transparency=max(15,min(90,int(value)));self.save_settings();self.update()
    def set_glass(self,value):self.set_transparency(value)
    def save_settings(self):
        if self.persist:
            host=self.host_window();self.settings.setValue('position',host.pos());self.settings.setValue('transparency',self.transparency);self.settings.setValue('preset',self.glass_preset)
            if host is not self:self.settings.setValue('scale',host.scale)
    def host_window(self):return getattr(self,'scaled_host',None) or self
    def global_point(self,widget,point):
        host=getattr(self,'scaled_host',None)
        return host.to_global(widget,point) if host else widget.mapToGlobal(point)
    def toggle_top(self,checked):
        self.pin.blockSignals(True);self.pin.setChecked(checked);self.pin.blockSignals(False);host=self.host_window();host.setWindowFlag(Qt.WindowStaysOnTopHint,checked);host.show()
        if hasattr(self,'appearance'):
            self.appearance.top.blockSignals(True);self.appearance.top.setChecked(checked);self.appearance.top.blockSignals(False)
    def toggle_compact(self):
        self.compact_mode=not self.compact_mode;self.body.setVisible(not self.compact_mode);self.cost_title.setVisible(not self.compact_mode);self.tokens_title.setVisible(not self.compact_mode)
        self.mini_chart.setVisible(self.compact_mode);self.mini_quota.setVisible(self.compact_mode);self.expand.setVisible(self.compact_mode);self.setFixedSize(440,188 if self.compact_mode else self.FULL_SIZE[1])
        self.cost.setGeometry(30,65,158,46) if self.compact_mode else self.cost.setGeometry(30,136,255,58)
        self.tokens.setGeometry(212,71,94,35) if self.compact_mode else self.tokens.setGeometry(283,145,124,43)
        host=getattr(self,'scaled_host',None)
        if host:host.sync_size();host.clamp_to_screen()
        self.fit_metrics();self.update()
    def show_menu(self):self.menu.popup(self.global_point(self.more,QPoint(-120,30)))
    def show_appearance(self):
        point=self.global_point(self,QPoint(-340,70));screen=QApplication.screenAt(self.host_window().frameGeometry().center()) or QApplication.primaryScreen();area=screen.availableGeometry()
        if point.x()<area.left():point=self.global_point(self,QPoint(100,90))
        self.appearance.adjustSize();point.setX(min(max(point.x(),area.left()),max(area.left(),area.right()-self.appearance.width()+1)));point.setY(min(max(point.y(),area.top()),max(area.top(),area.bottom()-self.appearance.height()+1)))
        self.appearance.move(point);self.appearance.show();self.appearance.raise_()
    def select_model(self,name):
        self.selected=name;_,models=window_data(self.data,self.scope);r=models.get(name,{})
        self.detail_popup.setText(f'{name}\n\n输入   {compact(r.get("input_tokens"))}\n缓存   {compact(r.get("cached_input_tokens"))}\n输出   {compact(r.get("output_tokens"))}\n推理   {compact(r.get("reasoning_output_tokens"))}\n\n缓存包含在输入内，推理包含在输出内。')
        self.detail_popup.adjustSize();point=self.global_point(self,QPoint(30,410));area=(QApplication.screenAt(point) or QApplication.primaryScreen()).availableGeometry()
        point.setX(max(area.left(),min(point.x(),area.right()-self.detail_popup.width()+1)));point.setY(max(area.top(),min(point.y(),area.bottom()-self.detail_popup.height()+1)))
        self.detail_popup.move(point);self.detail_popup.show();QTimer.singleShot(6500,self.detail_popup.hide)
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton and event.y()<65:self.drag=event.globalPos()-self.host_window().pos()
    def mouseMoveEvent(self,event):
        if self.drag is not None and event.buttons() & Qt.LeftButton:self.host_window().move(event.globalPos()-self.drag)
    def mouseReleaseEvent(self,event):
        if self.drag is not None:
            host=self.host_window();area=(QApplication.screenAt(host.frameGeometry().center()) or QApplication.primaryScreen()).availableGeometry()
            host.move(max(area.left(),min(host.x(),area.right()-host.width()+1)),max(area.top(),min(host.y(),area.bottom()-host.height()+1)));self.save_settings()
        self.drag=None
    def mouseDoubleClickEvent(self,event):
        if event.y()<65:self.toggle_compact()
    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Escape:self.hide_to_tray()
        else:super().keyPressEvent(event)
    def hide_to_tray(self):
        self.appearance.hide();self.detail_popup.hide()
        host=self.host_window()
        if QSystemTrayIcon.isSystemTrayAvailable():host.hide()
        else:host.showMinimized()
    def restore(self):
        host=self.host_window();host.showNormal();host.raise_();host.activateWindow();self.fetch()
    def tray_activate(self,reason):
        if reason==QSystemTrayIcon.Trigger:self.hide_to_tray() if self.host_window().isVisible() else self.restore()
    def closeEvent(self,event):self.hide_to_tray();event.ignore()
    def quit_widget(self):
        self.save_settings();self.tray.hide();self.detail_popup.hide();QApplication.quit()
    def open_dashboard(self):
        from native_dashboard import open_dashboard
        return open_dashboard(self)
    def launch_backend(self):
        if not self.start_backend or self.url!='http://127.0.0.1:8081':return
        if self.backend_process is not None and self.backend_process.poll() is None:return
        if self.backend_attempts>=3 or time.monotonic()<self.backend_next_attempt:return
        self.backend_attempts+=1;self.backend_next_attempt=time.monotonic()+30
        try:
            runtime=Path(os.environ.get('LOCALAPPDATA',str(Path.cwd())))/'CodexGlass';runtime.mkdir(parents=True,exist_ok=True)
            command=[sys.executable,'--backend','--no-browser'] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).with_name('web_dashboard.py')),'--no-browser']
            with (runtime/'backend.log').open('a',encoding='utf-8') as log:
                self.backend_process=subprocess.Popen(command,cwd=str(Path(sys.executable).parent if getattr(sys,'frozen',False) else Path(__file__).parent),stdout=log,stderr=log,env={**os.environ,'PYINSTALLER_RESET_ENVIRONMENT':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except OSError as exc:self.dot.setToolTip('后台启动失败：'+str(exc))
    def retry_backend(self):
        self.backend_attempts=0;self.backend_next_attempt=0;self.fetch()
    def fetch(self):
        if self.network_busy:return
        self.network_busy=True;request=QNetworkRequest(QUrl(self.url+'/api/widget'));request.setTransferTimeout(10000);self.manager.get(request)
    def received(self,reply):
        self.network_busy=False
        try:
            if reply.error():self.connected=False;self.update_status();self.launch_backend();return
            data=json.loads(bytes(reply.readAll()).decode('utf-8'))
            if not isinstance(data,dict) or not isinstance(data.get('total'),dict):raise ValueError('invalid payload')
            self.apply_data(data)
        except (ValueError,TypeError,KeyError,OverflowError):self.connected=False;self.update_status()
        finally:reply.deleteLater()

def main():
    if '--backend' in sys.argv:
        sys.argv.remove('--backend');runtime=Path(os.environ.get('LOCALAPPDATA',str(Path.cwd())))/'CodexGlass';runtime.mkdir(parents=True,exist_ok=True)
        if sys.stdout is None:sys.stdout=(runtime/'backend.log').open('a',encoding='utf-8')
        if sys.stderr is None:sys.stderr=sys.stdout
        import web_dashboard
        return web_dashboard.main()
    parser=argparse.ArgumentParser(description='Codex Glass desktop widget')
    parser.add_argument('--url',default='http://127.0.0.1:8081');parser.add_argument('--no-start-backend',action='store_true')
    parser.add_argument('--capture');parser.add_argument('--scope',choices=['today','last_5_hours','all'],default='today');args=parser.parse_args()
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling);QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
    app=QApplication(sys.argv);app.setQuitOnLastWindowClosed(False);server=None
    if not args.capture:
        socket=QLocalSocket();socket.connectToServer('codex-glass-frosted-v3')
        if socket.waitForConnected(500):socket.write(b'show');socket.flush();socket.waitForBytesWritten(300);return 0
        server=QLocalServer();QLocalServer.removeServer('codex-glass-frosted-v3')
        if not server.listen('codex-glass-frosted-v3'):return 1
    widget=GlassWidget(args.url,not args.no_start_backend,persist=not bool(args.capture));widget.change_scope(args.scope);window=widget
    if not args.capture:
        from glass_host import ScaledSurfaceHost
        saved_scale=widget.settings.value('scale',1.0,type=float) if widget.persist else 1.0
        window=ScaledSurfaceHost(widget,scale=saved_scale,min_scale=.7,max_scale=1.5);window.scaleChanged.connect(lambda value:widget.save_settings())
    if server:
        def restore():
            client=server.nextPendingConnection()
            if client:client.disconnectFromServer();client.deleteLater()
            widget.restore()
        server.newConnection.connect(restore)
    window.show()
    if args.capture:
        deadline=time.time()+180
        def capture():
            if widget.data.get('by_model') and widget.data.get('charts'):
                widget.set_motion(False);widget.grab().save(args.capture);app.quit()
            elif time.time()>deadline:app.exit(2)
        timer=QTimer();timer.timeout.connect(capture);timer.start(2000)
    return app.exec_()

if __name__=='__main__':sys.exit(main())
