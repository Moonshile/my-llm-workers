"""
调度器 curses 仪表盘，类似 top 命令的实时刷新界面。

使用方式：
    from dashboard import Dashboard, WorkerState, EventLog
    dashboard = Dashboard(workers, event_log)
    dashboard.run()  # blocking, 按 q 退出
"""

import curses
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime


# ============================================================
# 数据模型（线程安全）
# ============================================================

@dataclass
class WorkerState:
    name: str
    cron: str
    command: str = ""
    next_run: str = "-"
    last_run: str = "-"
    last_status: str = "-"   # ✓ 成功 / ✗ 失败 / ⏱ 超时
    run_count: int = 0
    success_count: int = 0
    fail_count: int = 0


class EventLog:
    """线程安全的事件日志（固定大小环形缓冲）。"""

    def __init__(self, maxlen: int = 200):
        self._deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, timestamp: str, worker: str, message: str):
        with self._lock:
            self._deque.appendleft((timestamp, worker, message))

    def snapshot(self) -> list:
        with self._lock:
            return list(self._deque)


class SharedState:
    """调度器与仪表盘之间的共享状态。"""

    def __init__(self):
        self.workers: dict[str, WorkerState] = {}
        self.events = EventLog()
        self.running = True
        self.lock = threading.Lock()

    def update_worker(self, name: str, **kwargs):
        with self.lock:
            if name not in self.workers:
                return
            for k, v in kwargs.items():
                if hasattr(self.workers[name], k):
                    setattr(self.workers[name], k, v)

    def get_workers_snapshot(self) -> list:
        with self.lock:
            return list(self.workers.values())


# ============================================================
# curses 仪表盘
# ============================================================

class Dashboard:
    def __init__(self, shared: SharedState, refresh_interval: float = 0.5):
        self.shared = shared
        self.refresh_interval = refresh_interval
        self.start_time = time.time()
        self.stdscr = None

    def run(self):
        """阻塞运行仪表盘直到用户按 q 退出。"""
        curses.wrapper(self._main_loop)

    def _main_loop(self, stdscr):
        self.stdscr = stdscr
        curses.curs_set(0)  # 隐藏光标
        stdscr.nodelay(True)

        # 初始化颜色（如果终端支持）
        try:
            curses.start_color()
            curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)   # 成功
            curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)     # 失败
            curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)  # 警告
            curses.init_pair(4, curses.COLOR_CYAN, curses.COLOR_BLACK)    # 标题
            curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)    # 表头
            self.use_colors = True
        except Exception:
            self.use_colors = False

        while self.shared.running:
            self._draw()
            stdscr.refresh()

            try:
                key = stdscr.getch()
                if key == ord("q"):
                    self.shared.running = False
                    break
                elif key == ord("r"):
                    # 手动刷新
                    pass
                elif key == curses.KEY_RESIZE:
                    self._draw()
            except Exception:
                pass

            time.sleep(self.refresh_interval)

    def _draw(self):
        if not self.stdscr:
            return
        self.stdscr.erase()
        max_y, max_x = self.stdscr.getmaxyx()

        # 计算各区域高度
        header_h = 2
        table_title_h = 1
        table_rows = min(len(self.shared.get_workers_snapshot()), max(1, max_y - 10))
        footer_h = 1
        log_h = max(2, max_y - header_h - table_title_h - table_rows - footer_h - 1)

        # ---- 顶部标题栏 ----
        self._draw_header(0, max_x)

        # ---- Worker 表格 ----
        table_start = header_h
        self._draw_table_header(table_start, max_x)
        self._draw_table_body(table_start + table_title_h, max_x, table_rows)

        # ---- 事件日志 ----
        log_start = table_start + table_title_h + table_rows
        self._draw_log(log_start, max_x, log_h)

        # ---- 底部状态栏 ----
        self._draw_footer(max_y - 1, max_x)

    def _draw_header(self, y, width):
        attrs = curses.A_BOLD | curses.color_pair(4) if self.use_colors else curses.A_BOLD
        self._write(y, 0, " My LLM Workers Scheduler ", attrs, width, True)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ts = f"{now} "
        self._write_rt(0, width, ts, attrs)

        # 第二行：统计 + 运行时间
        workers = self.shared.get_workers_snapshot()
        active = sum(1 for w in workers if w.cron != "-")
        uptime_s = int(time.time() - self.start_time)
        uptime = f"{uptime_s // 3600:02d}:{(uptime_s % 3600) // 60:02d}:{uptime_s % 60:02d}"
        stats = f" Workers: {active} | Uptime: {uptime} "
        self._write(y + 1, 0, stats, curses.A_DIM, width, True)

        # 分隔线
        sep = "─" * (width - 1)
        self.stdscr.addstr(y + 1, len(stats), sep[:width - len(stats) - 1], curses.A_DIM)

    def _draw_table_header(self, y, width):
        attrs = curses.A_BOLD | curses.color_pair(5) if self.use_colors else curses.A_REVERSE
        # 动态列宽
        name_w = max(20, (width - 48) // 2) if width > 70 else 15
        header = (
            f" {'Name':<{name_w}} {'Cron':<12} {'Next Run':<10} {'Last Run':<10} {'Status':<8}"
        )
        self._write(y, 0, header, attrs, width, True)

    def _draw_table_body(self, y, width, max_rows):
        workers = self.shared.get_workers_snapshot()
        name_w = max(20, (width - 48) // 2) if width > 70 else 15

        for i, w in enumerate(workers[:max_rows]):
            row_y = y + i
            if row_y >= curses.LINES - 5:
                break

            status = w.last_status
            status_attr = curses.A_NORMAL
            if self.use_colors:
                if status == "✓":
                    status_attr = curses.color_pair(1)
                elif status in ("✗", "⏱"):
                    status_attr = curses.color_pair(2)

            line = (
                f" {w.name:<{name_w}} "
                f"{w.cron:<12} "
                f"{w.next_run:<10} "
                f"{w.last_run:<10} "
            )
            self.stdscr.addstr(row_y, 0, line[:width - 1], curses.A_NORMAL)
            self.stdscr.addstr(row_y, len(line), status.center(8), status_attr | curses.A_BOLD)

    def _draw_log(self, y, width, max_h):
        if max_h < 3:
            return

        # 分隔线 + 标题
        sep = "─" * (width - 1)
        self.stdscr.addstr(y, 0, sep, curses.A_DIM)
        self._write(y + 1, 0, " Event Log", curses.A_BOLD, width)
        self.stdscr.addstr(y + 1, 11, "─" * (width - 12), curses.A_DIM)

        events = self.shared.events.snapshot()
        log_rows = min(len(events), max_h - 3)

        for i in range(log_rows):
            row_y = y + 2 + i
            ts, worker, msg = events[i]
            wname = worker if worker else "-"
            self.stdscr.addstr(row_y, 1, ts, curses.A_DIM)
            self.stdscr.addstr(row_y, 10, f" {wname:<25}", curses.A_NORMAL)
            self.stdscr.addstr(row_y, 36, msg[:width - 38], curses.A_NORMAL)

    def _draw_footer(self, y, width):
        sep = "─" * (width - 1)
        self.stdscr.addstr(y, 0, sep, curses.A_DIM)
        self._write(y + 1, 0, " q: quit  r: refresh", curses.A_DIM, width)

    def _write(self, y, x, text, attrs=curses.A_NORMAL, max_w=80, fill=False):
        """写入文本，fill=True 时用空格填充到 max_w。"""
        if fill:
            text = text.ljust(max_w - x - 1)[:max_w - x - 1]
        try:
            self.stdscr.addstr(y, x, text, attrs)
        except curses.error:
            pass

    def _write_rt(self, y, width, text, attrs=curses.A_NORMAL):
        """右对齐写入。"""
        x = max(0, width - len(text) - 1)
        try:
            self.stdscr.addstr(y, x, text, attrs)
        except curses.error:
            pass
