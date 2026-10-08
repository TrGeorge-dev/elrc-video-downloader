"""Exercise the hidden Tk window with a fake worker and synthetic course data."""
import gc
import json
import threading
import time

import pytest

tk = pytest.importorskip('tkinter')
from tkinter import messagebox, ttk

import elrc_downloader as app


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def test_select_only_requested_camera_and_persist_folder(tmp_path, monkeypatch):
    commands, failures = [], []
    destination = tmp_path / '中文 保存位置'

    class FakeWorker:
        def __init__(self, events, state_dir):
            self.events = events
            self.cancel, self.stop = threading.Event(), threading.Event()
            self.commands = self

        def start(self):
            pass

        def is_alive(self):
            return False

        def put(self, command):
            commands.append(command)
            if command['kind'] == 'analyze':
                self.events.put({'kind': 'videos', 'title': '示例课程 · 第2周', 'videos': [
                    app.Video('https://example.invalid/near.mp4', '教师近景画面', 600, 1920, 1080),
                    app.Video('https://example.invalid/screen.mp4', '屏幕画面', 600, 1920, 1080),
                ]})
            else:
                self.events.put({'kind': 'done', 'files': ['sample.mp4']})

    real_init = tk.Tk.__init__
    real_mainloop = tk.Tk.mainloop

    def hidden_init(root, *args, **kwargs):
        try:
            real_init(root, *args, **kwargs)
        except tk.TclError:
            pytest.skip('Tk display is unavailable; run this test in a desktop session.')
        root.withdraw()

    def harness(root, *args, **kwargs):
        deadline = time.monotonic() + 5

        def finish_with_error(error):
            failures.append(repr(error))
            root.destroy()

        def guarded(callback):
            def invoke():
                try:
                    if time.monotonic() > deadline:
                        raise AssertionError('Timed out waiting for the GUI event queue.')
                    callback()
                except Exception as error:
                    finish_with_error(error)
            return invoke

        def button(text):
            return next(widget for widget in descendants(root)
                        if isinstance(widget, ttk.Button) and widget.cget('text') == text)

        def parse():
            entries = [widget for widget in descendants(root) if isinstance(widget, ttk.Entry)]
            entries[0].insert(0, 'https://elrc.shanghaitech.edu.cn/learn/videoreview/fixture-course-001?week=2')
            entries[1].insert(0, str(destination))
            button('1. 解析链接').invoke()
            root.after(50, guarded(select))

        def select():
            checks = [widget for widget in descendants(root) if isinstance(widget, ttk.Checkbutton)]
            if not checks:
                root.after(50, guarded(select))
                return
            assert len(checks) == 2
            assert all('selected' not in widget.state() for widget in checks)
            download = button('2. 下载所选')
            assert 'disabled' in download.state()
            checks[1].invoke()
            assert 'disabled' not in download.state()
            download.invoke()
            assert 'disabled' in download.state()
            root.after(50, guarded(finish))

        def finish():
            if 'disabled' in button('2. 下载所选').state():
                root.after(50, guarded(finish))
                return
            assert [command['kind'] for command in commands] == ['analyze', 'download']
            assert commands[-1]['indices'] == [1]
            assert commands[-1]['folder'] == str(destination)
            settings = json.loads((tmp_path / 'settings.json').read_text(encoding='utf-8'))
            assert settings['folder'] == str(destination)
            root.destroy()

        root.report_callback_exception = lambda kind, value, trace: finish_with_error(value)
        root.after(0, guarded(parse))
        return real_mainloop(root, *args, **kwargs)

    monkeypatch.setattr(app, 'BrowserWorker', FakeWorker)
    monkeypatch.setattr(tk.Tk, '__init__', hidden_init)
    monkeypatch.setattr(tk.Tk, 'mainloop', harness)
    monkeypatch.setattr(messagebox, 'showerror', lambda *args, **kwargs: failures.append(str(args)))
    monkeypatch.setattr(messagebox, 'showinfo', lambda *args, **kwargs: failures.append(str(args)))
    app.gui(tmp_path)
    # Dispose Tk variable cycles on this thread, before HTTP fixture threads start.
    gc.collect()
    assert not failures, failures
