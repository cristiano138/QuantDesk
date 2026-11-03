"""D02：事件总线测试（DoD：10k 事件无丢失；单 handler 异常隔离；线程安全）。"""

import threading

from quantdesk.core.events import Event, EventBus, EventType


def test_publish_10k_no_loss():
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(None, received.append)  # 通配订阅
    for i in range(10_000):
        bus.publish(Event(EventType.TICK, data=i))
    assert len(received) == 10_000
    assert [e.data for e in received[:3]] == [0, 1, 2]


def test_handler_exception_isolated():
    bus = EventBus()
    ok: list[Event] = []

    def boom(_: Event) -> None:
        raise RuntimeError("boom")

    bus.subscribe(EventType.BAR, boom)
    bus.subscribe(EventType.BAR, ok.append)
    called = bus.publish(Event(EventType.BAR))
    assert called == 2       # 两个 handler 都被调用
    assert len(ok) == 1      # 异常不影响第二个订阅者


def test_str_type_and_specific_subscription():
    bus = EventBus()
    bars: list[Event] = []
    ticks: list[Event] = []
    bus.subscribe("bar", bars.append)
    bus.subscribe(EventType.TICK, ticks.append)
    bus.publish(Event(EventType.BAR))
    bus.publish(Event(EventType.TICK))
    assert len(bars) == 1 and len(ticks) == 1


def test_unsubscribe():
    bus = EventBus()
    received: list[Event] = []
    sid = bus.subscribe(EventType.LOG, received.append)
    assert bus.unsubscribe(sid) is True
    assert bus.unsubscribe(sid) is False
    bus.publish(Event(EventType.LOG))
    assert received == []


def test_multithread_publish_no_loss():
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe(None, received.append)
    n_threads, per_thread = 4, 1_000

    def worker(tag: int) -> None:
        for i in range(per_thread):
            bus.publish(Event(EventType.TIMER, data=(tag, i)))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(received) == n_threads * per_thread  # 无丢失
    assert all(isinstance(e.data, tuple) for e in received)


def test_subscriber_count():
    bus = EventBus()
    a = bus.subscribe(EventType.BAR, lambda e: None)
    bus.subscribe(None, lambda e: None)
    assert bus.subscriber_count() == 2
    assert bus.subscriber_count(EventType.BAR) == 2  # 通配 + BAR 专属
    bus.unsubscribe(a)
    assert bus.subscriber_count(EventType.BAR) == 1
