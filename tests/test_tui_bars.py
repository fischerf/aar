"""Tests for the HeaderBar context-window fill indicator.

Requires the ``textual`` optional dependency (``pip install "aar-agent[tui-fixed]"``).
The entire module is skipped when textual is not installed.
"""

from __future__ import annotations

import pytest

# Skip the whole module if textual is not available.
pytest.importorskip("textual")


# ---------------------------------------------------------------------------
# _ctx_fill_bar — pure logic, no Textual app context needed
# ---------------------------------------------------------------------------


class TestCtxFillBar:
    def _h(self):
        """Return a default HeaderStyle for use in assertions."""
        from agent.transports.themes.models import HeaderStyle

        return HeaderStyle()

    def test_returns_empty_when_window_zero(self):
        from agent.transports.tui_widgets.bars import _ctx_fill_bar

        assert _ctx_fill_bar(100, 0, 0, self._h()) == []

    def test_bar_is_correct_width(self):
        from agent.transports.tui_widgets.bars import _BAR_WIDTH, _ctx_fill_bar

        result = _ctx_fill_bar(4096, 8192, 0, self._h())
        bar_text = result[0][0]
        assert len(bar_text) == _BAR_WIDTH

    def test_half_fill_has_equal_filled_empty(self):
        from agent.transports.tui_widgets.bars import (
            _BAR_EMPTY,
            _BAR_FILLED,
            _BAR_WIDTH,
            _ctx_fill_bar,
        )

        result = _ctx_fill_bar(4096, 8192, 0, self._h())  # exactly 50 %
        bar_text = result[0][0]
        assert bar_text.count(_BAR_FILLED) == round(0.5 * _BAR_WIDTH)
        assert bar_text.count(_BAR_FILLED) + bar_text.count(_BAR_EMPTY) == _BAR_WIDTH

    def test_style_green_below_60pct(self):
        from agent.transports.tui_widgets.bars import _ctx_fill_bar

        h = self._h()
        result = _ctx_fill_bar(1000, 8192, 0, h)  # ≈ 12 %
        assert result[0][1] == h.tokens_style

    def test_style_mid_warning_60_to_80pct(self):
        from agent.transports.tui_widgets.bars import _ctx_fill_bar

        h = self._h()
        result = _ctx_fill_bar(6000, 8192, 0, h)  # ≈ 73 %
        assert result[0][1] == h.tokens_warning_mid_style

    def test_style_full_warning_above_80pct(self):
        from agent.transports.tui_widgets.bars import _ctx_fill_bar

        h = self._h()
        result = _ctx_fill_bar(7500, 8192, 0, h)  # ≈ 92 %
        assert result[0][1] == h.tokens_warning_style

    def test_eviction_glyph_present_when_dropped(self):
        from agent.transports.tui_widgets.bars import _ctx_fill_bar

        result = _ctx_fill_bar(3000, 8192, 5, self._h())
        texts = [p[0] for p in result]
        assert any("\u21b75" in t for t in texts)

    def test_no_eviction_glyph_when_zero_dropped(self):
        from agent.transports.tui_widgets.bars import _ctx_fill_bar

        result = _ctx_fill_bar(3000, 8192, 0, self._h())
        texts = [p[0] for p in result]
        assert not any("\u21b7" in t for t in texts)

    def test_clamps_to_full_bar_when_over_limit(self):
        from agent.transports.tui_widgets.bars import _BAR_FILLED, _BAR_WIDTH, _ctx_fill_bar

        result = _ctx_fill_bar(12000, 8192, 0, self._h())  # > 100 %
        bar_text = result[0][0]
        assert bar_text == _BAR_FILLED * _BAR_WIDTH

    def test_label_uses_k_suffix_for_thousands(self):
        from agent.transports.tui_widgets.bars import _ctx_fill_bar

        result = _ctx_fill_bar(4096, 8192, 0, self._h())
        label = result[1][0]
        assert "k" in label  # e.g. "4.1k/8.2k"

    def test_label_uses_plain_number_for_small_values(self):
        from agent.transports.tui_widgets.bars import _ctx_fill_bar

        result = _ctx_fill_bar(100, 500, 0, self._h())
        label = result[1][0]
        assert "100" in label
        assert "500" in label

    def test_all_parts_share_same_style(self):
        """Bar, label (and optional eviction glyph) should use the same style."""
        from agent.transports.tui_widgets.bars import _ctx_fill_bar

        result = _ctx_fill_bar(3000, 8192, 3, self._h())
        styles = [p[1] for p in result]
        assert len(set(styles)) == 1  # all parts use the same style


# ---------------------------------------------------------------------------
# HeaderBar — context state attributes and update_context()
# ---------------------------------------------------------------------------


class TestHeaderBarContextState:
    def _bar(self):
        from agent.transports.themes.models import Theme
        from agent.transports.tui_widgets.bars import HeaderBar

        return HeaderBar(Theme(name="test"))

    def test_initial_context_fields_are_zero(self):
        bar = self._bar()
        assert bar.ctx_tokens == 0
        assert bar.ctx_window == 0
        assert bar.msgs_dropped == 0

    def test_update_context_stores_values(self):
        bar = self._bar()
        bar.update_context(4096, 8192, 3)
        assert bar.ctx_tokens == 4096
        assert bar.ctx_window == 8192
        assert bar.msgs_dropped == 3

    def test_update_context_defaults_msgs_dropped_to_zero(self):
        bar = self._bar()
        bar.update_context(2000, 8192)
        assert bar.msgs_dropped == 0

    def test_update_context_overwrites_previous(self):
        bar = self._bar()
        bar.update_context(1000, 8192, 5)
        bar.update_context(2000, 8192, 0)
        assert bar.ctx_tokens == 2000
        assert bar.msgs_dropped == 0


# ---------------------------------------------------------------------------
# format_ctx_window_bar — shared helper in tui_utils/formatting.py
# (same logic as _ctx_fill_bar but returns rich.text.Text)
# ---------------------------------------------------------------------------


class TestFormatCtxWindowBar:
    def test_returns_none_when_window_zero(self):
        from agent.transports.tui_utils.formatting import format_ctx_window_bar

        assert format_ctx_window_bar(100, 0) is None

    def test_returns_text_object(self):
        from rich.text import Text

        from agent.transports.tui_utils.formatting import format_ctx_window_bar

        result = format_ctx_window_bar(4096, 8192)
        assert isinstance(result, Text)

    def test_contains_bar_characters(self):
        from agent.transports.tui_utils.formatting import format_ctx_window_bar

        result = format_ctx_window_bar(4096, 8192)
        assert result is not None
        assert "█" in result.plain
        assert "░" in result.plain

    def test_contains_label(self):
        from agent.transports.tui_utils.formatting import format_ctx_window_bar

        result = format_ctx_window_bar(4096, 8192)
        assert result is not None
        assert "k" in result.plain  # 4.1k/8.2k format

    def test_eviction_glyph_when_dropped(self):
        from agent.transports.tui_utils.formatting import format_ctx_window_bar

        result = format_ctx_window_bar(3000, 8192, msgs_dropped=3)
        assert result is not None
        assert "↷3" in result.plain

    def test_no_eviction_glyph_when_none(self):
        from agent.transports.tui_utils.formatting import format_ctx_window_bar

        result = format_ctx_window_bar(3000, 8192, msgs_dropped=0)
        assert result is not None
        assert "↷" not in result.plain

    def test_style_applied(self):
        from agent.transports.tui_utils.formatting import format_ctx_window_bar

        result = format_ctx_window_bar(
            7500,
            8192,
            tokens_warning_style="bold red",  # > 80 %
        )
        assert result is not None
        assert result.style == "bold red"

    def test_consistent_with_ctx_fill_bar(self):
        """format_ctx_window_bar and _ctx_fill_bar must agree on label content."""
        from agent.transports.themes.models import HeaderStyle
        from agent.transports.tui_utils.formatting import format_ctx_window_bar
        from agent.transports.tui_widgets.bars import _ctx_fill_bar

        h = HeaderStyle()
        parts = _ctx_fill_bar(4000, 8192, 2, h)
        bar_text = format_ctx_window_bar(
            4000,
            8192,
            msgs_dropped=2,
            tokens_style=h.tokens_style,
            tokens_warning_mid_style=h.tokens_warning_mid_style,
            tokens_warning_style=h.tokens_warning_style,
        )
        assert bar_text is not None
        # Both should mention the eviction count
        combined_parts = "".join(p[0] for p in parts)
        assert "↷2" in combined_parts
        assert "↷2" in bar_text.plain


# ---------------------------------------------------------------------------
# TUIRenderer — ContextWindowEvent state tracking
# ---------------------------------------------------------------------------


class TestTUIRendererContextState:
    def _renderer(self):
        import io

        from rich.console import Console

        from agent.transports.tui import TUIRenderer

        return TUIRenderer(console=Console(file=io.StringIO(), width=120))

    def test_initial_context_fields_zero(self):
        r = self._renderer()
        assert r._ctx_tokens == 0
        assert r._ctx_window == 0
        assert r._msgs_dropped == 0

    def test_context_window_event_updates_state(self):
        from agent.core.events import ContextWindowEvent

        r = self._renderer()
        r.render_event(
            ContextWindowEvent(
                ctx_tokens=4096,
                ctx_window=8192,
                msgs_dropped=3,
                strategy="sliding_window",
            )
        )
        assert r._ctx_tokens == 4096
        assert r._ctx_window == 8192
        assert r._msgs_dropped == 3

    def test_context_window_event_no_console_output(self):
        """ContextWindowEvent must not print anything — it only updates state."""
        import io

        from rich.console import Console

        from agent.core.events import ContextWindowEvent
        from agent.transports.tui import TUIRenderer

        buf = io.StringIO()
        r = TUIRenderer(console=Console(file=buf, width=120))
        r.render_event(ContextWindowEvent(ctx_tokens=3000, ctx_window=8192, msgs_dropped=0))
        assert buf.getvalue() == ""
