import time
import threading
from scheduler.dashboard import SharedState, WorkerState, EventLog


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
