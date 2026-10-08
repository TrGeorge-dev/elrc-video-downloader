"""ELRC 录像下载器。登录在独立浏览器中完成，脚本不读取或保存密码。"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit, urlunsplit

import requests
from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout

from transfer import download_media, DownloadError, Cancelled

SITE = 'elrc.shanghaitech.edu.cn'
DEFAULT_STATE = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ELRCVideoDownloader'


def normalize_url(value: str) -> str:
    value = value.strip().replace(r'\&', '&')
    if value.startswith('[') and '](' in value and value.endswith(')'):
        value = value.split('](', 1)[1][:-1]
    value = value.strip('<> \"\'')
    p = urlsplit(value)
    if (p.scheme != 'https' or p.hostname != SITE or p.username or p.password
            or p.port not in (None, 443)
            or not re.fullmatch(r'/learn/videoreview/[A-Za-z0-9_-]+/?', p.path)):
        raise ValueError('请粘贴 https://elrc.shanghaitech.edu.cn/learn/videoreview/ 开头的课程录像链接。')
    return urlunsplit(('https', SITE, p.path.rstrip('/'), p.query, ''))


def same_lesson(current: str, requested: str) -> bool:
    a, b = urlsplit(current), urlsplit(requested)
    if a.hostname != b.hostname or a.path.rstrip('/') != b.path.rstrip('/'):
        return False
    qa, qb = dict(parse_qsl(a.query, keep_blank_values=True)), dict(parse_qsl(b.query, keep_blank_values=True))
    return all(qa.get(k) == v for k, v in qb.items() if k in ('id', 'week', 'weekDay', 'realSection'))


def safe_name(value: str, limit: int = 72) -> str:
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')[:limit].strip(' .') or '录像'
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', result):
        result = '_' + result
    return result


def duration_text(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return '时长待确认'
    n = round(seconds)
    return f'{n // 3600:02}:{n // 60 % 60:02}:{n % 60:02}'


@dataclass
class Video:
    url: str
    label: str
    duration: float
    width: int
    height: int
    filename: str = ''


EXTRACT = r"""() => Array.from(document.querySelectorAll('video')).map(v => {
  const src = v.currentSrc || v.src;
  const box = v.closest('.player_item') || v.parentElement;
  const text = (box ? box.textContent : '').trim();
  const match = text.match(/^(教师近景画面|教师全景画面|屏幕画面|学生全景画面|学生近景画面)/);
  return {url: src, label: match ? match[1] : '',
    duration: Number.isFinite(v.duration) ? v.duration : 0,
    width: v.videoWidth || 0, height: v.videoHeight || 0};
}).filter(x => x.url)"""


class BrowserWorker(threading.Thread):
    def __init__(self, events: queue.Queue, state_dir: Path):
        super().__init__(daemon=True)
        self.events, self.state_dir = events, state_dir
        self.commands = queue.Queue()
        self.cancel = threading.Event()
        self.stop = threading.Event()
        self.context = None
        self.videos: list[Video] = []
        self.page = None
        self.media_requests = {}
        self.source = ''

    def emit(self, kind, **data):
        self.events.put({'kind': kind, **data})

    def log(self, message):
        self.emit('log', message=message)

    def check_cancel(self):
        if self.cancel.is_set() or self.stop.is_set():
            raise Cancelled('已停止，未完成的文件会保留以便下次续传。')

    def close_browser(self):
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
        self.context, self.page = None, None

    def run(self):
        try:
            with sync_playwright() as pw:
                while not self.stop.is_set():
                    try:
                        command = self.commands.get(timeout=.2)
                    except queue.Empty:
                        if self.page and not self.page.is_closed():
                            try:
                                self.page.wait_for_timeout(50)
                            except Exception:
                                pass
                        continue
                    try:
                        if command['kind'] == 'analyze':
                            self.analyze(pw, command['url'])
                        elif command['kind'] == 'download':
                            self.download(command['indices'], command['folder'])
                    except Cancelled as e:
                        self.emit('cancelled', message=str(e))
                    except (DownloadError, ValueError) as e:
                        self.emit('error', message=str(e))
                    except OSError:
                        self.emit('error', message='无法写入保存位置，请检查磁盘空间、文件夹权限和文件是否被占用。')
                    except Exception as e:
                        # Browser exceptions may contain request URLs/headers. Do not log their raw text.
                        self.emit('error', message=f'操作未完成（{type(e).__name__}）。请确认网络、浏览器窗口和登录状态，然后重新解析链接。')
                self.close_browser()
        finally:
            self.close_browser()

    def analyze(self, pw, url):
        self.close_browser()
        self.videos, self.media_requests, self.source = [], {}, url
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log('正在打开独立浏览器；如显示统一身份认证，请在浏览器中登录。')
        channel = 'chrome'
        try:
            self.context = pw.chromium.launch_persistent_context(
                str(self.state_dir / 'chrome-profile'), channel=channel, headless=False,
                viewport={'width': 1280, 'height': 820}, accept_downloads=True,
                chromium_sandbox=True,
            )
        except Exception:
            raise DownloadError('无法启动 Chrome。请确认已安装 Google Chrome，并关闭本工具上次打开的 Chrome 窗口。')
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

        def observed(request):
            p = urlsplit(request.url)
            if p.hostname == SITE and p.path.lower().endswith('.mp4'):
                self.media_requests[request.url] = request

        self.page.on('request', observed)
        try:
            self.page.goto(url, wait_until='domcontentloaded', timeout=60000)
        except BrowserTimeout:
            self.log('页面加载较慢，继续等待登录或播放器。')
        started = time.monotonic()
        authenticated_at = None
        restored = False
        stable, last_key, stable_since = [], None, 0
        while time.monotonic() - started < 600:
            self.check_cancel()
            if self.page.is_closed():
                raise DownloadError('浏览器已关闭。请重新解析链接。')
            if urlsplit(self.page.url).hostname == SITE:
                authenticated_at = authenticated_at or time.monotonic()
                if not same_lesson(self.page.url, url) and not restored:
                    # CAS may discard URL parameters after the first ampersand.
                    self.media_requests.clear()
                    self.page.goto(url, wait_until='domcontentloaded', timeout=60000)
                    restored = True
                    continue
                if same_lesson(self.page.url, url):
                    raw = []
                    for frame in self.page.frames:
                        try:
                            raw.extend(frame.evaluate(EXTRACT))
                        except Exception:
                            continue
                    found = {}
                    for item in raw:
                        parsed = urlsplit(item['url'])
                        if parsed.scheme == 'https' and parsed.hostname == SITE and parsed.path.lower().endswith('.mp4'):
                            found[item['url']] = Video(**item)
                    key = tuple(sorted(found))
                    if key != last_key:
                        last_key, stable_since = key, time.monotonic()
                    stable = list(found.values())
                    metadata_ready = all(v.duration > 0 and v.width > 0 for v in stable)
                    if stable and time.monotonic() - stable_since >= 2 and (metadata_ready or time.monotonic() - stable_since >= 15):
                        break
                if authenticated_at and time.monotonic() - authenticated_at > 90:
                    raise DownloadError('登录后未找到可下载的 MP4。请确认该节已有录像且账号有观看权限；当前版本不处理加密流或直播。')
            self.page.wait_for_timeout(300)
        else:
            raise DownloadError('等待登录超时。请重新解析链接并在浏览器中登录。')
        title = self.page.title().split('-上海科技大学')[0].strip() or '课程录像'
        query = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        lesson = '_'.join(x for x in [f"第{query['week']}周" if query.get('week') else '', query.get('weekDay', ''), f"第{query['realSection']}节" if query.get('realSection') else ''] if x)
        for n, video in enumerate(stable, 1):
            video.label = video.label or f'画面 {n}'
            date = re.search(r'/video/(\d{4})/(\d{1,2})/(\d{1,2})/', unquote(video.url))
            day = '-'.join([date[1], date[2].zfill(2), date[3].zfill(2)]) if date else lesson
            media_id = safe_name(Path(urlsplit(video.url).path).stem, 12)
            video.filename = f'{safe_name(title, 45)}_{safe_name(day)}_{safe_name(lesson, 40)}_{safe_name(video.label, 24)}_{media_id}.mp4'
        self.videos = stable
        self.emit('videos', title=f'{title}  ·  {lesson}', videos=stable)
        self.log(f'已找到 {len(stable)} 路录像。请勾选需要的画面，再点击“下载所选”。')

    def download(self, indices, folder):
        if not self.context or not self.videos:
            raise DownloadError('请先解析链接。')
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        completed = []
        for i, index in enumerate(indices, 1):
            self.check_cancel()
            video = self.videos[index]
            self.emit('item', message=f'正在下载 {i}/{len(indices)}：{video.label}')
            with requests.Session() as session:
                for cookie in self.context.cookies([video.url]):
                    session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'], secure=cookie['secure'])
                observed = self.media_requests.get(video.url)
                captured = observed.all_headers() if observed else {}
                headers = {k: v for k, v in captured.items() if k.lower() not in ('cookie', 'host', 'range', 'if-range', 'content-length', 'connection', 'accept-encoding') and not k.startswith(':')}
                headers['Referer'] = self.source
                headers['Accept-Encoding'] = 'identity'
                if 'user-agent' not in headers:
                    headers['User-Agent'] = self.page.evaluate('navigator.userAgent')
                saved = download_media(session, video.url, folder / video.filename, headers,
                    self.cancel, lambda done, total: self.emit('progress', done=done, total=total), self.log)
                completed.append(str(saved))
                self.log(f'已保存：{saved.name}')
        self.emit('done', files=completed)


def gui(state_dir: Path):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    root = tk.Tk()
    root.title('ELRC 录像下载器')
    root.geometry('840x690')
    root.minsize(720, 580)
    style = ttk.Style(root)
    if 'vista' in style.theme_names():
        style.theme_use('vista')
    style.configure('TLabel', font=('Microsoft YaHei UI', 10))
    style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=(12, 6))
    state_dir.mkdir(parents=True, exist_ok=True)
    # Hold an OS file lock for the entire GUI lifetime; a crash releases it automatically.
    lock_handle = (state_dir / 'app.lock').open('a+b')
    try:
        lock_handle.seek(0)
        if sys.platform == 'win32':
            import msvcrt
            if lock_handle.read(1) == b'':
                lock_handle.write(b'0')
                lock_handle.flush()
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        messagebox.showinfo('已在运行', '下载器已打开，请切换到现有窗口。', parent=root)
        lock_handle.close()
        root.destroy()
        return
    config_path = state_dir / 'settings.json'
    try:
        settings = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        settings = {}
    events = queue.Queue()
    worker = BrowserWorker(events, state_dir)
    worker.start()
    url_var = tk.StringVar()
    folder_var = tk.StringVar(value=settings.get('folder', ''))
    status_var = tk.StringVar(value='粘贴链接后解析，再自行选择需要下载的画面。')
    title_var = tk.StringVar(value='等待解析')
    progress_var = tk.StringVar(value='')
    busy, parsed_url, closing = False, '', False
    choices = []
    box = ttk.Frame(root, padding=22)
    box.pack(fill='both', expand=True)
    ttk.Label(box, text='ELRC 录像下载器', font=('Microsoft YaHei UI', 20, 'bold')).pack(anchor='w')
    ttk.Label(box, text='粘贴课程录像链接，选择画面与保存位置。').pack(anchor='w', pady=(4, 20))
    ttk.Label(box, text='课程录像链接').pack(anchor='w')
    url_entry = ttk.Entry(box, textvariable=url_var, font=('Segoe UI', 10))
    url_entry.pack(fill='x', pady=(6, 12))
    ttk.Label(box, text='保存到').pack(anchor='w')
    dest_row = ttk.Frame(box)
    dest_row.pack(fill='x', pady=(6, 12))
    dest_entry = ttk.Entry(dest_row, textvariable=folder_var)
    dest_entry.pack(side='left', fill='x', expand=True)

    def choose_folder():
        selected = filedialog.askdirectory(parent=root, title='选择录像保存文件夹', initialdir=folder_var.get() or str(Path.home()))
        if selected:
            folder_var.set(selected)

    browse = ttk.Button(dest_row, text='选择文件夹…', command=choose_folder)
    browse.pack(side='left', padx=(8, 0))
    action_row = ttk.Frame(box)
    action_row.pack(fill='x', pady=(0, 16))
    ttk.Label(box, textvariable=title_var, font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w')
    selection_frame = ttk.LabelFrame(box, text='可下载画面 · 可以多选', padding=10)
    selection_frame.pack(fill='x', pady=(8, 12))
    placeholder = ttk.Label(selection_frame, text='解析后会显示各路画面、清晰度和时长。')
    placeholder.pack(anchor='w', pady=8)
    bar = ttk.Progressbar(box, maximum=100)
    bar.pack(fill='x', pady=(4, 4))
    ttk.Label(box, textvariable=progress_var).pack(anchor='w')
    ttk.Label(box, textvariable=status_var, wraplength=760).pack(anchor='w', pady=(10, 6))
    log_view = tk.Text(box, height=7, state='disabled', wrap='word', font=('Microsoft YaHei UI', 9), relief='solid', borderwidth=1)
    log_view.pack(fill='both', expand=True, pady=(2, 6))
    ttk.Label(box, text='首次或登录过期时，请在独立浏览器窗口中登录；完成后会自动继续。', foreground='#666666').pack(anchor='w')

    def log(text):
        log_view.configure(state='normal')
        log_view.insert('end', text + '\n')
        log_view.see('end')
        log_view.configure(state='disabled')

    def update_buttons():
        analyze_button.configure(state='disabled' if busy else 'normal')
        download_button.configure(state='normal' if not busy and choices and any(v.get() for v in choices) else 'disabled')
        cancel_button.configure(state='normal' if busy else 'disabled')
        for widget in (url_entry, dest_entry, browse):
            widget.configure(state='disabled' if busy else 'normal')

    def analyze():
        nonlocal busy, parsed_url
        try:
            parsed_url = normalize_url(url_var.get())
        except (ValueError, TypeError) as e:
            messagebox.showerror('链接格式', str(e), parent=root)
            return
        url_var.set(parsed_url)
        choices.clear()
        for widget in selection_frame.winfo_children():
            widget.destroy()
        ttk.Label(selection_frame, text='正在读取播放器…').pack(anchor='w', pady=8)
        busy = True
        worker.cancel.clear()
        status_var.set('如需登录，请在新打开的浏览器中完成；本窗口会自动继续。')
        progress_var.set('')
        bar['value'] = 0
        update_buttons()
        worker.commands.put({'kind': 'analyze', 'url': parsed_url})

    def download():
        nonlocal busy
        try:
            if normalize_url(url_var.get()) != parsed_url:
                raise ValueError('链接已改变，请先重新解析。')
            folder = folder_var.get().strip().strip('"')
            if not folder:
                choose_folder()
                folder = folder_var.get().strip()
            if not folder:
                return
            if not Path(folder).is_absolute():
                raise ValueError('请选择绝对路径的保存文件夹。')
            Path(folder).mkdir(parents=True, exist_ok=True)
            config_path.write_text(json.dumps({'folder': folder}, ensure_ascii=False, indent=2), encoding='utf-8')
        except (ValueError, OSError) as e:
            messagebox.showerror('保存位置', str(e), parent=root)
            return
        busy = True
        worker.cancel.clear()
        update_buttons()
        worker.commands.put({'kind': 'download', 'indices': [i for i, v in enumerate(choices) if v.get()], 'folder': folder})

    def cancel():
        worker.cancel.set()
        status_var.set('正在停止，请稍候；已下载的部分会保留。')

    analyze_button = ttk.Button(action_row, text='1. 解析链接', command=analyze)
    analyze_button.pack(side='left')
    download_button = ttk.Button(action_row, text='2. 下载所选', command=download, state='disabled')
    download_button.pack(side='left', padx=8)
    cancel_button = ttk.Button(action_row, text='停止', command=cancel, state='disabled')
    cancel_button.pack(side='left')

    def poll():
        nonlocal busy
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            kind = event['kind']
            if kind == 'log':
                log(event['message'])
            elif kind == 'videos':
                busy = False
                title_var.set(event['title'])
                choices.clear()
                for widget in selection_frame.winfo_children():
                    widget.destroy()
                for video in event['videos']:
                    value = tk.BooleanVar(value=False)
                    choices.append(value)
                    resolution = f'{video.width} × {video.height}' if video.width else '清晰度待确认'
                    ttk.Checkbutton(selection_frame, text=f'{video.label}    |    {resolution}    |    {duration_text(video.duration)}', variable=value, command=update_buttons).pack(anchor='w', pady=3)
                status_var.set('请勾选需要的画面，然后点击“下载所选”。')
            elif kind == 'item':
                status_var.set(event['message'])
                bar['value'] = 0
            elif kind == 'progress':
                done, total = event['done'], event['total']
                bar['value'] = 100 * done / total if total else 0
                progress_var.set(f'{done / 1048576:,.1f} MB' + (f' / {total / 1048576:,.1f} MB  ({100 * done / total:.1f}%)' if total else '  已下载'))
            elif kind == 'done':
                busy = False
                bar['value'] = 100
                status_var.set(f"下载完成，已保存 {len(event['files'])} 个文件。")
                log('保存文件夹：' + folder_var.get())
            elif kind in ('error', 'cancelled'):
                busy = False
                status_var.set(event['message'])
                log(event['message'])
                if kind == 'error' and not closing:
                    messagebox.showerror('未完成', event['message'], parent=root)
            update_buttons()
        if closing and not worker.is_alive():
            root.destroy()
        else:
            root.after(150, poll)

    def close():
        nonlocal closing
        closing = True
        worker.cancel.set()
        worker.stop.set()
        status_var.set('正在结束下载并关闭浏览器…')

    root.protocol('WM_DELETE_WINDOW', close)
    root.after(150, poll)
    url_entry.focus_set()
    root.mainloop()
    lock_handle.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ELRC 录像下载器')
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE, help='独立浏览器与保存位置设置的目录')
    args = parser.parse_args()
    gui(args.state_dir)
