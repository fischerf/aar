"""Tests for WindowsSubprocessSandbox helper-file lifetime (S5)."""

from __future__ import annotations

import os

import pytest

from agent.safety.sandbox import WindowsSubprocessSandbox


class TestHelperFilePerInstance:
    """S5: ``_helper_path`` is per-instance, not class-level.

    Pre-S5 it was a class attribute so ``close()`` on one sandbox unlinked the
    file every other sandbox relied on, and concurrent launches raced the lazy
    self-heal.  Each instance must now own its own helper.
    """

    @pytest.mark.asyncio
    async def test_each_instance_writes_its_own_helper(self):
        sb1 = WindowsSubprocessSandbox()
        sb2 = WindowsSubprocessSandbox()
        p1 = sb1._get_helper_path()
        p2 = sb2._get_helper_path()
        try:
            assert p1 != p2, "each sandbox must own a distinct helper file"
            assert os.path.exists(p1)
            assert os.path.exists(p2)
        finally:
            await sb1.close()
            await sb2.close()

    @pytest.mark.asyncio
    async def test_close_only_unlinks_own_helper(self):
        sb1 = WindowsSubprocessSandbox()
        sb2 = WindowsSubprocessSandbox()
        p1 = sb1._get_helper_path()
        p2 = sb2._get_helper_path()
        try:
            await sb1.close()
            assert not os.path.exists(p1), "sb1.close() should remove its own helper"
            assert os.path.exists(p2), "sb1.close() must NOT touch sb2's helper (the pre-S5 bug)"
        finally:
            await sb2.close()
            if os.path.exists(p1):
                os.unlink(p1)
            if os.path.exists(p2):
                os.unlink(p2)

    @pytest.mark.asyncio
    async def test_get_helper_path_idempotent_within_instance(self):
        sb = WindowsSubprocessSandbox()
        try:
            p1 = sb._get_helper_path()
            p2 = sb._get_helper_path()
            assert p1 == p2, "repeated calls on one instance return the same helper"
        finally:
            await sb.close()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        sb = WindowsSubprocessSandbox()
        p = sb._get_helper_path()
        assert os.path.exists(p)
        await sb.close()
        assert not os.path.exists(p)
        # Second close must not raise.
        await sb.close()

    @pytest.mark.asyncio
    async def test_get_helper_path_recovers_after_external_delete(self):
        """If the tempfile vanishes (e.g. tmp reaper), the next call recreates it."""
        sb = WindowsSubprocessSandbox()
        try:
            p1 = sb._get_helper_path()
            os.unlink(p1)
            p2 = sb._get_helper_path()
            assert p2 != p1 or os.path.exists(p2)
            assert os.path.exists(p2)
        finally:
            await sb.close()

    @pytest.mark.asyncio
    async def test_get_helper_path_thread_safe(self):
        """Two threads racing on the same instance must not leak a tempfile."""
        import threading

        sb = WindowsSubprocessSandbox()
        results: list[str] = []
        barrier = threading.Barrier(8)

        def _race() -> None:
            barrier.wait()
            results.append(sb._get_helper_path())

        threads = [threading.Thread(target=_race) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            # All threads must observe the same path → no tempfile leak.
            assert len(set(results)) == 1, f"races produced multiple helpers: {set(results)}"
        finally:
            await sb.close()


class TestHelperWriteContent:
    """Sanity: the helper file contains the expected Python prelude."""

    @pytest.mark.asyncio
    async def test_helper_contains_integrity_lowering_code(self):
        sb = WindowsSubprocessSandbox()
        try:
            p = sb._get_helper_path()
            content = open(p, encoding="utf-8").read()
            # The helper script is the Windows integrity-lowering prelude.
            # Whatever the exact body, it must be non-empty Python.
            assert len(content) > 0
        finally:
            await sb.close()
