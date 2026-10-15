import pytest

import magnet
import magnet_suite
import suite_client
from suite_client import SuiteArgumentError, SuiteError, SuiteUnavailable


def entries(n=2):
    return [dict(key=str(i), path=f"probe{i}.mkv", real_name=f"movie{i}.mkv", real_size=100, coverage="head") for i in range(n)]


class Client:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {"tool_version": "0.4.0", "records": [{"best_player": {"tv": "kodi"}}]}
        self.error = error
        self.calls = []

    def score_partial(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if self.error:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def version(monkeypatch):
    monkeypatch.setattr(magnet_suite.suite_version, "ensure", lambda c: {"tool_version": "0.4.0", "capabilities": ["score_partial"]})


def run(client=None, **kwargs):
    return magnet_suite.score_partials(entries(kwargs.pop("n", 2)), kwargs.pop("emit", lambda m: None),
                                      kwargs.pop("cancel_check", lambda: False), client_factory=lambda: client or Client(), **kwargs)


def test_empty_and_ok():
    assert magnet_suite.score_partials([], lambda m: None, lambda: False, client_factory=lambda: pytest.fail()) == {}
    c = Client()
    messages = []
    out = run(c, emit=messages.append)
    assert out["0"] == dict(status="ok", tool_version="0.4.0", partial=True, record=c.result["records"][0], best_player={"tv": "kodi"})
    assert messages == ["VideoSuite: scoring movie0.mkv (1/2)", "VideoSuite: scoring movie1.mkv (2/2)"]
    assert c.calls[0][1]["coverage"] == "head" and callable(c.calls[0][1]["cancel"])


def test_unavailable_and_capability(monkeypatch):
    def unavailable():
        raise SuiteUnavailable()
    out = magnet_suite.score_partials(entries(), lambda m: None, lambda: False, client_factory=unavailable)
    assert all(v == dict(status="unavailable", message=magnet_suite.UNAVAILABLE_DETAIL) for v in out.values())
    monkeypatch.setattr(magnet_suite.suite_version, "ensure", lambda c: dict(capabilities=[], tool_version="0.3.0"))
    assert run()["0"] == dict(status="unavailable", message=magnet_suite.TOO_OLD_MESSAGE, tool_version="0.3.0")


@pytest.mark.parametrize("status", ["error", "unchecked"])
def test_failed_version_check_is_not_reported_as_too_old(monkeypatch, status):
    # The check itself failed (timeout, crash, garbage): the suite may be perfectly current.
    detail = "VideoSuite did not answer the version check (timeout)."
    monkeypatch.setattr(magnet_suite.suite_version, "ensure",
                        lambda c: dict(status=status, capabilities=[], tool_version=None, message=detail))
    out = run()
    for value in out.values():
        assert value["status"] == "error" and value["error_kind"] == "internal"
        assert "could not be checked" in value["message"] and detail in value["message"]
        assert "0.4.0" not in value["message"] and "reinstall" not in value["message"].lower()


def test_definite_too_old_and_missing_suite_keep_reinstall_wording(monkeypatch):
    monkeypatch.setattr(magnet_suite.suite_version, "ensure",
                        lambda c: dict(status="too_old", capabilities=["devices"], tool_version="0.3.0"))
    assert run()["0"] == dict(status="unavailable", message=magnet_suite.TOO_OLD_MESSAGE, tool_version="0.3.0")
    monkeypatch.setattr(magnet_suite.suite_version, "ensure",
                        lambda c: dict(status="unavailable", capabilities=[], tool_version=None, message="x"))
    out = run()["0"]
    assert out["status"] == "unavailable" and out["message"] == magnet_suite.TOO_OLD_MESSAGE


@pytest.mark.parametrize("error,kind,message", [(SuiteArgumentError("bad"), "usage", "bad")] +
                         [(SuiteError(k, "failed"), k, "failed") for k in suite_client.ERROR_KINDS] +
                         [(RuntimeError("secret"), "internal", "VideoSuite scoring failed")])
def test_errors(error, kind, message):
    assert run(Client(error=error))["0"] == dict(status="error", error_kind=kind, message=message)


def test_no_records_and_none():
    assert run(Client(result={"records": []}))["0"]["error_kind"] == "contract"
    c = Client()
    c.result = None
    assert run(c)["0"] == dict(status="skipped", message="cancelled")


def test_cap_cancel_and_budget():
    c = Client()
    out = run(c, n=9)
    assert len(c.calls) == 8 and out["8"]["message"] == "only the first 8 video files of a torrent are scored by VideoSuite"
    assert run(cancel_check=lambda: True)["0"]["message"] == "cancelled"
    times = iter([0, 299, 301])
    assert run(clock=lambda: next(times))["1"]["message"] == "VideoSuite time budget (300 s) used up"


def test_version_time_counts(monkeypatch):
    now = [0]
    def ensure(c):
        now[0] += 290
        return dict(capabilities=["score_partial"])
    monkeypatch.setattr(magnet_suite.suite_version, "ensure", ensure)
    c = Client()
    original = c.score_partial
    def score(*args, **kwargs):
        now[0] += 20
        return original(*args, **kwargs)
    c.score_partial = score
    out = run(c, clock=lambda: now[0])
    assert out["0"]["status"] == "ok" and out["1"]["status"] == "skipped"
    assert magnet_suite.SUITE_MAGNET_BUDGET_S + suite_client.SCORE_BASE_S + suite_client.SCORE_PER_FILE_S + 5 < magnet.SUITE_STAGE_MAX_S


@pytest.mark.parametrize("where", ["factory", "ensure", "emit", "cancel", "clock"])
def test_surprise_exceptions_never_raise(where, monkeypatch):
    def boom(*args):
        raise RuntimeError("secret")
    kwargs = {}
    if where == "ensure":
        monkeypatch.setattr(magnet_suite.suite_version, "ensure", boom)
    if where == "factory":
        out = magnet_suite.score_partials(entries(), lambda m: None, lambda: False, client_factory=boom)
    else:
        kwargs[{"emit": "emit", "cancel": "cancel_check", "clock": "clock"}.get(where, "emit")] = boom if where != "ensure" else lambda m: None
        out = run(**kwargs)
    assert all(v == dict(status="error", error_kind="internal", message="VideoSuite scoring failed") for v in out.values())
