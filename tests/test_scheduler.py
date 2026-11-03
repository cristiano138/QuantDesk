"""D27：盘后定时任务测试（调度接线 + 任务体可注入）。"""

from quantdesk.core.events import EventBus
from quantdesk.services.scheduler import PostCloseScheduler


def test_scheduler_cron_wired():
    """18:00 工作日排程 + 启停不抛。"""
    s = PostCloseScheduler(symbols=["600000.SH"], bus=EventBus())
    s.start()
    try:
        jobs = s._scheduler.get_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "post_close_refresh"
        fields = {f.name: str(f) for f in jobs[0].trigger.fields}
        assert fields["hour"] == "18" and fields["minute"] == "0"
        assert fields["day_of_week"] in ("mon-fri", "0-4")
    finally:
        s.stop()


def test_refresh_job_publishes_log():
    """任务体运行（不触网的桩路径：依赖缺失也被结构化捕获）并广播 LOG。"""
    events = []
    bus = EventBus()
    bus.subscribe("log", lambda e: events.append(e.data))
    s = PostCloseScheduler(symbols=["600000.SH"], bus=bus)
    # 不注入真实 feed：refresh_job 内部 import 真实源——CI 环境有 akshare，
    # 网络失败会落入 failed 列表（结构化），不抛异常
    out = s.refresh_job(symbols=["600000.SH"])
    assert "ran_at" in out and "ok" in out and "failed" in out
    assert any(e.get("message", "").startswith("盘后刷新完成") for e in events)
