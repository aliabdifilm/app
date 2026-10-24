"""Tests for app.services.eventbus (isolation, idempotency, signatures)."""

import logging
import threading

import pytest
from flask import Flask

from app.extensions import db
from app.services import eventbus


@pytest.fixture(autouse=True)
def isolated_bus():
    """Each test sees an empty bus; real module subscribers are restored after."""
    with eventbus.isolated():
        yield


def test_publish_delivers_payload_as_keyword_arguments():
    received = []
    eventbus.subscribe("invoice.saved", lambda **payload: received.append(payload))
    result = eventbus.publish("invoice.saved", invoice_id=7, created=True)
    assert received == [{"invoice_id": 7, "created": True}]
    assert (result.delivered, result.failed, result.total) == (1, 0, 1)


def test_publish_without_subscribers_is_a_noop():
    result = eventbus.publish("nobody.listens", x=1)
    assert (result.delivered, result.failed) == (0, 0)


def test_handlers_run_in_registration_order():
    order = []
    eventbus.subscribe("e", lambda **_: order.append("a"))
    eventbus.subscribe("e", lambda **_: order.append("b"))
    eventbus.subscribe("e", lambda **_: order.append("c"))
    eventbus.publish("e")
    assert order == ["a", "b", "c"]


def test_failing_subscriber_never_breaks_publisher_or_other_subscribers(caplog):
    calls = []

    def broken(**_):
        raise RuntimeError("boom")

    eventbus.subscribe("assignment.changed", broken)
    eventbus.subscribe("assignment.changed", lambda **p: calls.append(p["assignment_id"]))
    with caplog.at_level(logging.ERROR, logger="app.services.eventbus"):
        result = eventbus.publish("assignment.changed", assignment_id=3)  # must not raise
    assert calls == [3]  # the second handler still ran
    assert (result.delivered, result.failed) == (1, 1)
    assert "boom" in caplog.text and "broken" in caplog.text


def test_failing_subscriber_with_wrong_signature_is_isolated(caplog):
    def needs_unknown(missing_param):  # TypeError: payload has no such key
        return missing_param

    eventbus.subscribe("x", needs_unknown)
    with caplog.at_level(logging.ERROR, logger="app.services.eventbus"):
        result = eventbus.publish("x", a=1)
    assert result.failed == 1


def test_subscribe_is_idempotent_for_same_function_and_name():
    calls = []

    def handler(**_):
        calls.append(1)

    eventbus.subscribe("e", handler)
    eventbus.subscribe("e", handler)
    eventbus.publish("e")
    assert calls == [1]
    assert eventbus.subscribers("e") == (handler,)


def test_same_function_may_listen_to_different_events():
    calls = []

    def handler(**payload):
        calls.append(payload)

    eventbus.subscribe("a", handler)
    eventbus.subscribe("b", handler)
    eventbus.publish("a", n=1)
    eventbus.publish("b", n=2)
    assert calls == [{"n": 1}, {"n": 2}]


def test_decorator_form_returns_the_function():
    seen = []

    @eventbus.subscribe("event.status_changed")
    def on_change(event_id, new_status, **_):
        seen.append((event_id, new_status))

    assert callable(on_change)  # still usable as a plain function
    eventbus.publish("event.status_changed", event_id=1, new_status="done", old_status="x")
    assert seen == [(1, "done")]


def test_handler_receives_only_the_parameters_it_declares():
    seen = []

    def narrow(invoice_id):
        seen.append(invoice_id)

    eventbus.subscribe("invoice.saved", narrow)
    result = eventbus.publish("invoice.saved", invoice_id=9, event_id=1, actor_id=2)
    assert seen == [9]
    assert result.failed == 0


def test_bound_methods_are_deduplicated_and_callable():
    class Listener:
        def __init__(self):
            self.n = 0

        def handle(self, **_):
            self.n += 1

    obj = Listener()
    eventbus.subscribe("e", obj.handle)
    eventbus.subscribe("e", obj.handle)
    eventbus.publish("e")
    assert obj.n == 1


def test_unsubscribe():
    calls = []

    def handler(**_):
        calls.append(1)

    eventbus.subscribe("e", handler)
    assert eventbus.unsubscribe("e", handler) is True
    assert eventbus.unsubscribe("e", handler) is False
    eventbus.publish("e")
    assert calls == []


def test_unsubscribing_during_delivery_does_not_affect_current_publish():
    calls = []

    def first(**_):
        calls.append("first")
        eventbus.unsubscribe("e", second)

    def second(**_):
        calls.append("second")

    eventbus.subscribe("e", first)
    eventbus.subscribe("e", second)
    eventbus.publish("e")
    assert calls == ["first", "second"]  # snapshot semantics
    calls.clear()
    eventbus.publish("e")
    assert calls == ["first"]


def test_clear_one_name_and_all():
    eventbus.subscribe("a", lambda **_: None)
    eventbus.subscribe("b", lambda **_: None)
    eventbus.clear("a")
    assert eventbus.subscribers("a") == ()
    assert len(eventbus.subscribers("b")) == 1
    eventbus.clear()
    assert eventbus.subscribers("b") == ()


def test_isolated_restores_previous_handlers():
    def keep(**_):
        pass

    eventbus.subscribe("keep.me", keep)
    with eventbus.isolated():
        assert eventbus.subscribers("keep.me") == ()
        eventbus.subscribe("temp", lambda **_: None)
    assert eventbus.subscribers("keep.me") == (keep,)
    assert eventbus.subscribers("temp") == ()


@pytest.mark.parametrize("bad", ["", "   ", None, 5])
def test_invalid_event_names_are_rejected(bad):
    with pytest.raises(ValueError):
        eventbus.subscribe(bad, lambda **_: None)
    with pytest.raises(ValueError):
        eventbus.publish(bad)


def test_non_callable_subscriber_is_rejected():
    with pytest.raises(TypeError):
        eventbus.subscribe("e", "not callable")


def test_documented_event_names():
    assert set(eventbus.KNOWN_EVENTS) == {
        "invoice.saved",
        "assignment.changed",
        "event.status_changed",
        "equipment.assigned",
    }
    assert eventbus.INVOICE_SAVED == "invoice.saved"
    assert eventbus.ASSIGNMENT_CHANGED == "assignment.changed"
    assert eventbus.EVENT_STATUS_CHANGED == "event.status_changed"
    assert eventbus.EQUIPMENT_ASSIGNED == "equipment.assigned"


def test_failing_subscriber_does_not_corrupt_the_publishers_transaction():
    """A subscriber that raises after the publisher wrote must leave the unit of work intact."""
    from app.models import Setting

    app = Flask(__name__)
    app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
    db.init_app(app)

    def exploding(**_):
        raise RuntimeError("subscriber bug")

    eventbus.subscribe("invoice.saved", exploding)
    with app.app_context():
        db.create_all()
        db.session.add(Setting(key="k", value={"a": 1}))
        db.session.flush()
        eventbus.publish("invoice.saved", invoice_id=1)  # swallowed
        db.session.commit()
        assert Setting.get("k") == {"a": 1}
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_concurrent_subscribe_and_publish_is_safe():
    counter = {"n": 0}
    lock = threading.Lock()

    def handler(**_):
        with lock:
            counter["n"] += 1

    errors = []

    def worker(i):
        try:
            for _ in range(50):
                eventbus.subscribe("shared", handler)  # idempotent
                eventbus.publish("shared", i=i)
        except Exception as exc:  # pragma: no cover - would fail the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert eventbus.subscribers("shared") == (handler,)
    assert counter["n"] == 8 * 50
