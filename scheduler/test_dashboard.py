import time
import threading
from scheduler.dashboard import SharedState, WorkerState, EventLog, Dashboard, SPINNER


# ============================================================
# SPINNER
# ============================================================

def test_spinner_not_empty():
    assert len(SPINNER) > 0


# ============================================================
# Dashboard._format_duration
# ============================================================

def test_format_duration_zero():
    assert Dashboard._format_duration(0) == "-"
    assert Dashboard._format_duration(-1) == "-"


def test_format_duration_seconds():
    assert Dashboard._format_duration(1.0) == "1.0s"
    assert Dashboard._format_duration(59.9) == "59.9s"


def test_format_duration_minutes():
    assert Dashboard._format_duration(60) == "1m00s"
    assert Dashboard._format_duration(125) == "2m05s"


def test_format_duration_hours():
    assert Dashboard._format_duration(3661) == "1h01m"


# ============================================================
# EventLog
# ============================================================

def test_event_log_add_and_snapshot():
    log = EventLog(maxlen=5)
    log.add("12:00", "worker-a", "started")
    log.add("12:01", "worker-a", "done")

    events = log.snapshot()
    assert len(events) == 2
    # 最新的在 index 0
    assert events[0] == ("12:01", "worker-a", "done")
    assert events[1] == ("12:00", "worker-a", "started")


def test_event_log_caps_at_maxlen():
    log = EventLog(maxlen=3)
    for i in range(5):
        log.add(f"12:0{i}", "w", f"msg{i}")

    events = log.snapshot()
    assert len(events) == 3
    assert events[0] == ("12:04", "w", "msg4")
    assert events[2] == ("12:02", "w", "msg2")


def test_event_log_thread_safety():
    log = EventLog(maxlen=100)
    errors = []

    def writer():
        try:
            for i in range(100):
                log.add(f"t", "w", f"msg{i}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(log.snapshot()) == 100


# ============================================================
# SharedState
# ============================================================

def test_shared_state_update_worker():
    state = SharedState()
    state.workers["test"] = WorkerState(name="test", cron="* * * * *")

    state.update_worker("test", last_run="12:00", last_status="✓", run_count=3)

    w = state.workers["test"]
    assert w.last_run == "12:00"
    assert w.last_status == "✓"
    assert w.run_count == 3


def test_shared_state_get_workers_snapshot():
    state = SharedState()
    state.workers["a"] = WorkerState(name="a", cron="* * * * *")
    state.workers["b"] = WorkerState(name="b", cron="disabled")

    snapshot = state.get_workers_snapshot()
    assert len(snapshot) == 2


# ============================================================
# WorkerState
# ============================================================

def test_worker_state_defaults():
    w = WorkerState(name="test", cron="0 9 * * *")
    assert w.name == "test"
    assert w.cron == "0 9 * * *"
    assert w.last_run == "-"
    assert w.last_status == "-"
    assert w.run_count == 0
    assert w.success_count == 0
    assert w.fail_count == 0
    # P0/P1 新增字段
    assert w.running is False
    assert w.last_duration == 0.0
    assert w.last_returncode == 0
    assert w.last_stdout == ""
    assert w.last_stderr == ""


def test_worker_state_running_flag():
    w = WorkerState(name="test", cron="* * * * *")
    assert w.running is False
    w.running = True
    assert w.running is True


def test_shared_state_update_new_fields():
    state = SharedState()
    state.workers["test"] = WorkerState(name="test", cron="* * * * *")

    state.update_worker(
        "test",
        running=True,
        last_duration=3.5,
        last_returncode=1,
        last_stdout="hello world",
        last_stderr="error msg",
    )

    w = state.workers["test"]
    assert w.running is True
    assert w.last_duration == 3.5
    assert w.last_returncode == 1
    assert w.last_stdout == "hello world"
    assert w.last_stderr == "error msg"
