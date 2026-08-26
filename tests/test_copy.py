"""DreamCopy execution tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.dreamcopy.models import CopyExecution
from services.copy_service import detect_and_process_copy


@pytest.mark.django_db
def test_duplicate_source_trade_does_not_create_duplicate_copy_execution(
    copy_relationship,
    source_trade,
    wallet,
):
    with patch("services.copy_service.evaluate_copy_score") as mock_score:
        from decimal import Decimal

        from services.copy_score import CopyScoreResult

        mock_score.return_value = CopyScoreResult(
            decision="COPY",
            overall=80,
            confidence=Decimal("0.75"),
            pillars={},
            why=["Mock approves copy"],
            risks=[],
            skip_reasons=[],
            liquidity=Decimal("5000"),
        )
        first = detect_and_process_copy(source_trade)
        second = detect_and_process_copy(source_trade)

    assert len(first) == 1
    assert len(second) == 0
    assert CopyExecution.objects.filter(source_trade=source_trade).count() == 1
