"""Tests for CompanionEngine, helper functions, and get_git_health."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.transports.companion_state import (
    LEVEL_THRESHOLDS,
    CompanionEngine,
    GitHealth,
    Mood,
    get_git_health,
    steps_to_level,
    xp_fraction,
)

# ---------------------------------------------------------------------------
# steps_to_level — boundary values
# ---------------------------------------------------------------------------


class TestStepsToLevel:
    """Verify level thresholds: (0, 5, 15, 30, 50)."""

    def test_zero_steps_is_level_1(self) -> None:
        assert steps_to_level(0) == 1

    def test_four_steps_still_level_1(self) -> None:
        """One below level-2 threshold."""
        assert steps_to_level(4) == 1

    def test_five_steps_is_level_2(self) -> None:
        """Exactly at level-2 threshold."""
        assert steps_to_level(5) == 2

    def test_fourteen_steps_still_level_2(self) -> None:
        """One below level-3 threshold."""
        assert steps_to_level(14) == 2

    def test_fifteen_steps_is_level_3(self) -> None:
        assert steps_to_level(15) == 3

    def test_twenty_nine_steps_still_level_3(self) -> None:
        assert steps_to_level(29) == 3

    def test_thirty_steps_is_level_4(self) -> None:
        assert steps_to_level(30) == 4

    def test_forty_nine_steps_still_level_4(self) -> None:
        assert steps_to_level(49) == 4

    def test_fifty_steps_is_level_5(self) -> None:
        assert steps_to_level(50) == 5

    def test_one_hundred_steps_capped_at_level_5(self) -> None:
        """Well beyond any threshold — must not exceed 5."""
        assert steps_to_level(100) == 5


# ---------------------------------------------------------------------------
# xp_fraction — boundary values and level-5 cap
# ---------------------------------------------------------------------------


class TestXpFraction:
    """Verify XP progress fractions are accurate and clamped."""

    def test_zero_steps_level_1_is_zero(self) -> None:
        assert xp_fraction(0, 1) == pytest.approx(0.0)

    def test_halfway_through_level_1(self) -> None:
        # threshold[0]=0, threshold[1]=5 → 2/5 = 0.4
        assert xp_fraction(2, 1) == pytest.approx(0.4)

    def test_at_level_2_threshold_xp_resets(self) -> None:
        # 5 steps, now level 2 → (5-5)/(15-5) = 0.0
        assert xp_fraction(5, 2) == pytest.approx(0.0)

    def test_halfway_through_level_2(self) -> None:
        # threshold[1]=5, threshold[2]=15 → (10-5)/10 = 0.5
        assert xp_fraction(10, 2) == pytest.approx(0.5)

    def test_level_5_always_returns_one(self) -> None:
        assert xp_fraction(50, 5) == pytest.approx(1.0)

    def test_level_5_far_beyond_still_returns_one(self) -> None:
        assert xp_fraction(9999, 5) == pytest.approx(1.0)

    def test_full_xp_bar_clamped_to_one(self) -> None:
        # Passing steps >= nxt should clamp at 1.0, not exceed it.
        assert xp_fraction(LEVEL_THRESHOLDS[1], 1) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# GitHealth — stress_level and total_issues
# ---------------------------------------------------------------------------


class TestGitHealth:
    """Verify GitHealth derived properties."""

    def test_clean_repo_stress_level_zero(self) -> None:
        h = GitHealth(dirty_files=0, untracked_files=0)
        assert h.stress_level == 0

    def test_clean_repo_total_issues_zero(self) -> None:
        h = GitHealth(dirty_files=0, untracked_files=0)
        assert h.total_issues == 0

    def test_three_issues_is_minor_chaos(self) -> None:
        h = GitHealth(dirty_files=2, untracked_files=1)
        assert h.stress_level == 1

    def test_five_issues_is_full_chaos(self) -> None:
        h = GitHealth(dirty_files=3, untracked_files=2)
        assert h.stress_level == 2

    def test_total_issues_sums_both_fields(self) -> None:
        h = GitHealth(dirty_files=4, untracked_files=7)
        assert h.total_issues == 11

    def test_exactly_four_issues_is_still_minor_chaos(self) -> None:
        """Boundary: < 5 → stress_level 1."""
        h = GitHealth(dirty_files=4, untracked_files=0)
        assert h.stress_level == 1


# ---------------------------------------------------------------------------
# CompanionEngine — state machine
# ---------------------------------------------------------------------------


class TestCompanionEngineInitialState:
    """Engine should start in a clean, happy state."""

    def test_initial_mood_is_happy(self) -> None:
        e = CompanionEngine()
        assert e.mood == Mood.HAPPY

    def test_initial_level_is_one(self) -> None:
        e = CompanionEngine()
        assert e.level == 1

    def test_initial_steps_is_zero(self) -> None:
        e = CompanionEngine()
        assert e.steps == 0

    def test_initial_errors_is_zero(self) -> None:
        e = CompanionEngine()
        assert e.errors == 0

    def test_initial_xp_is_zero(self) -> None:
        e = CompanionEngine()
        assert e.xp == pytest.approx(0.0)


class TestCompanionEngineOnStep:
    """on_step() — step counting, mood, level-up detection."""

    def test_on_step_returns_false_without_level_up(self) -> None:
        e = CompanionEngine()
        levelled = e.on_step()
        assert levelled is False

    def test_on_step_sets_focused_mood(self) -> None:
        e = CompanionEngine()
        e.on_step()
        assert e.mood == Mood.FOCUSED

    def test_on_step_increments_steps(self) -> None:
        e = CompanionEngine()
        for _ in range(3):
            e.on_step()
        assert e.steps == 3

    def test_on_step_returns_true_on_level_2_boundary(self) -> None:
        e = CompanionEngine()
        for _ in range(4):
            e.on_step()  # steps 1-4: no level-up
        levelled = e.on_step()  # step 5 → level 2
        assert levelled is True

    def test_on_step_sets_level_up_mood_on_threshold(self) -> None:
        e = CompanionEngine()
        for _ in range(5):
            e.on_step()
        assert e.mood == Mood.LEVEL_UP

    def test_on_step_increments_level_on_threshold(self) -> None:
        e = CompanionEngine()
        for _ in range(5):
            e.on_step()
        assert e.level == 2

    def test_on_step_returns_true_on_level_3_boundary(self) -> None:
        e = CompanionEngine()
        for _ in range(14):
            e.on_step()
        levelled = e.on_step()  # step 15 → level 3
        assert levelled is True

    def test_level_up_mood_preserved_through_subsequent_on_step(self) -> None:
        """Steps taken while LEVEL_UP animation runs must not reset the mood."""
        e = CompanionEngine()
        for _ in range(5):
            e.on_step()  # triggers LEVEL_UP
        assert e.mood == Mood.LEVEL_UP
        e.on_step()  # should not overwrite LEVEL_UP
        assert e.mood == Mood.LEVEL_UP


class TestCompanionEngineMoodTransitions:
    """on_streaming, on_thinking, on_error, on_idle mood rules."""

    def test_on_streaming_sets_excited(self) -> None:
        e = CompanionEngine()
        e.on_streaming()
        assert e.mood == Mood.EXCITED

    def test_on_thinking_sets_thinking(self) -> None:
        e = CompanionEngine()
        e.on_thinking()
        assert e.mood == Mood.THINKING

    def test_on_error_sets_stressed(self) -> None:
        e = CompanionEngine()
        e.on_error()
        assert e.mood == Mood.STRESSED

    def test_on_error_increments_errors_counter(self) -> None:
        e = CompanionEngine()
        e.on_error()
        e.on_error()
        assert e.errors == 2

    def test_on_idle_happy_when_git_clean(self) -> None:
        e = CompanionEngine()
        e.git_health = GitHealth(dirty_files=0, untracked_files=0)
        e.on_idle()
        assert e.mood == Mood.HAPPY

    def test_on_idle_focused_when_minor_git_chaos(self) -> None:
        e = CompanionEngine()
        e.git_health = GitHealth(dirty_files=2, untracked_files=1)
        e.on_idle()
        assert e.mood == Mood.FOCUSED

    def test_on_idle_stressed_when_major_git_chaos(self) -> None:
        e = CompanionEngine()
        e.git_health = GitHealth(dirty_files=3, untracked_files=2)
        e.on_idle()
        assert e.mood == Mood.STRESSED

    def test_on_idle_is_noop_during_level_up(self) -> None:
        """on_idle must not interrupt the level-up animation."""
        e = CompanionEngine()
        for _ in range(5):
            e.on_step()  # → LEVEL_UP
        e.on_idle()
        assert e.mood == Mood.LEVEL_UP

    def test_on_error_during_level_up_suppresses_stressed(self) -> None:
        """Errors during level-up should count but not stomp the mood."""
        e = CompanionEngine()
        for _ in range(5):
            e.on_step()  # → LEVEL_UP
        e.on_error()
        assert e.mood == Mood.LEVEL_UP
        assert e.errors == 1

    def test_on_streaming_during_level_up_preserves_mood(self) -> None:
        e = CompanionEngine()
        for _ in range(5):
            e.on_step()
        e.on_streaming()
        assert e.mood == Mood.LEVEL_UP

    def test_on_thinking_during_level_up_preserves_mood(self) -> None:
        e = CompanionEngine()
        for _ in range(5):
            e.on_step()
        e.on_thinking()
        assert e.mood == Mood.LEVEL_UP


class TestCompanionEngineTick:
    """tick() — idle sleep and level-up countdown."""

    def test_tick_does_not_sleep_before_threshold(self) -> None:
        e = CompanionEngine()
        e.mood = Mood.HAPPY
        for _ in range(CompanionEngine.SLEEP_TICKS):
            e.tick()
        # At exactly SLEEP_TICKS, the counter equals the threshold but the
        # condition is *strictly greater than*, so mood is not yet sleeping.
        assert e.mood == Mood.HAPPY

    def test_tick_triggers_sleep_after_threshold(self) -> None:
        e = CompanionEngine()
        e.mood = Mood.HAPPY
        for _ in range(CompanionEngine.SLEEP_TICKS + 1):
            e.tick()
        assert e.mood == Mood.SLEEPING

    def test_tick_decrements_level_up_countdown(self) -> None:
        e = CompanionEngine()
        for _ in range(5):
            e.on_step()  # → LEVEL_UP, _level_up_ticks = LEVELUP_TICKS
        initial = e._level_up_ticks
        e.tick()
        assert e._level_up_ticks == initial - 1

    def test_tick_restores_idle_mood_after_level_up_expires(self) -> None:
        e = CompanionEngine()
        for _ in range(5):
            e.on_step()
        # Exhaust the level-up countdown.
        for _ in range(CompanionEngine.LEVELUP_TICKS):
            e.tick()
        # After countdown the engine calls on_idle(); git is clean → HAPPY.
        assert e.mood == Mood.HAPPY

    def test_tick_during_level_up_does_not_increment_idle_ticks(self) -> None:
        """Idle-tick counter must not accumulate while LEVEL_UP is displayed."""
        e = CompanionEngine()
        for _ in range(5):
            e.on_step()
        idle_before = e._idle_ticks
        e.tick()
        assert e._idle_ticks == idle_before  # unchanged during LEVEL_UP branch


class TestCompanionEngineApplyGitHealth:
    """apply_git_health() — updates stored health and refreshes resting mood."""

    def test_apply_git_health_updates_stored_health(self) -> None:
        e = CompanionEngine()
        new_health = GitHealth(dirty_files=6, untracked_files=0)
        e.apply_git_health(new_health)
        assert e.git_health is new_health

    def test_apply_git_health_refreshes_mood_when_resting(self) -> None:
        e = CompanionEngine()
        e.mood = Mood.HAPPY  # resting
        e.apply_git_health(GitHealth(dirty_files=3, untracked_files=2))
        assert e.mood == Mood.STRESSED

    def test_apply_git_health_clears_stress_when_repo_becomes_clean(self) -> None:
        e = CompanionEngine()
        e.git_health = GitHealth(dirty_files=5, untracked_files=0)
        e.mood = Mood.STRESSED
        e.apply_git_health(GitHealth(dirty_files=0, untracked_files=0))
        assert e.mood == Mood.HAPPY

    def test_apply_git_health_ignored_when_busy(self) -> None:
        """Engine must not change mood if companion is EXCITED (actively streaming)."""
        e = CompanionEngine()
        e.mood = Mood.EXCITED  # not a resting mood
        e.apply_git_health(GitHealth(dirty_files=5, untracked_files=0))
        # EXCITED is not in the resting set — mood should be unchanged.
        assert e.mood == Mood.EXCITED


class TestCompanionEngineXp:
    """xp property — delegates to xp_fraction correctly."""

    def test_xp_is_zero_at_start(self) -> None:
        e = CompanionEngine()
        assert e.xp == pytest.approx(0.0)

    def test_xp_advances_with_steps(self) -> None:
        e = CompanionEngine()
        for _ in range(2):
            e.on_step()
        # 2 steps into level 1 (threshold 0→5): 2/5 = 0.4
        assert e.xp == pytest.approx(0.4)

    def test_xp_is_one_at_level_5(self) -> None:
        e = CompanionEngine()
        for _ in range(50):
            e.on_step()
        assert e.xp == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# get_git_health — async, subprocess mocking
# ---------------------------------------------------------------------------


class TestGetGitHealth:
    """Integration-style tests for get_git_health using subprocess mocks."""

    @pytest.mark.asyncio
    async def test_clean_repo_returns_zero_counts(self) -> None:
        """Empty porcelain output → no dirty or untracked files."""
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = await get_git_health()

        assert result.dirty_files == 0
        assert result.untracked_files == 0

    @pytest.mark.asyncio
    async def test_known_output_counts_dirty_and_untracked(self) -> None:
        """Two modified files and one untracked file in porcelain output."""
        porcelain = b" M agent/foo.py\nM  agent/bar.py\n?? scratch.txt\n"
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(porcelain, b""))

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = await get_git_health()

        assert result.dirty_files == 2
        assert result.untracked_files == 1

    @pytest.mark.asyncio
    async def test_only_untracked_files(self) -> None:
        """Porcelain output that is entirely untracked entries."""
        porcelain = b"?? new_a.py\n?? new_b.py\n?? new_c.py\n"
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(porcelain, b""))

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = await get_git_health()

        assert result.dirty_files == 0
        assert result.untracked_files == 3

    @pytest.mark.asyncio
    async def test_subprocess_exception_returns_default_health(self) -> None:
        """Any exception during the subprocess call must be silently caught."""
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("git not found"),
        ):
            result = await get_git_health()

        assert result.dirty_files == 0
        assert result.untracked_files == 0

    @pytest.mark.asyncio
    async def test_cwd_forwarded_to_subprocess(self) -> None:
        """The cwd argument must be passed through to create_subprocess_exec."""
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch(
            "asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)
        ) as mock_exec:
            await get_git_health(cwd="/some/project")

        _, kwargs = mock_exec.call_args
        assert kwargs.get("cwd") == "/some/project"

    @pytest.mark.asyncio
    async def test_timeout_exception_returns_default_health(self) -> None:
        """asyncio.TimeoutError (from wait_for) should be caught gracefully."""
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            result = await get_git_health()

        assert result.dirty_files == 0
        assert result.untracked_files == 0
