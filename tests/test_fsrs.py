"""P2.7 (iss_d13d1882): FSRS-4.5 scheduler pins.

Covers the properties the swap depends on — the interval/stability identity at
the 0.90 target (which is also the legacy-migration story), lapse behavior,
growth monotonicity, difficulty bounds, and the app-level integration.
"""
import time

import pytest

import mastery
from mastery import (FSRS_W, FSRS_MIN_INTERVAL, FSRS_MAX_INTERVAL,
                     fsrs_review, fsrs_interval, fsrs_retrievability, _fsrs_d0)


def test_interval_equals_stability_at_090_target():
    # by construction I(0.90, S) == S — this identity is also why legacy
    # interval_d values migrate directly as stability
    for s in (1.0, 3.7, 30.0, 200.0):
        assert fsrs_interval(s) == pytest.approx(s, rel=1e-9)


def test_interval_floor_and_cap():
    assert fsrs_interval(0.01) == FSRS_MIN_INTERVAL
    assert fsrs_interval(10_000.0) == FSRS_MAX_INTERVAL


def test_first_review_initializes_from_grade():
    s_good, d_good, iv, due = fsrs_review(None, None, 0.0, correct=True, now=1000.0)
    assert s_good == pytest.approx(FSRS_W[2])          # S0(Good)
    assert d_good == pytest.approx(_fsrs_d0(3))
    assert due == pytest.approx(1000.0 + iv * 86400)
    s_bad, d_bad, _, _ = fsrs_review(0, None, 0.0, correct=False)
    assert s_bad == pytest.approx(FSRS_W[0])           # S0(Again)
    assert d_bad > d_good                               # failing first is harder


def test_lapse_never_raises_stability_and_shortens_interval():
    s0 = 30.0
    s2, d2, iv, _ = fsrs_review(s0, 5.0, elapsed_d=30.0, correct=False)
    assert s2 < s0
    assert iv < 30.0
    assert 1.0 <= d2 <= 10.0
    assert d2 > 5.0                                     # a lapse raises difficulty


def test_success_grows_stability():
    s0 = 10.0
    s2, d2, iv, _ = fsrs_review(s0, 5.0, elapsed_d=10.0, correct=True)
    assert s2 > s0
    assert iv > 10.0
    assert d2 < 5.0                                     # success eases difficulty


def test_earlier_review_grows_less_than_on_time():
    # reviewing early (high retrievability) should add less stability than
    # reviewing at the scheduled point (desirable-difficulty property)
    on_time, _, _, _ = fsrs_review(10.0, 5.0, elapsed_d=10.0, correct=True)
    early, _, _, _ = fsrs_review(10.0, 5.0, elapsed_d=1.0, correct=True)
    assert early < on_time


def test_difficulty_stays_bounded():
    s, d = 5.0, 5.0
    for _ in range(50):
        s, d, _, _ = fsrs_review(s, d, 1.0, correct=False)
    assert 1.0 <= d <= 10.0
    for _ in range(200):
        s, d, _, _ = fsrs_review(s, d, 1.0, correct=True)
    assert 1.0 <= d <= 10.0


def test_retrievability_anchors():
    assert fsrs_retrievability(0.0, 10.0) == pytest.approx(1.0)
    assert fsrs_retrievability(10.0, 10.0) == pytest.approx(0.90, abs=1e-9)


def test_legacy_migration_passes_interval_as_stability():
    # an SM-2 row with interval_d=8 → stability 8; a correct on-time review
    # must produce a LONGER next interval than SM-2's 8*ease would floor at 1d
    s2, d2, iv, _ = fsrs_review(8.0, None, elapsed_d=8.0, correct=True)
    assert s2 > 8.0 and iv > 8.0
    assert d2 == pytest.approx(mastery._fsrs_d0(3), abs=1.0)  # seeded near D0(Good)


def test_app_review_path_writes_fsrs_state(tmp_path, monkeypatch):
    import importlib, os
    monkeypatch.setenv("LEARN_DB", str(tmp_path / "t.db"))
    import db as _db
    importlib.reload(_db)
    _db.init()
    with _db.conn() as c:
        c.execute("INSERT INTO users (username, pw_hash, pw_salt, created_at) "
                  "VALUES ('t', x'00', x'00', ?)", (time.time(),))
        uid = c.execute("SELECT id FROM users").fetchone()[0]
    now = time.time()
    _db.upsert_state(uid, "top", "c1", p_mastery=0.97, streak=3, unlocked=1,
                     mastered_at=now - 10 * 86400, interval_d=8.0, ease=2.5,
                     due_at=now)                        # legacy SM-2 row, due now
    s = _db.get_states(uid, "top")["c1"]
    prev_s = s["stability"] or s["interval_d"]
    s2, d2, iv, due = fsrs_review(prev_s, s["difficulty"], 8.0, correct=True)
    _db.upsert_state(uid, "top", "c1", stability=s2, difficulty=d2,
                     interval_d=iv, due_at=due)
    row = _db.get_states(uid, "top")["c1"]
    assert row["stability"] == pytest.approx(s2) and row["stability"] > 8.0
    assert row["difficulty"] == pytest.approx(d2)
