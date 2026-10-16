"""Desktop relay controls. Closing this window never stops the background Worker."""
import argparse
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from .console_model import ConsoleBackend, display_time
from .processes import InstanceLock

EVENT_LABELS={
    'worker_component_failed':'后台组件退出',
    'inference_attempt_started':'请求尝试开始','inference_attempt_failed':'请求尝试失败',
    'inference_retry_scheduled':'等待安全重试','inference_retry_exhausted':'重试时限已用完',
    'worker_started':'服务启动','worker_stopping':'服务停止中','worker_stopped':'服务停止','worker_failed':'服务异常',
    'openai_check_failed':'OpenAI 检查失败','openai_check_recovered':'OpenAI 连接恢复','openai_check_healthy':'OpenAI 检查正常',
    'readiness_changed':'就绪状态变化','credential_observed':'凭证已读取','credential_changed':'凭证更新',
    'credential_expired':'凭证到期','credential_valid':'凭证到期记录有效',
    'tunnel_started':'隧道启动','tunnel_exited':'隧道退出','tunnel_stopped':'隧道停止',
    'telemetry_failed':'心跳发送失败','telemetry_recovered':'心跳恢复','quota_failed':'额度采集失败','quota_recovered':'额度采集恢复',
    'inference_opened':'推理已连接','inference_open_failed':'推理连接失败','inference_stream_failed':'推理流中断',
    'inference_stream_finished':'推理流结束','bridge_request_started':'Bridge 收到请求','bridge_request_finished':'Bridge 请求结束'}


class RelayConsole:
    def __init__(self, root, backend, *, polling=True):
        self.root,self.backend=root,backend
        self.queue=queue.Queue();self.closed=threading.Event();self.wake=threading.Event()
        self.busy=False;self.snapshot=None;self.key_shown=False
        self.vars={};self.cards={};self.controls=[]
        root.title('Codex 中继控制台');root.geometry('960x760');root.minsize(900,720)
        root.configure(background='#eef2f6')
        style=ttk.Style(root);style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',10))
        style.configure('TFrame',background='#eef2f6')
        style.configure('Card.TFrame',background='#ffffff')
        style.configure('TLabel',background='#eef2f6',foreground='#273449')
        style.configure('Card.TLabel',background='#ffffff',foreground='#273449')
        style.configure('Muted.TLabel',background='#eef2f6',foreground='#69778c')
        style.configure('TButton',padding=(12,8),background='#ffffff')
        style.configure('Primary.TButton',background='#2463eb',foreground='#ffffff')
        style.map('Primary.TButton',background=[('disabled','#cbd5e1'),('active','#1d4ed8')])
        style.configure('Danger.TButton',foreground='#b42318')
        style.configure('Treeview',rowheight=29,background='#ffffff',fieldbackground='#ffffff',borderwidth=0)
        style.configure('Treeview.Heading',padding=(8,8),font=('Microsoft YaHei UI',10,'bold'))

        outer=ttk.Frame(root,padding=22);outer.pack(fill='both',expand=True)
        header=ttk.Frame(outer);header.pack(fill='x')
        ttk.Label(header,text='Codex 中继控制台',font=('Microsoft YaHei UI',22,'bold')).pack(side='left')
        self.pill=tk.Label(header,text='正在读取状态',font=('Microsoft YaHei UI',10,'bold'),background='#e2e8f0',foreground='#475569',padx=14,pady=7)
        self.pill.pack(side='right')
        ttk.Label(outer,text='控制本机后台服务，查看连接与登录凭证状态。',style='Muted.TLabel').pack(anchor='w',pady=(6,16))
        bar=ttk.Frame(outer);bar.pack(fill='x',pady=(0,8))
        self.start_button=ttk.Button(bar,text='启动服务',style='Primary.TButton',command=lambda:self.control('start'))
        self.stop_button=ttk.Button(bar,text='停止服务',style='Danger.TButton',command=lambda:self.control('stop'))
        self.restart_button=ttk.Button(bar,text='重启服务',command=lambda:self.control('restart'))
        for button in (self.start_button,self.stop_button,self.restart_button):button.pack(side='left',padx=(0,9));self.controls.append(button)
        ttk.Button(bar,text='刷新状态',command=self.refresh).pack(side='right')
        ttk.Label(outer,text='停止或重启会断开当前请求。关闭此面板不会停止服务。',style='Muted.TLabel').pack(anchor='w',pady=(0,10))
        self.notice=self.variable('notice','正在读取本机与服务器状态…')
        ttk.Label(outer,textvariable=self.notice,wraplength=880,foreground='#285ca6').pack(anchor='w',pady=(0,12))

        tabs=ttk.Notebook(outer);tabs.pack(fill='both',expand=True)
        overview_host=ttk.Frame(tabs);logs=ttk.Frame(tabs,padding=(12,14))
        tabs.add(overview_host,text='服务与连接');tabs.add(logs,text='最近运行日志')
        canvas=tk.Canvas(overview_host,background='#eef2f6',highlightthickness=0)
        overview_scroll=ttk.Scrollbar(overview_host,orient='vertical',command=canvas.yview)
        canvas.configure(yscrollcommand=overview_scroll.set);overview_scroll.pack(side='right',fill='y');canvas.pack(fill='both',expand=True)
        overview=ttk.Frame(canvas,padding=(12,14));item=canvas.create_window((0,0),window=overview,anchor='nw')
        overview.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(item,width=event.width))
        def wheel(event):
            if tabs.select()==str(overview_host):canvas.yview_scroll(-int(event.delta/120),'units');return 'break'
        root.bind('<MouseWheel>',wheel,add='+')
        cards=ttk.Frame(overview);cards.pack(fill='x',pady=(0,15))
        for i,(key,title) in enumerate((('worker','本机服务'),('openai','本地适配器'),('tunnel','SSH 隧道'),('server','服务器中继'))):
            cards.columnconfigure(i,weight=1,uniform='cards')
            card=ttk.Frame(cards,style='Card.TFrame',padding=14);card.grid(row=0,column=i,sticky='nsew',padx=(0,8) if i<3 else 0)
            ttk.Label(card,text=title,style='Card.TLabel').pack(anchor='w')
            label=tk.Label(card,text='读取中',font=('Microsoft YaHei UI',15,'bold'),background='#ffffff',foreground='#64748b')
            label.pack(anchor='w',pady=(9,0));self.cards[key]=label
        details=ttk.Frame(overview,style='Card.TFrame',padding=14);details.pack(fill='x')
        self.detail_row(details,0,'当前请求','requests')
        self.detail_row(details,1,'服务运行时长','uptime')
        self.detail_row(details,2,'最近推理成功','last_success')
        self.detail_row(details,3,'心跳 / 额度采集','telemetry')
        self.detail_row(details,4,'状态更新时间','observed')

        connection=ttk.Frame(overview,style='Card.TFrame',padding=14);connection.pack(fill='x',pady=(12,0))
        ttk.Label(connection,text='CCSwitch 连接信息',style='Card.TLabel',font=('Microsoft YaHei UI',11,'bold')).grid(row=0,column=0,columnspan=4,sticky='w',pady=(0,9))
        connection.columnconfigure(1,weight=1)
        ttk.Label(connection,text='Base URL',style='Card.TLabel').grid(row=1,column=0,sticky='w',padx=(0,14))
        ttk.Entry(connection,textvariable=self.variable('base_url','—'),state='readonly').grid(row=1,column=1,sticky='ew',padx=(0,9))
        ttk.Button(connection,text='复制地址',command=lambda:self.copy('base_url')).grid(row=1,column=2,padx=(0,7))
        ttk.Button(connection,text='管理网页',command=self.open_web).grid(row=1,column=3)
        ttk.Label(connection,text='API Key',style='Card.TLabel').grid(row=2,column=0,sticky='w',pady=(8,0))
        ttk.Entry(connection,textvariable=self.variable('key','尚未读取'),state='readonly').grid(row=2,column=1,sticky='ew',padx=(0,9),pady=(8,0))
        ttk.Button(connection,text='复制 Key',command=lambda:self.copy('key')).grid(row=2,column=2,padx=(0,7),pady=(8,0))
        self.key_button=ttk.Button(connection,text='显示 Key',command=self.toggle_key);self.key_button.grid(row=2,column=3,pady=(8,0))

        auth=ttk.Frame(overview,style='Card.TFrame',padding=14);auth.pack(fill='x',pady=(12,0))
        ttk.Label(auth,text='现有 Codex 登录',style='Card.TLabel',font=('Microsoft YaHei UI',11,'bold')).grid(row=0,column=0,columnspan=2,sticky='w',pady=(0,9))
        self.detail_row(auth,1,'凭证到期（北京时间）','expiry')
        self.detail_row(auth,2,'凭证剩余时间','remaining')
        self.detail_row(auth,3,'本机出口代理','proxy')
        ttk.Label(auth,text='到期时间为本地凭证记录；实际请求结果请查看运行日志。',style='Card.TLabel',foreground='#69778c').grid(row=4,column=0,columnspan=2,sticky='w',pady=(9,0))

        top=ttk.Frame(logs);top.pack(fill='x',pady=(0,10))
        ttk.Label(top,text='最近 60 条运行事件 · 不展示凭证与请求正文').pack(side='left')
        ttk.Button(top,text='打开日志目录',command=self.open_logs).pack(side='right')
        self.tree=ttk.Treeview(logs,columns=('time','event','details'),show='headings')
        for key,label,width in (('time','北京时间',135),('event','事件',180),('details','状态 / 错误 / 耗时',490)):
            self.tree.heading(key,text=label);self.tree.column(key,width=width,stretch=key=='details')
        scroll=ttk.Scrollbar(logs,orient='vertical',command=self.tree.yview);self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');self.tree.pack(fill='both',expand=True)
        ttk.Label(outer,textvariable=self.variable('footer','每 3 秒刷新本机状态，每 15 秒检查服务器'),style='Muted.TLabel').pack(anchor='w',pady=(12,0))
        root.protocol('WM_DELETE_WINDOW',self.close)
        if polling:
            threading.Thread(target=self.poll,daemon=True).start();root.after(100,self.drain)

    def variable(self,key,value=''):
        var=tk.StringVar(self.root,value=value);self.vars[key]=var;return var

    def detail_row(self,parent,row,label,key):
        parent.columnconfigure(1,weight=1)
        ttk.Label(parent,text=label,style='Card.TLabel',foreground='#69778c').grid(row=row,column=0,sticky='w',padx=(0,22),pady=3)
        ttk.Label(parent,textvariable=self.variable(key,'—'),style='Card.TLabel').grid(row=row,column=1,sticky='w',pady=3)

    def poll(self):
        while not self.closed.is_set():
            force=self.wake.is_set();self.wake.clear()
            try:self.queue.put(('snapshot',self.backend.snapshot(force)))
            except Exception:self.queue.put(('read_error',None))
            self.wake.wait(3)

    def drain(self):
        if self.closed.is_set():return
        while True:
            try:kind,value=self.queue.get_nowait()
            except queue.Empty:break
            if kind=='snapshot':self.render(value)
            elif kind=='read_error':
                self.notice.set('无法读取配置或状态，请检查本机中继配置。');self.pill.config(text='状态不可用',background='#fee2e2',foreground='#b91c1c')
                self.snapshot=None;self.buttons()
            elif kind=='operation_done':
                self.busy=False;self.notice.set(value);self.buttons();self.wake.set()
        self.root.after(100,self.drain)

    def buttons(self):
        running=bool(self.snapshot and self.snapshot['running'])
        retired=bool(self.snapshot and self.snapshot.get('migrated_to_server'))
        for button,enabled in ((self.start_button,not running),(self.stop_button,running),(self.restart_button,running)):
            button.configure(state='normal' if enabled and not self.busy and not retired else 'disabled')

    def card(self,name,text,ok=False):self.cards[name].config(text=text,foreground='#15803d' if ok else '#b45309')

    def render(self,s):
        self.snapshot=s;self.buttons()
        if s.get('migrated_to_server'):
            remote=s['remote'];online=remote.get('ok') and remote.get('connected')
            self.pill.config(text='服务器中继在线' if online else '服务器状态待确认',
                background='#dcfce7' if online else '#fef3c7',foreground='#15803d' if online else '#92400e')
            self.card('worker','已迁移到服务器',online)
            self.card('openai','按实际请求转发',online)
            self.card('tunnel','本机无需隧道',True)
            self.card('server','在线' if online else '待确认',online)
            for key in ('requests','uptime','last_success','observed'):
                self.vars[key].set('请在管理网页查看')
            self.vars['telemetry'].set('服务器自动上报')
            self.vars['base_url'].set(s['base_url']);self.vars['proxy'].set('服务器代理')
            self.vars['expiry'].set('服务器独立登录');self.vars['remaining'].set('官方 Codex 自动维护凭证')
            if not self.key_shown:self.vars['key'].set('gw_••••••••••••••••••••' if s['key_available'] else '未配置')
            self.notice.set('服务已迁移到服务器。本机可关机；点击“管理网页”查看状态和用量。')
            self.vars['footer'].set('服务器检查 '+display_time(remote.get('checked_at'))+' · 本机开关按钮已停用，避免误启旧中继')
            return
        running,fresh=s['running'],s['fresh'];online=running and fresh and s['openai_ready'] and s['tunnel_ready'] and s['remote'].get('ok') and s['remote'].get('connected')
        self.pill.config(text='中继在线' if online else '服务已停止' if not running else '连接待恢复',
            background='#dcfce7' if online else '#e2e8f0' if not running else '#fef3c7',foreground='#15803d' if online else '#475569' if not running else '#92400e')
        self.card('worker','运行中' if running and fresh else '状态过期' if running else '已停止',running and fresh)
        self.card('openai','已初始化' if s['openai_ready'] else '需恢复登录' if s['auth_state']=='reauth_required' else '未就绪' if running else '—',s['openai_ready'])
        self.card('tunnel','已连接' if s['tunnel_ready'] else '未连接' if running else '—',s['tunnel_ready'])
        remote=s['remote'];self.card('server','在线' if online else '已停止' if not running else '无法访问' if not remote.get('ok') else '未就绪',online)
        self.vars['requests'].set(f"{s['active_requests']} / {s['max_concurrency']}" if s['active_requests'] is not None else '—')
        uptime=s.get('uptime_seconds');self.vars['uptime'].set(f'{uptime//3600} 小时 {(uptime%3600)//60} 分钟' if uptime is not None else '—')
        self.vars['last_success'].set(display_time(s.get('last_success')))
        quota={'ok':'正常','unavailable':'暂不可用','reauth_required':'需恢复登录','unsupported':'不支持','unknown':'未知'}.get(s.get('quota_status'),'未知')
        self.vars['telemetry'].set(('发送正常' if s['telemetry_ok'] else '未发送 / 失败')+' · 额度 '+quota)
        self.vars['observed'].set(display_time(s.get('observed_at'))+('（记录过期）' if running and not fresh else ''))
        self.vars['base_url'].set(s['base_url']);self.vars['proxy'].set(s['proxy'])
        if not self.key_shown:self.vars['key'].set('gw_••••••••••••••••••••' if s['key_available'] else '未配置')
        cred=s['credential'];minutes=cred.get('remaining_minutes')
        if cred['status'] in {'available','expired'}:
            from datetime import datetime
            from .console_model import LOCAL_TZ
            d=datetime.strptime(cred['expires_at_local'],'%Y-%m-%d %H:%M:%S %z').astimezone(LOCAL_TZ)
            self.vars['expiry'].set(d.strftime('%Y-%m-%d %H:%M:%S'))
            self.vars['remaining'].set('已到期，请在 Codex 中恢复登录' if cred['status']=='expired' else f'{minutes//1440} 天 {(minutes%1440)//60} 小时 {minutes%60} 分钟')
        else:self.vars['expiry'].set('无法读取到期记录');self.vars['remaining'].set('—')
        if not self.busy:
            self.notice.set('已准备好。' if online else '点击“启动服务”开启中继。' if not running else '服务运行中，正在核对连接状态。')
            if s.get('update_pending'):self.notice.set('额度采集弹窗修复等待生效。方便时点击“重启服务”；当前服务继续运行。')
        log='日志写入正常' if s['diagnostic_ok'] is True else '日志状态未确认' if s['diagnostic_ok'] is None else '日志写入失败'
        self.vars['footer'].set(log+' · 服务器检查 '+display_time(remote.get('checked_at'))+' · 关闭面板不会停止中继')
        events=s.get('events',[]);fingerprint=tuple((e.get('at'),e.get('event')) for e in events)
        if getattr(self,'event_fingerprint',None)!=fingerprint:
            self.event_fingerprint=fingerprint
            self.tree.delete(*self.tree.get_children())
            for e in reversed(events):
                details=[]
                if e.get('component'):details.append(str(e['component']))
                if e.get('error_code'):details.append(str(e['error_code']))
                if 'http_status' in e:details.append('HTTP '+str(e['http_status']))
                if 'duration_ms' in e:details.append(str(e['duration_ms'])+' ms')
                if 'attempt_no' in e:details.append('尝试 '+str(e['attempt_no'])+'/'+str(e.get('max_attempts',3)))
                if 'retry_wait_ms' in e:details.append('等待 '+str(e['retry_wait_ms'])+' ms')
                if 'ready' in e:details.append('就绪' if e['ready'] else '未就绪')
                if e.get('consecutive_failures'):details.append('连续失败 '+str(e['consecutive_failures'])+' 次')
                self.tree.insert('', 'end',values=(display_time(e.get('at')),EVENT_LABELS.get(e.get('event'),e.get('event','—')),' · '.join(details)))

    def control(self,operation):
        if self.busy:return
        self.busy=True;self.buttons()
        label={'start':'启动','stop':'停止','restart':'重启'}[operation];self.notice.set('正在'+label+'服务，请稍候…')
        def execute():
            try:self.backend.control(operation);result=label+'命令已完成，正在更新状态。'
            except Exception:result=label+'未完成，请刷新状态并查看运行日志。'
            self.queue.put(('operation_done',result))
        # Closing the UI does not interrupt an explicitly requested control operation.
        threading.Thread(target=execute,daemon=False).start()

    def refresh(self):self.wake.set();self.notice.set('正在刷新状态…')

    def copy(self,kind):
        try:
            value=self.backend.key() if kind=='key' else self.vars[kind].get()
            self.root.clipboard_clear();self.root.clipboard_append(value);self.root.update_idletasks()
            self.notice.set('Key 已复制。' if kind=='key' else 'Base URL 已复制。')
        except Exception:self.notice.set('无法复制，请检查本机客户端 Key 配置。')

    def toggle_key(self):
        try:
            if self.key_shown:self.vars['key'].set('gw_••••••••••••••••••••');self.key_shown=False;self.key_button.config(text='显示 Key')
            else:self.vars['key'].set(self.backend.key());self.key_shown=True;self.key_button.config(text='隐藏 Key')
        except Exception:self.notice.set('无法读取客户端 Key。')

    def open_web(self):
        if self.snapshot and self.snapshot['origin']:webbrowser.open(self.snapshot['origin'])

    def open_logs(self):
        try:os.startfile(str(self.backend.log_directory()))
        except Exception:self.notice.set('无法打开日志目录。')

    def close(self):self.closed.set();self.wake.set();self.root.destroy()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config');args=parser.parse_args()
    root=tk.Tk();root.withdraw();lock=None
    try:
        backend=ConsoleBackend(args.config);config=backend.config()
        lock=InstanceLock(config.runtime_dir/'console.lock')
        try:lock.acquire()
        except RuntimeError:
            messagebox.showinfo('Codex 中继控制台','控制面板已打开，请查看任务栏。',parent=root);root.destroy();return
        RelayConsole(root,backend);root.deiconify();root.mainloop()
    except Exception:
        try:messagebox.showerror('Codex 中继控制台','控制面板未能启动，请检查本机中继配置与 Python 环境。',parent=root);root.destroy()
        except tk.TclError:pass
    finally:
        if lock:lock.release()


if __name__=='__main__':main()
