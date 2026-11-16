"""Unit tests for ``cw.installed_resolve`` (#3662).

Every timing test drives a fake clock and a fake sleep, so nothing waits in
real time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cw.installed_resolve import (
    DEFAULT_WAIT_S,
    IN_FLIGHT_MARKERS,
    POLL_INTERVAL_S,
    WAIT_ENV_VAR,
    State,
    classify,
    format_miss,
    format_recovered,
    format_report,
    git_in_flight,
    resolve_first_present,
)
from tests._shared.step25_gates import RESOLVE_INSTALLED_NO_WAIT_ENV


class FakeClock:
    """Monotonic clock that advances only when sleep() is called."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _write(path: Path, text: str = "content\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- classify ---------------------------------------------------------------


def test_classify_ok(tmp_path: Path) -> None:
    f = _write(tmp_path / "gate_key.py")
    report = classify(str(f))
    assert report.state is State.OK
    assert report.link_target is None


def test_classify_empty_is_zero_bytes(tmp_path: Path) -> None:
    f = _write(tmp_path / "gate_key.py", "")
    assert classify(str(f)).state is State.EMPTY


def test_classify_absent(tmp_path: Path) -> None:
    assert classify(str(tmp_path / "nope.py")).state is State.ABSENT


def test_classify_dangling_records_link_target(tmp_path: Path) -> None:
    link = tmp_path / "gate_key.py"
    Path(link).symlink_to(tmp_path / "gone" / "gate_key.py")
    report = classify(str(link))
    assert report.state is State.DANGLING
    assert report.link_target == str(tmp_path / "gone" / "gate_key.py")


def test_classify_directory_is_not_file(tmp_path: Path) -> None:
    d = tmp_path / "gate_key.py"
    d.mkdir()
    assert classify(str(d)).state is State.NOT_FILE


def test_classify_unreadable_when_parent_is_a_regular_file(tmp_path: Path) -> None:
    parent = _write(tmp_path / "file")
    report = classify(str(parent / "child.py"))
    assert report.state is State.UNREADABLE


# --- git_in_flight ----------------------------------------------------------


def _repo_with_git_dir(root: Path) -> Path:
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    return git_dir


@pytest.mark.parametrize("marker", IN_FLIGHT_MARKERS)
def test_git_in_flight_reports_each_marker(tmp_path: Path, marker: str) -> None:
    git_dir = _repo_with_git_dir(tmp_path / "repo")
    target = tmp_path / "repo" / "scripts" / "gate_key.py"
    if marker.startswith("rebase"):
        (git_dir / marker).mkdir()
    else:
        _write(git_dir / marker, "")
    _write(target, "x\n")
    assert git_in_flight(str(target)) == (marker,)


def test_git_in_flight_follows_linked_worktree_gitdir_file(tmp_path: Path) -> None:
    real_git = tmp_path / "main" / ".git" / "worktrees" / "wt"
    real_git.mkdir(parents=True)
    _write(real_git / "index.lock", "")
    wt = tmp_path / "wt"
    wt.mkdir()
    _write(wt / ".git", f"gitdir: {real_git}\n")
    assert git_in_flight(str(wt / "gate_key.py")) == ("index.lock",)


def test_git_in_flight_relative_gitdir_file(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    real_git = wt / "gitdata"
    real_git.mkdir(parents=True)
    _write(real_git / "MERGE_HEAD", "")
    _write(wt / ".git", "gitdir: gitdata\n")
    assert git_in_flight(str(wt / "x.py")) == ("MERGE_HEAD",)


def test_git_in_flight_no_repo_is_empty(tmp_path: Path) -> None:
    assert git_in_flight(str(tmp_path / "lonely.py")) == ()


def test_git_in_flight_malformed_git_file_is_empty(tmp_path: Path) -> None:
    _write(tmp_path / ".git", "not a gitdir line\n")
    assert git_in_flight(str(tmp_path / "x.py")) == ()


def test_git_in_flight_unreadable_git_file_is_empty(tmp_path: Path) -> None:
    git_file = tmp_path / ".git"
    git_file.write_bytes(b"\xff\xfe\x00bad")
    assert git_in_flight(str(tmp_path / "x.py")) == ()


def test_git_in_flight_empty_git_file_is_empty(tmp_path: Path) -> None:
    _write(tmp_path / ".git", "")
    assert git_in_flight(str(tmp_path / "x.py")) == ()


def test_git_in_flight_dangling_link_walks_to_target_ancestor(tmp_path: Path) -> None:
    git_dir = _repo_with_git_dir(tmp_path / "checkout")
    _write(git_dir / "index.lock", "")
    link = tmp_path / "home" / "gate_key.py"
    link.parent.mkdir(parents=True)
    Path(link).symlink_to(tmp_path / "checkout" / "scripts" / "gate_key.py")
    assert git_in_flight(str(link)) == ("index.lock",)


def test_git_in_flight_missing_everything_is_empty(tmp_path: Path) -> None:
    link = tmp_path / "home" / "gate_key.py"
    link.parent.mkdir(parents=True)
    Path(link).symlink_to(tmp_path / "nowhere" / "a" / "b.py")
    assert git_in_flight(str(link)) == ()


def test_git_in_flight_reports_nothing_when_path_has_no_existing_ancestor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import cw.installed_resolve as mod

    monkeypatch.setattr(mod, "_nearest_existing", lambda _p: None)
    assert git_in_flight(str(tmp_path / "x.py")) == ()


def test_git_in_flight_oserror_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import cw.installed_resolve as mod

    def boom(_p: Path) -> None:
        raise OSError(5, "disk gone")

    monkeypatch.setattr(mod, "_nearest_existing", boom)
    assert git_in_flight(str(tmp_path / "x.py")) == ()


def test_find_git_dir_oserror_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import cw.installed_resolve as mod

    _write(tmp_path / ".git", "gitdir: /x\n")

    def boom(_p: Path) -> Path:
        raise OSError(5, "unreadable")

    monkeypatch.setattr(mod, "_gitdir_of", boom)
    assert mod._find_git_dir(tmp_path) is None


# --- resolve_first_present --------------------------------------------------


def test_first_poll_hit_has_one_attempt_no_sleep(tmp_path: Path) -> None:
    f = _write(tmp_path / "gate_key.py")
    fake = FakeClock()
    res = resolve_first_present(
        [str(f)], wait_s=DEFAULT_WAIT_S, sleep=fake.sleep, clock=fake.clock
    )
    assert res.path == str(f)
    assert res.attempts == 1
    assert res.first_miss == ()
    assert fake.slept == []


def test_repo_local_absent_and_home_present_is_a_first_poll_hit(
    tmp_path: Path,
) -> None:
    home = _write(tmp_path / "home" / "gate_key.py")
    fake = FakeClock()
    res = resolve_first_present(
        [str(tmp_path / "repo" / "gate_key.py"), str(home)],
        wait_s=DEFAULT_WAIT_S,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    assert res.path == str(home)
    assert res.attempts == 1
    assert fake.slept == []


def test_candidate_order_wins_when_both_present(tmp_path: Path) -> None:
    first = _write(tmp_path / "a.py")
    second = _write(tmp_path / "b.py")
    res = resolve_first_present(
        [str(first), str(second)], wait_s=0, sleep=lambda _s: None
    )
    assert res.path == str(first)


def test_file_appearing_after_polls_recovers_with_first_miss(tmp_path: Path) -> None:
    target = tmp_path / "gate_key.py"
    fake = FakeClock()

    def appear_on_third(seconds: float) -> None:
        fake.sleep(seconds)
        if fake.now >= 1.0:
            _write(target)

    res = resolve_first_present(
        [str(target)],
        wait_s=10.0,
        interval_s=0.5,
        sleep=appear_on_third,
        clock=fake.clock,
    )
    assert res.path == str(target)
    assert res.attempts == 3
    assert len(res.first_miss) == 1
    assert res.first_miss[0].state is State.ABSENT


def test_zero_length_file_is_a_miss_then_becomes_ok(tmp_path: Path) -> None:
    target = _write(tmp_path / "gate_key.py", "")
    fake = FakeClock()

    def fill(seconds: float) -> None:
        fake.sleep(seconds)
        target.write_text("print()\n", encoding="utf-8")

    res = resolve_first_present([str(target)], wait_s=5.0, sleep=fill, clock=fake.clock)
    assert res.path == str(target)
    assert res.attempts == 2
    assert res.first_miss[0].state is State.EMPTY


def test_timeout_returns_none_within_budget_and_clamps_last_sleep(
    tmp_path: Path,
) -> None:
    fake = FakeClock()
    res = resolve_first_present(
        [str(tmp_path / "never.py")],
        wait_s=1.2,
        interval_s=0.5,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    assert res.path is None
    assert sum(fake.slept) <= 1.2 + 1e-9
    assert fake.slept[-1] == pytest.approx(0.2)
    assert res.attempts == len(fake.slept) + 1
    assert res.last[0].state is State.ABSENT


def test_wait_zero_makes_exactly_one_attempt_and_no_sleep(tmp_path: Path) -> None:
    fake = FakeClock()
    res = resolve_first_present(
        [str(tmp_path / "never.py")],
        wait_s=0,
        interval_s=POLL_INTERVAL_S,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    assert res.path is None
    assert res.attempts == 1
    assert fake.slept == []


def test_default_sleep_and_clock_are_used_when_not_injected(tmp_path: Path) -> None:
    res = resolve_first_present([str(tmp_path / "never.py")], wait_s=0)
    assert res.path is None
    assert res.attempts == 1


def test_default_sleep_path_is_exercised_with_real_clock(tmp_path: Path) -> None:
    # wait_s is tiny and the interval is zero, so the real sleep is a no-op
    # and the real clock does the bounding.
    res = resolve_first_present(
        [str(tmp_path / "never.py")], wait_s=0.01, interval_s=0.0
    )
    assert res.path is None
    assert res.attempts >= 1


def test_no_wait_env_constant_matches_the_module_constant() -> None:
    assert RESOLVE_INSTALLED_NO_WAIT_ENV == {WAIT_ENV_VAR: "0"}


# --- formatting -------------------------------------------------------------


def test_format_report_includes_target_and_in_flight(tmp_path: Path) -> None:
    from cw.installed_resolve import CandidateReport

    report = CandidateReport(
        str(tmp_path / "gate_key.py"),
        State.DANGLING,
        link_target="/x/y.py",
        in_flight=("index.lock", "MERGE_HEAD"),
    )
    text = format_report(report)
    assert "dangling -> /x/y.py" in text
    assert "git in flight: index.lock, MERGE_HEAD" in text


def test_format_miss_names_every_candidate_and_home(tmp_path: Path) -> None:
    fake = FakeClock()
    res = resolve_first_present(
        [str(tmp_path / "a.py"), str(tmp_path / "b.py")],
        wait_s=0.5,
        interval_s=0.5,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    text = format_miss(res, "/home/tester")
    assert "HOME=/home/tester" in text
    assert str(tmp_path / "a.py") in text
    assert str(tmp_path / "b.py") in text
    assert "2 attempt(s)" in text


def test_format_recovered_lists_first_miss(tmp_path: Path) -> None:
    target = tmp_path / "gate_key.py"
    fake = FakeClock()

    def fill(seconds: float) -> None:
        fake.sleep(seconds)
        _write(target)

    res = resolve_first_present([str(target)], wait_s=5.0, sleep=fill, clock=fake.clock)
    text = format_recovered(res, "/h")
    assert f"recovered {target} after 2 attempt(s)" in text
    assert "first miss" in text
    assert "absent" in text
