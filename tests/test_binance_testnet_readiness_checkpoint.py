from pathlib import Path


def test_binance_testnet_readiness_checkpoint_exists_and_documents_locked_state():
    checkpoint = Path("docs/binance_testnet_readiness_checkpoint.md")

    assert checkpoint.exists()
    text = checkpoint.read_text(encoding="utf-8")

    assert "BINANCE_TESTNET_SIGNED_READ_ONLY_OK" in text
    assert "BINANCE_TESTNET_ORDER_PREVIEW_VALID" in text
    assert "BINANCE_TESTNET_ORDER_BLOCKED_BY_GUARD" in text
    assert "BLOCKED_BELOW_BASELINE" in text
    assert 'execution_allowed = False' in text
    assert 'paper_only = True' in text
