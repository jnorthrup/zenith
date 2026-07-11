from zenith_harness.dispatcher import (
    MockDispatcher,
    MockTerminalReviewer,
    DispatchRequest,
)
from zenith_harness.models import (
    Task,
    WorkHandoff,
    ValidateHandoff,
    TerminalReviewHandoff,
)


def test_mock_dispatcher_dispatch():
    def responder(request: DispatchRequest):
        return WorkHandoff(
            node_id=request.task.id,
            done=True,
            report="ok",
            request_attention=False,
        )

    dispatcher = MockDispatcher(responder)

    task = Task(id="t1", type="work", body="test body", targets=["assert1"])
    request = DispatchRequest(project_id="p1", mission_id="m1", task=task, spawn_ts="123")

    result = dispatcher.dispatch(request)

    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0] == request

    assert isinstance(result, WorkHandoff)
    assert result.node_id == "t1"
    assert result.done is True
    assert result.report == "ok"


def test_mock_dispatcher_dispatch_batch():
    def responder(request: DispatchRequest):
        return WorkHandoff(
            node_id=request.task.id,
            done=True,
            report=f"ok {request.task.id}",
            request_attention=False,
        )

    dispatcher = MockDispatcher(responder)

    tasks = [
        Task(id="t1", type="work", body="test body", targets=["assert1"]),
        Task(id="t2", type="work", body="test body", targets=["assert1"]),
    ]
    requests = [
        DispatchRequest(project_id="p1", mission_id="m1", task=tasks[0], spawn_ts="123"),
        DispatchRequest(project_id="p1", mission_id="m1", task=tasks[1], spawn_ts="123"),
    ]

    results = dispatcher.dispatch_batch(requests)

    assert len(dispatcher.calls) == 2
    # Order might vary
    call_ids = {c.task.id for c in dispatcher.calls}
    assert call_ids == {"t1", "t2"}

    assert len(results) == 2
    result_reports = {r.report for r in results}
    assert result_reports == {"ok t1", "ok t2"}


def test_mock_dispatcher_dispatch_batch_exception_validate():
    def responder(request: DispatchRequest):
        raise ValueError("simulated error")

    dispatcher = MockDispatcher(responder)

    task = Task(id="t1", type="validate", body="test body", targets=["assert1"])
    request = DispatchRequest(project_id="p1", mission_id="m1", task=task, spawn_ts="123")

    results = dispatcher.dispatch_batch([request])

    assert len(results) == 1
    result = results[0]

    assert isinstance(result, ValidateHandoff)
    assert result.node_id == "t1"
    assert result.done is False
    assert result.passed is False
    assert "Dispatcher crashed" in result.report
    assert "simulated error" in result.report


def test_mock_dispatcher_dispatch_batch_exception_work():
    def responder(request: DispatchRequest):
        raise ValueError("simulated error")

    dispatcher = MockDispatcher(responder)

    task = Task(id="t1", type="work", body="test body", targets=["assert1"])
    request = DispatchRequest(project_id="p1", mission_id="m1", task=task, spawn_ts="123")

    results = dispatcher.dispatch_batch([request])

    assert len(results) == 1
    result = results[0]

    assert isinstance(result, WorkHandoff)
    assert result.node_id == "t1"
    assert result.done is False
    assert result.request_attention is False
    assert "Dispatcher crashed" in result.report
    assert "simulated error" in result.report


def test_mock_terminal_reviewer():
    review_handoff = TerminalReviewHandoff(done=True, report="looks good")
    reviewer = MockTerminalReviewer(review_handoff)

    result = reviewer.review(project_id="p1", mission_id="m1", spawn_ts="123")

    assert result == review_handoff
    assert len(reviewer.calls) == 1
    assert reviewer.calls[0] == ("p1", "m1", "123")
