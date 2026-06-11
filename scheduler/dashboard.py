"""
调度器 curses 仪表盘，类似 top 命令的实时刷新界面。

使用方式：
    from dashboard import Dashboard, WorkerState, EventLog
    dashboard = Dashboard(workers, event_log)
    dashboard.run()  # blocking, 按 q 退出

交互：
    正常模式：
      ↑/↓        选择 worker 行
      Enter      打开选中 worker 的详情面板
      j/k        滚动事件日志（逐行）
      J/K        滚动事件日志（整页）
      r          手动刷新
      q          退出
    详情模式：
      ↑/↓ 或 j/k 滚动详情内容
      Esc/q      退出详情，返回正常模式
"""

import curses
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

# ============================================================
# 运行中动画字符
# ============================================================

SPINNER = ['⣾', '⣽', '⣻', '⢿', '⡿', '⣟', '⣯', '⣷']


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
    last_status: str = "-"       # ✓ 成功 / ✗ 失败 / ⏱ 超时
    run_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    # ---- P0 / P1 新增 ----
    running: bool = False         # 当前是否正在执行
    last_duration: float = 0.0    # 上次执行耗时（秒），0 表示尚无数据
    last_returncode: int = 0      # 上次退出码
    last_stdout: str = ""         # 上次运行的 stdout（最多 50KB）
    last_stderr: str = ""         # 上次运行的 stderr（最多 50KB）


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

        # Worker 选择
        self.selected_worker_idx = 0

        # 事件日志滚动
        self.log_scroll_offset = 0

        # 详情面板
        self.detail_mode = False
        self.detail_scroll = 0

        # 动画
        self.spinner_idx = 0
        self.use_colors = False

    # --------------------------------------------------------
    # 公开入口
    # --------------------------------------------------------

    def run(self):
        """阻塞运行仪表盘直到用户按 q 退出。"""
        curses.wrapper(self._main_loop)

    # --------------------------------------------------------
    # 主循环
    # --------------------------------------------------------

    def _main_loop(self, stdscr):
        self.stdscr = stdscr
        curses.curs_set(0)  # 隐藏光标
        # 用 timeout 替代 nodelay：让 getch() 等待完整按键序列（如方向键的 \x1b[A）
        # timeout 同时控制刷新频率，无需额外的 time.sleep
        stdscr.timeout(int(self.refresh_interval * 1000))

        # 初始化颜色
        try:
            curses.start_color()
            curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)   # 成功
            curses.init_pair(2, curses.COLOR_RED, curses.COLOR_BLACK)     # 失败/超时
            curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)  # 运行中 / 警告
            curses.init_pair(4, curses.COLOR_CYAN, curses.COLOR_BLACK)    # 标题
            curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)    # 表头
            self.use_colors = True
        except Exception:
            self.use_colors = False

        while self.shared.running:
            # 更新动画帧
            self.spinner_idx = (self.spinner_idx + 1) % len(SPINNER)

            self._draw()
            stdscr.refresh()

            key = self._safe_getch(stdscr)

            # -1 = timeout（无按键），跳过处理
            if key == -1:
                pass

            # ---- 详情模式按键 ----
            elif self.detail_mode:
                if key in (ord("q"), 27):          # q / Esc → 退出详情
                    self.detail_mode = False
                    self.detail_scroll = 0
                elif key == ord("j") or key == curses.KEY_DOWN:
                    self.detail_scroll += 1
                elif key == ord("k") or key == curses.KEY_UP:
                    self.detail_scroll = max(0, self.detail_scroll - 1)
                elif key == ord("J"):               # Shift+J: 详情翻页
                    self.detail_scroll += 10
                elif key == ord("K"):               # Shift+K: 详情翻页
                    self.detail_scroll = max(0, self.detail_scroll - 10)
                elif key == ord("g"):               # g → 跳到顶部
                    self.detail_scroll = 0
                elif key == ord("G"):               # G → 跳到底部
                    self.detail_scroll = 999999     # _draw_detail 中会被 clamp

            # ---- 正常模式按键 ----
            elif key == ord("q"):
                self.shared.running = False
                break
            elif key == curses.KEY_UP:
                self.selected_worker_idx = max(0, self.selected_worker_idx - 1)
            elif key == curses.KEY_DOWN:
                workers = self.shared.get_workers_snapshot()
                if workers:
                    self.selected_worker_idx = min(
                        len(workers) - 1, self.selected_worker_idx + 1
                    )
            elif key == ord("j"):
                self.log_scroll_offset += 1
            elif key == ord("k"):
                self.log_scroll_offset = max(0, self.log_scroll_offset - 1)
            elif key == ord("J"):
                self.log_scroll_offset += 15          # 大跳
            elif key == ord("K"):
                self.log_scroll_offset = max(0, self.log_scroll_offset - 15)
            elif key == ord("g"):
                self.log_scroll_offset = 0            # 跳到顶部
            elif key == ord("G"):
                self.log_scroll_offset = 999999       # 跳到底部（_draw_log 中 clamp）
            elif key in (10, 13, ord("\n")):         # Enter
                self.detail_mode = True
                self.detail_scroll = 0
                self.log_scroll_offset = 0
            elif key == ord("r"):
                pass                                   # 手动刷新（自然重绘）
            elif key == curses.KEY_RESIZE:
                # 终端尺寸变化：同步模块变量 + 重建内部窗口结构
                curses.update_lines_cols()
                if hasattr(curses, 'resize_term'):
                    try:
                        curses.resize_term(*self.stdscr.getmaxyx())
                    except Exception:
                        pass
                # 立即清屏重绘，避免残留
                self.stdscr.clear()
                self.stdscr.refresh()
                self.log_scroll_offset = 0
                self.detail_scroll = 0

    def _safe_getch(self, stdscr):
        """安全获取按键，KEY_RESIZE 等异常静默处理。"""
        try:
            return stdscr.getch()
        except Exception:
            return -1

    # --------------------------------------------------------
    # 绘制入口
    # --------------------------------------------------------

    def _draw(self):
        if not self.stdscr:
            return
        self.stdscr.clear()
        max_y, max_x = self.stdscr.getmaxyx()
        if max_y < 10 or max_x < 60:
            self._write(0, 0, "Terminal too small", curses.A_BOLD)
            return

        header_h = 2
        table_title_h = 1
        worker_count = len(self.shared.get_workers_snapshot())
        # 表格至少 1 行，至多留 12 行给下方区域
        table_rows = max(1, min(worker_count, max(1, max_y - 12)))
        footer_lines = 2  # 分隔线 + 帮助文字

        # 顶部标题栏（行 0-1）
        self._draw_header(0, max_x)

        # Worker 表格（行 2 开始）
        table_start = header_h
        self._draw_table_header(table_start, max_x)
        self._draw_table_body(table_start + table_title_h, max_x, table_rows)

        # 下方区域：详情面板或事件日志
        lower_start = table_start + table_title_h + table_rows
        lower_height = max(0, max_y - lower_start - footer_lines)

        if self.detail_mode:
            self._draw_detail(lower_start, max_x, lower_height)
        else:
            self._draw_log(lower_start, max_x, lower_height)

        # 底部状态栏（最后 2 行）
        self._draw_footer(max_y - footer_lines, max_x)

    # --------------------------------------------------------
    # 标题栏
    # --------------------------------------------------------

    def _draw_header(self, y, width):
        attrs = curses.A_BOLD | curses.color_pair(4) if self.use_colors else curses.A_BOLD
        self._write(y, 0, " My LLM Workers Scheduler ", attrs, width, True)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write_rt(y, width, f"{now} ", attrs)

        # 第二行：统计 + 运行时间
        workers = self.shared.get_workers_snapshot()
        active = sum(1 for w in workers if w.cron not in ("-", "disabled"))
        running = sum(1 for w in workers if w.running)
        uptime_s = int(time.time() - self.start_time)
        uptime = f"{uptime_s // 3600:02d}:{(uptime_s % 3600) // 60:02d}:{uptime_s % 60:02d}"

        stats = f" Workers: {active} enabled"
        if running > 0:
            stats += f" | {running} running"
        stats += f" | Uptime: {uptime} "
        self._write(y + 1, 0, stats, curses.A_DIM, width, True)

        # 分隔线（安全计算，防止负宽度）
        sep_x = min(len(stats), width - 1)
        sep_w = max(0, width - sep_x - 1)
        if sep_w > 0:
            try:
                self.stdscr.addstr(y + 1, sep_x, "─" * sep_w, curses.A_DIM)
            except curses.error:
                pass

    # --------------------------------------------------------
    # Worker 表格
    # --------------------------------------------------------

    def _draw_table_header(self, y, width):
        attrs = curses.A_BOLD | curses.color_pair(5) if self.use_colors else curses.A_REVERSE
        # 动态列宽：Name 占剩余空间
        # 布局: " Name  Cron  Next  Last  Dur  St"
        name_w = max(16, width - 58) if width >= 80 else 12
        header = (
            f" {'Name':<{name_w}} {'Cron':<12} {'Next':<10} {'Last':<10} {'Dur':<7} {'St'}"
        )
        self._write(y, 0, header, attrs, width, True)

    def _draw_table_body(self, y, width, max_rows):
        workers = self.shared.get_workers_snapshot()
        name_w = max(16, width - 58) if width >= 80 else 12

        # 确保 selected 不越界
        if workers:
            self.selected_worker_idx = min(self.selected_worker_idx, len(workers) - 1)

        for i, w in enumerate(workers[:max_rows]):
            row_y = y + i

            is_selected = (i == self.selected_worker_idx and not self.detail_mode)
            prefix = ">" if is_selected else " "

            # 状态 + 颜色
            if w.running:
                status = SPINNER[self.spinner_idx]
                status_attr = (
                    (curses.A_BOLD | curses.color_pair(3))
                    if self.use_colors
                    else curses.A_BOLD
                )
            elif w.last_status == "✓":
                status = "✓"
                status_attr = (
                    curses.color_pair(1) if self.use_colors else curses.A_NORMAL
                )
            elif w.last_status == "✗":
                status = "✗"
                status_attr = (
                    curses.color_pair(2) if self.use_colors else curses.A_NORMAL
                )
            elif w.last_status == "⏱":
                status = "⏱"
                status_attr = (
                    curses.color_pair(2) if self.use_colors else curses.A_NORMAL
                )
            else:
                status = w.last_status
                status_attr = curses.A_NORMAL

            # 耗时
            dur = self._format_duration(w.last_duration)

            # 选中行用粗体 / 反白
            row_base_attr = curses.A_BOLD if is_selected else curses.A_NORMAL

            line = (
                f"{prefix}{w.name:<{name_w}} "
                f"{w.cron:<12} "
                f"{w.next_run:<10} "
                f"{w.last_run:<10} "
                f"{dur:<7} "
            )
            # 先画主体，确保不超出屏幕宽度
            displayed = line[: max(0, width - 3)]
            self.stdscr.addstr(row_y, 0, displayed, row_base_attr)
            # status 紧接主体之后，不超出 width
            sx = len(displayed)
            if sx < width - 1:
                try:
                    self.stdscr.addstr(row_y, sx, status, status_attr)
                except curses.error:
                    pass

    # --------------------------------------------------------
    # 事件日志（支持滚动）
    # --------------------------------------------------------

    def _draw_log(self, y, width, max_h):
        if max_h < 3:
            return

        # 分隔线 + 标题
        sep_w = max(0, width - 1)
        if sep_w > 0:
            try:
                self.stdscr.addstr(y, 0, "─" * sep_w, curses.A_DIM)
            except curses.error:
                pass
        title = " Event Log"
        self._write(y + 1, 0, title, curses.A_BOLD, width)
        # 右侧提示
        hint = " j/k:scroll "
        self._write_rt(y + 1, width, hint, curses.A_DIM)

        events = self.shared.events.snapshot()
        viewport_h = max_h - 3          # 减去分隔线+标题+内边距
        if viewport_h < 1:
            return

        total_events = len(events)
        max_scroll = max(0, total_events - viewport_h)
        self.log_scroll_offset = max(0, min(self.log_scroll_offset, max_scroll))

        for i in range(viewport_h):
            row_y = y + 2 + i
            ev_idx = self.log_scroll_offset + i
            if ev_idx >= total_events:
                break
            ts, worker, msg = events[ev_idx]
            try:
                self.stdscr.addstr(row_y, 1, ts, curses.A_DIM)
            except curses.error:
                pass
            wname = worker if worker else "-"
            try:
                self.stdscr.addstr(row_y, 10, f" {wname:<25}", curses.A_NORMAL)
            except curses.error:
                pass
            # 消息区：从第 36 列开始，安全计算可用宽度
            msg_start = min(36, width - 2)
            msg_w = max(0, width - msg_start - 1)
            if msg_w > 0:
                msg_display = msg[:msg_w]
                try:
                    self.stdscr.addstr(row_y, msg_start, msg_display, curses.A_NORMAL)
                except curses.error:
                    pass

        # 滚动位置指示器
        if total_events > viewport_h:
            pct = int(self.log_scroll_offset / max_scroll * 100) if max_scroll > 0 else 0
            indicator = f" {pct}% "
            self._write_rt(y + 2 + viewport_h - 1, width, indicator, curses.A_DIM)

    # --------------------------------------------------------
    # Worker 详情面板
    # --------------------------------------------------------

    def _draw_detail(self, y, width, max_h):
        if max_h < 5:
            return

        workers = self.shared.get_workers_snapshot()
        if self.selected_worker_idx >= len(workers):
            return
        w = workers[self.selected_worker_idx]

        # ---- 构建详情内容行 ----
        lines = []

        # 标题
        lines.append(("title", f" Worker Detail: {w.name}"))
        lines.append(("sep", "─" * (width - 2)))

        # 基本信息
        lines.append(("keyval", ("Command", w.command)))
        lines.append(("keyval", ("Cron", w.cron)))

        # 状态行
        status_str = w.last_status
        if w.running:
            status_str = f"{SPINNER[self.spinner_idx]} running"
        elif w.last_returncode != 0 and w.last_status == "✗":
            status_str = f"✗ exit {w.last_returncode}"
        dur_str = self._format_duration(w.last_duration)
        lines.append(("keyval", ("Status", f"{status_str}  |  Duration: {dur_str}")))

        # 统计
        total = w.run_count
        success = w.success_count
        fail = w.fail_count
        if total > 0:
            rate = f"{success / total * 100:.1f}%"
        else:
            rate = "-"
        lines.append(
            (
                "keyval",
                ("Runs", f"{total} total, {success} success, {fail} failed ({rate})"),
            )
        )

        lines.append(("blank", ""))

        # ---- STDOUT ----
        lines.append(("subheading", " Last STDOUT:"))
        if w.last_stdout:
            stdout_lines = w.last_stdout.split("\n")
            if len(stdout_lines) > 200:
                stdout_lines = stdout_lines[:200]
                stdout_lines.append("... (truncated)")
            for sl in stdout_lines:
                lines.append(("stdout", sl))
        else:
            lines.append(("dim", "  (empty)"))

        lines.append(("blank", ""))

        # ---- STDERR ----
        lines.append(("subheading", " Last STDERR:"))
        if w.last_stderr:
            stderr_lines = w.last_stderr.split("\n")
            if len(stderr_lines) > 200:
                stderr_lines = stderr_lines[:200]
                stderr_lines.append("... (truncated)")
            for sl in stderr_lines:
                lines.append(("stderr", sl))
        else:
            lines.append(("dim", "  (empty)"))

        # ---- clamp scroll ----
        max_scroll = max(0, len(lines) - max_h)
        self.detail_scroll = max(0, min(self.detail_scroll, max_scroll))

        # ---- 渲染 ----
        # 颜色对
        title_attr = curses.A_BOLD | (
            curses.color_pair(4) if self.use_colors else 0
        )
        sep_attr = curses.A_DIM
        key_attr = curses.A_BOLD
        val_attr = curses.A_NORMAL
        subheading_attr = curses.A_BOLD | (
            curses.color_pair(5) if self.use_colors else curses.A_REVERSE
        )
        stdout_attr = curses.A_NORMAL
        stderr_attr = curses.color_pair(2) if self.use_colors else curses.A_NORMAL
        dim_attr = curses.A_DIM

        for i in range(max_h):
            line_idx = self.detail_scroll + i
            if line_idx >= len(lines):
                break

            row_y = y + i
            kind, content = lines[line_idx]

            if kind == "title":
                self._safe_addstr(row_y, 0, content[: max(0, width - 1)], title_attr)
            elif kind == "sep":
                self._safe_addstr(row_y, 0, content[: max(0, width - 1)], sep_attr)
            elif kind == "keyval":
                key, val = content
                self._safe_addstr(row_y, 1, f"{key}: ", key_attr)
                val_w = max(0, width - 4 - len(key))
                if val_w > 0:
                    self._safe_addstr(row_y, 2 + len(key), val[:val_w])
            elif kind == "subheading":
                self._safe_addstr(row_y, 0, content[: max(0, width - 1)], subheading_attr)
            elif kind == "stdout":
                self._safe_addstr(row_y, 1, content[: max(0, width - 2)], stdout_attr)
            elif kind == "stderr":
                self._safe_addstr(row_y, 1, content[: max(0, width - 2)], stderr_attr)
            elif kind == "dim":
                self._safe_addstr(row_y, 1, content[: max(0, width - 2)], dim_attr)
            elif kind == "blank":
                pass  # just leave the row empty

        # 底部提示
        if max_scroll > 0:
            pct = int(self.detail_scroll / max_scroll * 100) if max_scroll > 0 else 0
            indicator = f" {pct}% "
            self._write_rt(y + max_h - 1, width, indicator, curses.A_DIM)

    # --------------------------------------------------------
    # 底部状态栏
    # --------------------------------------------------------

    def _draw_footer(self, y, width):
        sep_w = max(0, width - 1)
        if sep_w > 0:
            try:
                self.stdscr.addstr(y, 0, "─" * sep_w, curses.A_DIM)
            except curses.error:
                pass
        if self.detail_mode:
            help_text = " q/Esc: back  ↑↓/jk: scroll  g/G: top/bottom"
        else:
            help_text = " q: quit  ↑↓: select  Enter: detail  j/k: log  g/G: log top/bottom  r: refresh"
        self._write(y + 1, 0, help_text, curses.A_DIM, width)

    # --------------------------------------------------------
    # 工具函数
    # --------------------------------------------------------

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """将秒数格式化为易读字符串。0 表示无数据。"""
        if seconds <= 0:
            return "-"
        if seconds < 60:
            return f"{seconds:.1f}s"
        if seconds < 3600:
            m = int(seconds // 60)
            s = int(seconds % 60)
            return f"{m}m{s:02d}s"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h{m:02d}m"

    def _safe_addstr(self, y, x, text, attrs=curses.A_NORMAL):
        """写入文本，自动捕获 curses.error（防止越界折行）。"""
        try:
            self.stdscr.addstr(y, x, text, attrs)
        except curses.error:
            pass

    def _write(self, y, x, text, attrs=curses.A_NORMAL, max_w=80, fill=False):
        """写入文本，fill=True 时用空格填充到 max_w。"""
        if fill:
            text = text.ljust(max_w - x - 1)[: max_w - x - 1]
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
